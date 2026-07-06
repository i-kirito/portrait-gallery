"""Group chat persistence and Hermes Agent bridge payload helpers.

This module is intentionally route-agnostic. Importing it does not start
network clients, call Hermes, or touch schedule_data.json.
"""
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

import copy
import json
import logging
import os
import tempfile
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

STORE_VERSION = 1
GROUP_CHAT_FILENAME = "group_chat.json"
GROUP_CHAT_LOCK_FILENAME = "group_chat.lock"

INTEGRATION_DISABLED = "disabled"
INTEGRATION_CONFIG_MISSING = "config_missing"
INTEGRATION_READY = "ready"
INTEGRATION_STATUSES = {
    INTEGRATION_DISABLED,
    INTEGRATION_CONFIG_MISSING,
    INTEGRATION_READY,
}

DEFAULT_HERMES_BRIDGE_CONFIG = {
    "enabled": False,
    "endpoint": "",
    "workspace_id": "",
    "group_id": "",
    "api_key_env": "HERMES_AGENT_API_KEY",
    "timeout_seconds": 30,
}


class GroupChatStore:
    """File-locked, atomic JSON store for group chat rooms and messages."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, GROUP_CHAT_FILENAME)
        self.lock_path = os.path.join(data_dir, GROUP_CHAT_LOCK_FILENAME)
        os.makedirs(data_dir, exist_ok=True)

    def load(self) -> dict:
        """Read group_chat.json under a shared lock."""
        with open(self.lock_path, "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_SH)
            try:
                return self._read_unlocked()
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def save(self, data: dict) -> None:
        """Atomically write group_chat.json under an exclusive lock."""
        with open(self.lock_path, "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                self._write_unlocked(_normalize_data(data))
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def update(self, callback: Callable[[dict], Any]) -> Any:
        """Run read-modify-write under an exclusive lock.

        The callback receives mutable normalized store data. Its return value is
        returned to the caller after the store is written.
        """
        with open(self.lock_path, "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                data = self._read_unlocked()
                result = callback(data)
                self._write_unlocked(data)
                return copy.deepcopy(result)
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def list_rooms(self) -> list[dict]:
        """Return rooms sorted by most recently updated first."""
        data = self.load()
        rooms = list(data.get("rooms", {}).values())
        rooms.sort(key=lambda room: room.get("updated_at", ""), reverse=True)
        return copy.deepcopy(rooms)

    def create_room(
        self,
        name: str | Mapping[str, Any] = "",
        participants: list[dict] | None = None,
        metadata: dict | None = None,
        room_id: str = "",
        description: str = "",
    ) -> dict:
        """Create a room and optional character-agent participant bindings."""
        payload: Mapping[str, Any] = {}
        if isinstance(name, Mapping):
            payload = name
            name = _clean_text(payload.get("name"))
            participants = payload.get("participants", participants)
            metadata = payload.get("metadata", metadata)
            room_id = _clean_text(payload.get("id") or payload.get("room_id") or room_id)
            description = _clean_text(payload.get("description") or description)

        def _create(data: dict) -> dict:
            rooms = data.setdefault("rooms", {})
            now = _now_iso()
            new_id = _clean_text(room_id) or _new_id("room")
            while new_id in rooms:
                new_id = _new_id("room")

            room = _normalize_room({
                "id": new_id,
                "name": _clean_text(name) or "Group Chat",
                "description": _clean_text(description),
                "participants": participants or [],
                "metadata": metadata or {},
                "created_at": now,
                "updated_at": now,
                "message_count": 0,
            })
            rooms[new_id] = room
            data.setdefault("messages", {}).setdefault(new_id, [])
            data["updated_at"] = now
            return room

        return self.update(_create)

    def get_room(self, room_id: str) -> dict | None:
        """Return a room by id, or None when it does not exist."""
        data = self.load()
        room = data.get("rooms", {}).get(_clean_text(room_id))
        return copy.deepcopy(room) if room else None

    def update_room(
        self,
        room_id: str,
        name: str | None = None,
        description: str | None = None,
        participants: list[dict] | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Update room metadata and participants without touching messages."""
        clean_room_id = _clean_text(room_id)

        def _update(data: dict) -> dict:
            room = data.get("rooms", {}).get(clean_room_id)
            if not room:
                raise KeyError(f"group chat room not found: {clean_room_id}")

            now = _now_iso()
            if name is not None:
                room["name"] = _clean_text(name) or room.get("name") or "Group Chat"
            if description is not None:
                room["description"] = _clean_text(description)
            if participants is not None:
                room["participants"] = [
                    _normalize_participant(item, now)
                    for item in participants
                    if isinstance(item, Mapping)
                ]
            if metadata is not None:
                merged_metadata = _json_safe_mapping(room.get("metadata"))
                merged_metadata.update(_json_safe_mapping(metadata))
                room["metadata"] = merged_metadata
            room["updated_at"] = now
            data["updated_at"] = now
            return _normalize_room(room)

        return self.update(_update)

    def add_message(
        self,
        room_id: str,
        content: Any = None,
        sender: dict | None = None,
        metadata: dict | None = None,
        message_type: str = "text",
        role: str = "",
        message: dict | None = None,
    ) -> dict:
        """Append a message to a room and update room counters.

        For route handlers, the second argument may also be a full message dict.
        """
        if isinstance(content, Mapping) and message is None:
            message_payload = dict(content)
        else:
            message_payload = dict(message or {})
            if content is not None:
                message_payload["content"] = content
        if sender is not None:
            message_payload["sender"] = sender
        if metadata is not None:
            message_payload["metadata"] = metadata
        if message_type:
            message_payload["type"] = message_type
        if role:
            message_payload["role"] = role

        clean_room_id = _clean_text(room_id)

        def _append(data: dict) -> dict:
            room = data.get("rooms", {}).get(clean_room_id)
            if not room:
                raise KeyError(f"group chat room not found: {clean_room_id}")

            now = _now_iso()
            item = _normalize_message(clean_room_id, message_payload, now)
            messages = data.setdefault("messages", {}).setdefault(clean_room_id, [])
            messages.append(item)
            room["message_count"] = len(messages)
            room["updated_at"] = now
            data["updated_at"] = now
            return item

        return self.update(_append)

    def list_messages(
        self,
        room_id: str,
        limit: int | None = None,
        offset: int = 0,
        after_id: str = "",
        before_id: str = "",
    ) -> list[dict]:
        """Return room messages in chronological order.

        With a limit and no cursor/offset, the newest N messages are returned,
        still sorted oldest-to-newest for display.
        """
        data = self.load()
        messages = list(data.get("messages", {}).get(_clean_text(room_id), []))
        messages = _slice_messages(messages, limit=limit, offset=offset, after_id=after_id, before_id=before_id)
        return copy.deepcopy(messages)

    def truncate_after_message(self, room_id: str, message_id: str) -> dict:
        """Remove messages after message_id and return the kept target message."""
        clean_room_id = _clean_text(room_id)
        clean_message_id = _clean_text(message_id)
        if not clean_message_id:
            raise ValueError("message_id_required")

        def _truncate(data: dict) -> dict:
            room = data.get("rooms", {}).get(clean_room_id)
            if not room:
                raise KeyError(f"group chat room not found: {clean_room_id}")

            messages = data.setdefault("messages", {}).setdefault(clean_room_id, [])
            index = _message_index(messages, clean_message_id)
            if index < 0:
                raise ValueError("message_not_found")

            now = _now_iso()
            target = messages[index]
            removed = messages[index + 1:]
            data["messages"][clean_room_id] = messages[:index + 1]
            room["message_count"] = len(data["messages"][clean_room_id])
            room["updated_at"] = now
            data["updated_at"] = now
            return {
                "message": copy.deepcopy(target),
                "removed_count": len(removed),
                "messages": copy.deepcopy(data["messages"][clean_room_id]),
            }

        return self.update(_truncate)

    def bind_agent(
        self,
        room_id: str,
        character_id: str | Mapping[str, Any],
        agent_id: str = "",
        display_name: str = "",
        provider: str = "hermes",
        enabled: bool = True,
        metadata: dict | None = None,
    ) -> dict:
        """Bind or update a room participant's Hermes Agent configuration."""
        if isinstance(character_id, Mapping):
            participant_payload = dict(character_id)
        else:
            participant_payload = {
                "character_id": character_id,
                "agent_id": agent_id,
                "display_name": display_name,
                "provider": provider,
                "enabled": enabled,
                "metadata": metadata or {},
            }

        clean_room_id = _clean_text(room_id)

        def _bind(data: dict) -> dict:
            room = data.get("rooms", {}).get(clean_room_id)
            if not room:
                raise KeyError(f"group chat room not found: {clean_room_id}")

            now = _now_iso()
            participant = _normalize_participant(participant_payload, now)
            if not participant.get("character_id"):
                raise ValueError("character_id is required to bind an agent")

            replaced = False
            participants = room.setdefault("participants", [])
            for index, current in enumerate(participants):
                if current.get("character_id") == participant["character_id"]:
                    merged = dict(current)
                    merged.update(participant)
                    merged["created_at"] = current.get("created_at") or participant.get("created_at") or now
                    merged["updated_at"] = now
                    participants[index] = _normalize_participant(merged, now)
                    participant = participants[index]
                    replaced = True
                    break
            if not replaced:
                participants.append(participant)

            room["participants"] = [_normalize_participant(item, now) for item in participants]
            room["updated_at"] = now
            data["updated_at"] = now
            return participant

        return self.update(_bind)

    def room_payload(
        self,
        room_id: str,
        message_limit: int | None = 50,
        include_messages: bool = True,
        include_hermes_bridge: bool = True,
        integration_config: dict | None = None,
    ) -> dict | None:
        """Return a route-ready payload for a room and optional bridge config."""
        data = self.load()
        clean_room_id = _clean_text(room_id)
        room = data.get("rooms", {}).get(clean_room_id)
        if not room:
            return None

        messages = []
        if include_messages:
            messages = _slice_messages(data.get("messages", {}).get(clean_room_id, []), limit=message_limit)

        payload = {
            "room": copy.deepcopy(room),
            "participants": copy.deepcopy(room.get("participants", [])),
            "messages": copy.deepcopy(messages),
        }
        if include_hermes_bridge:
            latest_message = messages[-1] if messages else None
            payload["hermes_bridge"] = build_hermes_bridge_payload(
                room=room,
                message=latest_message,
                participants=room.get("participants", []),
                integration_config=integration_config,
            )
        return payload

    def _read_unlocked(self) -> dict:
        if not os.path.exists(self.path):
            return _empty_data()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return _normalize_data(json.load(f))
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"GroupChatStore load error: {e}")
            return _empty_data()

    def _write_unlocked(self, data: dict) -> None:
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=self.data_dir, prefix=".group_chat_", suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(_normalize_data(data), f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.path)
        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise


