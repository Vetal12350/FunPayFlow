"""Additive migration preserves every existing table and legacy draft record."""
import log_isolation
from contextlib import closing
import sqlite3

from funpayflow.product_store import ProductStore
from funpayflow.product_templates import TemplateService
from funpayflow.state import ReviewReceiptStore


def snapshot(db):
    tables = dict(db.execute("SELECT name, sql FROM sqlite_master WHERE type='table'"))
    return {name: (schema, list(db.execute('SELECT * FROM "' + name.replace('"', '""') + '"')))
            for name, schema in tables.items()}


def test_preexisting_database_migration_and_legacy_reader_compatibility(tmp_path):
    path = tmp_path / 'state.sqlite3'
    old = ReviewReceiptStore(path)
    old.initialize()
    old.record_review_receipt('ABCD1234')
    old.record_audit_event('system', 'SETTINGS', 'settings', 'SAVED')
    old.autobump_cycle([77])
    legacy = old.create_lot_template_draft('1', 42, 11, 100,
        {'title_ru': 'Legacy', 'title_en': '', 'description_ru': 'Legacy description',
         'description_en': '', 'price': '10', 'amount': '1'}, [11])
    old.initialize()  # Settle the existing review-receipt backfill before this migration.
    with closing(sqlite3.connect(path)) as db, db:
        # Simulate the old DB: every original table is retained with its real schema.
        for table in ('product_batches', 'product_drafts', 'game_routes', 'product_templates'):
            db.execute('DROP TABLE ' + table)
        before = snapshot(db)
    upgraded = ReviewReceiptStore(path)
    upgraded.initialize()
    upgraded.initialize()
    TemplateService(ProductStore(upgraded, '1', 42)).ensure_builtin()
    with closing(sqlite3.connect(path)) as db:
        after = snapshot(db)
        assert all(after[name] == value for name, value in before.items()), {
            name: {'schema_same': after[name][0] == value[0],
                   'old_rows': len(value[1]), 'new_rows': len(after[name][1])}
            for name, value in before.items() if after[name] != value}
        assert set(after) - set(before) == {'product_templates', 'game_routes', 'product_drafts', 'product_batches'}
        assert db.execute('PRAGMA quick_check').fetchall() == [('ok',)]
    # Existing-reader contracts still work after new registry records are added.
    assert old.get_lot_template_draft('1', legacy['draft_id']) == legacy
    assert old.has_review_receipt('ABCD1234')
