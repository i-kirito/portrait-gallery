"""日程数据模型 - 单日穿搭+日程"""
from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class DailyEntry:
    """单日数据"""
    date: str  # yyyy-mm-dd
    outfit_style: str = ""
    base_style: str = ""  # cool / girly / sweet, chosen by LLM for the day's reference model
    outfit: str = ""
    schedule: str = ""
    schedule_prompt: str = ""  # English schedule used for image prompt injection
    schedule_details: list[dict] = field(default_factory=list)  # per-slot action/scene/outfit/hair details
    image_path: str = ""
    image_filename: str = ""
    prompt: str = ""
    caption: str = ""
    status: str = "ok"  # ok / failed / generating
    source: str = ""  # cron / web / custom / hermes_api
    shot_type: str = ""  # selfie / half_body / full_body for custom generation
    prompt_mode: str = ""  # injected / pure for custom generation
    pure_prompt: bool = False  # true when custom generation skips persona/appearance injection
    custom_prompt: str = ""  # 原始自定义输入，用于重抽时重新套用最新视角规则
    custom_ref_mode: str = ""  # text2img / reference / pure for custom generation
    outfit_keywords: str = ""  # LLM 提取的穿搭关键词（英文，逗号分隔）
    scene_keywords: str = ""   # LLM 提取的场景关键词（英文，逗号分隔）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "DailyEntry":
        pure_prompt_raw = data.get("pure_prompt", False)
        if isinstance(pure_prompt_raw, str):
            pure_prompt = pure_prompt_raw.strip().lower() in {"1", "true", "yes", "on"}
        else:
            pure_prompt = bool(pure_prompt_raw)
        return cls(
            date=data.get("date", ""),
            outfit_style=data.get("outfit_style", ""),
            base_style=data.get("base_style", ""),
            outfit=data.get("outfit", ""),
            schedule=data.get("schedule", ""),
            schedule_prompt=data.get("schedule_prompt", ""),
            schedule_details=data.get("schedule_details", []) if isinstance(data.get("schedule_details"), list) else [],
            image_path=data.get("image_path", ""),
            image_filename=data.get("image_filename", ""),
            prompt=data.get("prompt", ""),
            caption=data.get("caption", ""),
            status=data.get("status", "ok"),
            source=data.get("source", ""),
            shot_type=data.get("shot_type", ""),
            prompt_mode=data.get("prompt_mode", ""),
            pure_prompt=pure_prompt,
            custom_prompt=data.get("custom_prompt", ""),
            custom_ref_mode=data.get("custom_ref_mode", ""),
            outfit_keywords=data.get("outfit_keywords", ""),
            scene_keywords=data.get("scene_keywords", ""),
        )