def build_hermes_bridge_payload(
    room: dict,
    message: dict | None = None,
    participants: list[dict] | None = None,
    integration_config: dict | None = None,
) -> dict:
    """Build a Hermes Agent group-chat adapter payload without sending it."""
    room_data = _normalize_room(room)
    bridge_config = _normalize_hermes_bridge_config(
        integration_config if integration_config is not None else room_data.get("metadata", {}).get("hermes_bridge")
    )
    participant_items = [
        _normalize_participant(item)
        for item in (participants if participants is not None else room_data.get("participants", []))
        if isinstance(item, Mapping)
    ]
    agent_participants = [
        item for item in participant_items
        if item.get("enabled") and item.get("agent_id")
    ]

    missing = []
    if bridge_config.get("enabled"):
        if not bridge_config.get("endpoint"):
            missing.append("endpoint")
        if not bridge_config.get("group_id"):
            missing.append("group_id")
        if not agent_participants:
            missing.append("enabled_agent_participants")

    if not bridge_config.get("enabled"):
        status = INTEGRATION_DISABLED
    elif missing:
        status = INTEGRATION_CONFIG_MISSING
    else:
        status = INTEGRATION_READY

    return {
        "integration_status": status,
        "missing": missing,
        "adapter": "hermes_agent_group_chat",
        "config": bridge_config,
        "room": {
            "id": room_data.get("id", ""),
            "name": room_data.get("name", ""),
            "description": room_data.get("description", ""),
            "metadata": room_data.get("metadata", {}),
        },
        "participants": participant_items,
        "agent_participants": agent_participants,
        "message": _normalize_bridge_message(message, room_data.get("id", "")) if message else None,
    }


