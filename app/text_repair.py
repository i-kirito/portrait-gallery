"""Small text repair helpers for display copy."""
import re


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_C1_RE = re.compile(r"[\u0080-\u009f]")
_LATIN1_CHUNK_RE = re.compile(r"[\u0000-\u00ff]+")
_MOJIBAKE_MARKERS = (
    "Ã",
    "Â",
    "â",
    "ã",
    "å",
    "æ",
    "ç",
    "è",
    "é",
    "ï",
    "ð",
)


def _cjk_count(text: str) -> int:
    return len(_CJK_RE.findall(text or ""))


def _mojibake_score(text: str) -> int:
    value = str(text or "")
    score = len(_C1_RE.findall(value)) * 3
    score += sum(value.count(marker) * 2 for marker in _MOJIBAKE_MARKERS)
    score += value.count("�") * 4
    return score


def _repair_latin1_chunks(text: str) -> str:
    def _repair_match(match: re.Match) -> str:
        chunk = match.group(0)
        try:
            return chunk.encode("latin-1").decode("utf-8")
        except UnicodeError:
            return chunk

    return _LATIN1_CHUNK_RE.sub(_repair_match, text)


def _repair_lost_nel_spaces(text: str) -> str:
    try:
        data = bytearray(text.encode("latin-1"))
    except UnicodeError:
        return text
    changed = False
    for index in range(1, len(data) - 1):
        if data[index] != 0x20:
            continue
        if 0xE4 <= data[index - 1] <= 0xE9 and 0x80 <= data[index + 1] <= 0xBF:
            data[index] = 0x85
            changed = True
        elif (
            index >= 2
            and 0xE4 <= data[index - 2] <= 0xE9
            and 0x80 <= data[index - 1] <= 0xBF
            and not (0x80 <= data[index + 1] <= 0xBF)
        ):
            data[index] = 0xA0
            changed = True
    if not changed:
        return text
    try:
        return bytes(data).decode("utf-8")
    except UnicodeError:
        return text


def repair_mojibake_text(text: str) -> str:
    """Repair common UTF-8-as-Latin-1 mojibake when the result is clearly better."""
    original = str(text or "")
    if not original:
        return original

    candidates = []
    try:
        candidates.append(original.encode("latin-1").decode("utf-8"))
    except UnicodeError:
        pass
    candidates.append(_repair_lost_nel_spaces(original))
    candidates.append(_repair_latin1_chunks(original))

    repaired = candidates[-1]
    for _ in range(2):
        next_repaired = _repair_latin1_chunks(repaired)
        if next_repaired == repaired:
            break
        candidates.append(next_repaired)
        repaired = next_repaired

    original_score = _mojibake_score(original)
    original_cjk = _cjk_count(original)
    best = original
    best_score = original_score
    best_cjk = original_cjk

    for candidate in candidates:
        if not candidate or candidate == original:
            continue
        score = _mojibake_score(candidate)
        cjk = _cjk_count(candidate)
        if cjk > best_cjk and score <= best_score:
            best = candidate
            best_score = score
            best_cjk = cjk
        elif original_score > 0 and score < best_score and cjk >= best_cjk:
            best = candidate
            best_score = score
            best_cjk = cjk

    if best != original and best_cjk > original_cjk and best_score < original_score:
        return best
    return original
