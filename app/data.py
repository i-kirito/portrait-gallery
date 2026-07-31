"""日程数据模型 - 单日穿搭+日程"""
from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class DailyEntry:
    """单日数据"""
    date: str  # yyyy-mm-dd
    time: str = ""  # HH:MM for generated image cards
    outfit_style: str = ""
    base_style: str = ""  # legacy compatibility only; reference images are selected from profiles
    reference_query: str = ""  # natural-language reference-image matching hint from LLM
    outfit: str = ""
    schedule: str = ""
    schedule_prompt: str = ""  # English schedule used for image prompt injection
    schedule_details: list[dict] = field(default_factory=list)  # per-slot action/scene/outfit/hair details
    image_path: str = ""
    image_filename: str = ""
    prompt: str = ""
    caption: str = ""
    caption_status: str = ""  # pending / ready / failed for async visual captions
    status: str = "ok"  # ok / failed / generating
    source: str = ""  # cron / web / custom / hermes_api
    shot_type: str = ""  # selfie / half_body / full_body for custom generation
    prompt_mode: str = ""  # injected / pure for custom generation
    pure_prompt: bool = False  # true when custom generation skips persona/appearance injection
    custom_prompt: str = ""  # 原始自定义输入，用于重抽时重新套用最新视角规则
    custom_ref_mode: str = ""  # text2img / reference / pure for custom generation
    outfit_keywords: str = ""  # LLM 提取的穿搭关键词（英文，逗号分隔）
    scene_keywords: str = ""   # LLM 提取的场景关键词（英文，逗号分隔）
    photo_style_en: str = ""  # LLM 根据当日日程判断的摄影/镜头语言（英文）
    schedule_llm_model: str = ""  # 生成该日程所用的 LLM 模型名
    xiaohongshu_search_query: str = ""  # 先于日程选择的真人穿搭搜索词
    generation_type: str = ""  # character / group_photo / chat 等扩展生图类型
    character_id: str = ""  # 单角色生图绑定的角色 ID
    character_ids: list[str] = field(default_factory=list)  # 合照/群聊关联角色
    character_names: list[str] = field(default_factory=list)
    chat_room_id: str = ""

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
            time=data.get("time", ""),
            outfit_style=data.get("outfit_style", ""),
            base_style=data.get("base_style", ""),
            reference_query=data.get("reference_query", ""),
            outfit=data.get("outfit", ""),
            schedule=data.get("schedule", ""),
            schedule_prompt=data.get("schedule_prompt", ""),
            schedule_details=data.get("schedule_details", []) if isinstance(data.get("schedule_details"), list) else [],
            image_path=data.get("image_path", ""),
            image_filename=data.get("image_filename", ""),
            prompt=data.get("prompt", ""),
            caption=data.get("caption", ""),
            caption_status=str(data.get("caption_status") or "").strip(),
            status=data.get("status", "ok"),
            source=data.get("source", ""),
            shot_type=data.get("shot_type", ""),
            prompt_mode=data.get("prompt_mode", ""),
            pure_prompt=pure_prompt,
            custom_prompt=data.get("custom_prompt", ""),
            custom_ref_mode=data.get("custom_ref_mode", ""),
            outfit_keywords=data.get("outfit_keywords", ""),
            scene_keywords=data.get("scene_keywords", ""),
            photo_style_en=data.get("photo_style_en", ""),
            schedule_llm_model=str(data.get("schedule_llm_model") or data.get("llm_model") or "").strip(),
            xiaohongshu_search_query=str(data.get("xiaohongshu_search_query") or "").strip(),
            generation_type=data.get("generation_type", ""),
            character_id=data.get("character_id", ""),
            character_ids=data.get("character_ids", []) if isinstance(data.get("character_ids"), list) else [],
            character_names=data.get("character_names", []) if isinstance(data.get("character_names"), list) else [],
            chat_room_id=data.get("chat_room_id", ""),
        )
