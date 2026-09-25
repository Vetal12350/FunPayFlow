"""Offline Telegram sales-import flow with synthetic ZIPs and fake Bot.download."""
import log_isolation

import asyncio
import csv
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import import_funpay_sales as engine
import telegram as ui
from state import ReviewReceiptStore


def sales_zip(order_id="AA000001"):
    row = {key: "" for key in engine.REQUIRED}
    row.update(order_uid=order_id, game_id="10", game_name="Game",
               section_type_id="digital", section_local_id="2", section_name="Section",
               buyer_user_id="42", buyer_name="PrivateBuyer", currency="USD",
               amount="12.50", created_at="2026-09-24T20:50:00Z",
               paid_at="2026-09-24T21:30:00Z", closed_at="2026-09-24T21:35:00Z",
               status="closed", role="seller", review_text="PrivateReview",
               review_rating="5", type_data=json.dumps({
                   "fields": {"summary": {"value": {"ru": "PrivateLot"}}}}))
    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=sorted(engine.REQUIRED))
    writer.writeheader()
    writer.writerow(row)
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("sales.csv", csv_buffer.getvalue().encode("utf-8-sig"))
    return result.getvalue()


class FakeBot:
    def __init__(self, data):
        self.data = data
        self.downloads = 0

    async def download(self, document, *, destination, seek):
        self.downloads += 1
        for offset in range(0, len(self.data), 64):
            destination.write(self.data[offset:offset + 64])
            destination.flush()


class FakeMessage:
    def __init__(self, bot, *, user=1, document=None, text=None):
        self.bot = bot
        self.from_user = SimpleNamespace(id=user)
        self.document = document
        self.text = text
        self.replies = []

    async def answer(self, text, **kwargs):
        self.replies.append((text, kwargs))


