import unittest
import pytest

from funpayflow.product_templates import (ProductTemplate, ProductValidationError, default_template,
    builtin_template, BUILTIN_TYPES, TemplateService)


class ProductTemplateTests(unittest.TestCase):
    def test_builtin_bilingual_defaults_and_explicit_overrides(self):
        rendered = ProductTemplate(default_template()).render({"game": "Game", "product_name": "Key"})
        self.assertIn("Steam | Global | Standard", rendered["title_ru"])
        self.assertIn("Quantity: 1", rendered["description_en"])
        self.assertIn("Количество: 1", rendered["description_ru"])
        changed = ProductTemplate(default_template()).render(
            {"game": "Game", "product_name": "Key"}, {"emoji": "⭐", "variables": {"region": "EU"}})
        self.assertEqual(changed["emoji"], "⭐")
        self.assertIn("EU", changed["title_en"])

    def test_unknown_and_missing_variables_and_unsafe_syntax_fail(self):
        model = ProductTemplate(default_template())
        for values in ({}, {"game": "Game", "product_name": "X", "secret": "hidden"},
                       {"game": "{{price}}", "product_name": "Key"},
                       {"game": "G", "product_name": "P", "quantity": True}):
            with self.subTest(values=values), self.assertRaises(ProductValidationError):
                model.render(values)
        for syntax in ("{{game.name}}", "{{game[0]}}", "{{game!r}}", "{{{{game}}}}", "{game}"):
            body = default_template()
            body["title_ru"] = syntax
            with self.subTest(syntax=syntax), self.assertRaises(ProductValidationError):
                ProductTemplate(body)

    def test_required_bilingual_text_unknown_fields_and_validation_rules(self):
        for field in ("title_ru", "title_en", "description_ru", "description_en"):
            body = default_template()
            body[field] = ""
            with self.assertRaises(ProductValidationError):
                ProductTemplate(body)
        body = default_template()
        body["validation_rules"] = {"region": {"choices": ["EU"], "max_length": 5}}
        with self.assertRaises(ProductValidationError):
            ProductTemplate(body).render({"game": "Game", "product_name": "Key"})
        with self.assertRaises(ProductValidationError):
            ProductTemplate(default_template()).render({"game": "Game", "product_name": "Key"}, {"node_id": 1})

    def test_title_emoji_is_rendered_once_and_final_length_is_bounded(self):
        body = default_template()
        body["title_ru"] = "🔑 {{game}}"
        rendered = ProductTemplate(body).render({"game": "Game", "product_name": "Key"})
        self.assertEqual(rendered["title_ru"], "🔑 Game")
        body["title_ru"] = "x" * 200
        with self.assertRaises(ProductValidationError):
            ProductTemplate(body).render({"game": "Game", "product_name": "Key"})

    def test_credential_and_service_variables_cannot_be_requested(self):
        names = ("golden_key", "csrf", "csrf_token", "form_created_at", "phpsessid", "cookie",
                 "cookies", "password", "passwords", "bot_token", "api_key", "token", "offer_id",
                 "node_id", "publish_state", "delivery_secrets", "price", "amount", "account_id",
                 "access_token", "refresh_token", "session_id", "api_secret", "secret_key", "authorization")
        for name in names:
            body = default_template()
            body["allowed_variables"].append(name)
            with self.subTest(name=name), self.assertRaises(ProductValidationError):
                ProductTemplate(body)
        body = default_template()
        body["allowed_variables"].append("delivery_note")
        body["description_en"] = "{{delivery_note}}"
        self.assertEqual(ProductTemplate(body).render({"game": "G", "product_name": "P", "delivery_note": "Public note"})["description_en"], "Public note")


if __name__ == "__main__":
    unittest.main()


def test_opt_in_presets_concurrent_initialization_and_customization(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from funpayflow.product_store import ProductStore
    from funpayflow.state import ReviewReceiptStore

    state = ReviewReceiptStore(tmp_path / 'state.sqlite3')
    state.initialize()
    store = ProductStore(state, 1, 42)
    service = TemplateService(store)
    assert service.list() == []
    for kind, _ in BUILTIN_TYPES:
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(lambda _: TemplateService(store).ensure_builtin(kind), range(8)))
        assert all(row == rows[0] for row in rows)
        body = rows[0]
        assert body['id'] == kind and body['revision'] == 1
        assert set(default_template()['allowed_variables']) <= set(body['allowed_variables'])
        assert not body.get('node_bindings')
        edited = service.save(dict(body, name='Custom ' + kind, display_name='Owner name',
            default_emoji='⭐', description_en='Owner text'), kind, body['revision'])
        reopened = TemplateService(ProductStore(ReviewReceiptStore(state.path), 1, 42))
        assert reopened.ensure_builtin(kind) == edited
    assert len(service.list()) == 7
    assert ProductStore(state, 2, 42).list_templates() == []
    assert ProductStore(state, 1, 43).list_templates() == []
    with unittest.TestCase().assertRaises(ProductValidationError):
        service.ensure_builtin('arbitrary-id')
    assert builtin_template() == default_template()


