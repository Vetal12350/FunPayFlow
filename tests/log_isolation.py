"""Route application logger output from offline tests to a temporary directory."""

import tempfile

import logger


_test_logs = tempfile.TemporaryDirectory(prefix="funpay-offline-tests-")
logger.LOGS_DIR = _test_logs.name
