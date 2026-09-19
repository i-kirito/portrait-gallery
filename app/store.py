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
import contextlib
import json
import logging
import os
import tempfile
import threading
import time

logger = logging.getLogger(__name__)

# Lock degradation warning de-duplication: warn once per lock path per interval.
_DEGRADE_WARN_INTERVAL_SECONDS = 300
_degrade_state: dict[str, float] = {}
_degrade_state_lock = threading.Lock()


def _warn_lock_degraded(lock_path: str, exc: BaseException) -> None:
    """Log a rate-limited warning that file locking has been degraded."""
    now = time.monotonic()
    with _degrade_state_lock:
        last = _degrade_state.get(lock_path, 0.0)
        if now - last < _DEGRADE_WARN_INTERVAL_SECONDS:
            return
        _degrade_state[lock_path] = now
    logger.warning(
        "文件锁不可用，已降级为无锁读写（数据仍会原子写入，但跨进程并发保护失效）: %s (%s: %s)",
        lock_path,
        type(exc).__name__,
        exc,
    )


class LockedJsonDictStore:
    """File-locked JSON object store with atomic same-directory replacement.

    If the lock file cannot be opened (e.g. ``PermissionError`` from macOS TCC
    restrictions on an external volume), locking degrades to a no-op so readers
    and writers still work; atomic replace keeps single-process writes safe.
    """

    def __init__(self, path: str, lock_path: str | None = None):
        self.path = os.path.abspath(path)
        self.data_dir = os.path.dirname(self.path)
        self.lock_path = lock_path or f"{self.path}.lock"
        os.makedirs(self.data_dir, exist_ok=True)

    @contextlib.contextmanager
    def _locked(self, operation: int):
        """Hold the advisory lock, degrading to no locking if unavailable."""
        try:
            lock_file = open(self.lock_path, "w")
        except (PermissionError, OSError) as e:
            _warn_lock_degraded(self.lock_path, e)
            yield
            return
        try:
            fcntl.flock(lock_file.fileno(), operation)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()

    def _load_unlocked(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("JSON store must contain an object")
            return data
        except (json.JSONDecodeError, OSError) as e:
            logger.error("LockedJsonDictStore load error: %s", e)
            raise

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
        with self._locked(fcntl.LOCK_SH):
            return self._load_unlocked()

    def save(self, data: dict) -> None:
        with self._locked(fcntl.LOCK_EX):
            self._write_unlocked(data if isinstance(data, dict) else {})

    def update(self, callback) -> dict:
        with self._locked(fcntl.LOCK_EX):
            data = self._load_unlocked()
            updated = callback(data)
            if updated is not None:
                data = updated
            if not isinstance(data, dict):
                raise TypeError("JSON store callback must return a dict or None")
            self._write_unlocked(data)
            return data


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
                        raise
                if not isinstance(data, dict):
                    raise ValueError("Schedule store must contain an object")
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