def list_rooms(data_dir: str) -> list[dict]:
    """Convenience wrapper for GroupChatStore(data_dir).list_rooms()."""
    return GroupChatStore(data_dir).list_rooms()


def create_room(data_dir: str, *args, **kwargs) -> dict:
    """Convenience wrapper for GroupChatStore(data_dir).create_room()."""
    return GroupChatStore(data_dir).create_room(*args, **kwargs)


def get_room(data_dir: str, room_id: str) -> dict | None:
    """Convenience wrapper for GroupChatStore(data_dir).get_room()."""
    return GroupChatStore(data_dir).get_room(room_id)


def update_room(data_dir: str, room_id: str, *args, **kwargs) -> dict:
    """Convenience wrapper for GroupChatStore(data_dir).update_room()."""
    return GroupChatStore(data_dir).update_room(room_id, *args, **kwargs)


def add_message(data_dir: str, room_id: str, *args, **kwargs) -> dict:
    """Convenience wrapper for GroupChatStore(data_dir).add_message()."""
    return GroupChatStore(data_dir).add_message(room_id, *args, **kwargs)


def list_messages(data_dir: str, room_id: str, *args, **kwargs) -> list[dict]:
    """Convenience wrapper for GroupChatStore(data_dir).list_messages()."""
    return GroupChatStore(data_dir).list_messages(room_id, *args, **kwargs)


