"""Helpers shared by automatic and manual outbound delivery paths."""

import json
import re


DELIVERY_UNCERTAIN_ERROR = "telegram_delivery_uncertain"

_TIMEOUT_RE = re.compile(r"(?:\btimeout\b|timed out|deadline exceeded)", re.IGNORECASE)
_MEDIA_CONTEXT_RE = re.compile(
    r"(?:failed to send media|send(?:ing)? media|media (?:send|upload|delivery)|"
    r"upload(?:ing)? media|telegram media)",
    re.IGNORECASE,
)
_DEFINITE_REJECTION_RE = re.compile(
    r"(?:\bhttp\s*4\d\d\b|too many requests|rate[\s_-]*limit|unauthori[sz]ed|"
    r"forbidden|bad request|chat not found|file too large|invalid (?:chat|token|request))",
    re.IGNORECASE,
)


def _flatten_delivery_output(value) -> list[str]:
    if isinstance(value, dict):
        items = []
        for nested in value.values():
            items.extend(_flatten_delivery_output(nested))
        return items
    if isinstance(value, (list, tuple)):
        items = []
        for nested in value:
            items.extend(_flatten_delivery_output(nested))
        return items
    if value in (None, ""):
        return []
    return [str(value)]


def is_ambiguous_delivery_timeout(output: str) -> bool:
    """Return whether a media send timed out after its delivery result became unknown.

    Generic connection or preprocessing timeouts are not enough. The diagnostic
    must mention a media-send context, and explicit 4xx-style rejections remain
    normal failures so callers can apply their existing retry policy.
    """
    raw = str(output or "").strip()
    if not raw:
        return False
    try:
        parts = _flatten_delivery_output(json.loads(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        parts = [raw]
    text = " ".join(parts)
    if _DEFINITE_REJECTION_RE.search(text):
        return False
    media_matches = list(_MEDIA_CONTEXT_RE.finditer(text))
    timeout_matches = list(_TIMEOUT_RE.finditer(text))
    return any(
        abs(media.start() - timeout.start()) <= 240
        for media in media_matches
        for timeout in timeout_matches
    )


def is_delivery_uncertain_error(error: str) -> bool:
    """Return whether a delivery result was intentionally recorded as uncertain."""
    return str(error or "").strip() == DELIVERY_UNCERTAIN_ERROR
