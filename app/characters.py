"""Character registry and prompt helpers for single and group portraits."""
from __future__ import annotations

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for local tests
    class _FcntlFallback:
        LOCK_SH = 1
        LOCK_EX = 2
        LOCK_UN = 8

        @staticmethod
        def flock(_fd, _op):
            return None

    fcntl = _FcntlFallback()

from dataclasses import dataclass, field
import copy
import json
import os
import re
import tempfile
import unicodedata
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

try:
    from settings import GENERIC_APPEARANCE
except ImportError:  # pragma: no cover - supports package-style imports in tests
    try:
        from .settings import GENERIC_APPEARANCE
    except ImportError:  # pragma: no cover
        GENERIC_APPEARANCE = (
            "adult portrait subject with natural facial features, realistic body "
            "proportions, polished everyday styling, clear face, expressive eyes, "
            "and a coherent personal look"
        )


Character = dict[str, Any]

_ID_LIMIT = 64
_TEXT_LIMIT = 1200
_PERSONA_LIMIT = 2400
_PROMPT_LIMIT = 3600
_DEFAULT_NAME = "角色"
_DEFAULT_PERSONA = (
    "A configurable portrait gallery character whose persona, appearance, and "
    "daily scene can be used to generate outfit photos and captions."
)
_FALSE_VALUES = {"0", "false", "no", "n", "off", "disabled", "disable"}
_TRUE_VALUES = {"1", "true", "yes", "y", "on", "enabled", "enable"}
LOCAL_CHARACTER_FILENAME = "characters.json"
LOCAL_CHARACTER_LOCK_FILENAME = "characters.lock"
LOCAL_CHARACTER_SOURCE = "local.characters"

_NAME_KEYS = (
    "name",
    "character_name",
    "characterName",
    "display_name",
    "displayName",
    "nickname",
)
_PERSONA_KEYS = (
    "persona",
    "character_persona",
    "role_persona",
    "identity",
    "description",
    "bio",
    "profile",
)
_APPEARANCE_KEYS = (
    "appearance",
    "character_appearance",
    "characterAppearance",
    "visual",
    "look",
    "image_prompt",
    "visual_prompt",
)
_VOICE_KEYS = ("voice", "caption_voice", "captionVoice", "tone", "vibe")
_LLM_MODEL_KEYS = ("llm_model", "llmModel", "chat_model", "chatModel", "model")
_RELATION_KEYS = (
    "relationship",
    "relationship_prompt",
    "relationshipPrompt",
    "relation",
    "group_role",
    "groupRole",
)
_POSITION_KEYS = (
    "position",
    "placement",
    "standing",
    "standing_prompt",
    "standingPrompt",
    "composition",
)
_IMAGE_ADULT_GUARD = (
    "All depicted people are clearly adults aged 21 or older. "
    "Non-sexual everyday portrait with age-appropriate styling."
)
_IMAGE_PROMPT_REPLACEMENTS = (
    (
        r"(?i)\b(?:classic\s+)?(?:Japanese\s+)?JK(?:\s+school)?\s+uniform\b",
        "Japanese sailor-inspired fashion outfit",
    ),
    (r"(?i)\bschool\s+uniform\b", "preppy sailor-inspired fashion outfit"),
    (r"(?i)\b(?:1[0-9]|20)[ -]?year[- ]old\b", "clearly adult, age 21 or older"),
    (r"(?i)\b(?:teenage|teenaged|teenager|teen)\b", "adult"),
    (r"(?i)\byoung[- ]looking\b", "adult"),
    (r"(?i)\byoung\s+(?:girl|woman)\b", "adult woman"),
    (r"(?i)\byoung\s+(?:boy|man)\b", "adult man"),
    (r"(?i)\bgirl\b", "adult woman"),
    (r"(?i)\bboy\b", "adult man"),
    (
        r"(?i)\b(?:large|prominent|voluptuous)(?:\s+natural)?\s+(?:breasts?|bust)\b",
        "natural adult body proportions",
    ),
    (r"(?i)\b(?:breasts?|bust|cleavage)\b", "adult body proportions"),
    (r"(?i)\bhourglass\s+figure\b", "balanced adult proportions"),
    (r"(?i)\bslim\s+waist\b", "natural waistline"),
    (r"(?i)\bseductive\b", "warm and confident"),
    (r"(?i)\b(?:erotic|sexualized|sexually suggestive|lustful)\b", "non-sexual"),
    (r"(?i)\b(?:sexy|sensual|provocative|fetishized)\b", "stylish"),
    (r"(?<!\d)(?:1[0-9]|20)\s*岁", "21岁以上"),
    (r"少女风", "甜美风"),
    (r"(?:未成年(?:人)?|少女|女孩|女童)", "成年女性"),
    (r"(?:男孩|男童)", "成年男性"),
    (r"(?:JK\s*)?校服", "水手领学院风时装"),
    (r"(?:学生妹|高中生|中学生|小学生)", "成年时尚模特"),
    (r"(?:爆乳|巨乳|大胸|丰满胸部|胸部丰满)", "自然成年人体态"),
    (r"(?:性感|色气|色情|情色|诱惑|挑逗|魅魔|涩涩|好色|欲求不满)", "自然"),
)