class FakeCallback:
    def __init__(self, action, user=1):
        self.data = action
        self.from_user = SimpleNamespace(id=user)
        self.answers = []
        self.edits = []
        self.message = self

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class TelegramSalesImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "state.sqlite3"
        self.store = ReviewReceiptStore(self.db, Path(self.tmp.name) / "missing.json")
        self.store.initialize()
        self.previous_client = ui._runtime_client
        self.previous_lock = ui._sales_import_lock
        ui._runtime_client = SimpleNamespace(review_state=self.store)
        ui._sales_import_lock = asyncio.Lock()
        ui._sales_import_session = None
        self.auth = patch.object(ui, "is_authorized", side_effect=lambda user: user == 1)
        self.auth.start()
        self.bot = FakeBot(sales_zip())

    async def asyncTearDown(self):
        async with ui._sales_import_lock:
            ui._sales_import_cleanup()
        ui._sales_import_lock = self.previous_lock
        ui._runtime_client = self.previous_client
        self.auth.stop()
        self.tmp.cleanup()

    async def open_ui(self):
        callback = FakeCallback("sales_import_open")
        await ui.callback_handler(callback)
        return callback, ui._sales_import_session["nonce"]

    async def upload(self, *, data=None, name="history.zip", size=None):
        if data is not None:
            self.bot.data = data
        document = SimpleNamespace(file_name=name,
                                   file_size=len(self.bot.data) if size is None else size,
                                   file_id="synthetic")
        message = FakeMessage(self.bot, document=document)
        await ui.text_handler(message)
        return message

    def order_count(self):
        with closing(sqlite3.connect(self.db)) as connection:
            return connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]

    async def test_owner_ui_upload_preview_dry_run_and_refresh(self):
        opened, nonce = await self.open_ui()
        self.assertIn("Импорт истории FunPay", opened.edits[0][0])
        self.assertIn("📥 Импорт продаж", str(ui._analytics_keyboard("30d")))
        before = self.db.read_bytes()
        with patch.object(ui, "import_zip", wraps=engine.import_zip) as importer:
            preview = await self.upload()
            self.assertEqual(importer.call_count, 1)
            self.assertTrue(importer.call_args.kwargs["dry_run"])
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(self.order_count(), 0)
        text = preview.replies[0][0]
        self.assertIn("Заказов: 1", text)
        for private in ("AA000001", "PrivateBuyer", "PrivateReview", "PrivateLot", "42"):
            self.assertNotIn(private, text)
        self.assertIn(nonce, str(preview.replies[0][1]["reply_markup"]))
        path = ui._sales_import_session["path"]
        self.assertTrue(path.exists())
        self.assertNotEqual(path.parent, Path.cwd())

        confirmed = FakeCallback(f"sales_import_confirm:{nonce}")
        await ui.callback_handler(confirmed)
        self.assertIn("Добавлено: 1", confirmed.edits[-1][0])
        self.assertIsNone(ui._sales_import_session)
        self.assertFalse(path.exists())
        self.assertEqual(self.order_count(), 1)
        analytics = FakeCallback("ana_over:all")
        await ui.callback_handler(analytics)
        self.assertIn("Заказов: 1", analytics.edits[0][0])
        self.assertTrue(list(self.db.parent.glob("*.pre-sales-import*.bak")))

    async def test_unauthorized_and_upload_outside_state(self):
        denied = FakeCallback("sales_import_open", user=2)
        await ui.callback_handler(denied)
        self.assertIsNone(ui._sales_import_session)
        self.assertTrue(denied.answers[0][1]["show_alert"])
        await ui.text_handler(FakeMessage(self.bot, user=2,
                                          document=SimpleNamespace(file_name="x.zip")))
        outside = await self.upload()
        self.assertEqual(self.bot.downloads, 0)
        self.assertEqual(outside.replies, [])

    async def test_non_zip_and_oversized_rejected_without_download(self):
        await self.open_ui()
        wrong = await self.upload(name="history.pdf")
        self.assertIn("ZIP-файл", wrong.replies[0][0])
        huge = await self.upload(size=20_000_001)
        self.assertIn("слишком большой", huge.replies[0][0])
        self.assertIn("uv run python", huge.replies[0][0])
        self.assertEqual(self.bot.downloads, 0)
        self.assertEqual(ui._sales_import_session["phase"], "upload")

    async def test_actual_download_limit_without_file_size(self):
        await self.open_ui()
        with patch.object(ui, "_MAX_SALES_ZIP_BYTES", 100):
            rejected = await self.upload(size=0)
        self.assertIn("Не удалось проверить", rejected.replies[0][0])
        self.assertIsNone(ui._sales_import_session)
        self.assertEqual(self.bot.downloads, 1)

    async def test_stale_confirmation_and_superseded_temp(self):
        await self.open_ui()
        await self.upload()
        old_nonce = ui._sales_import_session["nonce"]
        old_path = ui._sales_import_session["path"]
        await self.upload(data=sales_zip("BB000002"))
        new_nonce = ui._sales_import_session["nonce"]
        self.assertNotEqual(old_nonce, new_nonce)
        self.assertFalse(old_path.exists())
        stale = FakeCallback(f"sales_import_confirm:{old_nonce}")
        await ui.callback_handler(stale)
        self.assertEqual(self.order_count(), 0)
        self.assertTrue(stale.answers[0][1]["show_alert"])
        await ui.callback_handler(FakeCallback(f"sales_import_confirm:{new_nonce}"))
        self.assertEqual(self.order_count(), 1)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT order_id FROM orders").fetchone()[0],
                             "BB000002")

    async def test_other_authorized_user_cannot_claim_session(self):
        await self.open_ui()
        await self.upload()
        nonce = ui._sales_import_session["nonce"]
        path = ui._sales_import_session["path"]
        with patch.object(ui, "is_authorized", return_value=True):
            other_confirm = FakeCallback(f"sales_import_confirm:{nonce}", user=2)
            await ui.callback_handler(other_confirm)
            other_open = FakeCallback("sales_import_open", user=2)
            await ui.callback_handler(other_open)
        self.assertEqual(self.order_count(), 0)
        self.assertEqual(ui._sales_import_session["nonce"], nonce)
        self.assertTrue(path.exists())
        self.assertTrue(other_confirm.answers[0][1]["show_alert"])
        self.assertTrue(other_open.answers[0][1]["show_alert"])

    async def test_double_confirm_and_reimport_idempotent(self):
        await self.open_ui()
        await self.upload()
        nonce = ui._sales_import_session["nonce"]
        first, second = await asyncio.gather(
            ui.callback_handler(FakeCallback(f"sales_import_confirm:{nonce}")),
            ui.callback_handler(FakeCallback(f"sales_import_confirm:{nonce}")))
        self.assertEqual(self.order_count(), 1)
        self.assertEqual(len(list(self.db.parent.glob("*.pre-sales-import*.bak"))), 1)
        await self.open_ui()
        preview = await self.upload()
        self.assertIn("Новых: 0", preview.replies[0][0])
        self.assertIn("Будет обновлено: 0", preview.replies[0][0])
        self.assertIn("Без изменений: 1", preview.replies[0][0])
        nonce = ui._sales_import_session["nonce"]
        again = FakeCallback(f"sales_import_confirm:{nonce}")
        await ui.callback_handler(again)
        self.assertIn("Добавлено: 0", again.edits[-1][0])
        self.assertIn("Обновлено: 0", again.edits[-1][0])
        self.assertIn("Без изменений: 1", again.edits[-1][0])
        self.assertEqual(self.order_count(), 1)

    async def test_cancel_and_parse_error_cleanup(self):
        await self.open_ui()
        await self.upload()
        nonce = ui._sales_import_session["nonce"]
        path = ui._sales_import_session["path"]
        await ui.callback_handler(FakeCallback(f"sales_import_cancel:{nonce}"))
        self.assertFalse(path.exists())
        self.assertIsNone(ui._sales_import_session)
        stale = FakeCallback(f"sales_import_confirm:{nonce}")
        await ui.callback_handler(stale)
        self.assertEqual(self.order_count(), 0)

        await self.open_ui()
        bad = io.BytesIO()
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("sales.csv", "wrong,columns\n1,2\n")
        message = await self.upload(data=bad.getvalue())
        self.assertIn("Не удалось проверить", message.replies[0][0])
        self.assertIsNone(ui._sales_import_session)
        self.assertEqual(self.order_count(), 0)

    async def test_archive_path_rejected_and_ready_session_expires(self):
        await self.open_ui()
        bad = io.BytesIO()
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("../sales.csv", "unused")
        rejected = await self.upload(data=bad.getvalue())
        self.assertIn("Не удалось проверить", rejected.replies[0][0])
        self.assertIsNone(ui._sales_import_session)

        await self.open_ui()
        with patch.object(ui, "_SALES_IMPORT_TTL", 0.01):
            await self.upload(data=sales_zip())
            path = ui._sales_import_session["path"]
            await asyncio.sleep(0.05)
        self.assertIsNone(ui._sales_import_session)
        self.assertFalse(path.exists())

    async def test_import_error_safe_no_retry_and_changed_file_rejected(self):
        await self.open_ui()
        await self.upload()
        nonce = ui._sales_import_session["nonce"]
        path = ui._sales_import_session["path"]
        with (patch.object(ui, "import_zip", side_effect=RuntimeError("PrivateBuyer secret")) as failing,
              patch.object(ui.logger, "warning") as warning):
            failed = FakeCallback(f"sales_import_confirm:{nonce}")
            await ui.callback_handler(failed)
            self.assertEqual(failing.call_count, 1)
            self.assertNotIn("PrivateBuyer", str(warning.call_args_list))
        self.assertNotIn("PrivateBuyer", failed.edits[-1][0])
        self.assertIsNone(ui._sales_import_session)
        self.assertFalse(path.exists())
        self.assertEqual(self.order_count(), 0)

        await self.open_ui()
        await self.upload()
        nonce = ui._sales_import_session["nonce"]
        ui._sales_import_session["path"].write_bytes(sales_zip("BB000002"))
        with patch.object(ui, "import_zip") as importer:
            await ui.callback_handler(FakeCallback(f"sales_import_confirm:{nonce}"))
            importer.assert_not_called()
        self.assertEqual(self.order_count(), 0)

    async def test_cli_dry_run_regression(self):
        path = Path(self.tmp.name) / "synthetic.zip"
        path.write_bytes(sales_zip())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(engine.main([str(path), "--db", str(self.db), "--dry-run"]), 0)
        report = out.getvalue()
        self.assertIn('"unique_order_ids": 1', report)
        self.assertNotIn("PrivateBuyer", report)
        self.assertEqual(self.order_count(), 0)


if __name__ == "__main__":
    unittest.main()
