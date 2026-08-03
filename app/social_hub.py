"""Persistent settings for connecting local galleries to a shared social hub."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
from typing import Any
from urllib.parse import urlparse

from store import LockedJsonDictStore


INSTANCE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{8,80}$")
CLIENT_TOKEN_RE = re.compile(r"^[a-zA-Z0-9_-]{24,256}$")
GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GITHUB_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
GITHUB_IMAGE_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
GITHUB_TOKEN_RE = re.compile(r"^[A-Za-z0-9_]{20,256}$")
DEFAULT_REQUEST_TIMEOUT_SECONDS = 45


class SocialHubSettingsStore:
    """Keep one stable client identity plus hub credentials outside config.yaml."""

    def __init__(self, config: dict, data_dir: str, *, hub_only: bool = False):
        self.config = config if isinstance(config, dict) else {}
        self.hub_only = bool(hub_only)
        self.store = LockedJsonDictStore(
            os.path.join(data_dir, "social_hub.json"),
            os.path.join(data_dir, "social_hub.lock"),
        )
        if not self.hub_only:
            self._ensure_identity()

    def _config(self) -> dict:
        value = self.config.get("social")
        return value if isinstance(value, dict) else {}

    def _ensure_identity(self) -> None:
        configured = self._config()

        def _ensure(data: dict) -> dict:
            server_token = _text(data.get("server_token"))
            if len(server_token) < 24:
                data["server_token"] = secrets.token_urlsafe(32)
            client_token = _text(data.get("client_token"))
            if not CLIENT_TOKEN_RE.fullmatch(client_token):
                client_token = secrets.token_urlsafe(32)
                data["client_token"] = client_token
            instance_id = instance_id_for_client_token(client_token)
            data["instance_id"] = instance_id
            if not _text(data.get("display_name")):
                configured_name = _text(configured.get("display_name"))
                data["display_name"] = configured_name or f"用户-{instance_id[-4:]}"
            # Older builds registered every client token hash on the hub. The
            # instance ID is now derived from the private token, so the hub no
            # longer needs to retain a client registry.
            data.pop("hub_clients", None)
            data["version"] = 2
            return data

        self.store.update(_ensure)

    def effective(self) -> dict:
        configured = self._config()
        # A dedicated hub has no local gallery identity or upstream connection.
        # Its shared secret is supplied at runtime and is never written to data/.
        persisted = {} if self.hub_only else self.store.load()
        instance_id = _text(persisted.get("instance_id"))
        hub_url = normalize_hub_url(
            os.environ.get("SOCIAL_HUB_URL")
            or persisted.get("hub_url")
            or configured.get("hub_url")
            or ""
        )
        hub_token = _text(
            os.environ.get("SOCIAL_HUB_TOKEN")
            or persisted.get("hub_token")
            or configured.get("hub_token")
        )
        display_name = _text(
            os.environ.get("SOCIAL_DISPLAY_NAME")
            or persisted.get("display_name")
            or configured.get("display_name")
        )[:80] or f"用户-{instance_id[-4:]}"
        server_token = _text(
            os.environ.get("SOCIAL_SERVER_TOKEN")
            or persisted.get("server_token")
            or configured.get("server_token")
        )
        client_token = _text(persisted.get("client_token"))
        github_repo = _text(
            os.environ.get("GALLERY_GITHUB_REPO")
            or persisted.get("github_repo")
            or configured.get("github_repo")
        )
        github_branch = _text(
            os.environ.get("GALLERY_GITHUB_BRANCH")
            or persisted.get("github_branch")
            or configured.get("github_branch")
        ) or "master"
        github_image_path = _text(
            os.environ.get("GALLERY_GITHUB_IMAGE_PATH")
            or persisted.get("github_image_path")
            or configured.get("github_image_path")
        ).strip("/") or "img"
        github_token = _text(
            os.environ.get("GALLERY_GITHUB_TOKEN")
            or persisted.get("github_token")
            or configured.get("github_token")
        )
        try:
            timeout = int(
                os.environ.get("SOCIAL_HUB_TIMEOUT")
                or configured.get("timeout_seconds")
                or DEFAULT_REQUEST_TIMEOUT_SECONDS
            )
        except (TypeError, ValueError):
            timeout = DEFAULT_REQUEST_TIMEOUT_SECONDS
        return {
            "instance_id": instance_id,
            "display_name": display_name,
            "hub_url": hub_url,
            "hub_token": hub_token,
            "server_token": server_token,
            "client_token": client_token,
            "github_repo": github_repo,
            "github_branch": github_branch,
            "github_image_path": github_image_path,
            "github_token": github_token,
            "remote": bool(hub_url),
            "timeout_seconds": max(5, min(timeout, 180)),
        }

    def verify_or_register_hub_client(
        self,
        instance_id: Any,
        client_token: Any,
    ) -> bool:
        """Verify a gallery identity without retaining a hub-side client registry."""
        try:
            normalized_instance_id = normalize_instance_id(instance_id)
            normalized_client_token = normalize_client_token(client_token)
        except ValueError:
            return False
        expected = instance_id_for_client_token(normalized_client_token)
        return hmac.compare_digest(normalized_instance_id, expected)

    def update_client(
        self,
        *,
        hub_url: str,
        display_name: str,
        hub_token: Any = None,
        github_repo: Any = None,
        github_branch: Any = None,
        github_image_path: Any = None,
        github_token: Any = None,
    ) -> dict:
        if self.hub_only:
            raise RuntimeError("social_hub_only")
        normalized_url = normalize_hub_url(hub_url)
        normalized_name = _text(display_name)[:80]
        if not normalized_name:
            raise ValueError("display_name_required")
        token_was_supplied = hub_token is not None
        normalized_token = _text(hub_token) if token_was_supplied else ""
        effective_token = normalized_token if token_was_supplied else self.effective()["hub_token"]
        if normalized_url and not effective_token:
            raise ValueError("hub_token_required")

        repo_was_supplied = github_repo is not None
        normalized_repo = _text(github_repo) if repo_was_supplied else ""
        if normalized_repo and not GITHUB_REPO_RE.fullmatch(normalized_repo):
            raise ValueError("invalid_github_repo")
        branch_was_supplied = github_branch is not None
        normalized_branch = _text(github_branch) if branch_was_supplied else ""
        if normalized_branch and not GITHUB_BRANCH_RE.fullmatch(normalized_branch):
            raise ValueError("invalid_github_branch")
        path_was_supplied = github_image_path is not None
        normalized_path = _text(github_image_path).strip("/") if path_was_supplied else ""
        if normalized_path and not GITHUB_IMAGE_PATH_RE.fullmatch(normalized_path):
            raise ValueError("invalid_github_image_path")
        github_token_was_supplied = github_token is not None
        normalized_github_token = _text(github_token) if github_token_was_supplied else ""
        if normalized_github_token and not GITHUB_TOKEN_RE.fullmatch(normalized_github_token):
            raise ValueError("invalid_github_token")

        def _update(data: dict) -> dict:
            data["hub_url"] = normalized_url
            data["display_name"] = normalized_name
            if not normalized_url:
                data.pop("hub_token", None)
            elif token_was_supplied and normalized_token:
                data["hub_token"] = normalized_token
            if repo_was_supplied:
                data["github_repo"] = normalized_repo
            if branch_was_supplied:
                data["github_branch"] = normalized_branch or "master"
            if path_was_supplied:
                data["github_image_path"] = normalized_path or "img"
            if github_token_was_supplied:
                if normalized_github_token:
                    data["github_token"] = normalized_github_token
                else:
                    data.pop("github_token", None)
            data["version"] = 2
            return data

        self.store.update(_update)
        result = self.effective()
        return result

    def public_payload(self, *, include_server_token: bool = False) -> dict:
        settings = self.effective()
        payload = {
            "mode": "remote" if settings["remote"] else "hub",
            "instance_id": settings["instance_id"],
            "display_name": settings["display_name"],
            "hub_url": settings["hub_url"],
            "hub_token_configured": bool(settings["hub_token"]),
            "server_token_configured": bool(settings["server_token"]),
            "github_repo": settings["github_repo"],
            "github_branch": settings["github_branch"],
            "github_image_path": settings["github_image_path"],
            "github_token_configured": bool(settings["github_token"]),
        }
        if include_server_token:
            payload["server_token"] = settings["server_token"]
        return payload


def normalize_hub_url(value: Any) -> str:
    raw = _text(value).rstrip("/")
    if not raw:
        return ""
    parsed = urlparse(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid_hub_url")
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise ValueError("https_hub_url_required")
    return raw


def normalize_instance_id(value: Any) -> str:
    instance_id = _text(value)
    if not INSTANCE_ID_RE.fullmatch(instance_id):
        raise ValueError("invalid_instance_id")
    return instance_id


def normalize_client_token(value: Any) -> str:
    client_token = _text(value)
    if not CLIENT_TOKEN_RE.fullmatch(client_token):
        raise ValueError("invalid_client_token")
    return client_token


def instance_id_for_client_token(value: Any) -> str:
    client_token = normalize_client_token(value)
    digest = hashlib.sha256(client_token.encode("utf-8")).hexdigest()[:32]
    return f"gallery_{digest}"


def _is_loopback_host(host: str) -> bool:
    normalized = _text(host).lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _text(value: Any) -> str:
    return str(value or "").strip()
