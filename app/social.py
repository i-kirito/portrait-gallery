"""Atomic local store for the character social feed."""
from __future__ import annotations

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

import base64
import copy
import hashlib
import hmac
import json
import os
import re
import tempfile
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any


REACTION_KINDS = ("like", "repost", "bookmark")
DEFAULT_PAGE_LIMIT = 40
MAX_PAGE_LIMIT = 100
MAX_POST_TEXT_LENGTH = 1200
MAX_AUTHOR_NAME_LENGTH = 80
_CAPABILITY_RECORD_RE = re.compile(
    r"^(?P<kind>post|comment)_(?P<nonce>[0-9a-f]{32})_(?P<tag>[0-9a-f]{32})$"
)
_UNOWNED_RECORD_RE = re.compile(
    r"^(?P<kind>post|comment)_(?P<nonce>[0-9a-f]{32})$"
)
_LEGACY_OWNED_RECORD_RE = re.compile(
    r"^(?P<kind>post|comment)_(?P<owner>[a-zA-Z0-9_-]{8,80})_[0-9a-f]{32}$"
)
_SOCIAL_AVATAR_URL_RE = re.compile(
    r"^/api/social/media/avatar_[0-9a-f]{32}\.(?:jpg|png|webp)$"
)
_SOCIAL_IMAGE_FILENAME_RE = re.compile(
    r"^social_[0-9a-f]{32}\.(?:jpg|png|webp|gif)$"
)


class SocialStoreCorruptError(RuntimeError):
    """Refuse to overwrite a social store that cannot be safely decoded."""


