"""Helpers shared by automatic and manual outbound delivery paths."""


AMBIGUOUS_TIMEOUT_MARKERS = (
    "timeout",
    "timed out",
    "deadline exceeded",
)


def is_ambiguous_delivery_timeout(output: str) -> bool:
    """Return whether a transport timeout may have happened after acceptance."""
    text = str(output or "").lower()
    return any(marker in text for marker in AMBIGUOUS_TIMEOUT_MARKERS)
