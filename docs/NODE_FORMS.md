# Read-only Node Form Schema

`node_form.py` uses the existing authorized Account and shared Account RLock to
perform only `GET lots/offerEdit?node=<positive node ID>`. It never constructs a
second Account, copies cookies, submits the form, or calls `save_lot`/`offerSave`.
FunPayAPI 1.1.0 and dependencies remain unchanged. Publish remains blocked.

## Sources and limits

The adapter was checked against public reference code:

- [Cardinal Account, c09dd13](https://github.com/sidor0912/FunPayCardinal/blob/c09dd13be25072bb3d5cdbba33d1e0d70e477710/FunPayAPI/account.py): node-specific GET and `form.form-offer-editor`.
- [Sigma Account, eabb45f](https://github.com/qorexdevs/FunPaySigma/blob/eabb45f6488c7a843f5a3f1959fe6124d7038b71/FunPayAPI/account.py): form-group/technical-key/option metadata extraction.
- [Sigma LotFields types](https://github.com/qorexdevs/FunPaySigma/blob/eabb45f6488c7a843f5a3f1959fe6124d7038b71/FunPayAPI/types.py) and [editor](https://github.com/qorexdevs/FunPaySigma/blob/eabb45f6488c7a843f5a3f1959fe6124d7038b71/tg_bot/lot_editor_cp.py): field options and overlay pattern.

These are read-only reference findings, not a captured authenticated form from
the owner's account. Cyberpunk/Elden Ring fixtures reproduce the reported UI
shape; their category-specific names/option IDs are explicitly **synthetic**.
No production game/node names are hardcoded. Future deployment needs an owner
smoke-test against live forms; unexpected markup fails closed.

## Safe failure diagnosis

`NodeFormError` keeps the same fixed user-facing message. Its internal `reason`
and `diagnostic` distinguish HTTP/auth/redirect, final URL validation, account
scope, app-data/user ID, editor/action/node context and SELECT/label/option
validation. `NodeFormService.get()` emits one `NodeForm failure {JSON}` warning
per failed call. The JSON is allowlisted: fixed reason and URL category, integer
HTTP status, capped editor count and visible SELECT count when available. It
never includes the URL itself, node/account IDs, field names/values, app-data,
exception text, headers or HTML. Pure parser calls do not log.

The owner-only Telegram create wizard also displays just the allowlisted `reason`
(e.g. `FORM_COUNT` or `HTTP_AUTH`) under the same generic failure message.
No HTML, IDs, option values, network exception text or private log lines are
exposed. This does not itself fix an incompatible live form; it provides a
safe actionable reason after the diagnostics build is deployed.

## Live FIELD_NAME_UNSUPPORTED follow-up (2026-10-18)

An authenticated owner wizard after staging deployment of `42a33c7` returned
`FIELD_NAME_UNSUPPORTED`. This confirms the parser rejected one visible
select's technical name; it **does not** identify the name or prove that it is
safe to accept or ignore. Neither a transport/auth failure nor a successful
schema parse was observed in that UI attempt.

The next minimal diagnostic adds one fixed, allowlisted
`unsupported_name_shape` to this failure (also visible to the owner, without
the original field name): `plain`, `other_namespace`, `fields_deep`,
`fields_format`, `service_like` or `other_format`. No raw HTML, field names,
option values, IDs, account data or exception text are emitted, persisted or
displayed. This does not change accepted field names or weaken rejection gates.
Synthetic regression tests cover every category and sanitizer behavior.

The follow-up requires a separately approved code rollout and exactly one
owner action in the normal Telegram wizard to observe the category; the live
FunPay issue remains **unfixed** until the underlying supported input structure
is confirmed and re-tested.

The production failure reported after `6d74ca8` has **not** yet been localized
to a specific validation check. No parser acceptance rules were relaxed on a
guess. In particular string/boolean user IDs, unsupported names/action URLs and
ambiguous labels still fail closed, now with a distinct internal reason.

There is no in-process diagnostic RPC in the running bot. Importing Account in
an SSH Python process would create a second object/session and cannot reuse the
live Account safely. Do not copy credentials, attach a debugger, or inject code.
After separately authorized deployment/restart of diagnostics, the owner can
open nodes 976 and 986 once through the normal Telegram create wizard. This uses
the existing runtime Account/RLock and GET-only path. For each failure collect
only the allowlisted JSON warning; do not save HTML or raw journal lines. Use
the confirmed reason to choose the next minimal fix and regression fixture.

For diagnosis without owner Telegram clicks, see the
[in-process design and offline feasibility tests](NODE_FORM_IN_PROCESS_DIAGNOSIS.md).
That review selected the task's design-only safety fallback: the supported client
cannot suppress redirects per call, and shared-lock HTTP can delay the runtime.
No opt-in hook has been enabled or implemented; the rollout contract and remaining
acceptance tests are documented there. Do not treat this as a command to run a
second process or as evidence of a live result.

Unlike a general form-copying implementation, this adapter retains only safe
visible SELECT metadata. It does not retain every input or hidden/session
field, does not persist HTML, and does not implement publication/ID guessing.

## Offline audit: nodes 976 and 986 (2026-10-18)

Reviewed source baseline `f7e2d324713c295cc027c0140b06d6ec43578839`,
`node_form.py`, `telegram_node_fields.py`, and the committed synthetic examples
and parser/diagnostic/draft tests. No cause of the reported production failure
was reproduced from this evidence; parser acceptance rules remain unchanged.
The existing reason codes and owner-safe error display were not duplicated.

Added deterministic coverage in `tests/test_node_form.py`: both committed
examples pass through the fake Account GET path with matching integer app-data
user scope for ru/en/uk, including localized final editor URLs. Separate
synthetic forms for both node IDs preserve independent full technical names
`fields[game][edition]` and `fields[extra][edition]` despite equal labels.
These namespace variants are illustrative, not observed live field names.
The parser, diagnostics and owner error suites passed **134 tests** normally;
including node-product integration, **182 tests** passed using the existing
virtualenv. Windows sandbox denied access to
directories created with mode 0700; the successful test process substituted
mode 0777 for `os.mkdir` and used temporary directories under pytest's ignored
cache. No repository or application permission logic was changed.

Remaining unknowns are the actual failure reason, response status/final URL
category, embedded user-ID type/scope, and live editor/action/label/SELECT
structure. The conceptual fixtures cannot establish live compatibility for
either category. No live requests, real Account/session construction, POST or
deployment were performed. Further diagnosis needs the existing allowlisted
reason from a separately authorized normal runtime wizard attempt; do not
infer a parser relaxation or request raw HTML/credentials from this audit.

## Safe schema and cache

Schema: `node_id`, ordered `fields`, `fetched_at`, SHA-256 `fingerprint`, and
runtime-only `generation`. Each field has `identity == html_name`, optional
`html_id`, original cleaned `label`, UI-only `display_label`, `kind=select`,
`required`, `disabled`, `order`, options, and browser-selected/default value.
Options retain technical `value`, label, selected/disabled flags and DOM order.
Two identical labels remain separate fields; only display labels get `(2)`.

The parser validates successful response, Account initialization/ID, response
URL/node context, editor marker, login/error markers, and embedded identity if
present. Hidden `node_id`/`offer_id` may confirm context transiently but never
enter schema. Malformed controls, duplicate names, empty selects, missing
labels and ambiguous visible service-like names fail closed. Multiple select,
radio, checkbox and text-field editing are outside this version.

Service/hidden controls, CSRF, form timestamps, cookies/session/request values,
standard content controls and raw HTML are excluded. Neither logs nor errors
contain response/exception text. SHA-256 includes only ordered safe structure,
labels, constraints and option values/labels/disabled flags; service values and
default selections do not contribute.

TTL is 900 seconds, lazy, bounded to 32 scopes on the client, keyed by owner,
account ID/object, node and requested locale. No worker or SQLite catalog is
created. Force refresh removes only that entry, issues another GET and rotates
generation even if structure is identical. Failed refresh invalidates old
context; stale cache is not used for node forms.

## Content and Variant binding

Node Fields and content variables are different entities. New create flow asks
actual selects in DOM order, then remaining content variables/quantity/price.
Game/product name defaults to the verified GameRoute. An absent Region select
does not produce a generic Region prompt.

Optional ProductTemplate `node_bindings` maps content-variable names to verified
technical select names. Configure it through the existing full JSON editor;
there is no label-only or guessed semantic binding. Bound placeholders render
the selected option's current label. Unbound legacy platform/region/edition
placeholders show `—` with a warning instead of fabricated defaults; configure
bindings or content text before using the draft elsewhere. This is a local
draft, not a publish-ready offer.

Variant `node_values` maps technical field identities to technical option values.
The current schema validates presets, including verified alternate nodes of
the same game. Legacy Variant variable defaults require explicit binding and
a unique exact option value/label match. Conflicting content/selected values
are rejected; explicit node selection differing from a node preset is visibly
warned. Emoji/text inheritance remains separate.

## Draft lifecycle

Draft JSON stores ordered `node_fields` (technical name, selected technical
value, label/display/option-label snapshots), safe `node_schema` and fingerprint,
explicit `node_input_values`, bindings and applied Variant preset provenance.
It stores no hidden fields or raw HTML; no SQLite schema migration is needed.

Reopen/clone fetch current schema through cache. Missing/disabled options fail
with “FunPay изменил параметры раздела. Выберите значение заново.” Existing
values with changed labels may acquire fresh display snapshots without editing
the source. Save of both new and edited existing dynamic drafts revalidates
the reviewed schema. Cancelled lifecycle metadata survives reopen.

Create Similar remains transient until Save and never edits source. Independent
duplicate-label selections and optional omissions survive cloning/editing other
fields. A verified Variant node override uses that node's current schema, with
technical-value validation; incompatible values need resolution. Legacy drafts
remain readable with a warning, and clone requires fresh real-field selection
without guessing how old generic values correspond to FunPay options.

Callbacks contain only token/index, not raw names/values. Context/generation
checks run before input cleanup. Refresh/Back/Cancel invalidate old controls;
completing a similar edit also clears text input. New mass rows report
`needs_resolution`; no hidden bulk GET/confirmation loop or fake generic drafts.
Already staged legacy batches retain their existing confirmation behavior.

## Standard controls confirmed for future research

Public reference code names (not enabled for publication):

| Purpose | Technical name |
| --- | --- |
| RU/EN short description | `fields[summary][ru]`, `fields[summary][en]` |
| RU/EN full description | `fields[desc][ru]`, `fields[desc][en]` |
| RU/EN payment message | `fields[payment_msg][ru]`, `fields[payment_msg][en]` |
| Price / quantity | `price`, `amount` |
| Automatic delivery | `auto_delivery` |
| Images | `fields[images]` (reference uses image IDs) |

The SELECT adapter does not persist these standard controls or their existing
values from the GET response. No upload, automatic delivery configuration,
payment-message editing, POST or real publish was implemented or tested live.
