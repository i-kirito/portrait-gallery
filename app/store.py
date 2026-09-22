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
import hashlib
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
_local_locks: dict[str, threading.RLock] = {}


def _local_lock(lock_path: str) -> threading.RLock:
    key = os.path.abspath(lock_path)
    with _degrade_state_lock:
        return _local_locks.setdefault(key, threading.RLock())


def _fallback_lock_path(lock_path: str) -> str:
    digest = hashlib.sha256(os.path.abspath(lock_path).encode("utf-8")).hexdigest()[:32]
    return os.path.join(tempfile.gettempdir(), f"portrait-gallery-lock-{digest}.lock")


def _warn_lock_degraded(lock_path: str, exc: BaseException, fallback_path: str = "") -> None:
    """Log a rate-limited warning about a lock path problem."""
    now = time.monotonic()
    with _degrade_state_lock:
        last = _degrade_state.get(lock_path, 0.0)
        if now - last < _DEGRADE_WARN_INTERVAL_SECONDS:
            return
        _degrade_state[lock_path] = now
    if fallback_path:
        logger.warning(
            "文件锁路径不可用，已改用共享临时锁: %s -> %s (%s: %s)",
            lock_path,
            fallback_path,
            type(exc).__name__,
            exc,
        )
    else:
        logger.warning(
            "文件锁不可用，已降级为无锁读写（数据仍会原子写入，但跨进程并发保护失效）: %s (%s: %s)",
            lock_path,
            type(exc).__name__,
            exc,
        )


@contextlib.contextmanager
def _locked_file(lock_path: str, operation: int):
    """Use a shared file lock, with a safe fallback for inaccessible lock paths."""
    local_lock = _local_lock(lock_path)
    with local_lock:
        lock_file = None
        try:
            try:
                lock_file = open(lock_path, "a")
            except OSError as original_error:
                fallback_path = _fallback_lock_path(lock_path)
                try:
                    lock_file = open(fallback_path, "a")
                except OSError:
                    _warn_lock_degraded(lock_path, original_error)
                    if operation == fcntl.LOCK_EX:
                        raise original_error
                    yield
                    return
                _warn_lock_degraded(lock_path, original_error, fallback_path)

            try:
                fcntl.flock(lock_file.fileno(), operation)
            except OSError as lock_error:
                _warn_lock_degraded(lock_path, lock_error)
                if operation == fcntl.LOCK_EX:
                    raise
                yield
                return

            try:
                yield
            finally:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except OSError as unlock_error:
                    _warn_lock_degraded(lock_path, unlock_error)
        finally:
            if lock_file is not None:
                lock_file.close()


class LockedJsonDictStore:
    """File-locked JSON object store with atomic same-directory replacement.

    If the lock file cannot be opened (e.g. ``PermissionError`` from macOS TCC
    restrictions on an external volume), a shared temporary lock is used.
    Exclusive operations fail closed if both lock locations are unavailable;
    read-only operations may continue without a lock in that last-resort case.
    """

    def __init__(self, path: str, lock_path: str | None = None):
        self.path = os.path.abspath(path)
        self.data_dir = os.path.dirname(self.path)
        self.lock_path = lock_path or f"{self.path}.lock"
        os.makedirs(self.data_dir, exist_ok=True)

    @contextlib.contextmanager
    def _locked(self, operation: int):
        """Hold the advisory lock, using a shared temporary lock if needed."""
        with _locked_file(self.lock_path, operation):
            yield

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

    @contextlib.contextmanager
    def _locked(self, operation: int):
        with _locked_file(self.lock_path, operation):
            yield

    def load(self) -> dict:
        """Read schedule_data.json under a shared lock. Returns {} if missing."""
        if not os.path.exists(self.path):
            return {}
        with self._locked(fcntl.LOCK_SH):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.error(f"ScheduleStore load error: {e}")
                return {}

    def save(self, data: dict) -> None:
        """Atomically write schedule_data.json under an exclusive lock."""
        with self._locked(fcntl.LOCK_EX):
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

    def update(self, callback) -> None:
        """Read-modify-write under exclusive lock.

        callback(data: dict) -> dict  — receives current data, returns updated data.
        """
        with self._locked(fcntl.LOCK_EX):
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
