"""Thread-safe schedule_data.json store with file locking and atomic writes."""
try:
    import fcntl
except ImportError:
    class _FcntlFallback:
        LOCK_SH = 1
        LOCK_EX = 2
        LOCK_UN = 8

        @staticmethod
        def flock(_fd, _op):
            return None

    fcntl = _FcntlFallback()
import json
import logging
import os
import tempfile

logger = logging.getLogger(__name__)


class LockedJsonDictStore:
    """File-locked JSON object store with atomic same-directory replacement."""

    def __init__(self, path: str, lock_path: str | None = None):
        self.path = os.path.abspath(path)
        self.data_dir = os.path.dirname(self.path)
        self.lock_path = lock_path or f"{self.path}.lock"
        os.makedirs(self.data_dir, exist_ok=True)

    def _load_unlocked(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError) as e:
            logger.error("LockedJsonDictStore load error: %s", e)
            return {}

    def _write_unlocked(self, data: dict) -> None:
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=self.data_dir,
            prefix=f".{os.path.basename(self.path)}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def load(self) -> dict:
        with open(self.lock_path, "w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
            try:
                return self._load_unlocked()
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def save(self, data: dict) -> None:
        with open(self.lock_path, "w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                self._write_unlocked(data if isinstance(data, dict) else {})
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def update(self, callback) -> dict:
        with open(self.lock_path, "w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                data = self._load_unlocked()
                updated = callback(data)
                if updated is not None:
                    data = updated
                if not isinstance(data, dict):
                    raise TypeError("JSON store callback must return a dict or None")
                self._write_unlocked(data)
                return data
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class ImageMetadataStore(LockedJsonDictStore):
    """Shared image_metadata.json store used by web and generator processes."""

    def __init__(self, data_dir: str):
        super().__init__(
            os.path.join(data_dir, "image_metadata.json"),
            os.path.join(data_dir, ".image_metadata.lock"),
        )


class ScheduleStore:
    """File-locked, atomic read-modify-write store for schedule_data.json.

    Uses a separate lock file so concurrent processes (web server, cron,
    zhuzhu generate scripts) cannot corrupt the data.
    """

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, "schedule_data.json")
        self.lock_path = os.path.join(data_dir, "schedule_data.lock")
        os.makedirs(data_dir, exist_ok=True)

    def load(self) -> dict:
        """Read schedule_data.json under a shared lock. Returns {} if missing."""
        if not os.path.exists(self.path):
            return {}
        with open(self.lock_path, "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_SH)
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.error(f"ScheduleStore load error: {e}")
                return {}
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def save(self, data: dict) -> None:
        """Atomically write schedule_data.json under an exclusive lock."""
        with open(self.lock_path, "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                tmp_fd, tmp_path = tempfile.mkstemp(
                    dir=self.data_dir, prefix=".schedule_", suffix=".tmp"
                )
                try:
                    with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    os.replace(tmp_path, self.path)
                except Exception:
                    # Clean up temp file on failure
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                    raise
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def update(self, callback) -> None:
        """Read-modify-write under exclusive lock.

        callback(data: dict) -> dict  — receives current data, returns updated data.
        """
        with open(self.lock_path, "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                # Read
                data = {}
                if os.path.exists(self.path):
                    try:
                        with open(self.path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                    except (json.JSONDecodeError, OSError):
                        data = {}
                # Modify
                data = callback(data)
                # Atomic write
                tmp_fd, tmp_path = tempfile.mkstemp(
                    dir=self.data_dir, prefix=".schedule_", suffix=".tmp"
                )
                try:
                    with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    os.replace(tmp_path, self.path)
                except Exception:
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                    raise
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