def bind_agent(data_dir: str, room_id: str, *args, **kwargs) -> dict:
    """Convenience wrapper for GroupChatStore(data_dir).bind_agent()."""
    return GroupChatStore(data_dir).bind_agent(room_id, *args, **kwargs)


def room_payload(data_dir: str, room_id: str, *args, **kwargs) -> dict | None:
    """Convenience wrapper for GroupChatStore(data_dir).room_payload()."""
    return GroupChatStore(data_dir).room_payload(room_id, *args, **kwargs)


def _empty_data() -> dict:
    now = _now_iso()
    return {
        "version": STORE_VERSION,
        "created_at": now,
        "updated_at": now,
        "rooms": {},
        "messages": {},
    }


def _normalize_data(data: Any) -> dict:
    now = _now_iso()
    if not isinstance(data, Mapping):
        data = {}

    normalized = {
        "version": STORE_VERSION,
        "created_at": _clean_text(data.get("created_at")) or now,
        "updated_at": _clean_text(data.get("updated_at")) or now,
        "rooms": {},
        "messages": {},
    }

    raw_rooms = data.get("rooms", {})
    if isinstance(raw_rooms, list):
        room_items = raw_rooms
    elif isinstance(raw_rooms, Mapping):
        room_items = raw_rooms.values()
    else:
        room_items = []

    for raw_room in room_items:
        if not isinstance(raw_room, Mapping):
            continue
        room = _normalize_room(raw_room)
        if room.get("id"):
            normalized["rooms"][room["id"]] = room

    raw_messages = data.get("messages", {})
    if isinstance(raw_messages, Mapping):
        for raw_room_id, raw_items in raw_messages.items():
            room_id = _clean_text(raw_room_id)
            if not room_id or not isinstance(raw_items, list):
                continue
            normalized["messages"][room_id] = [
                _normalize_message(room_id, item, _clean_text(item.get("created_at")) or now)
                for item in raw_items
                if isinstance(item, Mapping)
            ]

    for room_id, room in normalized["rooms"].items():
        normalized["messages"].setdefault(room_id, [])
        room["message_count"] = len(normalized["messages"][room_id])

    return normalized


def _normalize_room(room: Any) -> dict:
    now = _now_iso()
    if not isinstance(room, Mapping):
        room = {}
    room_id = _clean_text(room.get("id") or room.get("room_id"))
    metadata = _json_safe_mapping(room.get("metadata"))
    metadata["hermes_bridge"] = _normalize_hermes_bridge_config(metadata.get("hermes_bridge"))
    participants = room.get("participants", [])
    if not isinstance(participants, list):
        participants = []

    return {
        "id": room_id,
        "name": _clean_text(room.get("name")) or "Group Chat",
        "description": _clean_text(room.get("description")),
        "participants": [
            _normalize_participant(item)
            for item in participants
            if isinstance(item, Mapping)
        ],
        "metadata": metadata,
        "created_at": _clean_text(room.get("created_at")) or now,
        "updated_at": _clean_text(room.get("updated_at")) or now,
        "message_count": _as_int(room.get("message_count"), 0),
    }


def _normalize_participant(participant: Any, now: str | None = None) -> dict:
    now = now or _now_iso()
    if not isinstance(participant, Mapping):
        participant = {}
    character_id = _clean_text(participant.get("character_id") or participant.get("characterId"))
    display_name = _clean_text(participant.get("display_name") or participant.get("displayName"))
    return {
        "character_id": character_id,
        "agent_id": _clean_text(participant.get("agent_id") or participant.get("agentId")),
        "display_name": display_name or character_id,
        "provider": _clean_text(participant.get("provider")) or "hermes",
        "enabled": _as_bool(participant.get("enabled"), True),
        "metadata": _json_safe_mapping(participant.get("metadata")),
        "created_at": _clean_text(participant.get("created_at")) or now,
        "updated_at": _clean_text(participant.get("updated_at")) or now,
    }


