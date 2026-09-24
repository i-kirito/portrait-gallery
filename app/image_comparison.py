"""Non-destructive gallery projection: Qwen edits belong to their source card.

Files and stored entries stay independent. Only the list view is grouped, so a
missing/deleted source never strands an edit and removing this feature is safe.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit


def _gallery_source(value: object, image_dir: Path, available: set[str]) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc:
        return ""  # Do not infer local provenance from an arbitrary URL.
    path = unquote(parsed.path)
    if path.startswith("/images/"):
        name = path[len("/images/"):]
    elif Path(path).is_absolute():
        try:
            name = str(Path(path).resolve().relative_to(image_dir))
        except (ValueError, OSError):
            return ""
    elif "/" not in path and "\\" not in path:
        name = path
    else:
        return ""
    if name not in available or "/" in name or "\\" in name or name in {".", ".."}:
        return ""
    return name


def _primary_reference(meta: dict, entry: dict) -> object:
    refs = meta.get("ref_images")
    if isinstance(refs, (list, tuple)) and refs:
        return refs[0]  # Never group by the secondary face/style reference.
    for source in (meta, entry):
        for key in ("requested_ref_image_path", "ref_image_path", "ref_image"):
            if source.get(key):
                return source[key]
    return ""


def _edited_at(meta: dict, entry: dict) -> tuple:
    try:
        timestamp = float(meta.get("created_at") or 0)
    except (ValueError, TypeError, OverflowError):
        timestamp = 0
    return timestamp, str(entry.get("date") or ""), str(entry.get("time") or ""), str(entry.get("image_filename") or "")


def _image_info(name: str, entry: dict, meta: dict, image_dir: Path) -> dict:
    stat = (image_dir / name).stat()
    revision = f"{stat.st_mtime_ns:x}-{stat.st_size:x}"
    model = str(meta.get("model") or meta.get("model_name") or entry.get("model_name") or "")
    label = "Qwen-Image-2.1 Q8" if model.lower() in {"qwen-image-2.1-q8_0", "qwen-image-2.1 q8"} else model
    return {
        "filename": name,
        "url": f"/images/{quote(name, safe='')}?v={revision}",
        "model_name": label,
        "width": meta.get("width") or entry.get("width") or 0,
        "height": meta.get("height") or entry.get("height") or 0,
        "created_at": meta.get("created_at"),
        "date": entry.get("date", ""),
        "time": entry.get("time", ""),
    }


def group_qwen_edits(entries: list[dict], metadata: dict, image_dir: str) -> list[dict]:
    """Group existing, explicitly linked Qwen edits before filtering/pagination.

    Returns copies, never mutates metadata/entries, never rewrites or deletes
    images. Chains are traced to their original card. Cycles stay ungrouped.
    """
    base = Path(image_dir).expanduser().resolve()
    by_name = {e.get("image_filename"): e for e in entries if isinstance(e, dict) and e.get("image_filename")}
    available = set(by_name)
    parents = {}
    for name, entry in by_name.items():
        meta = metadata.get(name) or {}
        if not isinstance(meta, dict):
            continue
        model = str(meta.get("model") or meta.get("model_name") or entry.get("model_name") or "").lower()
        if not model.startswith("qwen-image-2.1"):
            continue
        mode = str(meta.get("generation_mode") or entry.get("generation_mode") or "").lower()
        if mode and not mode.startswith(("img2img", "image-to-image")):
            continue
        parent = _gallery_source(_primary_reference(meta, entry), base, available)
        if parent and parent != name:
            parents[name] = parent

    children = defaultdict(list)
    for name in parents:
        current, visited = name, set()
        while current in parents and current not in visited:
            visited.add(current)
            current = parents[current]
        if current in visited or current == name:
            continue
        children[current].append(name)

    comparisons, hidden = {}, set()
    for root, names in children.items():
        try:
            before = _image_info(root, by_name[root], metadata.get(root) or {}, base)
        except OSError:
            continue
        ordered = sorted(names, key=lambda n: _edited_at(metadata.get(n) or {}, by_name[n]))
        edits = []
        for name in ordered:
            try:
                edit = _image_info(name, by_name[name], metadata.get(name) or {}, base)
            except OSError:
                continue
            edit["source_filename"] = parents[name]
            edits.append(edit)
        if not edits:
            continue
        comparisons[root] = {
            "before": before,
            "after": edits[-1],
            "edits": edits,
            "edit_count": len(edits),
            "root_filename": root,
        }
        hidden.update(edit["filename"] for edit in edits)

    result = []
    for entry in entries:
        name = entry.get("image_filename")
        if name in hidden:
            continue
        item = dict(entry)
        if name in comparisons:
            item["image_comparison"] = comparisons[name]
        result.append(item)
    return result
