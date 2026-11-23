"""Logged StateError labels name the fixed message and the hidden SQLite code, nothing else."""
import log_isolation
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from funpayflow.state import ReviewReceiptStore, StateError, error_label


class ErrorLabelTests(unittest.TestCase):
    def test_locked_database_names_sqlite_busy(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReviewReceiptStore(Path(directory) / "state.sqlite3")
            store.initialize()
            with closing(sqlite3.connect(store.path)) as holder:
                holder.execute("BEGIN EXCLUSIVE")
                store._connect = lambda: sqlite3.connect(store.path, timeout=0)
                with self.assertRaises(StateError) as caught:
                    store.record_withdrawal_observation("tx1", 1, "USD")
        self.assertEqual(
            error_label(caught.exception),
            "StateError: Persistent withdrawal write failed. (OperationalError SQLITE_BUSY)",
        )

    def test_state_error_without_cause_and_other_errors(self):
        self.assertEqual(error_label(StateError("Invalid withdrawal ID.")),
                         "StateError: Invalid withdrawal ID.")
        self.assertEqual(error_label(RuntimeError("secret token")), "RuntimeError")


if __name__ == "__main__":
    unittest.main()
