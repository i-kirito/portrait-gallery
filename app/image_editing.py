"""Shared validation and prompts for precision gallery image edits."""
import re


MAX_IMAGE_EDIT_INSTRUCTION_LENGTH = 800
MAX_IMAGE_EDIT_SCHEDULE_DESCRIPTION_LENGTH = 160

IMAGE_EDIT_TARGETS = {
    "background": {
        "label": "背景",
        "scope": (
            "Replace only the background and distant environment behind the subject and existing "
            "foreground objects. Preserve the complete subject and every foreground object."
        ),
    },
    "outfit": {
        "label": "穿搭",
        "scope": (
            "Change only the clothing, footwear, and accessories explicitly named in the request. "
            "Preserve the person's body, face, hair, pose, and all scene elements."
        ),
    },
    "hair": {
        "label": "发型",
        "scope": (
            "Change only the hairstyle or hair accessories explicitly named in the request. "
            "Preserve hair color unless the request explicitly changes it."
        ),
    },
    "expression": {
        "label": "表情",
        "scope": (
            "Change only the requested facial expression and gaze. Preserve facial identity, facial "
            "structure, hair, body, outfit, pose, framing, and scene."
        ),
    },
    "object": {
        "label": "道具",
        "scope": (
            "Change only the explicitly named object or prop. Preserve the subject, all unnamed "
            "objects, the environment, pose, framing, and visual style."
        ),
    },
    "custom": {
        "label": "其他",
        "scope": (
            "Change only the elements explicitly named in the request. Treat every unnamed element "
            "as locked and preserve it from the source image."
        ),
    },
}

INTERNAL_IMAGE_EDIT_TARGETS = {
    "schedule": {
        "label": "日程",
        "scope": (
            "Change the depicted activity to match the updated schedule, including only the subject "
            "action, pose, scene, and props required by that activity."
        ),
    },
}

_IMAGE_EDIT_CHANGE_VERBS = r"(?:替换成|替换为|改成|换成|变成|改为|换为)"
_BACKGROUND_LOCATION_TERMS = (
    "创意园区",
    "阳光咖啡馆",
    "咖啡馆",
    "咖啡店",
    "商业街",
    "步行街",
    "布料市场",
    "市场",
    "夜市",
    "商场",
    "超市",
    "书店",
    "图书馆",
    "工作室",
    "办公室",
    "餐厅",
    "公园",
    "花园",
    "广场",
    "街道",
    "街角",
    "巷子",
    "海边",
    "沙滩",
    "河边",
    "湖边",
    "森林",
    "山顶",
    "校园",
    "教室",
    "车站",
    "机场",
    "酒店",
    "卧室",
    "客厅",
    "厨房",
    "露台",
    "天台",
)


def normalize_image_edit_target(value: str, *, allow_internal: bool = False) -> str:
    target = str(value or "").strip().lower()
    if target in IMAGE_EDIT_TARGETS:
        return target
    if allow_internal and target in INTERNAL_IMAGE_EDIT_TARGETS:
        return target
    return ""


def normalize_image_edit_instruction(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:MAX_IMAGE_EDIT_INSTRUCTION_LENGTH]