def _clean_text(value: Any, limit: int = _TEXT_LIMIT) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (Mapping, list, tuple, set)):
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -_`*：:")
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return text


def _first_text(data: Mapping[str, Any], keys: Sequence[str], limit: int = _TEXT_LIMIT) -> str:
    for key in keys:
        if key not in data:
            continue
        text = _clean_text(data.get(key), limit)
        if text:
            return text
    return ""


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    text = _clean_text(value, 32).lower()
    if not text:
        return default
    if text in _FALSE_VALUES:
        return False
    if text in _TRUE_VALUES:
        return True
    return default


def slugify_id(value: Any, fallback: str = "character") -> str:
    """Return a stable ASCII slug suitable for config and URL identifiers."""
    text = _clean_text(value, 128)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    if not text:
        fallback_text = _clean_text(fallback, 128)
        fallback_text = unicodedata.normalize("NFKD", fallback_text)
        fallback_text = fallback_text.encode("ascii", "ignore").decode("ascii").lower()
        text = re.sub(r"[^a-z0-9]+", "-", fallback_text)
        text = re.sub(r"-{2,}", "-", text).strip("-")
    text = text[:_ID_LIMIT].strip("-")
    return text or "character"


def normalize_character_id(
    value: Any,
    fallback_base: str = "character",
    used_ids: set[str] | None = None,
) -> str:
    """Normalize a character id and make it unique within used_ids when supplied."""
    base = slugify_id(value, fallback_base)
    if used_ids is None:
        return base

    candidate = base
    suffix = 2
    while candidate in used_ids:
        marker = f"-{suffix}"
        prefix = base[: _ID_LIMIT - len(marker)].rstrip("-") or "character"
        candidate = f"{prefix}{marker}"
        suffix += 1
    used_ids.add(candidate)
    return candidate


def _copy_character(character: Mapping[str, Any]) -> Character:
    copied = dict(character)
    copied["enabled"] = bool(copied.get("enabled", True))
    return copied


def _normalize_character(
    raw: Mapping[str, Any],
    fallback_id: str,
    used_ids: set[str] | None = None,
    source: str = "",
    defaults: Mapping[str, Any] | None = None,
) -> Character:
    defaults = defaults or {}
    raw_id = _clean_text(raw.get("id") or raw.get("slug") or raw.get("key"), 128)
    fallback_base = raw_id or _first_text(raw, _NAME_KEYS, 80) or fallback_id
    character_id = normalize_character_id(raw_id, fallback_base, used_ids)

    name = _first_text(raw, _NAME_KEYS, 80) or _clean_text(defaults.get("name"), 80)
    persona = _first_text(raw, _PERSONA_KEYS, _PERSONA_LIMIT) or _clean_text(
        defaults.get("persona"), _PERSONA_LIMIT
    )
    appearance = _first_text(raw, _APPEARANCE_KEYS, _PROMPT_LIMIT) or _clean_text(
        defaults.get("appearance"), _PROMPT_LIMIT
    )
    voice = _first_text(raw, _VOICE_KEYS, _TEXT_LIMIT) or _clean_text(defaults.get("voice"), _TEXT_LIMIT)
    llm_model = _first_text(raw, _LLM_MODEL_KEYS, 160) or _clean_text(defaults.get("llm_model"), 160)

    character = {
        "id": character_id,
        "name": name or _DEFAULT_NAME,
        "persona": persona or _DEFAULT_PERSONA,
        "appearance": appearance or GENERIC_APPEARANCE,
        "reference_style": _clean_text(raw.get("reference_style"), _TEXT_LIMIT),
        "reference_image": _clean_text(raw.get("reference_image"), _TEXT_LIMIT),
        "prompt_prefix": _clean_text(raw.get("prompt_prefix"), _PROMPT_LIMIT),
        "voice": voice,
        "llm_model": llm_model,
        "agent_id": _clean_text(raw.get("agent_id"), 160),
        "enabled": _as_bool(raw.get("enabled"), True),
        "source": source,
    }

    for key in (*_RELATION_KEYS, *_POSITION_KEYS):
        text = _clean_text(raw.get(key), _TEXT_LIMIT)
        if text:
            character[key] = text
    return character


def normalize_character(
    raw: Mapping[str, Any] | None,
    fallback_id: str = "character",
    used_ids: set[str] | None = None,
) -> Character:
    """Normalize an arbitrary character mapping into the registry data model."""
    if not isinstance(raw, Mapping):
        raw = {}
    return _normalize_character(raw, fallback_id=fallback_id, used_ids=used_ids)


def _manual_store_paths(data_dir: str | os.PathLike[str] | None) -> tuple[str, str]:
    root = os.fspath(data_dir or "data")
    return (
        os.path.join(root, LOCAL_CHARACTER_FILENAME),
        os.path.join(root, LOCAL_CHARACTER_LOCK_FILENAME),
    )


def _manual_store_default() -> dict[str, Any]:
    return {"version": 1, "characters": []}


def _read_manual_store_unlocked(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return _manual_store_default()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _manual_store_default()
    if isinstance(data, list):
        return {"version": 1, "characters": data}
    if not isinstance(data, Mapping):
        return _manual_store_default()
    characters = data.get("characters")
    if not isinstance(characters, list):
        characters = data.get("items")
    return {
        "version": int(data.get("version") or 1),
        "characters": characters if isinstance(characters, list) else [],
    }


def _write_manual_store_unlocked(data_dir: str, path: str, data: Mapping[str, Any]) -> None:
    os.makedirs(data_dir, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=data_dir, prefix=".characters_", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "version": int(data.get("version") or 1),
                    "characters": list(data.get("characters") or []),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _manual_store_load(data_dir: str | os.PathLike[str] | None) -> dict[str, Any]:
    path, lock_path = _manual_store_paths(data_dir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
        try:
            return _read_manual_store_unlocked(path)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _manual_store_update(data_dir: str | os.PathLike[str] | None, callback) -> Any:
    path, lock_path = _manual_store_paths(data_dir)
    root = os.path.dirname(path) or "."
    os.makedirs(root, exist_ok=True)
    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            data = _read_manual_store_unlocked(path)
            result = callback(data)
            _write_manual_store_unlocked(root, path, data)
            return copy.deepcopy(result)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _manual_character_base_id(raw: Mapping[str, Any]) -> str:
    raw_id = _clean_text(raw.get("id") or raw.get("slug") or raw.get("key"), 128)
    if raw_id:
        return slugify_id(raw_id, raw_id)
    name_slug = slugify_id(_first_text(raw, _NAME_KEYS, 80), "")
    if name_slug and name_slug != "character":
        return name_slug
    return f"manual-{uuid.uuid4().hex[:8]}"


def _stored_manual_character(raw: Mapping[str, Any], used_ids: set[str] | None = None) -> Character:
    data = dict(raw)
    if not _clean_text(data.get("id"), 128):
        data["id"] = _manual_character_base_id(data)
    return _normalize_character(
        data,
        fallback_id=str(data.get("id") or "manual-character"),
        used_ids=used_ids,
        source=LOCAL_CHARACTER_SOURCE,
    )


def load_manual_characters(
    data_dir: str | os.PathLike[str] | None,
    used_ids: set[str] | None = None,
) -> list[Character]:
    """Load manually added local characters from data/characters.json."""
    if not data_dir:
        return []
    data = _manual_store_load(data_dir)
    characters: list[Character] = []
    for raw in data.get("characters", []):
        if not isinstance(raw, Mapping):
            continue
        characters.append(_stored_manual_character(raw, used_ids=used_ids))
    return characters


def upsert_manual_character(
    data_dir: str | os.PathLike[str] | None,
    raw: Mapping[str, Any],
    reserved_ids: set[str] | None = None,
) -> Character:
    """Create or update one local character and return the stored normalized row."""
    if not isinstance(raw, Mapping):
        raw = {}
    reserved = set(reserved_ids or set())
    payload = dict(raw)
    requested_id = _clean_text(payload.get("id") or payload.get("slug") or payload.get("key"), 128)
    if not requested_id:
        payload["id"] = _manual_character_base_id(payload)

    result: Character = {}

    def _update(data: dict[str, Any]) -> Character:
        nonlocal result
        items = [dict(item) for item in data.get("characters", []) if isinstance(item, Mapping)]
        existing_ids = {
            slugify_id(item.get("id") or item.get("slug") or item.get("key"), "")
            for item in items
            if _clean_text(item.get("id") or item.get("slug") or item.get("key"), 128)
        }
        match_id = slugify_id(requested_id, "") if requested_id else ""
        used = set(reserved)
        used.update(existing_id for existing_id in existing_ids if existing_id and existing_id != match_id)

        stored_id = normalize_character_id(payload.get("id"), str(payload.get("id") or "manual-character"), used)
        payload["id"] = stored_id
        stored = _stored_manual_character(payload)
        clean = {
            key: value
            for key, value in stored.items()
            if key
            in {
                "id",
                "name",
                "persona",
                "appearance",
                "reference_style",
                "reference_image",
                "prompt_prefix",
                "voice",
                "llm_model",
                "agent_id",
                "enabled",
                "relationship",
                "relationship_prompt",
                "relationshipPrompt",
                "relation",
                "group_role",
                "groupRole",
                "position",
                "placement",
                "standing",
                "standing_prompt",
                "standingPrompt",
                "composition",
            }
        }
        clean["source"] = LOCAL_CHARACTER_SOURCE

        replaced = False
        for index, item in enumerate(items):
            item_id = slugify_id(item.get("id") or item.get("slug") or item.get("key"), "")
            if item_id and item_id == stored_id:
                items[index] = clean
                replaced = True
                break
        if not replaced:
            items.append(clean)
        data["version"] = 1
        data["characters"] = items
        result = _stored_manual_character(clean)
        return result

    return _manual_store_update(data_dir, _update)


def delete_manual_character(
    data_dir: str | os.PathLike[str] | None,
    character_id: Any,
) -> Character | None:
    """Delete one local character by stored id."""
    target_id = slugify_id(character_id, "")
    if not target_id:
        return None
    deleted: Character | None = None

    def _update(data: dict[str, Any]) -> Character | None:
        nonlocal deleted
        kept = []
        for raw in data.get("characters", []):
            if not isinstance(raw, Mapping):
                continue
            character = _stored_manual_character(raw)
            if character.get("id") == target_id:
                deleted = character
                continue
            kept.append(dict(raw))
        data["version"] = 1
        data["characters"] = kept
        return deleted

    return _manual_store_update(data_dir, _update)


def _legacy_defaults(config: Mapping[str, Any]) -> Character:
    raw = config.get("character") if isinstance(config.get("character"), Mapping) else {}
    legacy_raw = dict(raw)
    if not _clean_text(legacy_raw.get("id"), 128):
        legacy_raw["id"] = "default"
    return _normalize_character(
        legacy_raw,
        fallback_id="default",
        used_ids=set(),
        source="config.character" if raw else "default",
        defaults={
            "name": _DEFAULT_NAME,
            "persona": _DEFAULT_PERSONA,
            "appearance": GENERIC_APPEARANCE,
        },
    )


def _runtime_character_raw(runtime_persona: Mapping[str, Any] | None) -> Character | None:
    if not isinstance(runtime_persona, Mapping):
        return None

    persona_source = _clean_text(runtime_persona.get("persona_source"), 64).lower()
    source = _clean_text(runtime_persona.get("source"), 256)
    sources = runtime_persona.get("sources") if isinstance(runtime_persona.get("sources"), Mapping) else {}
    source_values = {_clean_text(value, 256) for value in sources.values()}
    useful_sources = {value for value in (*source_values, source) if value and value != "default"}
    if useful_sources <= {"config.character"}:
        return None

    name = _clean_text(runtime_persona.get("name"), 80)
    persona = _clean_text(runtime_persona.get("persona"), _PERSONA_LIMIT)
    appearance = _clean_text(runtime_persona.get("appearance"), _PROMPT_LIMIT)
    voice = _clean_text(
        runtime_persona.get("caption_voice") or runtime_persona.get("voice"),
        _TEXT_LIMIT,
    )
    if not any((name and name != _DEFAULT_NAME, persona, appearance, voice)):
        return None

    source_label = persona_source if persona_source in {"hermes", "openclaw"} else "runtime"
    agent_label = {
        "hermes": "Hermes Agent",
        "openclaw": "OpenClaw Agent",
    }.get(source_label, "Runtime Persona")
    raw: Character = {
        "id": source_label,
        "name": name or _DEFAULT_NAME,
        "persona": persona,
        "appearance": appearance,
        "voice": voice,
        "agent_id": agent_label,
        "source": f"runtime.{source_label}",
        "enabled": True,
    }
    return raw


def _nested_text(config: Mapping[str, Any], path: str) -> str:
    current: Any = config
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return ""
        current = current.get(part)
    return _clean_text(current, 128)


def _configured_default_id(config: Mapping[str, Any]) -> str:
    keys = (
        "default_character_id",
        "default_character",
        "character_id",
        "gallery.default_character_id",
        "gallery.default_character",
    )
    for key in keys:
        text = _nested_text(config, key) if "." in key else _clean_text(config.get(key), 128)
        if text:
            return text
    return ""


def _build_aliases(characters: Sequence[Character], default_id: str) -> dict[str, str]:
    aliases = {"default": default_id}
    name_counts: dict[str, int] = {}
    for character in characters:
        name_slug = slugify_id(character.get("name"), "")
        if name_slug:
            name_counts[name_slug] = name_counts.get(name_slug, 0) + 1
    for character in characters:
        name_slug = slugify_id(character.get("name"), "")
        if name_slug and name_counts.get(name_slug) == 1:
            aliases[name_slug] = character["id"]
        if character.get("source") in {"config.character", "default"}:
            aliases["legacy"] = character["id"]
        source = _clean_text(character.get("source"), 80)
        if source.startswith("runtime."):
            aliases["runtime"] = character["id"]
            aliases[source.removeprefix("runtime.")] = character["id"]
    return aliases


@dataclass(frozen=True)
class CharacterRegistry:
    """Normalized, JSON-friendly character registry."""

    characters: tuple[Character, ...]
    default_id: str
    aliases: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_id", {character["id"]: character for character in self.characters})

    @property
    def default_character(self) -> Character:
        return self.get_character(self.default_id, fallback=True)

    @property
    def enabled_characters(self) -> list[Character]:
        return [_copy_character(character) for character in self.characters if character.get("enabled", True)]

    def get_character(self, character_id: Any = "", fallback: bool = True) -> Character | None:
        query = slugify_id(character_id, "") if _clean_text(character_id, 128) else ""
        if not query:
            return _copy_character(self._by_id[self.default_id]) if fallback else None

        target_id = query
        if target_id not in self._by_id:
            target_id = self.aliases.get(query, query)
        character = self._by_id.get(target_id)
        if character:
            return _copy_character(character)
        if fallback:
            return _copy_character(self._by_id[self.default_id])
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "default_id": self.default_id,
            "characters": [_copy_character(character) for character in self.characters],
            "enabled_characters": self.enabled_characters,
        }


_CURRENT_REGISTRY: CharacterRegistry | None = None


def load_character_registry(
    config: Mapping[str, Any] | None,
    runtime_persona: Mapping[str, Any] | None = None,
    data_dir: str | os.PathLike[str] | None = None,
) -> CharacterRegistry:
    """Load runtime, local, and config characters with legacy fallback last."""
    if not isinstance(config, Mapping):
        config = {}

    legacy = _legacy_defaults(config)
    raw_characters = config.get("characters")
    used_ids: set[str] = set()
    characters: list[Character] = []
    runtime_raw = _runtime_character_raw(runtime_persona)

    if runtime_raw:
        characters.append(
            _normalize_character(
                runtime_raw,
                fallback_id=str(runtime_raw.get("id") or "runtime"),
                used_ids=used_ids,
                source=str(runtime_raw.get("source") or "runtime"),
                defaults=legacy,
            )
        )

    manual_data = _manual_store_load(data_dir) if data_dir else _manual_store_default()
    for raw in manual_data.get("characters", []):
        if not isinstance(raw, Mapping):
            continue
        raw_id = slugify_id(raw.get("id") or raw.get("slug") or raw.get("key"), "")
        existing_index = next(
            (
                index
                for index, character in enumerate(characters)
                if raw_id and character.get("id") == raw_id
            ),
            -1,
        )
        if existing_index >= 0:
            override = _stored_manual_character(raw)
            merged = dict(characters[existing_index])
            for key in (
                "name",
                "persona",
                "appearance",
                "reference_style",
                "reference_image",
                "prompt_prefix",
                "voice",
                "llm_model",
                "agent_id",
                "enabled",
                "relationship",
                "relationship_prompt",
                "relationshipPrompt",
                "relation",
                "group_role",
                "groupRole",
                "position",
                "placement",
                "standing",
                "standing_prompt",
                "standingPrompt",
                "composition",
            ):
                if key == "enabled":
                    merged[key] = override.get(key, merged.get(key, True))
                elif override.get(key):
                    merged[key] = override[key]
            merged["local_override"] = True
            characters[existing_index] = merged
            continue
        characters.append(_stored_manual_character(raw, used_ids=used_ids))

    if isinstance(raw_characters, list):
        for index, raw in enumerate(raw_characters, start=1):
            if not isinstance(raw, Mapping):
                continue
            characters.append(
                _normalize_character(
                    raw,
                    fallback_id=f"character-{index}",
                    used_ids=used_ids,
                    source=f"config.characters[{index - 1}]",
                    defaults=legacy,
                )
            )

    if not characters:
        legacy_raw = config.get("character") if isinstance(config.get("character"), Mapping) else {}
        if legacy_raw or not characters:
            legacy_raw = dict(legacy_raw)
            if not _clean_text(legacy_raw.get("id"), 128):
                legacy_raw["id"] = "default"
            characters.append(
                _normalize_character(
                    legacy_raw,
                    fallback_id="default",
                    used_ids=used_ids,
                    source=legacy.get("source", "default"),
                    defaults=legacy,
                )
            )

    if not characters:
        used_ids.clear()
        legacy_raw = config.get("character") if isinstance(config.get("character"), Mapping) else {}
        legacy_raw = dict(legacy_raw)
        if not _clean_text(legacy_raw.get("id"), 128):
            legacy_raw["id"] = "default"
        characters.append(
            _normalize_character(
                legacy_raw,
                fallback_id="default",
                used_ids=used_ids,
                source=legacy.get("source", "default"),
                defaults=legacy,
            )
        )

    requested_default_raw = _configured_default_id(config)
    requested_default = normalize_character_id(requested_default_raw, "default") if requested_default_raw else ""
    by_id = {character["id"]: character for character in characters}
    enabled_ids = [character["id"] for character in characters if character.get("enabled", True)]
    default_id = requested_default if requested_default in by_id else (enabled_ids[0] if enabled_ids else characters[0]["id"])
    registry = CharacterRegistry(
        characters=tuple(characters),
        default_id=default_id,
        aliases=_build_aliases(characters, default_id),
    )

    global _CURRENT_REGISTRY
    _CURRENT_REGISTRY = registry
    return registry


def _active_registry(registry: CharacterRegistry | None = None) -> CharacterRegistry:
    global _CURRENT_REGISTRY
    if registry is not None:
        return registry
    if _CURRENT_REGISTRY is None:
        _CURRENT_REGISTRY = load_character_registry({})
    return _CURRENT_REGISTRY


def get_character(
    character_id: Any = "",
    registry: CharacterRegistry | None = None,
    fallback: bool = True,
) -> Character | None:
    """Return a normalized character from a registry or the most recently loaded one."""
    return _active_registry(registry).get_character(character_id, fallback=fallback)


def default_character(registry: CharacterRegistry | None = None) -> Character:
    return _active_registry(registry).default_character


def enabled_characters(registry: CharacterRegistry | None = None) -> list[Character]:
    return _active_registry(registry).enabled_characters


def _prompt_sentence(label: str, value: Any) -> str:
    text = _clean_text(value, _PROMPT_LIMIT)
    return f"{label}: {text}." if text else ""


def sanitize_image_prompt(value: Any, limit: int = _PROMPT_LIMIT) -> str:
    """Return a clearly adult, non-sexual visual prompt fragment."""
    source_limit = max(limit * 2, _PROMPT_LIMIT) if limit else 0
    text = _clean_text(value, source_limit)
    if not text:
        return ""
    for pattern, replacement in _IMAGE_PROMPT_REPLACEMENTS:
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"\s+([,.;:，。；：])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text)
    return _clean_text(text, limit)


def build_character_prompt(character: Mapping[str, Any] | None) -> str:
    """Build a single-character image prompt fragment."""
    normalized = normalize_character(character, fallback_id="character")
    parts = [
        _clean_text(normalized.get("prompt_prefix"), _PROMPT_LIMIT),
        f"Character {normalized['name']} (id: {normalized['id']}).",
        _prompt_sentence("Identity and persona", normalized.get("persona")),
        _prompt_sentence("Appearance", normalized.get("appearance")),
        _prompt_sentence("Voice or mood", normalized.get("voice")),
        _prompt_sentence("Reference style", normalized.get("reference_style")),
    ]
    if normalized.get("reference_image"):
        parts.append(
            "Reference image hint: apply the configured reference image only to "
            f"{normalized['name']} when the generation pipeline provides it."
        )
    return " ".join(part for part in parts if part).strip()


def build_character_image_prompt(
    character: Mapping[str, Any] | None,
    *,
    include_safety_guard: bool = True,
) -> str:
    """Build an image-only prompt without conversational persona or voice."""
    normalized = normalize_character(character, fallback_id="character")
    parts = [
        sanitize_image_prompt(normalized.get("prompt_prefix")),
        f"Character {normalized['name']} (id: {normalized['id']}).",
        _prompt_sentence("Visual identity", sanitize_image_prompt(normalized.get("appearance"))),
        _prompt_sentence("Reference style", sanitize_image_prompt(normalized.get("reference_style"))),
    ]
    if normalized.get("reference_image"):
        parts.append(
            "Reference image hint: apply the configured reference image only to "
            f"{normalized['name']} when the generation pipeline provides it."
        )
    if include_safety_guard:
        parts.append(_IMAGE_ADULT_GUARD)
    return " ".join(part for part in parts if part).strip()


def _coerce_character_sequence(characters: Iterable[Mapping[str, Any]] | CharacterRegistry) -> list[Character]:
    if isinstance(characters, CharacterRegistry):
        return characters.enabled_characters or [characters.default_character]
    if isinstance(characters, Mapping):
        return [normalize_character(characters, fallback_id="character")]

    normalized: list[Character] = []
    used_ids: set[str] = set()
    for index, character in enumerate(characters or [], start=1):
        if isinstance(character, Mapping):
            normalized.append(normalize_character(character, fallback_id=f"character-{index}", used_ids=used_ids))
    if not normalized:
        normalized.append(default_character())
    return normalized


def _position_hint(character: Mapping[str, Any], index: int, total: int) -> str:
    configured = _first_text(character, _POSITION_KEYS, _TEXT_LIMIT)
    if configured:
        return configured
    if total <= 1:
        return "center of the frame"
    if total == 2:
        return "on the viewer's left" if index == 1 else "on the viewer's right"
    if total == 3:
        return ("on the viewer's left", "in the center", "on the viewer's right")[index - 1]
    return f"position {index} from left to right"


def build_group_prompt(
    characters: Iterable[Mapping[str, Any]] | CharacterRegistry,
    scene_prompt: Any = "",
) -> str:
    """Build a multi-character prompt suitable for ImageGenerator.generate(prompt=...)."""
    normalized = _coerce_character_sequence(characters)
    total = len(normalized)
    parts = [
        f"Group portrait with {total} distinct character{'s' if total != 1 else ''}.",
        "Keep each person's face, body features, outfit, and identity separate; do not merge or duplicate them.",
    ]

    scene = _clean_text(scene_prompt, _PROMPT_LIMIT)
    if scene:
        parts.append(f"Scene: {scene}.")

    lineup_names = ", ".join(character["name"] for character in normalized)
    if lineup_names:
        parts.append(f"Left-to-right lineup should stay consistent for: {lineup_names}.")

    for index, character in enumerate(normalized, start=1):
        relation = _first_text(character, _RELATION_KEYS, _TEXT_LIMIT)
        if not relation and total > 1:
            relation = "interacts naturally with the others while remaining visually distinct"
        position = _position_hint(character, index, total)
        parts.append(
            " ".join(
                part
                for part in (
                    f"Character {index}: {build_character_prompt(character)}",
                    _prompt_sentence("Relationship cue", relation),
                    _prompt_sentence("Placement cue", position),
                )
                if part
            )
        )

    parts.append("Compose them together in one coherent shared photo, with believable scale and eye lines.")
    return " ".join(part for part in parts if part).strip()


def build_group_image_prompt(
    characters: Iterable[Mapping[str, Any]] | CharacterRegistry,
    scene_prompt: Any = "",
) -> str:
    """Build a multi-character image prompt without chat-only relationship text."""
    normalized = _coerce_character_sequence(characters)
    total = len(normalized)
    parts = [
        f"Group portrait with {total} distinct character{'s' if total != 1 else ''}.",
        _IMAGE_ADULT_GUARD,
        "Keep each person's face, body features, outfit, and identity separate; do not merge or duplicate them.",
    ]

    scene = sanitize_image_prompt(scene_prompt)
    if scene:
        parts.append(f"Scene: {scene}.")

    lineup_names = ", ".join(character["name"] for character in normalized)
    if lineup_names:
        parts.append(f"Left-to-right lineup should stay consistent for: {lineup_names}.")

    for index, character in enumerate(normalized, start=1):
        position = _position_hint(character, index, total)
        parts.append(
            " ".join(
                part
                for part in (
                    f"Character {index}: {build_character_image_prompt(character, include_safety_guard=False)}",
                    _prompt_sentence("Placement cue", position),
                )
                if part
            )
        )

    parts.append(
        "Compose them together in one coherent shared photo with natural, non-sexual interaction, "
        "believable scale, and consistent eye lines."
    )
    return " ".join(part for part in parts if part).strip()


__all__ = [
    "Character",
    "CharacterRegistry",
    "LOCAL_CHARACTER_SOURCE",
    "build_character_image_prompt",
    "build_character_prompt",
    "build_group_image_prompt",
    "build_group_prompt",
    "default_character",
    "delete_manual_character",
    "enabled_characters",
    "get_character",
    "load_manual_characters",
    "load_character_registry",
    "normalize_character",
    "normalize_character_id",
    "sanitize_image_prompt",
    "slugify_id",
    "upsert_manual_character",
]
