"""Local dynamic drafts backed by GET-only node schema; no publication code."""
from copy import deepcopy
from .node_form import NodeFormService
from .product_drafts import DraftService
from .product_templates import ProductValidationError, validate_node_bindings


LEGACY_NODE_VARIABLES = frozenset({'platform', 'region', 'edition'})
LEGACY_WARNING = 'Черновик создан до динамических параметров FunPay. Проверьте поля.'
STALE_WARNING = 'FunPay изменил параметры раздела. Выберите значение заново.'
VARIANT_WARNING = 'Конфликт Variant/content и параметров FunPay. Проверьте node_bindings, node_values и выбранные значения.'


class NodeSelectionError(ProductValidationError):
    def __init__(self):
        super().__init__(STALE_WARNING)


class NodeVariantConflictError(ProductValidationError):
    def __init__(self):
        super().__init__(VARIANT_WARNING)


def selected_fields(schema, values):
    """Validate technical option values, keeping duplicate labels independent."""
    fields = {field['html_name']: field for field in schema['fields']}
    if not isinstance(values, dict) or set(values) - fields.keys():
        raise NodeSelectionError()
    rows = []
    for field in schema['fields']:
        key = field['html_name']
        if field['disabled']:
            if key in values:
                raise NodeSelectionError()
            continue
        value = values.get(key)
        if value is None:
            if field['required']:
                raise NodeSelectionError()
            continue
        option = next((item for item in field['options'] if item['value'] == value and not item['disabled']), None)
        if option is None or (field['required'] and not value):
            raise NodeSelectionError()
        rows.append({'html_name': key, 'html_id': field['html_id'], 'label': field['label'],
                     'display_label': field['display_label'], 'value': value,
                     'option_label': option['label'], 'order': field['order']})
    return rows


