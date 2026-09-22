import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from main import _ResilientTimedRotatingFileHandler  # noqa: E402
from logging.handlers import TimedRotatingFileHandler  # noqa: E402


class LoggingResilienceTest(unittest.TestCase):
    def test_rollover_oserror_is_recovered_without_overriding_write_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            handler = _ResilientTimedRotatingFileHandler(
                str(Path(tmpdir) / "gallery.log"),
                when="midnight",
                backupCount=1,
                encoding="utf-8",
            )
            try:
                with (
                    patch.object(TimedRotatingFileHandler, "doRollover", side_effect=OSError("rename denied")),
                    patch.object(handler, "_warn_rotate_failure") as warn,
                ):
                    handler.doRollover()

                warn.assert_called_once()
                self.assertIs(handler.handleError.__func__, logging.Handler.handleError)
            finally:
                handler.close()


if __name__ == "__main__":
    unittest.main()