def normalize_image_edit_schedule_description(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def image_schedule_description(schedule_time: str) -> str:
    """Return a card's activity text without its leading schedule clock."""
    text = normalize_image_edit_schedule_description(schedule_time)
    match = re.match(r"^\d{1,2}:\d{2}\s*(.*)$", text)
    return (match.group(1) if match else text).strip()


def _clean_image_edit_change_phrase(value: str, *, remove_scene_suffix: bool = False) -> str:
    phrase = re.split(r"[，。；;\n]", str(value or ""), maxsplit=1)[0]
    phrase = re.sub(r"\s*(?:其余|其他|其它)(?:内容|元素|部分)?.*$", "", phrase)
    phrase = phrase.strip(" \t\"'“”‘’：:")
    if remove_scene_suffix:
        phrase = re.sub(r"(?:的)?(?:背景|场景)$", "", phrase).strip()
    return normalize_image_edit_schedule_description(phrase)


def rewrite_image_edit_schedule_description(
    schedule_time: str,
    target: str,
    instruction: str,
) -> str:
    """Apply an explicit image edit to the existing activity sentence when unambiguous."""
    description = image_schedule_description(schedule_time)
    target = normalize_image_edit_target(target)
    instruction = normalize_image_edit_instruction(instruction)
    if not description or not target or not instruction:
        return description

    explicit_patterns = (
        rf"(?:把|将)\s*([^，。；]{{1,48}}?)\s*{_IMAGE_EDIT_CHANGE_VERBS}\s*([^，。；]{{1,80}})",
        rf"(?:背景|场景)?\s*从\s*([^，。；]{{1,48}}?)\s*{_IMAGE_EDIT_CHANGE_VERBS}\s*([^，。；]{{1,80}})",
        rf"^\s*([^，。；]{{1,48}}?)\s*{_IMAGE_EDIT_CHANGE_VERBS}\s*([^，。；]{{1,80}})",
    )
    for pattern in explicit_patterns:
        match = re.search(pattern, instruction)
        if not match:
            continue
        old_text = _clean_image_edit_change_phrase(match.group(1))
        new_text = _clean_image_edit_change_phrase(
            match.group(2),
            remove_scene_suffix=target == "background",
        )
        if old_text and new_text and old_text in description:
            candidate = description.replace(old_text, new_text, 1)
            return (
                candidate
                if len(candidate) <= MAX_IMAGE_EDIT_SCHEDULE_DESCRIPTION_LENGTH
                else description
            )

    if target != "background":
        return description
    replacement_match = re.search(
        rf"{_IMAGE_EDIT_CHANGE_VERBS}\s*([^，。；]{{1,80}})",
        instruction,
    )
    if not replacement_match:
        return description
    replacement = _clean_image_edit_change_phrase(
        replacement_match.group(1),
        remove_scene_suffix=True,
    )
    if not replacement or replacement in description:
        return description

    location_matches = [
        (description.find(term), -len(term), term)
        for term in _BACKGROUND_LOCATION_TERMS
        if term in description
    ]
    if not location_matches:
        return description
    _, _, original_location = min(location_matches)
    candidate = description.replace(original_location, replacement, 1)
    return (
        candidate
        if len(candidate) <= MAX_IMAGE_EDIT_SCHEDULE_DESCRIPTION_LENGTH
        else description
    )


def replace_image_schedule_description(
    schedule_time: str,
    fallback_time: str,
    description: str,
) -> str:
    """Replace a photo card's activity text while retaining its schedule clock."""
    description = normalize_image_edit_schedule_description(description)
    if not description:
        return str(schedule_time or "").strip()

    clock = ""
    for candidate in (schedule_time, fallback_time):
        match = re.match(r"^\s*(\d{1,2}:\d{2})", str(candidate or ""))
        if match:
            clock = match.group(1)
            break
    return f"{clock} {description}".strip()


def image_edit_target_label(target: str) -> str:
    normalized = normalize_image_edit_target(target, allow_internal=True)
    item = IMAGE_EDIT_TARGETS.get(normalized) or INTERNAL_IMAGE_EDIT_TARGETS.get(normalized, {})
    return str(item.get("label") or "局部")


def build_precision_image_edit_prompt(
    target: str,
    instruction: str,
    *,
    previous_schedule_description: str = "",
    schedule_description: str = "",
) -> str:
    target = normalize_image_edit_target(target, allow_internal=True)
    instruction = normalize_image_edit_instruction(instruction)
    previous_schedule_description = normalize_image_edit_schedule_description(
        previous_schedule_description
    )
    schedule_description = normalize_image_edit_schedule_description(schedule_description)
    schedule_changed = bool(
        schedule_description
        and schedule_description != previous_schedule_description
    )
    if not target:
        raise ValueError("invalid_edit_target")
    if not instruction and not schedule_changed:
        raise ValueError("edit_instruction_required")

    sections = [
        "Precision image editing task. The supplied image is the immutable source image. "
    ]
    if instruction:
        target_config = IMAGE_EDIT_TARGETS.get(target) or INTERNAL_IMAGE_EDIT_TARGETS[target]
        sections.append(
            f"EDIT SCOPE: {target_config['label']} ONLY. "
            f"Requested change: {instruction}. "
            f"{target_config['scope']} "
        )
    if schedule_changed:
        previous_text = (
            f"The source image previously represented this activity: {previous_schedule_description}. "
            if previous_schedule_description
            else ""
        )
        sections.append(
            f"UPDATED SCHEDULE ACTIVITY: {schedule_description}. "
            f"{previous_text}"
            "Make the visible image clearly depict the updated schedule. Change the subject's action "
            "and pose, the scene/background, and only the props necessary for the new activity. Do not "
            "preserve an old action, pose, scene, or prop when it conflicts with the updated schedule. "
            "Preserve the person's identity, face, body proportions, hair, outfit, and visual style. "
        )
    sections.append(
        "Apply only the requested change. Preserve every pixel-level visual attribute outside the "
        "edit scope as closely as the image model allows, including identity, face, body proportions, "
        "skin, hair, outfit, pose, hands, expression, foreground objects, camera angle, lens perspective, "
        "crop, framing, composition, resolution, aspect ratio, and rendering style unless an element is "
        "explicitly inside an edit scope or must change to depict the updated schedule. Do not redesign, "
        "beautify, restyle, recrop, zoom, add people, remove people, or introduce unrelated objects. "
        "Keep the output dimensions identical "
        "to the source image."
    )
    return "".join(sections)
