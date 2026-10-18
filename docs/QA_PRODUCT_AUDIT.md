# Product draft Telegram audit — phase 2

Date: 2026-10-18. Source baseline: `3bac74dda385fcf62f90e1e95819099e20162e92`,
branch `codex/issue-47`; initial tracked/untracked status was clean.
The supplied checkout was audited without fetching or asserting that it equals
remote main. Read `AGENTS.md`, `PRODUCT_TEMPLATES.md`, `NODE_FORMS.md`,
`DEVELOPMENT.md` and the relevant current implementation/tests.

## Reproducible bugs and fixes

### 1. Rejected batch callback destroys another draft's controls/input

Owner journey: prepare a route batch, retain its Confirm/Cancel button, open a
saved draft, optionally begin editing its price, then press the old batch button.
`_products_callback` checked the batch token **after** generic cleanup. A token
mismatch prevented the batch write, but removed the active `preview_token` or
`_interaction_state`. The current draft's Save/Edit buttons stopped working, or
the next price message no longer reached its editor. No persisted draft corruption
was reproduced in this case; the corruption affected the active UI session.

Fix: validate callback shape, nonempty current batch token and exact equality
before cleanup. Invalid Confirm/Cancel callbacks preserve the complete flow and
the pending input object. Tests cover both buttons against both an idle preview
and an active price editor, then use the still-valid controls to finish Save.

### 2. Previous batch remains confirmable after returning to import

Owner journey: preview route A, retain its batch button, select URL/bulk import
again in the same flow, then press the old button before submitting replacement
input. Navigation reused the flow dictionary and retained `batch_token`.
Confirm could persist route A after its confirmation screen was abandoned;
Confirm and Cancel both removed the new input handler.

Fix: navigation away from a staged batch confirmation drops its in-memory
`batch_token` and `kind`. This applies to route and legacy draft batch screens.
Repeated Confirm/Cancel on the current confirmation still uses the existing
durable idempotence. There is no migration or change to batch storage: abandoned
pending snapshots remain in SQLite as before, without a live confirmation handle.
Tests resume import, replay each old button, verify unchanged input and no route
write through a fresh store, then confirm only the replacement route B.

Before the fix the new journey file produced **6 failed, 6 passed**: all six
batch replay cases failed, while its original two and four new quantity cases
passed. Both defects are fixed in `src/funpayflow/telegram_products.py`.

## Coverage

Added 11 deterministic cases in `tests/test_qa_user_journeys.py` (13 total cases
in that file), reusing the real Telegram handlers, services, existing fakes and
temporary SQLite. No old assertions were weakened.

| Risk | Evidence exercised |
| --- | --- |
| Stale batch callbacks across drafts | New Confirm/Cancel × idle preview/price input; complete flow/input identity preserved; finish Save and read a fresh store |
| Abandoned confirmation in the same wizard | New Confirm/Cancel after reopening import; only replacement route is saved |
| Quantity Save/Reopen/edit | New custom quantity 1 and 999999999 for both committed node fixtures; rapid duplicate Save; reopen; reject 0, 1000000000 and 1.5 without mutation; edit to opposite boundary and verify one revision increment through a fresh store |
| Unauthorized owner actions | New replay of Save, Cancel, Clone, Edit, Open, route edit and batch controls as another user, through both outer Telegram authorization and module owner gates; owner input/source/routes unchanged |
| Draft > variant > route > template | `test_similar_drafts.py`, `test_product_variant_integration.py`, `test_product_variants.py`: explicit equal-default inputs, variables, emoji, all four RU/EN texts, current definitions versus saved snapshots |
| Clone source immutability | Original QA journey, `test_similar_drafts.py`, `test_product_pickers.py`, `test_node_products.py`: transient clone, cancel, edits, repeated Save, source revision/status/timestamps preserved |
| Equal labels, separate technical identities | Original QA journey and `test_node_products.py`: independent selects, options, display labels, clone edits and persisted technical names; parser coverage in `test_node_form.py` |
| Legacy compatibility | `test_product_compatibility.py`, similar/picker/node suites: existing tables and reader contracts, missing input provenance warning, fresh selection for legacy clones |
| Changed schema/fingerprint | Node suites: removed/disabled choices fail closed; changed labels refresh display; dirty Save rechecks schema; old generation callbacks fail; refresh failure never falls back to generic values |
| Route/variant changes | Variant integration, similar and node suites: alternate verified node uses its own schema; stale catalog/route and foreign/disabled variants fail; template/route scope checks |
| Rapid callbacks and validation | New quantity double Save plus existing batch/clone double confirmation, stale preview/picker/variant controls, enum/custom validation, price errors and source revision conflicts |

