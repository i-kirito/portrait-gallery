"""Picxazz upload integration for favorite gallery images."""
import json
import logging
import mimetypes
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


DEFAULT_BASE_URL = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _bool_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return default


@dataclass
class PicxazzSyncConfig:
    enabled: bool
    base_url: str
    api_key: str
    is_public: bool = False
    watermark: bool = False
    timeout_seconds: float = 60.0

    @property
    def upload_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/api/v1/upload"


class PicxazzSyncClient:
    """Upload favorite images to a configured picxazz instance."""

    def __init__(self, app_config: dict, data_dir: str):
        self.app_config = app_config or {}
        self.data_dir = data_dir

    def load_config(self) -> PicxazzSyncConfig:
        configured: dict[str, Any] = {}
        configured.update(
            ((self.app_config.get("integrations") or {}).get("picxazz") or {})
        )
        configured.update(self._load_runtime_config())

        base_url = (
            os.environ.get("PICXAZZ_BASE_URL")
            or configured.get("base_url")
            or DEFAULT_BASE_URL
        )
        base_url = str(base_url or "").strip().rstrip("/")
        api_key = (
            os.environ.get("PICXAZZ_API_KEY")
            or configured.get("api_key")
            or configured.get("token")
            or ""
        )
        enabled_raw = os.environ.get("PICXAZZ_SYNC_ENABLED")
        enabled = _bool_value(
            enabled_raw if enabled_raw is not None else configured.get("enabled"),
            default=False,
        ) and bool(base_url)

        timeout_raw = os.environ.get("PICXAZZ_SYNC_TIMEOUT") or configured.get("timeout_seconds", 60.0)
        try:
            timeout_seconds = max(5.0, float(timeout_raw))
        except (TypeError, ValueError):
            timeout_seconds = 60.0

        return PicxazzSyncConfig(
            enabled=enabled,
            base_url=base_url,
            api_key=str(api_key),
            is_public=_bool_value(
                os.environ.get("PICXAZZ_SYNC_PUBLIC", configured.get("is_public")),
                default=False,
            ),
            watermark=_bool_value(
                os.environ.get("PICXAZZ_SYNC_WATERMARK", configured.get("watermark")),
                default=False,
            ),
            timeout_seconds=timeout_seconds,
        )

    def _load_runtime_config(self) -> dict[str, Any]:
        path = Path(self.data_dir) / "plugin_config.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            value = data.get("picxazz_sync", {})
            return value if isinstance(value, dict) else {}
        except Exception as exc:
            logger.warning("Load picxazz sync config failed: %s", exc)
            return {}

    @staticmethod
    def should_skip(entry: dict, force: bool = False) -> bool:
        if force:
            return False
        sync_state = entry.get("picxazz_sync") or {}
        return bool(sync_state.get("url") or sync_state.get("key")) and sync_state.get("status") == "synced"

    async def upload(self, image_path: str, entry: dict, force: bool = False) -> dict:
        if self.should_skip(entry, force=force):
            current = dict(entry.get("picxazz_sync") or {})
            current.setdefault("status", "synced")
            current.setdefault("skipped", True)
            return current

        cfg = self.load_config()
        if not cfg.enabled:
            return {
                "status": "disabled",
                "base_url": cfg.base_url,
                "updated_at": _utc_now(),
            }
        if not cfg.api_key:
            return {
                "status": "failed",
                "base_url": cfg.base_url,
                "error": "missing_api_key",
                "updated_at": _utc_now(),
            }

        path = Path(image_path)
        if not path.is_file():
            return {
                "status": "failed",
                "base_url": cfg.base_url,
                "error": "image_not_found",
                "updated_at": _utc_now(),
            }

        filename = entry.get("image_filename") or path.name
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        try:
            payload = path.read_bytes()
            form = aiohttp.FormData()
            form.add_field("file", payload, filename=filename, content_type=content_type)
            form.add_field("is_public", "true" if cfg.is_public else "false")
            form.add_field("watermark", "true" if cfg.watermark else "false")

            timeout = aiohttp.ClientTimeout(total=cfg.timeout_seconds)
            headers = {"Authorization": f"Bearer {cfg.api_key}"}
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.post(cfg.upload_url, data=form, headers=headers) as resp:
                    text = await resp.text()
                    if resp.status >= 400:
                        return {
                            "status": "failed",
                            "base_url": cfg.base_url,
                            "error": f"picxazz_http_{resp.status}",
                            "detail": text[:300],
                            "updated_at": _utc_now(),
                        }
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        return {
                            "status": "failed",
                            "base_url": cfg.base_url,
                            "error": "invalid_picxazz_response",
                            "updated_at": _utc_now(),
                        }

            upload_data = data.get("data") or {}
            if not data.get("status") or not upload_data:
                return {
                    "status": "failed",
                    "base_url": cfg.base_url,
                    "error": data.get("message") or "upload_failed",
                    "updated_at": _utc_now(),
                }

            image_url = upload_data.get("url")
            if isinstance(image_url, str) and image_url.startswith("/"):
                image_url = f"{cfg.base_url}{image_url}"

            return {
                "status": "synced",
                "base_url": cfg.base_url,
                "key": upload_data.get("key") or upload_data.get("pathname"),
                "name": upload_data.get("name"),
                "url": image_url,
                "size": upload_data.get("size"),
                "mimetype": upload_data.get("mimetype"),
                "md5": upload_data.get("md5"),
                "sha256": upload_data.get("sha256"),
                "uploaded_at": _utc_now(),
            }
        except Exception as exc:
            logger.warning("Picxazz favorite sync failed for %s: %s", filename, exc)
            return {
                "status": "failed",
                "base_url": cfg.base_url,
                "error": str(exc),
                "updated_at": _utc_now(),
            }