class NodeDraftService(DraftService):
    def node_setup(self, template_id, route_id, variant_id=None, force=False, *, _template=None):
        template = self.templates.get(template_id) if _template is None else _template
        parent = self._route(route_id, template_id)
        variant = self.variants.get(variant_id) if variant_id else None
        route = self.variants.resolve_route(variant, parent, template) if variant else parent
        schema = NodeFormService(self.routes.client, self.store).get(route['node_id'], force=force)
        # A binding is an explicit technical key, never inferred from display labels.
        fields = {f['html_name']: f for f in schema['fields']}
        if set(template.get('node_bindings', {}).values()) - fields.keys():
            raise NodeSelectionError()
        defaults = {f['html_name']: f['value'] for f in schema['fields']
                    if not f['disabled'] and f.get('value') is not None
                    and any(o['value'] == f['value'] and not o['disabled'] for o in f['options'])}
        defaults.update(variant.get('node_values', {}) if variant else {})
        for key, value in (variant.get('node_values', {}) if variant else {}).items():
            if key not in fields or fields[key]['disabled'] or not any(o['value'] == value and not o['disabled'] for o in fields[key]['options']):
                raise NodeSelectionError()
        if variant:
            for variable, value in variant.get('overrides', {}).get('variables', {}).items():
                identity = template.get('node_bindings', {}).get(variable)
                if identity and identity not in variant.get('node_values', {}):
                    # Explicit binding fixes the field identity; only an exact, unique
                    # option label/value may translate a legacy variant default.
                    options = [o for o in fields[identity]['options'] if not o['disabled']
                               and value in {o['label'], o['value']}]
                    if len(options) != 1:
                        raise NodeVariantConflictError()
                    defaults[identity] = options[0]['value']
        return template, route, variant, schema, defaults

    def create_node_preview(self, template_id, route_id, values, *, variables=None, price=None,
                            overrides=None, variant_id=None, schema=None, input_values=None, _template=None):
        template, route, variant, current, defaults = self.node_setup(template_id, route_id, variant_id, _template=_template)
        if schema is not None and (schema['node_id'] != current['node_id'] or schema['fingerprint'] != current['fingerprint']):
            raise NodeSelectionError()
        # Caller provides all reviewed selections; defaults are not silently merged at Save.
        selections = selected_fields(current, values)
        provenance = values if input_values is None else input_values
        if not isinstance(provenance, dict) or any(key not in values or values[key] != value for key, value in provenance.items()):
            raise NodeSelectionError()
        bindings = template.get('node_bindings', {})
        labels = {row['html_name']: row['option_label'] for row in selections}
        technical = {row['html_name']: row['value'] for row in selections}
        bound = {key: labels.get(identity, '—') for key, identity in bindings.items()}
        explicit = deepcopy(variables or {})
        overrides = deepcopy(overrides or {})
        inherited = [route.get('overrides', {}).get('variables', {}),
                     variant.get('overrides', {}).get('variables', {}) if variant else {},
                     explicit, overrides.get('variables', {})]
        for source in inherited:
            for key, value in source.items():
                if (key in bindings and value != technical.get(bindings[key])
                        and str(value).strip().casefold() != bound[key].strip().casefold()):
                    raise NodeVariantConflictError()
        if variant and (set(variant.get('overrides', {}).get('variables', {})) & LEGACY_NODE_VARIABLES) - bindings.keys():
            raise NodeVariantConflictError()
        # Retired generic defaults must not claim Steam/Global/Standard for another schema.
        neutral = {key: '—' for key in LEGACY_NODE_VARIABLES - bindings.keys()}
        render_template = deepcopy(template)
        for key in neutral:
            render_template['validation_rules'].pop(key, None)
        render_values = {**explicit, **neutral, **bound}
        effective_overrides = deepcopy(overrides)
        effective_overrides['variables'] = {**overrides.get('variables', {}), **neutral, **bound}
        result = self._render(render_template, self._route(route_id, template_id), render_values, price, effective_overrides, variant)
        result['input_variables'] = explicit
        result['overrides'] = overrides
        result['template_snapshot'] = deepcopy(template)
        result['node_fields'] = selections
        result['node_input_values'] = deepcopy(provenance)
        result['node_schema'] = {key: deepcopy(current[key]) for key in ('node_id', 'fields', 'fingerprint', 'fetched_at')}
        result['node_bindings'] = deepcopy(bindings)
        result['node_variant_values'] = dict(variant.get('node_values', {})) if variant else {}
        if variant:
            result['node_variant_values'].update({bindings[key]: defaults[bindings[key]]
                for key in variant.get('overrides', {}).get('variables', {}) if key in bindings})
        result['node_warning'] = ('Не все content variables связаны с полями FunPay; вместо generic defaults показано «—». '
                                  'Настройте node_bindings в ProductTemplate.' if neutral else '')
        if any(values.get(key) != value for key, value in result['node_variant_values'].items()):
            result['node_warning'] += ' Явный выбор параметра отличается от preset Variant; проверьте конфликт.'
        return result

    def preview_bindings(self, preview, bindings):
        """Validate/render proposed bindings without writing a template or draft."""
        template = self.templates.get(preview['template_id'])
        template['node_bindings'] = validate_node_bindings(bindings, template['allowed_variables'])
        result = self.rebuild(preview, _template=template)
        for key in ('id', 'revision', 'created_at', 'updated_at', 'status'):
            if key in preview:
                result[key] = preview[key]
        return result

    def rebuild(self, preview, *, price=None, overrides=None, variables=None, _template=None):
        if 'node_fields' not in preview:
            return super().rebuild(preview, price=price, overrides=overrides, variables=variables)
        merged = {**preview.get('overrides', {}), **(overrides or {})}
        merged['variables'] = {**preview.get('overrides', {}).get('variables', {}), **(overrides or {}).get('variables', {})}
        for key in variables or {}:
            merged['variables'].pop(key, None)
        if not merged['variables'] and 'variables' not in preview.get('overrides', {}) and 'variables' not in (overrides or {}):
            merged.pop('variables')
        result = self.create_node_preview(preview['template_id'], preview['route_id'],
            {row['html_name']: row['value'] for row in preview['node_fields']},
            variables={**self.variable_input(preview), **(variables or {})},
            price=preview['payload']['price'] if price is None else price, overrides=merged,
            variant_id=preview.get('variant_id'), schema=preview['node_schema'],
            input_values=preview.get('node_input_values', {}), _template=_template)
        # fetched_at is informational; identical safe structure is the same reviewed schema.
        result['node_schema'] = deepcopy(preview['node_schema'])
        return result

    def clone_preview(self, draft_id, expected_revision):
        source = self.store.get_draft(draft_id)
        if not source or source['revision'] != expected_revision or type(expected_revision) is not int or source.get('status') == 'cancelled':
            raise ProductValidationError('Source draft изменился или недоступен.')
        if 'node_fields' not in source:
            return super().clone_preview(draft_id, expected_revision)
        return self._canonical_node(source, source.get('variant_id'))

    def _canonical_node(self, preview, variant_id):
        template, route, variant, schema, defaults = self.node_setup(preview['template_id'], preview['route_id'], variant_id)
        # Preserve technical values independently. Only explicit node edits outrank a variant preset.
        values = {row['html_name']: row['value'] for row in preview['node_fields']}
        if variant_id != preview.get('variant_id'):
            for key in preview.get('node_variant_values', {}):
                if key not in preview.get('node_input_values', {}):
                    if key in defaults:
                        values[key] = defaults[key]
                    else:
                        values.pop(key, None)
        if variant:
            values.update({template['node_bindings'][key]: defaults[template['node_bindings'][key]]
                for key in variant.get('overrides', {}).get('variables', {}) if key in template.get('node_bindings', {})})
        values.update(variant.get('node_values', {}) if variant else {})
        values.update(preview.get('node_input_values', {}))
        return self.create_node_preview(preview['template_id'], preview['route_id'], values,
            variables=self.variable_input(preview), price=preview['payload']['price'],
            overrides=preview.get('overrides', {}), variant_id=variant_id,
            input_values=preview.get('node_input_values', {}))

    def refresh_preview(self, preview):
        result = self._canonical_node(preview, preview.get('variant_id'))
        for key in ('id', 'revision', 'created_at', 'updated_at', 'status'):
            if key in preview:
                result[key] = preview[key]
        return result

    def switch_variant(self, preview, variant_id):
        return self._canonical_node(preview, variant_id) if 'node_fields' in preview else super().switch_variant(preview, variant_id)

    def preview_bulk(self, template_id, text, common=None):
        # New node selections need one explicit resolution, not 100 hidden GET/confirm dialogs.
        if not isinstance(text, str) or len(text) > 100000:
            raise ProductValidationError('bulk: text limit exceeded')
        lines = [i for i, row in enumerate(text.splitlines(), 1) if row.strip()] if isinstance(text, str) else []
        if not 1 <= len(lines) <= 100:
            raise ProductValidationError('bulk: expected 1 to 100 rows')
        return {'previews': [], 'errors': [{'line': i, 'status': 'needs_resolution',
                 'error': 'Нужен выбор параметров FunPay в одиночном «Создать».'} for i in lines]}