## Verification on this checkout

Used the existing `.venv/Scripts/python.exe`; no dependency sync/download or
runtime startup was performed. Commands corresponding to PR CI were attempted
locally; no GitHub workflow or PR was started.

- Initial normal targeted `python -m pytest -q <seven requested files>`:
  **104 setup errors** from sandbox access to temporary directories created with
  mode 0700; this was an environment failure before the new tests were added.
- With the process-only temporary-directory adaptation below, the seven requested
  files after the first fix: **114 passed**. After adding the owner test and
  expanding to variant integration, realtime routes, NodeForm parser and
  diagnostics: **267 passed**.
- Full `python -m pytest -q tests --tb=short` with the same adaptation:
  **720 passed, 3 failed, 137 subtests passed**. All three failures are environment
  blockers: installer Bash syntax cannot create a signal pipe (Win32 error 5);
  installer second-instance and extracted-release tests cannot initialize the
  external uv cache (`sdists-v9/.git`, access denied). They were not skipped or
  weakened. PR CI is therefore **not fully verified** here.
- CI's root-level `pytest -q` collection was also attempted, but encountered two
  inaccessible `pytest-cache-files-*` directories left by the initial run. The
  complete `tests` directory was then run explicitly as above.
- `python -m compileall -q src/funpayflow tests scripts`: passed.
- `python tests/packaging_smoke.py` (same adaptation): attempted; offline
  `uv build` blocked by external uv cache access. No wheel/install success claimed.
- `C:/Program Files/Git/bin/bash.exe -n linux/install.sh`: attempted; blocked by
  signal-pipe creation, Win32 error 5. No Linux runner was available.
- `git diff --check`: passed; final source/test diff and this new report reviewed.

The temporary-directory adaptation was confined to the invoking Python process:
wrap `os.mkdir` to substitute 0777 only for requested mode 0700, set
`tempfile.tempdir` under `.pytest_cache/audit-tmp`, and call `pytest.main` with
`--basetemp` in that directory (or `runpy.run_path` for packaging smoke).
No application permission, parser, URL, HTML, user-scope, SAFE_MODE or late action
gate was changed. This sandbox-specific run is not evidence of native Linux CI.

The expanded targeted invocation inside that process was:

```python
pytest.main([
    '-q', 'tests/test_qa_user_journeys.py', 'tests/test_telegram_products.py',
    'tests/test_product_pickers.py', 'tests/test_similar_drafts.py',
    'tests/test_node_products.py', 'tests/test_product_variants.py',
    'tests/test_product_compatibility.py',
    'tests/test_product_variant_integration.py', 'tests/test_realtime_routes.py',
    'tests/test_node_form.py', 'tests/test_node_form_diagnostics.py',
    '--basetemp=' + str(root / 'targeted-final'), '--tb=short',
])
```

## Residual unknowns and boundaries

- Live NodeForm failure for Cyberpunk 976 / Elden Ring 986 remains **unverified**.
  Existing fixtures are conceptual/synthetic; their field names, options and
  labels do not establish either live form's actual structure or compatibility.
  No parser/security relaxation is justified by this audit.
- Offline tests model callback ordering and rapid delivery within the existing
  per-user lock. They do not establish live Telegram delivery/edit reliability,
  or real-world behavior when an account/schema changes between remote events.
- Generic navigation entry buttons can intentionally abandon a wizard. Coverage
  here establishes the listed stateful callback and batch guarantees, not a proof
  over every possible Telegram navigation sequence or forged callback string.
- `CURRENT_STATE.md` was reviewed. Capabilities, publish prohibition and live
  investigation WIP remain unchanged; the specific bug evidence belongs here.
- No real FunPay/Telegram, POST, publication, real Account/session or credentials,
  production access, deployment, data migration or dependency changes occurred.
  GET-shaped requests in the journeys were handled only by existing fake methods.