def _normalize_message(room_id: str, message: Any, now: str | None = None) -> dict:
    now = now or _now_iso()
    if not isinstance(message, Mapping):
        message = {"content": message}
    sender = _normalize_sender(message.get("sender"), message)
    role = _clean_text(message.get("role")) or _role_from_sender(sender)
    return {
        "id": _clean_text(message.get("id") or message.get("message_id")) or _new_id("msg"),
        "room_id": _clean_text(message.get("room_id") or room_id),
        "role": role,
        "type": _clean_text(message.get("type") or message.get("message_type")) or "text",
        "content": _json_safe(message.get("content", "")),
        "sender": sender,
        "metadata": _json_safe_mapping(message.get("metadata")),
        "created_at": _clean_text(message.get("created_at")) or now,
    }


def _normalize_sender(sender: Any, message: Mapping[str, Any]) -> dict:
    if not isinstance(sender, Mapping):
        sender = {}
    sender_type = _clean_text(sender.get("type") or message.get("sender_type")) or "user"
    sender_id = _clean_text(sender.get("id") or message.get("sender_id"))
    display_name = _clean_text(sender.get("display_name") or sender.get("displayName") or message.get("sender_name"))
    character_id = _clean_text(sender.get("character_id") or sender.get("characterId") or message.get("character_id"))
    agent_id = _clean_text(sender.get("agent_id") or sender.get("agentId") or message.get("agent_id"))
    return {
        "type": sender_type,
        "id": sender_id or character_id or agent_id,
        "display_name": display_name or sender_id or character_id or agent_id or sender_type,
        "character_id": character_id,
        "agent_id": agent_id,
    }


def _normalize_bridge_message(message: Any, room_id: str) -> dict:
    created_at = _clean_text(message.get("created_at")) if isinstance(message, Mapping) else ""
    item = _normalize_message(room_id, message, created_at or _now_iso())
    return {
        "id": item.get("id", ""),
        "room_id": item.get("room_id", ""),
        "role": item.get("role", ""),
        "type": item.get("type", ""),
        "content": item.get("content"),
        "sender": item.get("sender", {}),
        "metadata": item.get("metadata", {}),
        "created_at": item.get("created_at", ""),
    }


def _normalize_hermes_bridge_config(config: Any) -> dict:
    if not isinstance(config, Mapping):
        config = {}
    timeout = _as_int(config.get("timeout_seconds") or config.get("timeoutSeconds"), DEFAULT_HERMES_BRIDGE_CONFIG["timeout_seconds"])
    if timeout <= 0:
        timeout = DEFAULT_HERMES_BRIDGE_CONFIG["timeout_seconds"]
    return {
        "enabled": _as_bool(config.get("enabled"), DEFAULT_HERMES_BRIDGE_CONFIG["enabled"]),
        "endpoint": _clean_text(config.get("endpoint") or config.get("base_url") or config.get("baseUrl")),
        "workspace_id": _clean_text(config.get("workspace_id") or config.get("workspaceId")),
        "group_id": _clean_text(config.get("group_id") or config.get("groupId")),
        "api_key_env": _clean_text(config.get("api_key_env") or config.get("apiKeyEnv")) or DEFAULT_HERMES_BRIDGE_CONFIG["api_key_env"],
        "timeout_seconds": timeout,
    }


def _slice_messages(
    messages: list[dict],
    limit: int | None = None,
    offset: int = 0,
    after_id: str = "",
    before_id: str = "",
) -> list[dict]:
    items = list(messages)
    after_id = _clean_text(after_id)
    before_id = _clean_text(before_id)
    if after_id:
        index = _message_index(items, after_id)
        items = items[index + 1:] if index >= 0 else []
    if before_id:
        index = _message_index(items, before_id)
        items = items[:index] if index >= 0 else []

    clean_offset = max(_as_int(offset, 0), 0)
    if clean_offset:
        items = items[clean_offset:]

    if limit is None:
        return items
    clean_limit = _as_int(limit, 0)
    if clean_limit <= 0:
        return []
    if after_id or before_id or clean_offset:
        return items[:clean_limit]
    return items[-clean_limit:]


def _message_index(messages: list[dict], message_id: str) -> int:
    for index, item in enumerate(messages):
        if _clean_text(item.get("id")) == message_id:
            return index
    return -1


def _role_from_sender(sender: dict) -> str:
    sender_type = _clean_text(sender.get("type")).lower()
    if sender_type == "system":
        return "system"
    if sender_type in {"agent", "assistant", "character"}:
        return "assistant"
    return "user"


def _json_safe_mapping(value: Any) -> dict:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _json_safe(item) for key, item in value.items()}


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
