"""日程数据模型 - 单日穿搭+日程"""
from dataclasses import asdict, dataclass
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
    image_path: str = ""
    image_filename: str = ""
    prompt: str = ""
    caption: str = ""
    status: str = "ok"  # ok / failed / generating
    source: str = ""  # cron / web / custom
    shot_type: str = ""  # selfie / half_body / full_body for custom generation
    outfit_keywords: str = ""  # LLM 提取的穿搭关键词（英文，逗号分隔）
    scene_keywords: str = ""   # LLM 提取的场景关键词（英文，逗号分隔）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "DailyEntry":
        return cls(
            date=data.get("date", ""),
            outfit_style=data.get("outfit_style", ""),
            base_style=data.get("base_style", ""),
            outfit=data.get("outfit", ""),
            schedule=data.get("schedule", ""),
            schedule_prompt=data.get("schedule_prompt", ""),
            image_path=data.get("image_path", ""),
            image_filename=data.get("image_filename", ""),
            prompt=data.get("prompt", ""),
            caption=data.get("caption", ""),
            status=data.get("status", "ok"),
            source=data.get("source", ""),
            shot_type=data.get("shot_type", ""),
            outfit_keywords=data.get("outfit_keywords", ""),
            scene_keywords=data.get("scene_keywords", ""),
        )