class SocialStore:
    """Persist social posts without coupling them to group-chat history."""

    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self.path = os.path.join(data_dir, "social.json")
        self.lock_path = os.path.join(data_dir, "social.lock")
        self._remove_stale_temp_files(data_dir)
        # Rewrite older records through the v2 whitelist at startup so fields
        # removed for data minimization do not remain on disk indefinitely.
        self.update(lambda data: data)

    @staticmethod
    def _remove_stale_temp_files(data_dir: str) -> None:
        """Remove only interrupted atomic-write files owned by this store."""
        try:
            filenames = os.listdir(data_dir)
        except OSError:
            return
        for filename in filenames:
            if not (filename.startswith(".social_") and filename.endswith(".tmp")):
                continue
            path = os.path.join(data_dir, filename)
            try:
                if os.path.isfile(path) or os.path.islink(path):
                    os.unlink(path)
            except OSError:
                continue

    def load(self) -> dict:
        with open(self.lock_path, "w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
            try:
                return self._read_unlocked()
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def update(self, callback: Callable[[dict], Any]) -> Any:
        with open(self.lock_path, "w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                data = self._read_unlocked()
                result = callback(data)
                self._write_unlocked(data)
                return copy.deepcopy(result)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def list_posts(
        self,
        limit: int = 40,
        before: str = "",
        *,
        viewer_key: str = "user:user",
        viewer_instance_id: str = "",
    ) -> dict:
        posts = self.load()["posts"]
        posts.sort(key=_post_sort_key, reverse=True)
        cursor = _text(before)
        if cursor:
            cursor_key = _cursor_key(cursor)
            posts = [post for post in posts if _post_sort_key(post) < cursor_key]
        safe_limit = _page_limit(limit)
        page = posts[:safe_limit]
        return {
            "posts": [
                _public_post(
                    item,
                    viewer_key=viewer_key,
                    viewer_instance_id=viewer_instance_id,
                )
                for item in page
            ],
            "next_cursor": (
                _cursor_for_post(page[-1]) if len(posts) > len(page) and page else ""
            ),
        }

    def create_post(
        self,
        payload: dict,
        *,
        viewer_key: str = "user:user",
        viewer_instance_id: str = "",
    ) -> dict:
        if not isinstance(payload, Mapping):
            raise ValueError("invalid_payload")

        def _create(data: dict) -> dict:
            now = _now_iso()
            post = _normalize_post({
                **payload,
                "id": _new_owned_id("post", viewer_instance_id),
                "created_at": now,
                "comments": [],
            })
            if not post["text"] and not post["media"]:
                raise ValueError("content_required")
            data["posts"].append(post)
            data["updated_at"] = now
            return _public_post(
                post,
                viewer_key=viewer_key,
                viewer_instance_id=viewer_instance_id,
            )

        return self.update(_create)

    def attach_media_urls(
        self,
        post_id: str,
        urls_by_filename: Mapping[str, str],
        *,
        viewer_instance_id: str = "",
    ) -> dict:
        """Attach external (GitHub-hosted) URLs to a post's media records."""

        def _attach(data: dict) -> dict:
            post = _find_post(data, post_id)
            if not post:
                raise KeyError(post_id)
            _require_owned_by(post, viewer_instance_id)
            for item in post.get("media", []):
                remote_url = _text(urls_by_filename.get(item.get("image_filename") or ""))
                if remote_url:
                    item["remote_url"] = remote_url
                    item["image_url"] = remote_url
            data["updated_at"] = _now_iso()
            return {"updated": True}

        return self.update(_attach)

    def get_post(
        self,
        post_id: str,
        *,
        viewer_key: str = "user:user",
        viewer_instance_id: str = "",
    ) -> dict | None:
        post = _find_post(self.load(), post_id)
        return (
            _public_post(
                post,
                viewer_key=viewer_key,
                viewer_instance_id=viewer_instance_id,
            )
            if post else None
        )

    def delete_post(
        self,
        post_id: str,
        *,
        viewer_key: str = "user:user",
        viewer_instance_id: str = "",
    ) -> dict:
        def _delete(data: dict) -> dict:
            index = _post_index(data, post_id)
            if index < 0:
                raise KeyError(post_id)
            post = data["posts"][index]
            _require_owned_by(post, viewer_instance_id)
            post = data["posts"].pop(index)
            data["updated_at"] = _now_iso()
            return {
                "deleted": True,
                "post": _public_post(
                    post,
                    viewer_key=viewer_key,
                    viewer_instance_id=viewer_instance_id,
                ),
            }

        return self.update(_delete)

    def add_comment(
        self,
        post_id: str,
        payload: dict,
        *,
        viewer_key: str = "user:user",
        viewer_instance_id: str = "",
    ) -> dict:
        if not isinstance(payload, Mapping):
            raise ValueError("invalid_payload")

        def _add(data: dict) -> dict:
            post = _find_post(data, post_id)
            if not post:
                raise KeyError(post_id)
            now = _now_iso()
            comment = _normalize_comment({
                **payload,
                "id": _new_owned_id("comment", viewer_instance_id),
                "created_at": now,
            })
            if not comment["text"]:
                raise ValueError("content_required")
            post["comments"].append(comment)
            data["updated_at"] = now
            return {
                "comment": comment,
                "post": _public_post(
                    post,
                    viewer_key=viewer_key,
                    viewer_instance_id=viewer_instance_id,
                ),
            }

        return self.update(_add)

    def delete_comment(
        self,
        post_id: str,
        comment_id: str,
        *,
        viewer_key: str = "user:user",
        viewer_instance_id: str = "",
    ) -> dict:
        def _delete(data: dict) -> dict:
            post = _find_post(data, post_id)
            if not post:
                raise KeyError(post_id)
            index = next(
                (
                    i
                    for i, item in enumerate(post["comments"])
                    if item["id"] == comment_id
                ),
                -1,
            )
            if index < 0:
                raise ValueError("comment_not_found")
            comment = post["comments"][index]
            _require_owned_by(comment, viewer_instance_id)
            comment = post["comments"].pop(index)
            data["updated_at"] = _now_iso()
            return {
                "deleted": True,
                "comment": comment,
                "post": _public_post(
                    post,
                    viewer_key=viewer_key,
                    viewer_instance_id=viewer_instance_id,
                ),
            }

        return self.update(_delete)

    def toggle_reaction(self, post_id: str, kind: str, actor_key: str) -> dict:
        """Return the current post without persisting per-viewer interaction data."""
        clean_kind = str(kind or "").strip().lower()
        if clean_kind not in REACTION_KINDS:
            raise ValueError("invalid_reaction")
        clean_actor_key = _text(actor_key)
        if not clean_actor_key:
            raise ValueError("invalid_actor")
        post = _find_post(self.load(), post_id)
        if not post:
            raise KeyError(post_id)
        return {
            "active": False,
            "local_only": True,
            "post": _public_post(
                post,
                viewer_key=clean_actor_key,
                viewer_instance_id=_instance_from_actor_key(clean_actor_key),
            ),
        }

    def _read_unlocked(self) -> dict:
        if not os.path.exists(self.path):
            return _empty_data()
        try:
            with open(self.path, "r", encoding="utf-8") as file_obj:
                raw = json.load(file_obj)
        except FileNotFoundError:
            return _empty_data()
        except (json.JSONDecodeError, OSError) as exc:
            raise SocialStoreCorruptError("social_store_corrupt") from exc
        if not isinstance(raw, Mapping) or (
            "posts" in raw and not isinstance(raw.get("posts"), (list, tuple))
        ):
            raise SocialStoreCorruptError("social_store_corrupt")
        return _normalize_data(raw)

    def _write_unlocked(self, data: dict) -> None:
        file_descriptor, temp_path = tempfile.mkstemp(
            dir=os.path.dirname(self.path), prefix=".social_", suffix=".tmp"
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as file_obj:
                json.dump(_normalize_data(data), file_obj, ensure_ascii=False, indent=2)
            os.replace(temp_path, self.path)
        except Exception:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise


def _empty_data() -> dict:
    now = _now_iso()
    return {"version": 2, "created_at": now, "updated_at": now, "posts": []}


def _normalize_data(value: Any) -> dict:
    raw = value if isinstance(value, Mapping) else {}
    data = _empty_data()
    data["created_at"] = _normalize_timestamp(
        raw.get("created_at"),
        data["created_at"],
    )
    data["updated_at"] = _normalize_timestamp(
        raw.get("updated_at"),
        data["updated_at"],
    )
    raw_posts = (
        raw.get("posts") if isinstance(raw.get("posts"), (list, tuple)) else []
    )
    posts = []
    used_post_ids: set[str] = set()
    for item in raw_posts:
        if not isinstance(item, Mapping):
            continue
        post_id = _normalize_record_id("post", item.get("id"), used_post_ids)
        posts.append(_normalize_post(item, post_id=post_id))
    data["posts"] = posts
    return data


def _normalize_post(value: Mapping[str, Any], post_id: str = "") -> dict:
    now = _now_iso()
    normalized_post_id = _text(post_id) or _text(value.get("id")) or _new_id("post")
    raw_media = (
        value.get("media")
        if isinstance(value.get("media"), (list, tuple))
        else []
    )
    media = []
    for item in raw_media:
        if not isinstance(item, Mapping):
            continue
        normalized = _normalize_media(item)
        if normalized["image_filename"] or normalized["image_url"]:
            media.append(normalized)

    raw_comments = (
        value.get("comments")
        if isinstance(value.get("comments"), (list, tuple))
        else []
    )
    comments = []
    used_comment_ids: set[str] = set()
    for item in raw_comments:
        if not isinstance(item, Mapping):
            continue
        comment_id = _normalize_record_id(
            "comment",
            item.get("id"),
            used_comment_ids,
        )
        comments.append(
            _normalize_comment(item, comment_id=comment_id)
        )

    return {
        "id": normalized_post_id,
        "author_snapshot": _normalize_author_snapshot(value.get("author_snapshot")),
        "text": _text(value.get("text"))[:MAX_POST_TEXT_LENGTH],
        "media": media,
        "comments": comments,
        "created_at": _normalize_timestamp(value.get("created_at"), now),
    }


def _normalize_comment(
    value: Mapping[str, Any],
    comment_id: str = "",
) -> dict:
    return {
        "id": _text(comment_id) or _text(value.get("id")) or _new_id("comment"),
        "author_snapshot": _normalize_author_snapshot(value.get("author_snapshot")),
        "text": _text(value.get("text"))[:MAX_POST_TEXT_LENGTH],
        "created_at": _normalize_timestamp(value.get("created_at"), _now_iso()),
    }


def _normalize_media(value: Mapping[str, Any]) -> dict:
    filename = _text(value.get("image_filename") or value.get("filename"))
    if not _SOCIAL_IMAGE_FILENAME_RE.fullmatch(filename):
        return {
            "type": "image",
            "image_filename": "",
            "image_url": "",
        }
    remote_url = _text(value.get("remote_url"))
    if remote_url and not re.fullmatch(r"https?://[^\s]+", remote_url):
        remote_url = ""
    normalized = {
        "type": "image",
        "image_filename": filename,
        "image_url": f"/api/social/media/{filename}",
    }
    if remote_url:
        normalized["remote_url"] = remote_url
        normalized["image_url"] = remote_url
    return normalized


def _normalize_author_snapshot(value: Any) -> dict:
    raw = value if isinstance(value, Mapping) else {}
    avatar = _text(raw.get("avatar"))
    if avatar and not _SOCIAL_AVATAR_URL_RE.fullmatch(avatar):
        avatar = ""
    return {
        "display_name": _text(raw.get("display_name"))[:MAX_AUTHOR_NAME_LENGTH] or "角色",
        "avatar": avatar,
    }


def _public_post(
    post: dict,
    viewer_key: str = "user:user",
    viewer_instance_id: str = "",
) -> dict:
    result = copy.deepcopy(post)
    result["can_delete"] = _record_can_delete(
        result.get("id"),
        viewer_instance_id,
    )
    for comment in result.get("comments", []):
        comment["can_delete"] = _record_can_delete(
            comment.get("id"),
            viewer_instance_id,
        )
    result["comment_count"] = len(result.get("comments", []))
    result["reaction_counts"] = {
        kind: 0 for kind in REACTION_KINDS
    }
    result["viewer_reactions"] = []
    return result


def _instance_from_actor_key(actor_key: str) -> str:
    raw = _text(actor_key)
    if ":" not in raw:
        return ""
    return raw.split(":", 1)[1]


def _require_owned_by(value: Mapping[str, Any], viewer_instance_id: str) -> None:
    """Protect remotely owned records while retaining legacy local posts."""
    if not _record_can_delete(value.get("id"), viewer_instance_id):
        raise PermissionError("social_owner_required")


def _record_can_delete(record_id: Any, viewer_instance_id: Any) -> bool:
    viewer = _text(viewer_instance_id)
    capability = _CAPABILITY_RECORD_RE.fullmatch(_text(record_id))
    if capability:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", viewer):
            return False
        expected = _ownership_tag(
            capability.group("kind"),
            capability.group("nonce"),
            viewer,
        )
        return hmac.compare_digest(capability.group("tag"), expected)
    owner_instance_id = _record_owner(record_id)
    if owner_instance_id:
        return bool(viewer and hmac.compare_digest(owner_instance_id, viewer))
    return not viewer


def _record_owner(record_id: Any) -> str:
    raw = _text(record_id)
    if _CAPABILITY_RECORD_RE.fullmatch(raw):
        return ""
    match = _LEGACY_OWNED_RECORD_RE.fullmatch(raw)
    return match.group("owner") if match else ""


def _normalize_record_id(kind: str, value: Any, used_ids: set[str]) -> str:
    raw = _text(value)
    capability = _CAPABILITY_RECORD_RE.fullmatch(raw)
    unowned = _UNOWNED_RECORD_RE.fullmatch(raw)
    if capability and capability.group("kind") == kind:
        candidate = raw
    elif unowned and unowned.group("kind") == kind:
        candidate = raw
    else:
        legacy = _LEGACY_OWNED_RECORD_RE.fullmatch(raw)
        if legacy and legacy.group("kind") == kind:
            candidate = _new_owned_id(kind, legacy.group("owner"))
        else:
            candidate = _new_id(kind)
    while candidate in used_ids:
        candidate = _new_id(kind)
    used_ids.add(candidate)
    return candidate


def _normalize_timestamp(value: Any, fallback: str) -> str:
    raw = _text(value)
    if not raw or len(raw) > 64:
        return fallback
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        return fallback
    return parsed.astimezone(timezone.utc).isoformat()


def _post_index(data: dict, post_id: str) -> int:
    return next(
        (i for i, item in enumerate(data["posts"]) if item["id"] == post_id),
        -1,
    )


def _find_post(data: dict, post_id: str) -> dict | None:
    index = _post_index(data, post_id)
    return data["posts"][index] if index >= 0 else None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _page_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_PAGE_LIMIT
    return max(1, min(parsed, MAX_PAGE_LIMIT))


def _post_sort_key(post: Mapping[str, Any]) -> tuple[str, str]:
    return (_text(post.get("created_at")), _text(post.get("id")))


def _cursor_for_post(post: Mapping[str, Any]) -> str:
    payload = json.dumps(
        list(_post_sort_key(post)),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _cursor_key(cursor: str) -> tuple[str, str]:
    raw = _text(cursor)
    try:
        payload = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        decoded = json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise ValueError("invalid_cursor") from exc
    if (
        not isinstance(decoded, list)
        or len(decoded) != 2
        or not all(isinstance(value, str) for value in decoded)
    ):
        raise ValueError("invalid_cursor")
    key = tuple(value.strip() for value in decoded)
    if not all(key):
        raise ValueError("invalid_cursor")
    return key


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _new_owned_id(prefix: str, owner: Any) -> str:
    normalized_owner = _text(owner)
    nonce = uuid.uuid4().hex
    if re.fullmatch(r"[a-zA-Z0-9_-]{8,80}", normalized_owner):
        return f"{prefix}_{nonce}_{_ownership_tag(prefix, nonce, normalized_owner)}"
    return f"{prefix}_{nonce}"


def _ownership_tag(prefix: str, nonce: str, owner: str) -> str:
    message = f"{prefix}:{nonce}".encode("ascii")
    return hmac.new(
        owner.encode("ascii"),
        message,
        hashlib.sha256,
    ).hexdigest()[:32]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