def test_new_presets_require_actual_product_details_and_authorization():
    for kind, _ in BUILTIN_TYPES[1:]:
        body = builtin_template(kind)
        assert body['defaults'] == {'quantity': '1'}
        with unittest.TestCase().assertRaises(ProductValidationError):
            ProductTemplate(body).render({'game': 'Catalogue category', 'product_name': 'Category'})
    ai = builtin_template('AI_SUBSCRIPTION')
    values = dict(game='Category', product_name='Category', sale_name='Example service',
        plan='Example plan', duration='1 month', availability='unspecified',
        delivery_method='Permitted invitation', authorized_delivery='No permission')
    with unittest.TestCase().assertRaises(ProductValidationError):
        ProductTemplate(ai).render(values)


@pytest.mark.parametrize('kind,details', [
    ('DLC_CONTENT', dict(sale_name='Actual expansion', base_game='Required game',
        sale_platform='Owner platform', sale_edition='Expansion edition')),
    ('GAME_CURRENCY', dict(sale_name='Actual game', currency_name='Crystals',
        denomination='500 crystals')),
    ('GAMING_SUBSCRIPTION', dict(sale_name='Actual card', gaming_product_type='Подарочная карта / Gift card',
        term_or_value='25 EUR', sale_platform='Owner platform')),
])
def test_second_wave_required_values_bilingual_and_permission(kind, details):
    body = builtin_template(kind)
    model = ProductTemplate(body)
    values = dict(details, availability='EU', delivery_method='Permitted code delivery',
        authorized_delivery='Подтверждаю разрешение провайдера / I confirm provider permission', quantity='2')
    rendered = model.render(values)
    assert body['defaults'] == {'quantity': '1'}
    assert not body.get('node_bindings')
    for language in ('ru', 'en'):
        text = rendered['description_' + language]
        assert all(value in text for value in values.values())
        assert details['sale_name'] in rendered['title_' + language]
    # Every extra variable is required by the actual texts, with no invented default.
    for variable in values:
        if variable == 'quantity':
            continue
        for invalid in (None, '', '   '):
            missing = dict(values)
            if invalid is None:
                missing.pop(variable)
            else:
                missing[variable] = invalid
            with pytest.raises(ProductValidationError):
                model.render(missing)
    with pytest.raises(ProductValidationError):
        model.render(dict(values, authorized_delivery='Not confirmed'))
    if kind == 'DLC_CONTENT':
        assert 'базовая игра не включена' in rendered['description_ru']
        assert 'base game not included' in rendered['description_en']
    elif kind == 'GAME_CURRENCY':
        assert 'does not guarantee delivery' in rendered['description_en']
        assert 'No customer login, password, token or one-time codes' in rendered['description_en']
    else:
        with pytest.raises(ProductValidationError):
            model.render(dict(values, gaming_product_type='Account transfer'))
        assert 'No third-party account transfer' in rendered['description_en']


@pytest.mark.parametrize('product_type,term', [
    ('Подписка / Subscription', '30 days'),
    ('Подарочная карта / Gift card', '25 EUR'),
    ('Код / Code', '10 USD'),
])
def test_gaming_product_distinction_requires_explicit_term_or_face_value(product_type, term):
    model = ProductTemplate(builtin_template('GAMING_SUBSCRIPTION'))
    values = dict(sale_name='Owner named service or card', gaming_product_type=product_type,
        term_or_value=term, sale_platform='Owner platform', availability='EU',
        delivery_method='Permitted code delivery',
        authorized_delivery='Подтверждаю разрешение провайдера / I confirm provider permission')
    rendered = model.render(values)
    for lang in ('ru', 'en'):
        assert product_type in rendered['description_' + lang]
        assert term in rendered['title_' + lang]
