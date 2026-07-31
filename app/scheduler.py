"""日程生成器 - 调用 LLM 生成每日穿搭+日程"""
import asyncio
import base64
import json
import logging
import mimetypes
import os
import random
import re
from datetime import date, timedelta
from typing import Optional

from calendar_context import build_day_context
from data import DailyEntry
from keyword_cloud import build_schedule_keyword_prompt_block
from text_repair import repair_mojibake_text
from settings import (
    DEFAULT_OUTFIT_STYLES,
    llm_choice_text,
    llm_request_config,
    llm_response_excerpt,
    llm_text_from_value,
    llm_temperature_param_error,
    load_enabled_outfit_styles,
    load_runtime_persona,
    load_schedule_forbidden_keywords,
    service_today,
)

logger = logging.getLogger(__name__)

# 穿搭风格池
OUTFIT_STYLES = DEFAULT_OUTFIT_STYLES

# 心情色彩池
MOOD_COLORS = [
    "粉色", "米色", "蓝色", "紫色", "红色",
    "黑色", "白色", "绿色", "黄色", "灰色",
]

# 日程类型池
SCHEDULE_TYPES = [
    "工作日", "约会日", "宅家日", "购物日", "运动日",
    "学习日", "社交日", "旅行日", "创作日", "放松日",
]

BED_IDLE_TERMS = (
    "赖床",
    "床上",
    "被窝",
    "躺床",
    "躺在床",
    "窝在床",
    "翻手机",
    "刷手机",
    "玩手机",
)

COOKING_TERMS = (
    "做饭",
    "做早餐",
    "做午餐",
    "做晚餐",
    "准备早餐",
    "准备午餐",
    "准备晚餐",
    "下厨",
    "料理",
    "煮饭",
    "煮面",
    "煮一",
    "煮碗",
    "炒菜",
    "烤饼",
    "烤蛋糕",
    "烤吐司",
    "厨房为自己做",
    "厨房里做",
)

LOW_ENERGY_HOME_TERMS = (
    "窝在沙发",
    "窝在客厅",
    "抱着抱枕",
    "追番",
    "看动漫",
    "看电影",
    "刷剧",
    "发呆",
)

SCHEDULE_ACTIVITY_CATEGORY_ALIASES = {
    "stretch": ("拉伸", "瑜伽", "热身", "舒展"),
    "convenience_store": ("便利店", "超市", "杂货店"),
    "hydration": ("气泡水", "电解质水", "蛋白粉", "补充能量", "运动饮料"),
    "fitness": ("健身", "训练", "深蹲", "硬拉", "慢跑", "跑步", "运动场", "力量区"),
    "activity_tracking": ("运动数据", "训练数据", "同步到平板", "数据记录"),
    "return_home": ("回家", "返家", "骑共享单车"),
    "room_reset": ("整理房间", "收拾房间", "整理桌面", "打扫"),
    "meditation": ("冥想", "正念", "呼吸练习"),
    "bathing": ("泡澡", "热水澡", "洗澡"),
    "bookstore_reading": ("书店", "阅读", "看书", "杂志"),
    "creative_work": ("画画", "草图", "设计稿", "手账", "写作", "创作"),
    "shopping": ("逛街", "购物中心", "商场", "试穿"),
    "social": ("见朋友", "聚会", "约会", "一起吃", "聊天", "牵手", "双人", "共进"),
}

ACCESSORY_CATEGORY_ALIASES = {
    "necklace": ("锁骨链", "项链", "吊坠", "necklace", "pendant", "choker"),
    "earrings": ("耳钉", "耳环", "耳饰", "earrings", "earring", "ear studs", "stud earrings"),
    "bracelet": ("手链", "手镯", "bracelet", "bangle"),
    "ring": ("戒指", "ring"),
    "watch": ("手表", "腕表", "watch"),
    "hair_clip": ("发夹", "发簪", "hair clip", "hairpin", "barrette"),
    "hair_ribbon": ("发带", "发绳", "头绳", "hair ribbon", "hair tie", "scrunchie"),
    "bag": ("斜挎包", "手提包", "单肩包", "腋下包", "邮差包", "背包", "crossbody bag", "handbag", "shoulder bag", "messenger bag", "backpack"),
    "glasses": ("墨镜", "眼镜", "sunglasses", "glasses"),
    "belt": ("腰带", "皮带", "belt"),
}
ACCESSORY_CATEGORY_LABELS = {
    "necklace": "项链",
    "earrings": "耳饰",
    "bracelet": "手链",
    "ring": "戒指",
    "watch": "手表",
    "hair_clip": "发夹",
    "hair_ribbon": "发带/发绳",
    "bag": "包",
    "glasses": "眼镜",
    "belt": "腰带",
}
ACCESSORY_COLOR_ALIASES = {
    "silver": ("银色", "银白", "silver"),
    "gold": ("金色", "香槟金", "gold", "golden"),
    "black": ("黑色", "black"),
    "white": ("白色", "米白", "奶白", "white", "ivory"),
    "pink": ("粉色", "浅粉", "pink"),
    "red": ("红色", "酒红", "red", "burgundy"),
    "blue": ("蓝色", "浅蓝", "blue"),
    "green": ("绿色", "green"),
    "purple": ("紫色", "purple", "lavender"),
    "beige": ("米色", "杏色", "裸色", "beige", "nude"),
}
ACCESSORY_COLOR_LABELS = {
    "silver": "银色",
    "gold": "金色",
    "black": "黑色",
    "white": "白色",
    "pink": "粉色",
    "red": "红色",
    "blue": "蓝色",
    "green": "绿色",
    "purple": "紫色",
    "beige": "米杏色",
}
ACCESSORY_MOTIF_ALIASES = {
    "star": ("十字星", "星星", "星形", "星状", "star"),
    "heart": ("爱心", "心形", "heart"),
    "pearl": ("珍珠", "pearl"),
    "flower": ("花朵", "花形", "雏菊", "玫瑰", "flower", "floral", "daisy", "rose"),
    "bow": ("蝴蝶结", "bow"),
    "crystal": ("水晶", "crystal"),
    "gem": ("宝石", "红宝石", "蓝宝石", "gem", "ruby", "sapphire"),
}
ACCESSORY_MOTIF_LABELS = {
    "star": "星形",
    "heart": "爱心",
    "pearl": "珍珠",
    "flower": "花朵",
    "bow": "蝴蝶结",
    "crystal": "水晶",
    "gem": "宝石",
}

# Normalize the parts that make two outfits materially alike. Style names are
# intentionally excluded so a disliked look does not ban an entire style.
OUTFIT_GARMENT_ALIASES = {
    "knit_top": (
        "针织打底", "针织短袖", "针织上衣", "针织衫", "毛衣",
        "罗纹上衣", "螺纹上衣", "knit top", "knitted top", "knit shirt",
        "knit tee", "ribbed top", "sweater", "turtleneck top",
    ),
    "shirt": ("衬衫", "衬衣", "blouse", "button-up shirt", "button down shirt"),
    "t_shirt": (
        "t恤", "短袖衫", "短袖上衣", "t-shirt", "tee shirt", "ribbed tee", "tee",
    ),
    "camisole": ("吊带上衣", "吊带背心", "小背心", "camisole", "tank top"),
    "tailored_vest": (
        "西装马甲", "西装背心", "正装马甲", "tailored vest", "suit vest", "waistcoat",
    ),
    "blazer": ("西装外套", "西服外套", "blazer", "suit jacket"),
    "cardigan": ("针织开衫", "开衫", "cardigan"),
    "jacket": ("夹克", "短外套", "jacket"),
    "coat": ("大衣", "风衣", "长外套", "coat", "trench coat"),
    "dress": ("连衣裙", "裙装", "dress", "sundress"),
    "skirt": ("半身裙", "百褶裙", "短裙", "长裙", "skirt"),
    "trousers": (
        "西装裤", "阔腿裤", "直筒裤", "烟管裤", "喇叭裤", "灯笼裤", "锥形裤",
        "工装裤", "九分裤", "长裤", "裤子",
        "trousers", "wide-leg pants", "wide leg pants", "straight-leg pants",
        "straight leg pants", "tailored pants", "palazzo pants", "flared pants",
        "flare pants", "bell-bottoms", "culottes", "slacks", "pants",
    ),
    "jeans": ("牛仔裤", "jeans", "denim pants"),
    "shorts": ("短裤", "shorts"),
    "leggings": ("打底裤", "紧身裤", "leggings"),
    "loafers": ("乐福鞋", "loafers", "loafer shoes"),
    "sneakers": ("运动鞋", "帆布鞋", "小白鞋", "sneakers", "canvas shoes"),
    "boots": ("短靴", "长靴", "马丁靴", "boots", "ankle boots"),
    "heels": ("高跟鞋", "细跟鞋", "heels", "pumps"),
    "sandals": (
        "凉鞋", "凉拖", "拖鞋", "穆勒鞋", "sandals", "slides", "slippers", "mules",
        "mule shoes",
    ),
    "flats": ("平底鞋", "芭蕾鞋", "玛丽珍鞋", "flats", "ballet flats", "mary janes"),
}
OUTFIT_GARMENT_LABELS = {
    "knit_top": "针织上衣", "shirt": "衬衫", "t_shirt": "T恤", "camisole": "吊带上衣",
    "tailored_vest": "西装马甲", "blazer": "西装外套", "cardigan": "开衫",
    "jacket": "夹克", "coat": "大衣/风衣", "dress": "连衣裙", "skirt": "半身裙",
    "trousers": "长裤", "jeans": "牛仔裤", "shorts": "短裤", "leggings": "紧身裤",
    "loafers": "乐福鞋", "sneakers": "运动鞋", "boots": "靴子", "heels": "高跟鞋",
    "sandals": "凉鞋/凉拖", "flats": "平底鞋",
}
OUTFIT_COLOR_ALIASES = {
    "black": ("黑色", "纯黑", "black"),
    "white": ("白色", "纯白", "米白", "奶白", "ivory", "white", "off-white"),
    "gray": ("灰色", "深灰", "浅灰", "炭灰", "charcoal", "gray", "grey"),
    "beige": ("米色", "杏色", "奶杏", "奶油色", "cream", "beige", "nude"),
    "brown": ("棕色", "深棕", "咖色", "咖啡色", "brown", "chocolate"),
    "pink": ("粉色", "浅粉", "灰粉", "pink", "rose pink"),
    "red": ("红色", "酒红", "砖红", "red", "burgundy"),
    "blue": ("蓝色", "浅蓝", "深蓝", "藏蓝", "blue", "navy"),
    "green": ("绿色", "墨绿", "olive", "green"),
    "purple": ("紫色", "薰衣草色", "purple", "lavender"),
    "yellow": ("黄色", "鹅黄", "yellow"),
    "silver": ("银色", "银白", "silver"),
    "gold": ("金色", "香槟金", "gold", "golden"),
}
OUTFIT_COLOR_LABELS = {
    "black": "黑色", "white": "白色", "gray": "灰色", "beige": "米杏色",
    "brown": "棕色", "pink": "粉色", "red": "红色", "blue": "蓝色",
    "green": "绿色", "purple": "紫色", "yellow": "黄色", "silver": "银色",
    "gold": "金色",
}
OUTFIT_MATERIAL_ALIASES = {
    "knit": ("针织", "毛线", "罗纹", "螺纹", "knit", "knitted", "ribbed", "rib knit"),
    "suiting": ("西装面料", "西服面料", "suiting fabric", "tailored fabric"),
    "cotton": ("棉质", "纯棉", "cotton"),
    "denim": ("牛仔", "denim"),
    "leather": ("皮质", "皮革", "漆皮", "leather", "patent leather"),
    "chiffon": ("雪纺", "chiffon"),
    "satin": ("缎面", "缎质", "satin"),
    "lace": ("蕾丝", "lace"),
    "linen": ("亚麻", "linen"),
    "wool": ("羊毛", "呢料", "wool", "woolen"),
    "velvet": ("丝绒", "天鹅绒", "velvet"),
    "silk": ("真丝", "丝质", "silk"),
    "canvas": ("帆布", "canvas"),
}
OUTFIT_MATERIAL_LABELS = {
    "knit": "针织", "suiting": "西装面料", "cotton": "棉质", "denim": "牛仔",
    "leather": "皮革", "chiffon": "雪纺", "satin": "缎面", "lace": "蕾丝",
    "linen": "亚麻", "wool": "羊毛/呢料", "velvet": "丝绒", "silk": "真丝",
    "canvas": "帆布",
}
OUTFIT_SILHOUETTE_ALIASES = {
    "high_neck": ("高领", "半高领", "立领", "high-neck", "high neck", "turtleneck"),
    "v_neck": ("v领", "v 领", "v形领", "v形剪裁", "v-neck", "v neck"),
    "sleeveless": ("无袖", "sleeveless"),
    "short_sleeve": ("短袖", "short-sleeve", "short sleeve"),
    "long_sleeve": ("长袖", "long-sleeve", "long sleeve"),
    "fitted": ("修身", "贴身", "合体", "fitted", "slim fit", "body-hugging"),
    "cropped": ("短款", "露脐", "九分", "cropped", "crop top", "ankle-length"),
    "oversized": ("宽松", "宽松廓形", "oversized", "loose fit"),
    "high_waist": ("高腰", "high-waist", "high waist", "high-rise", "high rise"),
    "wide_leg": (
        "阔腿", "喇叭", "wide-leg", "wide leg", "palazzo", "flared", "flare-leg",
        "bell-bottom",
    ),
    "straight_leg": ("直筒", "straight-leg", "straight leg"),
    "pleated": ("百褶", "压褶", "pleated"),
    "a_line": ("a字", "a 字", "a-line", "a line"),
    "structured": ("挺括", "硬挺", "利落廓形", "structured", "crisp tailoring"),
    "draped": ("垂坠", "垂感", "draped", "flowing"),
}
OUTFIT_SILHOUETTE_LABELS = {
    "high_neck": "高领", "v_neck": "V领", "sleeveless": "无袖", "short_sleeve": "短袖",
    "long_sleeve": "长袖", "fitted": "修身", "cropped": "短款/九分", "oversized": "宽松",
    "high_waist": "高腰", "wide_leg": "阔腿", "straight_leg": "直筒", "pleated": "百褶",
    "a_line": "A字", "structured": "挺括廓形", "draped": "垂坠",
}
OUTFIT_HAIR_ALIASES = {
    "high_ponytail": ("高马尾", "high ponytail"),
    "low_ponytail": ("低马尾", "low ponytail"),
    "twin_ponytails": ("双马尾", "twin ponytails", "pigtails"),
    "bun": ("丸子头", "发髻", "hair bun", "chignon"),
    "twin_buns": ("双丸子头", "twin buns", "double buns"),
    "half_up": ("半扎", "half-up", "half up"),
    "braided": ("编发", "麻花辫", "braid", "braided"),
    "straight_down": ("中分直发", "顺直长发", "straight hair"),
    "loose_down": ("自然披散", "披肩长发", "披散", "hair worn down", "loose hair"),
}

SCHEDULE_DIVERSITY_DOMAINS = (
    "参与体验：课程、工作坊、排练、志愿活动、现场实践或需要亲手完成的任务。",
    "城市与文化探索：陌生街区、公共文化活动、限定展演、开放日、在地观察或主题路线。",
    "创作与挑战：完成一个有明确成果的作品、技能挑战、记录任务、策划任务或小型项目。",
    "自然与户外：结合当天天气与真实日历，设计有目的的观察、探索、体验或轻量活动。",
    "社交与生活事件：允许约会、见面、聚会或共同体验，但活动本身要有具体事件和推进。",
)

# This guard is deliberately broad. The LLM remains responsible for semantic
# novelty; these families only catch candidates that split one mundane mainline
# into many superficially different schedule rows.
SCHEDULE_BROAD_ACTIVITY_FAMILIES = {
    "retail": (
        "购物", "逛街", "商场", "购物中心", "市集", "专柜", "试穿", "试戴", "试闻",
        "买一", "买了", "购买", "新买", "新购", "选购", "挑选", "比较几款", "书店",
        "文具店", "花店", "小店",
    ),
    "organizing": (
        "整理", "收拾", "收纳", "归类", "分类", "打扫", "清单", "提纲", "复盘",
        "摆上", "挂进衣柜",
    ),
    "passive_media": (
        "刷剧", "追剧", "追番", "看动漫", "看动画", "看电影", "听播客", "刷视频",
    ),
    "exercise": (
        "运动", "跑步", "慢跑", "快走", "健身", "训练", "瑜伽", "拉伸", "骑行",
        "游泳", "徒步",
    ),
    "reading_writing": (
        "阅读", "看书", "读书", "小说", "杂志", "书架", "书籍", "文档", "笔记",
        "记录", "总结", "提纲", "计划", "手账", "日记", "便签", "明信片",
    ),
}

SCHEDULE_BROAD_ACTIVITY_FAMILY_LABELS = {
    "retail": "购物/选购",
    "organizing": "整理/规划",
    "passive_media": "被动影音消遣",
    "exercise": "运动训练",
    "reading_writing": "阅读/记录/规划",
}

BASE_STYLE_OPTIONS = {"cool", "girly", "sweet"}
SCHEDULE_PHOTO_STYLE_RULES = """【摄影风格判断 photo_style_en — 由你根据今日整体日程自行判断，不要固定套模板】
这不是穿搭风格，也不是固定 quality_prefix。
请阅读今天的 schedule / schedule_details / outfit_style / mood 后，用 1-3 句纯英文写出今天最合适的摄影语言。

判断原则（自行选择，不要写死某一套）：
- 若今天整体更像居家、通勤、便利店、整理房间、随手记录：偏 candid real-life smartphone / natural ambient light / imperfect framing。
- 若今天整体更像咖啡馆窗边、花市、城市散步、生活方式打卡：可加入 casual everyday snapshot / handheld friend-like framing，但仍保持真实手机感。
- 若今天整体更像夜景散步、餐厅、演出、精致出门：可加强 ambient practical light / warm practical lamps / shallow environmental context，但不要变成广告棚拍或电影海报。
- 始终是照片，不是 anime/illustration/CGI；不要塑料皮肤，不要过度精修。
- 不要写 Masterpiece / ultra-realistic / photorealistic / cinematic lighting / HDR glow / cinematic color grade 这类容易漂成大片或 AI 滤镜的词。
- 不要描述人物外貌、发型、服装单品（那些由 appearance 与 schedule_details 负责）。
- 只写镜头、光线、取景、色彩真实感、质感这些摄影语言。

示例方向（仅参考气质，禁止照抄整句）：
- casual phone snapshot, natural window light, true-to-life color, slightly imperfect framing
- candid handheld smartphone photo after dinner, practical street ambient light, mild optical softness
- quiet home snapshot, soft indoor ambient light, natural skin texture, no heavy retouching
"""

SCHEDULE_DETAIL_REQUIRED_FIELDS = (
    "time",
    "activity_zh",
    "activity_en",
    "action_en",
    "scene_en",
    "outfit_en",
    "hair_en",
)

DEFAULT_REQUIRED_PERIODS = [
    {"name": "morning", "label": "早", "start": "06:00", "end": "11:59"},
    {"name": "midday", "label": "中", "start": "12:00", "end": "13:59"},
    {"name": "afternoon", "label": "午", "start": "14:00", "end": "18:59"},
    {"name": "evening", "label": "晚", "start": "19:00", "end": "01:59"},
]
SCHEDULE_PHOTO_QUIET_START_MINUTE = 3 * 60
SCHEDULE_PHOTO_QUIET_END_MINUTE = 6 * 60


def _stream_text_fragment(value) -> str:
    """Preserve exact text fragments from OpenAI-compatible stream chunks."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "".join(_stream_text_fragment(item) for item in value)
    if isinstance(value, dict):
        for key in ("text", "content", "value", "output_text"):
            if key in value:
                return _stream_text_fragment(value.get(key))
    return ""


def _buffer_openai_sse_response(response):
    """Collect an OpenAI-compatible SSE response into a normal JSON response."""
    content_parts = []
    reasoning_parts = []
    finish_reason = None
    model = ""
    usage = None
    last_payload = None
    error_payload = None

    # Do not let requests guess an SSE charset. Some OpenAI-compatible
    # proxies omit charset=utf-8, causing requests to decode UTF-8 as
    # ISO-8859-1 and turn Chinese text into mojibake.
    for raw_line in response.iter_lines(decode_unicode=False):
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="replace").strip()
        else:
            line = str(raw_line or "").strip()
        if not line or line.startswith(":") or line.startswith("event:"):
            continue
        data_text = line[5:].strip() if line.startswith("data:") else line
        if data_text == "[DONE]":
            continue
        try:
            payload = json.loads(data_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        last_payload = payload
        if payload.get("error"):
            error_payload = payload
            break
        if payload.get("model"):
            model = str(payload.get("model") or "")
        if payload.get("usage") is not None:
            usage = payload.get("usage")
        choices = payload.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            container = choice.get("delta")
            if not isinstance(container, dict):
                container = choice.get("message")
            if isinstance(container, dict):
                content_parts.append(_stream_text_fragment(container.get("content")))
                reasoning_parts.append(_stream_text_fragment(container.get("reasoning_content")))
            else:
                content_parts.append(_stream_text_fragment(choice.get("text")))
            if choice.get("finish_reason") is not None:
                finish_reason = choice.get("finish_reason")

    if error_payload is not None:
        buffered = error_payload
        error = error_payload.get("error") if isinstance(error_payload.get("error"), dict) else {}
        status = error_payload.get("status") or error.get("status") or 502
        try:
            response.status_code = int(status)
        except (TypeError, ValueError):
            response.status_code = 502
    elif content_parts or reasoning_parts:
        message = {"role": "assistant", "content": "".join(content_parts)}
        reasoning_text = "".join(reasoning_parts)
        if reasoning_text:
            message["reasoning_content"] = reasoning_text
        buffered = {
            "choices": [{"message": message, "finish_reason": finish_reason}],
        }
        if model:
            buffered["model"] = model
        if usage is not None:
            buffered["usage"] = usage
    elif isinstance(last_payload, dict):
        buffered = last_payload
    else:
        buffered = {
            "error": {
                "type": "stream_error",
                "code": "empty_stream",
                "message": "stream response contained no readable data",
            }
        }
        response.status_code = 502

    response._content = json.dumps(buffered, ensure_ascii=False).encode("utf-8")
    response._content_consumed = True
    try:
        response.close()
    except Exception:
        pass
    return response

JSON_OUTPUT_CONTRACT = """【最高优先级输出协议】
只允许输出一个合法 JSON 对象。回复第一个字符必须是 {，最后一个字符必须是 }。
禁止输出 Markdown、代码块、解释、复述任务、思考过程、分析过程或“我们被要求/首先分析/下面是”等说明文字。"""

SCHEDULE_IMAGE_SAFETY_RULES = """所有生图字段必须描述明确 25 岁以上成年女性的非性化日常摄影；聊天人设中的年龄只属于对话设定，不能写进 reference_query、prompt、schedule_prompt 或 schedule_details。
禁止在生图字段使用 18-year-old、teen、girl、young-looking、youthful、innocent、seductive、sexualized、lingerie、sheer、cleavage 等年龄模糊或性化表达。
“性感风”只能解释为成熟、利落、时尚的成年人日常造型：服装必须完整、不透视、领口得体、适合当前活动，动作和镜头聚焦日程任务，不突出身体曲线，不使用暧昧卧室姿态。
outfit_en 必须使用具体的日常成衣名称；吊带类上衣要明确 opaque、full coverage、modest neckline，不能写成内衣、睡裙或透明罩衫。"""

SCHEDULE_SOLO_CAMERA_RULES = """【最高优先级·生图镜头原则（交给你自行判断，不靠关键词黑名单）】
中文展示日程与小心思可以自然写约会、见面、牵手、双人晚餐等与人相关的生活事件。
但你在写任何会进入生图的描述时，必须自己判断画面：镜头里只能清楚拍到角色本人。
如果日程语义涉及另一人，请把“和谁在一起”留在中文故事里；生图描述只表现角色自己的动作、表情、道具和氛围。
你可以保留约会/社交气氛（烛光、双人餐桌布置、对面空座、绿道傍晚），但不要把第二个人作为可见主体放进画面。
背景里若有路人，应虚化且不可识别，不能变成互动对象。
不要依赖固定禁用词表；用你的理解保证：同一活动既保留故事完整性，又让照片始终是角色单人出镜。"""


class DailyScheduler:
    """使用 LLM 生成每日穿搭和日程"""

    def __init__(self, config: dict, data_dir: str):
        self.config = config
        self.data_dir = data_dir
        self._llm_config = config.get("llm", {})
        self._char = config.get("character", {})

    def _configured_today(self) -> date:
        """Return today's date in the configured service timezone."""
        return service_today(self.config)

    def _day_context(self, target_date: date):
        return build_day_context(target_date, self.config)

    def _select_schedule_type(self, day_context) -> str:
        return random.choice(day_context.schedule_type_pool(SCHEDULE_TYPES))

    @staticmethod
    def _calendar_conflict_message(day_context, conflicts: list[str]) -> str:
        if not conflicts:
            return ""
        return (
            f"{day_context.date_label} 是{day_context.day_type_label}，"
            f"不应出现休息日工作/上学安排: {', '.join(conflicts)}"
        )

    @staticmethod
    def _activity_has(activity: str, terms: tuple[str, ...]) -> bool:
        compact = re.sub(r"\s+", "", str(activity or ""))
        return any(term in compact for term in terms)

    @staticmethod
    def _activity_signature(activity: str) -> str:
        text = re.sub(r"\s+", "", str(activity or ""))
        text = re.sub(r"[，,。.!！?？；;、：:（）()《》“”\"'‘’]", "", text)
        for filler in ("今天的", "今日的", "一份", "简单的", "精致的", "舒服的", "轻松的"):
            text = text.replace(filler, "")
        return text[:18]

    @classmethod
    def _activity_categories(cls, activity: str) -> set[str]:
        compact = re.sub(r"\s+", "", str(activity or ""))
        return {
            category
            for category, terms in SCHEDULE_ACTIVITY_CATEGORY_ALIASES.items()
            if any(term in compact for term in terms)
        }

    @staticmethod
    def _broad_activity_families(activity: str) -> set[str]:
        compact = re.sub(r"\s+", "", str(activity or ""))
        return {
            family
            for family, terms in SCHEDULE_BROAD_ACTIVITY_FAMILIES.items()
            if any(term in compact for term in terms)
        }

    def _schedule_diversity_prompt_block(self, schedule_history: str) -> str:
        domains = "\n".join(f"- {item}" for item in SCHEDULE_DIVERSITY_DOMAINS)
        history_text = schedule_history or "（无近期日程）"
        return f"""【近 3 天完整日程动作｜多样性参考】
{history_text}

输出 JSON 前，请在内部参考下面的多样性目标；判断过程不要输出：
1. 先把近 3 天每条日程归纳成「人在做什么」的动作族和当天任务主线，而不是只看地点、道具或名词。
2. 尽量避开与近 3 天相同、同义或属于同一任务主线的候选；只换说法、时间、地点、店铺或道具不算真正的新活动。必要的吃饭、休息等生活过渡可以自然重复。
3. 优先选择近 3 天没有出现的新主题，并尽量设计一个“精彩锚点”：今天最值得记住、需要角色真实参与且会推动一天进展的具体事件。
4. 尽量让 6-8 条日程覆盖多种实质不同的动作族和场景，避免购物、整理、阅读或运动等单一活动占据大半天。逛多个商店仍然只算“购物”，在不同房间连续整理仍然只算“整理”。
5. 保持自然转场和前后推进，避免把一次活动拆成多条凑数；尽量让每条活动都有不同的行动变化。
6. 最终自检时，优先提高近 3 天语义差异、当天丰富度、精彩锚点和行动变化；这些是生成质量目标，不是生成后的拒绝条件。

下面只是拓宽构思方向，不是固定动作池；请结合日期、人设和近 3 天历史自行创造，也可以设计这些方向以外的新事件：
{domains}"""

    def _load_schedule_data(self) -> dict:
        path = os.path.join(self.data_dir, "schedule_data.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _daily_schedule_entry(all_data: dict, date_str: str) -> dict:
        direct = all_data.get(date_str)
        if isinstance(direct, dict) and direct.get("status") == "ok":
            return direct
        for entry in all_data.values():
            if (
                isinstance(entry, dict)
                and entry.get("status") == "ok"
                and str(entry.get("date") or "") == date_str
                and entry.get("schedule")
            ):
                return entry
        return {}

    @staticmethod
    def _entry_outfit_text(entry: dict) -> str:
        if not isinstance(entry, dict):
            return ""
        outfit = entry.get("outfit")
        if isinstance(outfit, dict):
            outfit_text = "\n".join(
                f"{key}：{outfit.get(key)}"
                for key in ("风格", "发型", "穿搭")
                if str(outfit.get(key) or "").strip()
            )
        else:
            outfit_text = str(outfit or "")
        parts = [outfit_text]
        parts.extend(
            str(entry.get(field) or "")
            for field in ("prompt", "outfit_keywords", "reference_query")
        )
        details = entry.get("schedule_details")
        if isinstance(details, list):
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                parts.extend(
                    str(detail.get(field) or "")
                    for field in ("outfit_en", "hair_en")
                )
        return "\n".join(part for part in parts if part.strip())

    @staticmethod
    def _entry_outfit_similarity_texts(entry: dict) -> tuple[str, str]:
        """Return clothing and hair text without scene/style noise."""
        if not isinstance(entry, dict):
            return "", ""

        clothing_parts = []
        hair_parts = []
        outfit = entry.get("outfit")
        if isinstance(outfit, dict):
            clothing_parts.append(str(outfit.get("穿搭") or ""))
            hair_parts.append(str(outfit.get("发型") or ""))
        else:
            outfit_text = str(outfit or "")
            found_clothing = False
            for line in re.split(r"[\r\n]+", outfit_text):
                match = re.match(r"\s*(发型|穿搭)\s*[：:]\s*(.*)", line, re.IGNORECASE)
                if not match:
                    continue
                if match.group(1) == "发型":
                    hair_parts.append(match.group(2))
                else:
                    clothing_parts.append(match.group(2))
                    found_clothing = True
            if outfit_text.strip() and not found_clothing:
                clothing_parts.append(outfit_text)

        clothing_parts.append(str(entry.get("outfit_keywords") or ""))
        details = entry.get("schedule_details")
        if isinstance(details, list):
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                clothing_parts.append(str(detail.get("outfit_en") or ""))
                hair_parts.append(str(detail.get("hair_en") or ""))

        if not any(part.strip() for part in clothing_parts):
            clothing_parts.extend((
                str(entry.get("prompt") or ""),
                str(entry.get("reference_query") or ""),
            ))
        if not any(part.strip() for part in hair_parts):
            hair_parts.append(str(entry.get("prompt") or ""))
        return (
            "\n".join(part for part in clothing_parts if part.strip()),
            "\n".join(part for part in hair_parts if part.strip()),
        )

    @staticmethod
    def _text_has_alias(text: str, alias: str) -> bool:
        if not alias:
            return False
        if re.fullmatch(r"[a-z ]+", alias):
            return bool(re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", text))
        return alias in text

    @classmethod
    def _accessory_features(cls, text: str) -> dict[str, str]:
        lowered = str(text or "").lower()
        features = {}
        if not lowered:
            return features

        for category, aliases in ACCESSORY_CATEGORY_ALIASES.items():
            for alias in sorted(aliases, key=len, reverse=True):
                pattern = (
                    rf"(?<![a-z]){re.escape(alias)}(?![a-z])"
                    if re.fullmatch(r"[a-z ]+", alias)
                    else re.escape(alias)
                )
                for match in re.finditer(pattern, lowered):
                    prefix = lowered[max(0, match.start() - 48):match.start()]
                    prefix = re.split(r"[\n,，。；;|]", prefix)[-1]
                    prefix = re.split(r"(?:和|与|以及|\band\b)", prefix)[-1]
                    colors = sorted(
                        color
                        for color, color_aliases in ACCESSORY_COLOR_ALIASES.items()
                        if any(cls._text_has_alias(prefix, item) for item in color_aliases)
                    )
                    motifs = sorted(
                        motif
                        for motif, motif_aliases in ACCESSORY_MOTIF_ALIASES.items()
                        if any(cls._text_has_alias(prefix, item) for item in motif_aliases)
                    )
                    if not colors and not motifs:
                        continue
                    key = "|".join((category, ",".join(colors), ",".join(motifs)))
                    label = "".join(ACCESSORY_COLOR_LABELS[item] for item in colors)
                    label += "".join(ACCESSORY_MOTIF_LABELS[item] for item in motifs)
                    label += ACCESSORY_CATEGORY_LABELS.get(category, category)
                    features[key] = label
        return features

    @classmethod
    def _matched_alias_features(cls, text: str, aliases_by_feature: dict) -> set[str]:
        lowered = str(text or "").lower()
        if not lowered:
            return set()
        return {
            feature
            for feature, aliases in aliases_by_feature.items()
            if any(cls._text_has_alias(lowered, alias) for alias in aliases)
        }

    @classmethod
    def _outfit_similarity_features(cls, entry: dict) -> dict[str, set[str]]:
        clothing_text, hair_text = cls._entry_outfit_similarity_texts(entry)
        accessory_details = set(cls._accessory_features(clothing_text))
        return {
            "garments": cls._matched_alias_features(clothing_text, OUTFIT_GARMENT_ALIASES),
            "colors": cls._matched_alias_features(clothing_text, OUTFIT_COLOR_ALIASES),
            "materials": cls._matched_alias_features(clothing_text, OUTFIT_MATERIAL_ALIASES),
            "silhouettes": cls._matched_alias_features(clothing_text, OUTFIT_SILHOUETTE_ALIASES),
            "hair": cls._matched_alias_features(hair_text, OUTFIT_HAIR_ALIASES),
            "accessory_categories": cls._matched_alias_features(
                clothing_text,
                ACCESSORY_CATEGORY_ALIASES,
            ),
            "accessory_details": accessory_details,
        }

    @staticmethod
    def _feature_containment(left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        return len(left & right) / min(len(left), len(right))

    @classmethod
    def _outfit_similarity_from_features(
        cls,
        candidate_features: dict[str, set[str]],
        disliked_features: dict[str, set[str]],
    ) -> tuple[float, dict[str, set[str]]]:
        shared = {
            key: candidate_features[key] & disliked_features[key]
            for key in candidate_features
        }
        overlap = {
            key: cls._feature_containment(candidate_features[key], disliked_features[key])
            for key in candidate_features
        }
        weights = {
            "garments": 0.44,
            "colors": 0.15,
            "materials": 0.14,
            "silhouettes": 0.17,
            "hair": 0.04,
            "accessory_categories": 0.03,
            "accessory_details": 0.03,
        }
        score = sum(overlap[key] * weight for key, weight in weights.items())
        return score, shared

    @classmethod
    def _outfit_similarity(cls, candidate: dict, disliked: dict) -> tuple[float, dict[str, set[str]]]:
        return cls._outfit_similarity_from_features(
            cls._outfit_similarity_features(candidate),
            cls._outfit_similarity_features(disliked),
        )

    @classmethod
    def _is_disliked_outfit_similar(cls, candidate: dict, disliked: dict) -> tuple[bool, float, dict[str, set[str]]]:
        candidate_features = cls._outfit_similarity_features(candidate)
        disliked_features = cls._outfit_similarity_features(disliked)
        score, shared = cls._outfit_similarity_from_features(
            candidate_features,
            disliked_features,
        )
        garment_overlap = cls._feature_containment(
            candidate_features["garments"],
            disliked_features["garments"],
        )
        garment_matches = len(shared["garments"])
        detail_overlap = max(
            cls._feature_containment(candidate_features[key], disliked_features[key])
            for key in ("colors", "materials", "silhouettes")
        )

        similar = (
            (score >= 0.64 and garment_overlap >= 0.50)
            or (
                garment_matches >= 3
                and garment_overlap >= 0.65
                and score >= 0.54
            )
            or (
                garment_matches >= 2
                and garment_overlap >= 0.75
                and detail_overlap >= 0.50
                and score >= 0.56
            )
        )
        return similar, score, shared

    @staticmethod
    def _feature_labels(features: set[str], labels: dict[str, str]) -> str:
        return "、".join(labels.get(feature, feature) for feature in sorted(features))

    def _disliked_outfit_similarity_error(self, candidate: dict, disliked_items: list[dict]) -> str:
        strongest = None
        for disliked in disliked_items:
            if not isinstance(disliked, dict):
                continue
            similar, score, shared = self._is_disliked_outfit_similar(candidate, disliked)
            if similar and (strongest is None or score > strongest[0]):
                strongest = (score, shared, disliked)
        if strongest is None:
            return ""

        score, shared, disliked = strongest
        matched_parts = []
        if shared["garments"]:
            matched_parts.append(
                "单品 " + self._feature_labels(shared["garments"], OUTFIT_GARMENT_LABELS)
            )
        if shared["colors"]:
            matched_parts.append(
                "配色 " + self._feature_labels(shared["colors"], OUTFIT_COLOR_LABELS)
            )
        if shared["materials"]:
            matched_parts.append(
                "材质 " + self._feature_labels(shared["materials"], OUTFIT_MATERIAL_LABELS)
            )
        if shared["silhouettes"]:
            matched_parts.append(
                "版型 " + self._feature_labels(shared["silhouettes"], OUTFIT_SILHOUETTE_LABELS)
            )
        source = str(disliked.get("date") or disliked.get("id") or "历史反馈")
        return (
            f"与用户标记不喜欢的穿搭高度相似（{source}，相似度 {score:.0%}）："
            + "；".join(matched_parts[:4])
            + "。请更换核心单品组合，并同步改变配色、材质或版型；只改风格名或同义说法不算新穿搭"
        )

    def _recent_outfit_accessories(self, today: date, days: int = 3) -> dict[str, dict]:
        all_data = self._load_schedule_data()
        recent = {}
        for i in range(1, days + 1):
            date_str = (today - timedelta(days=i)).isoformat()
            entry = self._daily_schedule_entry(all_data, date_str)
            for key, label in self._accessory_features(self._entry_outfit_text(entry)).items():
                item = recent.setdefault(key, {"label": label, "dates": []})
                item["dates"].append(date_str)
        return recent

    def _outfit_accessory_repeat_error(self, candidate: dict, recent: dict[str, dict]) -> str:
        if not recent:
            return ""
        features = self._accessory_features(self._entry_outfit_text(candidate))
        repeated = []
        for key in sorted(set(features) & set(recent)):
            item = recent[key]
            dates = "、".join(item.get("dates") or [])
            repeated.append(f"{features[key]}（{dates}）")
        if not repeated:
            return ""
        return (
            "近 3 天已出现相同配饰: "
            + "；".join(repeated[:4])
            + "。请更换配饰类别、颜色或图案，不能只换同义说法"
        )

    def _get_schedule_history(self, today: date, days: int = 3) -> str:
        """Return recent full schedule actions for LLM-led anti-repeat judgment."""
        all_data = self._load_schedule_data()
        lines = []
        for i in range(1, days + 1):
            date_str = (today - timedelta(days=i)).isoformat()
            entry = self._daily_schedule_entry(all_data, date_str)
            if not entry:
                continue
            items = self._schedule_plan_items(entry.get("schedule", ""))
            if not items:
                continue
            day_lines = [f"{time_text} {activity}" for time_text, activity in items]
            # Keep full action text so the model can judge sameness itself.
            lines.append(f"[{date_str}]\n" + "\n".join(day_lines))
        return "\n".join(lines) if lines else "（无近期日程）"

    def _recent_schedule_actions(self, today: date, days: int = 3) -> list[dict]:
        """Collect recent schedule actions with date for post-check against LLM output."""
        all_data = self._load_schedule_data()
        actions = []
        for i in range(1, days + 1):
            date_str = (today - timedelta(days=i)).isoformat()
            entry = self._daily_schedule_entry(all_data, date_str)
            if not entry:
                continue
            for time_text, activity in self._schedule_plan_items(entry.get("schedule", "")):
                activity = str(activity or "").strip()
                if not activity:
                    continue
                actions.append({
                    "date": date_str,
                    "time": time_text,
                    "activity": activity,
                    "signature": self._activity_signature(activity),
                    "core": self._activity_action_core(activity),
                })
        return actions

    @staticmethod
    def _activity_action_core(activity: str) -> str:
        """Normalize an activity into a shorter action core for soft equality checks."""
        text = re.sub(r"\s+", "", str(activity or ""))
        text = re.sub(r"[，,。.!！?？；;、：:（）()《》“”\"'‘’]", "", text)
        for filler in (
            "今天的", "今日的", "一份", "简单的", "精致的", "舒服的", "轻松的",
            "然后", "顺便", "之后", "同时", "并", "和",
        ):
            text = text.replace(filler, "")
        # Drop very common leading scene/time glue without hardcoding activity themes.
        for prefix in ("起床后", "回家后", "出门前", "出门后", "在晨光中", "在柔和的灯光下"):
            if text.startswith(prefix):
                text = text[len(prefix):]
        return text[:28]

    @classmethod
    def _activities_are_same_action(cls, left: str, right: str) -> bool:
        """Judge whether two activities describe essentially the same schedule action."""
        a = cls._activity_action_core(left)
        b = cls._activity_action_core(right)
        if not a or not b:
            return False
        if a == b:
            return True
        # Containment after normalization: synonym rewrites that keep the same verb+object.
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        if len(shorter) >= 8 and shorter in longer:
            return True
        # High character overlap for near-paraphrases of the same action.
        set_a, set_b = set(a), set(b)
        shared = len(set_a & set_b)
        smaller = min(len(set_a), len(set_b))
        if smaller >= 8 and shared / smaller >= 0.72:
            return True
        return False

    def _recent_schedule_category_counts(self, today: date, days: int = 3) -> dict[str, int]:
        all_data = self._load_schedule_data()
        counts = {
            "cooking_days": 0,
            "low_energy_home_days": 0,
            "category_days": [],
            "family_days": [],
        }
        for i in range(1, days + 1):
            date_str = (today - timedelta(days=i)).isoformat()
            entry = all_data.get(date_str)
            if not isinstance(entry, dict) or entry.get("status") != "ok":
                continue
            items = self._schedule_plan_items(entry.get("schedule", ""))
            activities = [activity for _time_text, activity in items]
            if any(self._activity_has(activity, COOKING_TERMS) for activity in activities):
                counts["cooking_days"] += 1
            if any(self._activity_has(activity, LOW_ENERGY_HOME_TERMS) for activity in activities):
                counts["low_energy_home_days"] += 1
            categories = set().union(*(self._activity_categories(activity) for activity in activities))
            if categories:
                counts["category_days"].append({"date": date_str, "categories": categories})
            families = set().union(*(self._broad_activity_families(activity) for activity in activities))
            if families:
                counts["family_days"].append({"date": date_str, "families": families})
        return counts

    def _schedule_diversity_error(
        self,
        display_items: list[tuple[str, str]],
        recent_counts: Optional[dict[str, int]] = None,
        recent_actions: Optional[list[dict]] = None,
    ) -> str:
        """Return an advisory diversity note without deciding acceptance.

        The generator receives full recent history in its prompt. This helper
        only describes obvious repetition for logs and diagnostics; callers
        must not reject an otherwise valid schedule because of this note.
        """
        if not display_items:
            return ""
        recent_counts = recent_counts or {}
        recent_actions = recent_actions or []

        repeated = []
        for time_text, activity in display_items:
            for recent in recent_actions:
                if not self._activities_are_same_action(activity, recent.get("activity", "")):
                    continue
                repeated.append(
                    f"今天 {time_text}「{activity[:28]}」≈ {recent.get('date')} "
                    f"{recent.get('time', '')}「{str(recent.get('activity') or '')[:28]}」"
                )
                break
        if repeated:
            return (
                "近 3 天已出现相同或高度相似的日程动作，请重新设计全新动作/任务主线，不要同义改写："
                + "；".join(repeated[:4])
            )

        signatures = [self._activity_signature(activity) for _time_text, activity in display_items]
        duplicates = sorted({item for item in signatures if item and signatures.count(item) > 1})
        if duplicates:
            return "schedule 内部活动过于重复: " + "、".join(duplicates[:3])

        family_counts: dict[str, int] = {}
        for _time_text, activity in display_items:
            for family in self._broad_activity_families(activity):
                family_counts[family] = family_counts.get(family, 0) + 1
        dominant = sorted(
            (
                (family, count)
                for family, count in family_counts.items()
                if count > len(display_items) / 2
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        if dominant:
            family, count = dominant[0]
            label = SCHEDULE_BROAD_ACTIVITY_FAMILY_LABELS.get(family, family)
            recent_dates = [
                str(day.get("date") or "")
                for day in recent_counts.get("family_days", [])
                if family in set(day.get("families") or [])
            ]
            recent_note = (
                f"，而且近 3 天的 {', '.join(recent_dates[:3])} 已出现该主线"
                if recent_dates
                else ""
            )
            return (
                f"全天主线过于单一：{len(display_items)} 条中有 {count} 条属于「{label}」"
                f"{recent_note}。不要把同一活动拆成多个地点/物品凑数；请保留自然转场，"
                "改成更多实质不同的动作族，并加入一个需要真实参与的精彩锚点。"
            )

        repeated_families = []
        for family, count in family_counts.items():
            if count < 2:
                continue
            recent_dates = [
                str(day.get("date") or "")
                for day in recent_counts.get("family_days", [])
                if family in set(day.get("families") or [])
            ]
            if recent_dates:
                repeated_families.append((family, count, recent_dates))
        if repeated_families:
            family, count, recent_dates = sorted(
                repeated_families,
                key=lambda item: item[1],
                reverse=True,
            )[0]
            label = SCHEDULE_BROAD_ACTIVITY_FAMILY_LABELS.get(family, family)
            return (
                f"近 3 天任务主线重复：今天有 {count} 条属于「{label}」，"
                f"而 {', '.join(recent_dates[:3])} 已出现该动作族。"
                "单条必要生活过渡可以保留，但不要让旧动作族再次成为今天的主线或连续次主线；"
                "请换成不同的参与方式，并重新设计精彩锚点。"
            )
        return ""

    def _runtime_persona(self) -> dict:
        return load_runtime_persona(self.config, self.data_dir)

    def _schedule_forbidden_keywords(self) -> list[str]:
        return load_schedule_forbidden_keywords(self.config, self.data_dir)

    def _schedule_forbidden_prompt_block(self) -> str:
        keywords = self._schedule_forbidden_keywords()
        if not keywords:
            return "（无）"
        text = "、".join(keywords)
        return (
            f"禁止关键词：{text}\n"
            "硬性要求：outfit、schedule、schedule_prompt、schedule_details、prompt、caption、reference_query、"
            "outfit_keywords、scene_keywords 都不能出现这些关键词，也不要安排相关活动、道具、场景或配饰。"
            "如果禁词是中文，也要避开对应英文同义词；例如“包”要同时避开 bag、handbag、purse、tote、package、packing 等相关内容。"
        )

    def _schedule_keyword_cloud_prompt_block(
        self,
        limit: int = 5,
        selection_key: str = "",
    ) -> str:
        return build_schedule_keyword_prompt_block(
            self.data_dir,
            limit=limit,
            selection_key=selection_key,
        )

    @staticmethod
    def _schedule_forbidden_variants(keyword: str) -> set[str]:
        text = re.sub(r"\s+", " ", str(keyword or "")).strip().casefold()
        if not text:
            return set()
        variants = {text}
        if text == "包":
            variants.update({
                "bag",
                "bags",
                "handbag",
                "hand bag",
                "purse",
                "tote",
                "tote bag",
                "package",
                "packages",
                "packing",
                "pack",
                "parcel",
            })
        return variants

    def _schedule_forbidden_output_error(self, data: dict) -> str:
        keywords = self._schedule_forbidden_keywords()
        if not keywords:
            return ""
        fields = [
            data.get("outfit_style", ""),
            data.get("reference_query", ""),
            data.get("outfit", ""),
            data.get("schedule", ""),
            data.get("schedule_prompt", ""),
            data.get("prompt", ""),
            data.get("caption", ""),
            data.get("outfit_keywords", ""),
            data.get("scene_keywords", ""),
            data.get("photo_style_en", ""),
        ]
        details = data.get("schedule_details")
        if isinstance(details, list):
            for item in details:
                if isinstance(item, dict):
                    fields.extend(str(value or "") for value in item.values())
        text = "\n".join(self._text_field(field) for field in fields).casefold()
        for keyword in keywords:
            for variant in self._schedule_forbidden_variants(keyword):
                if not variant:
                    continue
                if re.search(r"[a-z0-9]", variant):
                    pattern = r"(?<![a-z0-9])" + re.escape(variant) + r"(?![a-z0-9])"
                    if re.search(pattern, text):
                        return f"输出包含禁词「{keyword}」相关内容: {variant}"
                    continue
                if variant in text:
                    return f"输出包含禁词「{keyword}」相关内容: {variant}"
        return ""

    def _read_config_key(self, key: str) -> str:
        """Read a value from api_keys_config.json (set via Web UI)."""
        config_path = os.path.join(self.data_dir, "api_keys_config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    val = data.get(key, "")
                    if val:
                        return val
            except Exception:
                pass
        # Fallback: environment variable
        env_map = {"cpa_url": "CPA_BASE_URL", "cpa_key": "CPA_API_KEY"}
        return os.getenv(env_map.get(key, ""), "")

    @staticmethod
    def _response_format_param_error(value) -> bool:
        excerpt = llm_response_excerpt(value, limit=500).lower()
        return "response_format" in excerpt or "json_object" in excerpt

    @staticmethod
    def _thinking_param_error(value) -> bool:
        excerpt = llm_response_excerpt(value, limit=500).lower()
        return "thinking" in excerpt and any(
            marker in excerpt
            for marker in (
                "unsupported",
                "unknown",
                "unrecognized",
                "not allowed",
                "invalid parameter",
            )
        )

    @staticmethod
    def _should_disable_thinking(model: str) -> bool:
        model_name = str(model or "").lower()
        return "deepseek" in model_name or "grok" in model_name

    @staticmethod
    def _request_exception_detail(error: Exception) -> str:
        """Return a concise, user-actionable request failure reason for logs."""
        message = str(error).strip()
        name = error.__class__.__name__
        if message:
            return f"{name}: {message[:500]}"
        return name

    @staticmethod
    def _choice_final_text(choice) -> str:
        """Prefer final assistant content and do not treat reasoning as schedule JSON."""
        if not isinstance(choice, dict):
            return llm_text_from_value(choice)
        message = choice.get("message")
        if isinstance(message, dict):
            for key in ("content", "text", "output_text"):
                text = llm_text_from_value(message.get(key))
                if text:
                    return text
        delta = choice.get("delta")
        if isinstance(delta, dict):
            for key in ("content", "text", "output_text"):
                text = llm_text_from_value(delta.get(key))
                if text:
                    return text
        for key in ("text", "content", "output_text"):
            text = llm_text_from_value(choice.get(key))
            if text:
                return text
        return ""

    async def _call_llm(
        self,
        prompt: str,
        timeout: int = 60,
        json_mode: bool = False,
        models_override: Optional[list[str]] = None,
        temperature: Optional[float] = 0.3,
        image_path: str = "",
        require_image: bool = False,
    ) -> Optional[str]:
        """调用 CPA LLM（异步，不阻塞事件循环）"""
        request_config = llm_request_config(self.config, self.data_dir)
        chat_url = request_config["chat_url"]
        api_key = request_config["api_key"]
        models = request_config["models"]
        if models_override is not None:
            models = [
                str(model).strip()
                for model in models_override
                if str(model or "").strip()
            ]
        stream_enabled = bool(request_config.get("stream", False))
        if not chat_url or not models:
            logger.error("LLM config missing: chat_url/models")
            return None

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # deepseek 模型对 system 角色有 reasoning 问题，全放 user 消息。
        # 视觉日程同样只使用一个 user 消息，保持各 OpenAI-compatible 中转兼容。
        user_content: object = prompt
        image_path = str(image_path or "").strip()
        if image_path:
            try:
                if os.path.getsize(image_path) > 12 * 1024 * 1024:
                    raise ValueError("reference image exceeds 12 MB")
                with open(image_path, "rb") as image_file:
                    image_b64 = base64.b64encode(image_file.read()).decode("ascii")
                mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
                user_content = [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
                ]
            except (OSError, ValueError) as exc:
                if require_image:
                    logger.warning("视觉请求图片读取失败，拒绝退回纯文本 LLM: %s", exc)
                    return None
                logger.warning("日程参考图读取失败，退回纯文本 LLM: %s", exc)
        messages = [
            {"role": "user", "content": user_content},
        ]

        loop = asyncio.get_running_loop()

        def _do_request(url, headers, json_data, timeout):
            import requests as req
            try:
                use_stream = bool(json_data.get("stream", False))
                response = req.post(
                    url,
                    headers=headers,
                    json=json_data,
                    timeout=timeout,
                    stream=use_stream,
                )
                if use_stream and response.status_code == 200:
                    response = _buffer_openai_sse_response(response)
                return response, None
            except req.exceptions.Timeout as exc:
                return None, f"请求超时（{self._request_exception_detail(exc)}）"
            except req.exceptions.ConnectionError as exc:
                return None, f"连接失败（{self._request_exception_detail(exc)}）"
            except req.exceptions.RequestException as exc:
                return None, self._request_exception_detail(exc)
            except Exception as exc:
                return None, self._request_exception_detail(exc)

        def _response_error(resp) -> str:
            if resp is None:
                return "no response"
            try:
                data = resp.json()
                if isinstance(data, dict):
                    error = data.get("error")
                    if isinstance(error, dict):
                        message = error.get("message") or error.get("code") or error.get("type")
                        if message:
                            return str(message)[:300]
                    if data.get("msg"):
                        return str(data.get("msg"))[:300]
                    if data.get("status") and "choices" not in data:
                        return json.dumps(data, ensure_ascii=False)[:300]
            except Exception:
                pass
            try:
                return (resp.text or "")[:300]
            except Exception:
                return f"HTTP {getattr(resp, 'status_code', 'unknown')}"

        def _response_json(resp):
            if resp is None:
                return None
            try:
                return resp.json()
            except Exception:
                try:
                    return resp.text
                except Exception:
                    return None

        retryable_statuses = {429, 500, 502, 503, 504}
        direct_fallback_statuses = {401, 403}
        per_model_attempts = 2

        def _model_unavailable(detail: str) -> bool:
            lower = str(detail or "").lower()
            return any(
                marker in lower
                for marker in (
                    "model not found",
                    "model_not_found",
                    "does not exist",
                    "not exist",
                    "no permission",
                    "permission denied",
                    "unauthorized",
                    "forbidden",
                )
            )

        for model in models:
            base_payload = {
                "model": model,
                "messages": messages,
                "max_tokens": 8192 if json_mode and self._should_disable_thinking(model) else 4096,
                "stream": stream_enabled,
            }
            if temperature is not None:
                try:
                    base_payload["temperature"] = max(0.0, min(2.0, float(temperature)))
                except (TypeError, ValueError):
                    base_payload["temperature"] = 0.3
            if json_mode:
                base_payload["response_format"] = {"type": "json_object"}
                if self._should_disable_thinking(model):
                    base_payload["thinking"] = {"type": "disabled"}

            payload = dict(base_payload)
            for attempt in range(1, per_model_attempts + 1):
                try:
                    resp, request_error = await loop.run_in_executor(
                        None,
                        lambda p=dict(payload): _do_request(chat_url, headers, p, timeout),
                    )
                    if resp is not None and resp.status_code == 400:
                        error_body = _response_json(resp)
                        if _model_unavailable(_response_error(resp)):
                            logger.error(
                                "LLM model unavailable: model=%s, detail=%s",
                                model,
                                _response_error(resp),
                            )
                            break
                        retry_payload = dict(payload)
                        retry_reasons = []
                        if "temperature" in retry_payload and llm_temperature_param_error(error_body):
                            retry_payload.pop("temperature", None)
                            retry_reasons.append("temperature")
                        if "response_format" in retry_payload and (
                            self._response_format_param_error(error_body) or json_mode
                        ):
                            retry_payload.pop("response_format", None)
                            retry_reasons.append("response_format")
                        if "thinking" in retry_payload and self._thinking_param_error(error_body):
                            retry_payload.pop("thinking", None)
                            retry_reasons.append("thinking")
                        if retry_reasons:
                            logger.warning(
                                "LLM model %s rejects %s; retrying without unsupported params",
                                model,
                                "/".join(retry_reasons),
                            )
                            resp, request_error = await loop.run_in_executor(
                                None,
                                lambda p=retry_payload: _do_request(chat_url, headers, p, timeout),
                            )
                            payload = retry_payload

                    if resp is not None and resp.status_code == 200:
                        data = resp.json()
                        choices = data.get("choices") if isinstance(data, dict) else None
                        if not choices:
                            logger.error(
                                "LLM call returned invalid response: model=%s, detail=%s",
                                model,
                                _response_error(resp),
                            )
                            break
                        content = self._choice_final_text(choices[0]) if json_mode else llm_choice_text(choices[0])
                        if content:
                            if stream_enabled:
                                logger.info("LLM stream completed: model=%s", model)
                            self._last_llm_model = str(model or "").strip()
                            return repair_mojibake_text(content)
                        if json_mode:
                            reasoning_excerpt = llm_response_excerpt(
                                choices[0].get("message", {}).get("reasoning_content", "") if isinstance(choices[0], dict) else "",
                                limit=220,
                            )
                            if reasoning_excerpt:
                                logger.warning(
                                    "LLM returned reasoning without final JSON content: model=%s, reasoning=%s",
                                    model,
                                    reasoning_excerpt,
                                )
                        logger.error("LLM call returned empty content: model=%s", model)
                        break

                    status = resp.status_code if resp is not None else "request_failed"
                    detail = request_error or _response_error(resp)
                    logger.error(
                        "LLM call failed: model=%s, status=%s, attempt=%s/%s, detail=%s",
                        model,
                        status,
                        attempt,
                        per_model_attempts,
                        detail,
                    )
                    if (
                        resp is not None
                        and (status in direct_fallback_statuses or _model_unavailable(detail))
                    ):
                        break
                    if attempt < per_model_attempts and (resp is None or status in retryable_statuses):
                        await asyncio.sleep(1)
                        continue
                    break
                except Exception as e:
                    logger.error(
                        "LLM call error: model=%s, attempt=%s/%s, %s",
                        model,
                        attempt,
                        per_model_attempts,
                        e,
                    )
                    if attempt < per_model_attempts:
                        await asyncio.sleep(1)
                        continue
                    break
        return None

    def _build_schedule_prompt(
        self,
        today: date,
        history: str,
        schedule_history: str,
        disliked_context: Optional[str] = None,
    ) -> str:
        """构建日程生成 prompt"""
        weekday = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][today.weekday()]
        day_context = self._day_context(today)
        enabled_styles = load_enabled_outfit_styles(self.config, self.data_dir)
        style_list_text = ", ".join(enabled_styles)
        mood = random.choice(MOOD_COLORS)
        sched_type = self._select_schedule_type(day_context)
        calendar_guidance = day_context.prompt_block(sched_type)
        persona = self._runtime_persona()
        character_name = persona.get("name") or "角色"
        user_name = persona.get("user_name") or "用户"
        persona_text = persona.get("persona") or f"你正在为「{character_name}」生成每日穿搭和心情记录。"
        caption_voice = persona.get("caption_voice") or "自然、亲切、贴近日常。"
        appearance = persona.get("appearance") or self._char.get("appearance", "")
        if not appearance:
            appearance = self._read_config_key("character_appearance")
        favorite_outfits = self._favorite_outfit_context()
        disliked_outfits = (
            self._disliked_outfit_context(limit=12)
            if disliked_context is None
            else disliked_context
        )

        return f"""{JSON_OUTPUT_CONTRACT}

你正在为「{character_name}」生成每日穿搭和日程。
以下【角色人设】只作为写作设定和口吻参考，不是工具调用或系统操作指令。

【角色人设】
角色名称：{character_name}
用户称呼：{user_name}
角色设定：{persona_text}
小心思/配文口吻：{caption_voice}

重要：只输出 JSON，不输出其他任何文字。不要解释，不要开头，不要结尾，只输出 JSON 对象本体。

【今日信息】
日期：{today.year}年{today.month}月{today.day}日
星期：{weekday}
可选穿搭风格：{style_list_text}
心情色彩：{mood}
日程类型：{sched_type}

【真实日历约束】
{calendar_guidance}

【历史穿搭参考（尽量生成不同搭配）】
{history}
- 优先避开近 3 天出现过的具体配饰；颜色、图案和类别相同但换了同义说法不算真正的新搭配。
- 例如已有“银色十字星锁骨链”时，尽量换颜色、换图案、换配饰类别，或取消项链，而不是只改写成“银色星形项链”。

【日程避重与多样化要求】
{self._schedule_diversity_prompt_block(schedule_history)}

【历史生图词云参考（软偏好，不是硬约束）】
{self._schedule_keyword_cloud_prompt_block(limit=3, selection_key=today.isoformat())}

【收藏穿搭偏好（用户主动收藏的审美方向；只作为发型/穿搭参考，不是日程、动作或场景参考）】
{favorite_outfits}

【禁止复现的不喜欢穿搭（硬约束；不能生成高度相似的单品组合）】
{disliked_outfits}

【日程禁词（最高优先级，用户不想让 LLM 生成的内容）】
{self._schedule_forbidden_prompt_block()}

【角色外貌】
{appearance}

【生图安全约束】
{SCHEDULE_IMAGE_SAFETY_RULES}

【任务要求】
请为今日生成一份完整的穿搭和日程计划。
必须严格服从【真实日历约束】：周末/法定节假日不要安排上班、上学、通勤、办公室会议、考试、作业或加班；调休上班日才允许按工作日处理。

{SCHEDULE_SOLO_CAMERA_RULES}

{SCHEDULE_PHOTO_STYLE_RULES}

⚠️ 你需要自己选择当天最合适的 outfit_style，并写出 reference_query：
- outfit_style 必须从 [{style_list_text}] 中选择一个，不要使用未启用的风格。
- reference_query 是给系统选择参考图用的自然语言提示，必须概括今天适合的参考图气质、风格、发型/脸部氛围、服装色系和场景 mood；不要写 cool/girly/sweet 三选一，不要写文件名。
- 如果存在收藏穿搭偏好，只影响 outfit_style、reference_query、outfit 和 prompt 里的发型/服装部分：参考服装气质、配色、版型、材质和搭配层次，生成相近但新的组合。
- 如果存在不喜欢穿搭反馈，必须避开高度相似的核心单品组合、配色、材质、版型、发型和配饰；只换风格名、颜色近义词、单品同义词或一件小配饰仍算相似，必须重新设计整套搭配。
- 不要机械禁用某个大类风格；同风格只有在核心服装、鞋履以及配色/材质/版型明显不同时才可以再次使用。
- 不要照抄收藏里的完整发型短语、单品组合或旧描述；不要参考、复用或联想收藏里的日程、动作、场景。schedule、schedule_prompt、动作、场景必须根据今日信息重新决定。

⚠️ outfit 字段必须包含以下五个部分，缺一不可：
1. 「风格：」+ 风格名（只能从 [{style_list_text}] 中选，不要使用未启用的风格）
2. 「发型：」+ 具体发型描述（15-40 个汉字）。必须自由创造，不要只从固定池子里挑；可以组合扎法/分缝/发饰/松紧状态（如：侧边麻花辫收低马尾、碎发夹发卡的半扎发、蝴蝶结高马尾、低盘发配珍珠夹等）。同一天不同时段可微调整理状态，但不要总是双马尾。不要披头散发
3. 「穿搭：」+ 详细穿搭描述（至少 70 个汉字），必须同时写清：上装、下装或裙装、鞋子、发饰/首饰等配饰、主色、材质、版型/廓形、一个细节亮点。不要只写“少女风造型”“精心搭配”等空泛词。
4. 「动作：」+ 当前的姿态/场景动作（20-40 个汉字，由你根据今天设定自主决定；示例只作格式参考，不要照抄：托腮趴在桌上、踮脚够书架上的书、蹲下系鞋带、靠在窗边喝咖啡等）
5. 「场景：」+ 当前中文场景描述（15-40 个汉字，由你根据今天设定自主决定；示例只作格式参考，不要照抄：晨光照进来的卧室窗边、安静咖啡馆的靠窗小桌、暖色路灯下的街角等）

⚠️ prompt 字段必须是纯英文，适合 AI 生图，必须包含：发型、服装细节、动作/姿势、场景、光影氛围

⚠️ 生成 schedule 时先参考近 3 天完整日程动作，尽量避开相同或同义动作/任务主线，主动提高今天的多样性。\n⚠️ schedule 是 WebUI 展示用，必须用中文，必须有 6-8 条，严格使用 \\n 分隔，每行一条，格式为「HH:mm 中文活动描述」：
   下面示例只展示格式，不要照抄活动内容：
   "08:12 起床整理今天的温柔穿搭\\n10:27 坐在咖啡馆窗边写手账\\n12:43 吃一份清爽午餐\\n14:18 在画室整理灵感草图\\n16:36 去公园散步拍照\\n20:17 回家做一顿简单晚餐\\n22:11 准备晚间直播\\n00:42 做睡前护肤准备休息"
   不要用"早上9点"、"下午2点"等中文时间格式，必须用 HH:mm 数字格式！每行之间必须用 \\n 换行，不要用空格或句号分隔！
   时间必须自然错开整点：分钟不能是 00，不要卡在 HH:00 这类整点；请用 08:12、10:27、12:43、15:42、20:17、22:11 这类上下浮动的分钟。
   不要安排 03:00-05:59 的日程生图时间；这个时段只用于系统生成全天计划，不出现在 schedule、schedule_prompt 或 schedule_details。
   每条活动描述必须用中文写，要具体到场景/动作/道具（12-30 个汉字），不要只写"做早餐""出门""休息"等短句。
   每个时段的动作、道具和场景都由你根据今日人设、心情色彩、日程类型和穿搭自主决定；后续生图会直接采用这些日程动作，不会再用代码模板补动作。
   中文 schedule 可以写约会/见面/互动；你自己判断哪些内容只留在故事里，哪些写成适合单人出镜的生图描述。

⚠️ schedule_prompt 是生图 prompt 注入用，必须用纯英文，条数和时间必须与 schedule 一一对应：
   下面示例只展示格式，不要照抄活动内容：
   "08:12 wake up and arrange today's soft outfit\\n10:27 write diary at a window table in a cafe\\n12:43 have a light refreshing lunch\\n14:18 organize inspiration sketches in an art studio\\n16:36 take a walk and photos in the park\\n20:17 cook a simple dinner at home\\n22:11 prepare for an evening livestream\\n00:42 do skincare and get ready for bedtime"
   schedule 给用户看中文；schedule_prompt 只给生图 prompt 使用英文。
   schedule_prompt 的每条英文活动必须明确 action + scene + props/time mood，不能只写 vague daily routine。
   若中文日程涉及另一人，schedule_prompt 请自行改写成角色单人可见画面，保留氛围与道具，不把第二人画进镜头。

⚠️ schedule_details 是生图链路的严格结构化明细，必须是数组，条数、顺序、time 必须与 schedule 和 schedule_prompt 完全一致：
   - 每个时间段都必须输出一个对象，不能遗漏任何一条 schedule。
   - 每个对象必须包含 time、activity_zh、activity_en、action_en、scene_en、outfit_en、hair_en，可选 props_en、lighting_en。
   - activity_zh 必须是中文，并和 schedule 当前行表达同一个活动。
   - activity_en、action_en、scene_en、outfit_en、hair_en、props_en、lighting_en 必须是纯英文。
   - action_en 必须写清角色在镜头中的身体/手部动作和道具；你自己判断如何在保留日程语义的同时只拍角色。
   - scene_en 必须写清具体地点、周围环境、关键道具和时间氛围。
   - scene_en 可保留社交/约会氛围，但由你判断避免第二人成为清晰主体。
   - time 是强约束：06:00-11:59 必须是 morning/daylight 氛围；12:00-13:59 必须是 midday daylight 氛围；14:00-18:59 必须是 afternoon daylight 或 early-evening dusk 氛围；19:00-01:59 才可写 evening/night 氛围；不能因为“街区/打卡/散步”等白天活动就写成 night/evening/sunset/neon/street lamps。
   - outfit_en 必须写清上装、下装/裙装、鞋子、配饰、颜色、材质/版型；如果当天整套穿搭不变，也要在每个时间段重复写清。
   - hair_en 必须写清具体发型、发饰/整理状态；不要只写 "nice hair"、"beautiful hairstyle" 等空话。
   - hair_en 必须自由发明，不要只从固定发型池挑选；要具体到扎法/分缝/发饰/松紧状态，并贴合当前时段活动。
   - 一天内发型可以有小变化（更利落/更松散/换发饰），但不要整天重复同一句发型模板，尤其不要默认 twin tails。
   - hair_en 不负责决定发色；发色必须跟【角色外貌提示词】里的 appearance 走，不要因为穿搭风格把发色改成黑色、棕色、金色等。
   - 后续生图会严格采用对应 time 的 schedule_details，不再用代码随机补动作、场景、服饰或发型。

⚠️ schedule 必须覆盖以下四个时间段，每个时间段至少 1 条：
   - 早：06:00-11:59
   - 中：12:00-13:59
   - 午：14:00-18:59
   - 晚：19:00-01:59（可跨到次日凌晨，如 20:17、22:11、00:42）
   “晚”段必须落在 19:00-01:59，不能用 14:00-18:59 的午后条目顶替。
   即使只输出 6 条，也必须至少包含 1 条早、1 条中、1 条午、1 条晚；不要把所有安排都集中在上午和下午。
   schedule_prompt 的时间必须和 schedule 一一对应，也要覆盖同样的四个时间段。

⚠️ caption 是 WebUI「今日穿搭方案」里的“小心思”，不是单张照片配文。
   它必须写成「{character_name}」在心里自然冒出来的全天计划小念头：
   - 像刚醒来或出门前在心里嘀咕“今天想怎么过”，轻轻带到 schedule 里的 2-4 个安排。
   - 不要用“心里把今天的节奏排了一遍”“上午先/午后留给/晚上再收尾”这类总结式模板。
   - 少用时段标签和清单感，句子要像自然想法，而不是系统概括日程。
   - 可以有一点自然期待和情绪，但要口语、具体，像脑子里的真实小念头。
   - 不要写文艺腔、散文腔、景物隐喻；不要写“水珠、叶尖、心情被擦亮、像被阳光揉、温柔照顾、书签、光落下来”等表达。
   - 不要写成自拍/画面/穿搭点评。
   - 禁止写主人互动、调情、亲一口、抱抱、被夸、等人来找、穿得好不好看等内容。
   - 禁止出现“画廊、拍照、美照、造型、穿搭很美、今天穿得”等记录或外观评价话术。
   - 输出 1-2 句中文，总长 40-90 个汉字；不要加标题、引号或 emoji。

⚠️ photo_style_en 字段：根据今天整体日程与氛围，自行判断今天最合适的摄影/镜头语言，用 1-3 句纯英文写。
   只写摄影语言（镜头、光线、取景、色彩真实感、质感），不要写人物外貌/发型/服装。
   不要固定套用同一套前缀；不要写 Masterpiece、ultra-realistic、cinematic lighting、HDR glow。
⚠️ outfit_keywords 字段：从 prompt 中提取穿搭相关英文关键词（服装+鞋子+配饰），逗号分隔，5-10个词。必须和 prompt 中的穿搭描述完全一致。
⚠️ scene_keywords 字段：从 prompt 中提取场景相关英文关键词（环境+道具+光线），逗号分隔，3-6个词。必须和 prompt 中的场景描述完全一致。

JSON 格式（字段名固定，value 替换为实际内容）：
{{
    "outfit_style": "风格名",
    "reference_query": "适合今天生图参考图的自然语言描述，包含气质、发型/脸部氛围、服装色系、场景 mood",
    "outfit": "风格：xxx \\n发型：xxx \\n穿搭：xxx \\n动作：xxx \\n场景：xxx",
    "schedule": "HH:mm 中文活动描述\\nHH:mm 中文活动描述\\n...",
    "schedule_prompt": "HH:mm English activity\\nHH:mm English activity\\n...",
    "schedule_details": [
        {{
            "time": "HH:mm",
            "activity_zh": "中文活动描述",
            "activity_en": "English activity",
            "action_en": "specific body and hand action in English",
            "scene_en": "specific location, surroundings, props, and time mood in English",
            "outfit_en": "specific outfit, shoes, accessories, colors, materials, silhouette in English",
            "hair_en": "specific hairstyle and hair accessory/status in English, without changing the character hair color",
            "props_en": "optional relevant props in English",
            "lighting_en": "optional lighting and ambience in English"
        }}
    ],
    "prompt": "English prompt with hairstyle, outfit details, pose, scene, lighting...",
    "caption": "{character_name}自然想着今天想怎么过的小心思。",
    "photo_style_en": "1-3 English sentences of photography language chosen for today's overall schedule mood",
    "outfit_keywords": "JK uniform, pleated skirt, white blouse, red ribbon, loafers",
    "scene_keywords": "coffee shop, cafe counter, warm ambient light"
}}"""

    def _build_compact_schedule_prompt(
        self,
        today: date,
        history: str,
        schedule_history: str,
        disliked_context: Optional[str] = None,
    ) -> str:
        """Build a shorter schedule prompt for providers that choke on the full context."""
        weekday = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][today.weekday()]
        day_context = self._day_context(today)
        enabled_styles = load_enabled_outfit_styles(self.config, self.data_dir)
        style_list_text = ", ".join(enabled_styles)
        mood = random.choice(MOOD_COLORS)
        sched_type = self._select_schedule_type(day_context)
        calendar_guidance = day_context.prompt_block(sched_type)
        persona = self._runtime_persona()
        character_name = persona.get("name") or "角色"
        user_name = persona.get("user_name") or "用户"
        caption_voice = persona.get("caption_voice") or "自然、亲切、贴近日常。"
        appearance = persona.get("appearance") or self._char.get("appearance", "")
        if not appearance:
            appearance = self._read_config_key("character_appearance")
        disliked_outfits = (
            self._disliked_outfit_context(limit=12)
            if disliked_context is None
            else disliked_context
        )

        return f"""{JSON_OUTPUT_CONTRACT}

为「{character_name}」生成 {today.isoformat()}（{weekday}）的每日穿搭和日程。
用户称呼：{user_name}
可选穿搭风格：{style_list_text}
心情色彩：{mood}
日程类型：{sched_type}
真实日历：
{calendar_guidance}
角色外貌：{str(appearance)[:700]}
生图安全：{SCHEDULE_IMAGE_SAFETY_RULES}
小心思口吻：{str(caption_voice)[:220]}
近 3 天穿搭参考（服装、发型、鞋包和首饰尽量生成不同组合）：{str(history)[:1200]}
具体配饰优先换颜色、图案或类别；不要只靠同义改写制造差异，例如银色十字星锁骨链与银色星形项链仍是相近搭配。
{self._schedule_diversity_prompt_block(str(schedule_history)[:1800])}
历史生图词云（低权重软参考）：{self._schedule_keyword_cloud_prompt_block(limit=2, selection_key=today.isoformat())[:700]}
收藏偏好（只参考穿搭/发型气质）：{self._favorite_outfit_context(limit=2)[:700]}
禁止复现的不喜欢穿搭（硬约束）：{disliked_outfits[:1800]}
日程禁词（最高优先级，相关活动/道具/场景/配饰都不要生成）：{self._schedule_forbidden_prompt_block()}

{SCHEDULE_SOLO_CAMERA_RULES}

{SCHEDULE_PHOTO_STYLE_RULES}

硬性要求：
0. 严格服从真实日历；休息日/节假日禁止写上班、上学、通勤、办公室会议、考试、作业或加班，调休上班日除外。
0.1. 多样性目标：参考近 3 天日程，尽量避开相同或同义的动作与任务主线，优先增加动作族、场景和精彩锚点；不要只换说法、时间、地点、店铺或物品来制造表面差异。
0.2. 不得生成与不喜欢记录高度相似的核心单品组合；不能靠改风格名或同义改写绕过。同风格换成明显不同的服装、鞋履、配色/材质/版型可以使用。
1. outfit_style 必须从可选穿搭风格中选一个。
2. outfit 必须是中文，包含「风格：」「发型：」「穿搭：」「动作：」「场景：」五段；穿搭写清上装、下装/裙装、鞋子、配饰、颜色、材质/版型。
3. schedule 必须是 6-8 行中文，每行「HH:mm 中文活动」，用 \\n 分隔，覆盖 06:00-11:59、12:00-13:59、14:00-18:59、19:00-01:59；分钟不能是 00，不要安排 03:00-05:59，时间要像 08:12、10:27、12:43、15:42、20:17、22:11 这样自然浮动。
4. schedule_prompt 必须与 schedule 时间逐条一致，纯英文，每条写清 action + scene + props/time mood。
5. schedule_details 必须是数组，条数和 schedule 一样；每项必须包含 time、activity_zh、activity_en、action_en、scene_en、outfit_en、hair_en，可选 props_en、lighting_en。除 activity_zh 外都用纯英文。
6. 白天时间不能写 night/evening/sunset/neon/street lamps；发色必须跟角色外貌，不要由风格改发色。
7. prompt 必须是纯英文生图提示词，包含发型、穿搭、动作、场景、光影。
8. caption 是今日计划的小心思，中文 40-90 字，口语自然，轻轻带到 2-4 个安排；不要文艺隐喻，不要自拍/美照/外貌点评。
9. photo_style_en 必须是纯英文 1-3 句，由你根据今天日程整体氛围自行判断摄影/镜头语言；只写镜头光线取景质感，不要写外貌服装，不要 Masterpiece/cinematic lighting/HDR。

只输出这个 JSON 对象：
{{
  "outfit_style": "风格名",
  "reference_query": "中文自然语言参考图选择描述",
  "outfit": "风格：...\\n发型：...\\n穿搭：...\\n动作：...\\n场景：...",
  "schedule": "HH:mm 中文活动\\nHH:mm 中文活动",
  "schedule_prompt": "HH:mm English activity\\nHH:mm English activity",
  "schedule_details": [
    {{
      "time": "HH:mm",
      "activity_zh": "中文活动",
      "activity_en": "English activity",
      "action_en": "specific body and hand action",
      "scene_en": "specific place, surroundings, props, and time mood",
      "outfit_en": "specific outfit, shoes, accessories, colors, materials, silhouette",
      "hair_en": "specific hairstyle and hair accessory/status",
      "props_en": "optional props",
      "lighting_en": "optional lighting"
    }}
  ],
  "prompt": "English image prompt",
  "caption": "{character_name}今天自然冒出来的小心思。",
  "photo_style_en": "English photography language for today's overall schedule mood",
  "outfit_keywords": "English outfit keywords",
  "scene_keywords": "English scene keywords"
}}"""

    def _build_emergency_schedule_prompt(
        self,
        today: date,
        schedule_history: str = "",
        outfit_history: str = "",
        disliked_context: Optional[str] = None,
    ) -> str:
        """Smallest strict prompt used when an upstream model times out on rich context."""
        weekday = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][today.weekday()]
        day_context = self._day_context(today)
        enabled_styles = load_enabled_outfit_styles(self.config, self.data_dir)
        sched_type = self._select_schedule_type(day_context)
        calendar_guidance = day_context.prompt_block(sched_type)
        persona = self._runtime_persona()
        character_name = persona.get("name") or "角色"
        appearance = persona.get("appearance") or self._char.get("appearance", "")
        if not appearance:
            appearance = self._read_config_key("character_appearance")
        disliked_outfits = (
            self._disliked_outfit_context(limit=12)
            if disliked_context is None
            else disliked_context
        )

        return f"""{JSON_OUTPUT_CONTRACT}

生成 {character_name} 的今日 JSON 日程。日期 {today.isoformat()} {weekday}。
可选风格：{", ".join(enabled_styles)}
真实日历：
{calendar_guidance}
外貌约束：{str(appearance)[:320]}
生图安全：{SCHEDULE_IMAGE_SAFETY_RULES}
禁词约束：{self._schedule_forbidden_prompt_block()}
近 3 天穿搭多样性参考：{str(outfit_history)[:800]}
配饰优先选择与近 3 天不同的颜色、图案或类别，不要只做同义改写。
禁止复现的不喜欢穿搭：{disliked_outfits[:1200]}
{self._schedule_diversity_prompt_block(str(schedule_history)[:1200])}
词云低权重软参考：{self._schedule_keyword_cloud_prompt_block(limit=1, selection_key=today.isoformat())[:500]}

{SCHEDULE_SOLO_CAMERA_RULES}

{SCHEDULE_PHOTO_STYLE_RULES}

只输出 minified JSON，不要换成数组，不要代码块。
要求：
- 严格服从真实日历；休息日/节假日禁止写上班、上学、通勤、办公室会议、考试、作业或加班，调休上班日除外。
- 多样性目标：参考近 3 天日程，尽量避开相同/同义动作与任务主线；优先覆盖不同动作族和场景，并设计一个需要真实参与的精彩锚点。
- 不得生成与不喜欢记录高度相似的核心单品组合；只改风格名或同义说法仍算重复。同风格的核心服装、鞋履、配色/材质/版型明显不同时可以使用。
- outfit_style 从可选风格中选。
- schedule 固定 6 行，时间用 08:12、10:27、12:43、15:42、20:17、22:11，每行中文活动，覆盖早/中/午/晚；不要用整点或 03:00-05:59。
- schedule_prompt 与 schedule 时间一致，纯英文。
- schedule_details 6 个对象，time 与 schedule 一致；必须含 activity_zh、activity_en、action_en、scene_en、outfit_en、hair_en；英文项不能有中文。
- outfit 中文含 风格/发型/穿搭/动作/场景。
- prompt 纯英文，写清发型、穿搭、动作、场景、光线。
- photo_style_en 纯英文 1-3 句，按今天日程氛围判断摄影语言（镜头/光线/取景/质感），不要写外貌服装，不要 Masterpiece/cinematic lighting。
- caption 中文 40-90 字，是今天计划的小心思。
- 白天不要写 night/evening/sunset/neon/street lamps；发色跟外貌约束。

JSON keys:
outfit_style, reference_query, outfit, schedule, schedule_prompt, schedule_details, prompt, caption, photo_style_en, outfit_keywords, scene_keywords."""

    def _extract_outfit_keywords(self, prompt: str) -> str:
        """从英文 prompt 中提取穿搭关键词（fallback）"""
        import re
        # 提取 "She is wearing ..." 部分
        m = re.search(r'She is wearing (.+?)\.?\s*(?:Background|She is|Her hair|$)', prompt)
        if m:
            return m.group(1).strip().rstrip('.')
        # fallback: 提取常见服装词
        outfit_words = re.findall(
            r'\b(?:dress|skirt|blouse|top|jeans|shorts|hoodie|cardigan|jacket|coat|'
            r'pants|trousers|sweater|t-shirt|crop|camisole|slip|robe|pajama|'
            r'bikini|swimsuit|lingerie|stockings|heels|sneakers|boots|sandals|loafers|'
            r'ribbon|necklace|earrings|bracelet|scrunchie|choker)\w*\b',
            prompt, re.IGNORECASE
        )
        return ', '.join(dict.fromkeys(outfit_words)) if outfit_words else ''

    def _extract_scene_keywords(self, prompt: str) -> str:
        """从英文 prompt 中提取场景关键词（fallback）"""
        import re
        # 提取 "Background: ..." 部分
        m = re.search(r'Background:\s*(.+?)\.?\s*(?:$)', prompt)
        if m:
            return m.group(1).strip().rstrip('.')
        # fallback: 提取常见场景词
        scene_words = re.findall(
            r'\b(?:bedroom|bathroom|kitchen|cafe|coffee|shop|park|street|rooftop|'
            r'balcony|window|mirror|desk|sofa|couch|beach|pool|garden|studio|'
            r'office|restaurant|bar|club|library|bookstore|mall|market)\w*\b',
            prompt, re.IGNORECASE
        )
        return ', '.join(dict.fromkeys(scene_words)) if scene_words else ''

    @staticmethod
    def _contains_cjk(value: str) -> bool:
        return bool(re.search(r'[\u4e00-\u9fff]', value or ""))

    @staticmethod
    def _normalize_base_style(value: str) -> str:
        text = (value or "").strip().lower()
        for option in BASE_STYLE_OPTIONS:
            if re.search(fr'\b{option}\b', text):
                return option
        return ""

    def _valid_display_outfit(self, outfit: str) -> bool:
        if not self._contains_cjk(outfit):
            return False
        required = ("风格", "发型", "穿搭", "动作", "场景")
        return all(re.search(fr'{name}[：:]\s*[\u4e00-\u9fff]', outfit or "") for name in required)

    @staticmethod
    def _text_field(value) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return ", ".join(str(item).strip() for item in value if str(item).strip())
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False)
        return str(value).strip()

    @classmethod
    def _schedule_text_field(cls, value, english: bool = False) -> str:
        if isinstance(value, str):
            return value.strip()
        if not isinstance(value, list):
            return cls._text_field(value)
        lines = []
        activity_keys = (
            ("activity_en", "activity", "text", "description")
            if english
            else ("activity_zh", "activity", "text", "description")
        )
        for item in value:
            if isinstance(item, dict):
                time_text = cls._text_field(item.get("time"))
                activity = ""
                for key in activity_keys:
                    activity = cls._text_field(item.get(key))
                    if activity:
                        break
                line = f"{time_text} {activity}".strip()
            else:
                line = cls._text_field(item)
            if line:
                lines.append(line)
        return "\n".join(lines)

    @classmethod
    def _outfit_text_field(cls, value, fallback_style: str = "") -> str:
        if isinstance(value, str):
            text = value.strip()
            if all(marker in text for marker in ("风格", "发型", "穿搭", "动作", "场景")):
                return text
            chunks = [part.strip() for part in re.split(r"[|｜]", text) if part.strip()]
            style = fallback_style
            hair = ""
            clothing = text
            action = ""
            scene = ""
            if len(chunks) >= 4:
                style = chunks[0] or fallback_style
                hair = chunks[1]
                clothing = chunks[2]
                action = chunks[3]
                scene = chunks[3]
            elif len(chunks) >= 3:
                style = chunks[0] or fallback_style
                hair_and_clothing = chunks[1]
                match = re.match(r"([^，,；;。]+)[，,；;。]\s*(.+)", hair_and_clothing)
                if match:
                    hair = match.group(1).strip()
                    clothing = match.group(2).strip()
                else:
                    clothing = hair_and_clothing
                action = chunks[2]
                scene = chunks[2]
            elif len(chunks) == 2:
                style = chunks[0] or fallback_style
                clothing = chunks[1]
            if not hair:
                hair = "按角色外貌整理的自然发型"
            if not action:
                action = "按照今日日程自然活动"
            if not scene:
                scene = "贴近日常安排的生活场景"
            parts = []
            if style:
                parts.append(f"风格：{style}")
            parts.extend([
                f"发型：{hair}",
                f"穿搭：{clothing}",
                f"动作：{action}",
                f"场景：{scene}",
            ])
            return "\n".join(parts)
        if not isinstance(value, dict):
            return cls._text_field(value)

        def first(*keys) -> str:
            for key in keys:
                text = cls._text_field(value.get(key))
                if text:
                    return text
            return ""

        style = first("风格", "style", "outfit_style") or fallback_style
        hair = first("发型", "hair", "hairstyle", "hair_style")
        clothing = first("穿搭", "outfit", "clothing", "clothes", "look")
        action = first("动作", "action", "pose")
        scene = first("场景", "scene", "setting")
        parts = []
        if style:
            parts.append(f"风格：{style}")
        if hair:
            parts.append(f"发型：{hair}")
        if clothing:
            parts.append(f"穿搭：{clothing}")
        if action:
            parts.append(f"动作：{action}")
        if scene:
            parts.append(f"场景：{scene}")
        return "\n".join(parts)

    @staticmethod
    def _normalize_hhmm(value: str) -> str:
        match = re.match(r'\s*(\d{1,2}):(\d{2})\s*$', str(value or ""))
        if not match:
            return ""
        hour = int(match.group(1))
        minute = int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return ""
        return f"{hour:02d}:{minute:02d}"

    @staticmethod
    def _time_to_minutes(value: str) -> Optional[int]:
        match = re.match(r'\s*(\d{1,2}):(\d{2})', value or "")
        if not match:
            return None
        hour = int(match.group(1))
        minute = int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour * 60 + minute

    def _required_periods(self) -> list[dict]:
        raw_periods = self.config.get("schedule", {}).get("required_periods", DEFAULT_REQUIRED_PERIODS)
        periods = []
        for item in raw_periods:
            if not isinstance(item, dict):
                continue
            start = self._time_to_minutes(str(item.get("start", "")))
            end = self._time_to_minutes(str(item.get("end", "")))
            label = str(item.get("label") or item.get("name") or "").strip()
            if start is None or end is None or not label:
                continue
            periods.append({"label": label, "start": start, "end": end})
        if periods:
            return periods
        return [
            {
                "label": item["label"],
                "start": self._time_to_minutes(item["start"]),
                "end": self._time_to_minutes(item["end"]),
            }
            for item in DEFAULT_REQUIRED_PERIODS
        ]

    def _schedule_minutes(self, schedule: str) -> list[int]:
        minutes = []
        for match in re.finditer(r'(?m)^\s*(\d{1,2}):(\d{2})\s+.+', schedule or ""):
            minute = self._time_to_minutes(f"{match.group(1)}:{match.group(2)}")
            if minute is not None:
                minutes.append(minute)
        return minutes

    def _missing_required_periods(self, schedule: str) -> list[str]:
        minutes = self._schedule_minutes(schedule)
        missing = []
        for period in self._required_periods():
            start = period["start"]
            end = period["end"]
            if start <= end:
                has_item = any(start <= minute <= end for minute in minutes)
            else:
                has_item = any(minute >= start or minute <= end for minute in minutes)
            if not has_item:
                missing.append(period["label"])
        return missing

    @staticmethod
    def _schedule_plan_items(schedule: str) -> list[tuple[str, str]]:
        items = []
        for line in str(schedule or "").splitlines():
            match = re.match(r'\s*(\d{1,2}):(\d{2})\s+(.+)', line)
            if not match:
                continue
            hour = int(match.group(1))
            minute = int(match.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                items.append((f"{hour:02d}:{minute:02d}", match.group(3).strip()))
        return items

    def _validate_schedule_alignment(self, schedule: str, schedule_prompt: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]], str]:
        display_items = self._schedule_plan_items(schedule)
        prompt_items = self._schedule_plan_items(schedule_prompt)
        if not (6 <= len(display_items) <= 8):
            return display_items, prompt_items, f"schedule 条数必须 6-8 条，实际 {len(display_items)}"
        if len(display_items) != len(prompt_items):
            return display_items, prompt_items, (
                f"schedule_prompt 条数必须和 schedule 一致: "
                f"display={len(display_items)}, prompt={len(prompt_items)}"
            )
        display_times = [item[0] for item in display_items]
        prompt_times = [item[0] for item in prompt_items]
        if display_times != prompt_times:
            return display_items, prompt_items, f"schedule 和 schedule_prompt 时间不一致: {display_times} != {prompt_times}"
        exact_hour_times = []
        quiet_times = []
        for time_text in display_times:
            minute = self._time_to_minutes(time_text)
            if minute is None:
                continue
            if minute % 60 == 0:
                exact_hour_times.append(time_text)
            if SCHEDULE_PHOTO_QUIET_START_MINUTE <= minute < SCHEDULE_PHOTO_QUIET_END_MINUTE:
                quiet_times.append(time_text)
        if exact_hour_times:
            return display_items, prompt_items, f"schedule 时间不要卡整点，分钟不能是 00: {exact_hour_times}"
        if quiet_times:
            return display_items, prompt_items, f"03:00-06:00 是日程生成时段，不安排生图日程: {quiet_times}"
        if self._contains_cjk(schedule_prompt):
            return display_items, prompt_items, "schedule_prompt 必须是纯英文，不能包含中文"
        for idx, (_time_text, activity) in enumerate(display_items, start=1):
            if not self._contains_cjk(activity):
                return display_items, prompt_items, f"schedule 第 {idx} 条活动必须是中文"
        for idx, (_time_text, activity) in enumerate(prompt_items, start=1):
            if not activity or self._contains_cjk(activity):
                return display_items, prompt_items, f"schedule_prompt 第 {idx} 条活动必须是纯英文"
        return display_items, prompt_items, ""

    def _normalize_schedule_details(
        self,
        raw_details,
        display_items: list[tuple[str, str]],
        prompt_items: list[tuple[str, str]],
    ) -> tuple[list[dict], str]:
        if not isinstance(raw_details, list):
            return [], "schedule_details 必须是数组"
        if len(raw_details) != len(display_items):
            return [], f"schedule_details 条数必须和 schedule 一致: details={len(raw_details)}, schedule={len(display_items)}"

        prompt_activity_by_time = {time_text: activity for time_text, activity in prompt_items}
        normalized = []
        for idx, (expected_time, _display_activity) in enumerate(display_items):
            item = raw_details[idx]
            if not isinstance(item, dict):
                return [], f"schedule_details 第 {idx + 1} 条必须是对象"

            actual_time = self._normalize_hhmm(item.get("time", ""))
            if actual_time != expected_time:
                return [], f"schedule_details 第 {idx + 1} 条时间必须是 {expected_time}，实际 {item.get('time', '')}"

            detail = {"time": expected_time}
            for field in SCHEDULE_DETAIL_REQUIRED_FIELDS:
                if field == "time":
                    continue
                value = re.sub(r"\s+", " ", str(item.get(field, ""))).strip()
                if not value:
                    return [], f"schedule_details 第 {idx + 1} 条缺少 {field}"
                detail[field] = value

            if not self._contains_cjk(detail["activity_zh"]):
                return [], f"schedule_details 第 {idx + 1} 条 activity_zh 必须是中文"

            english_fields = ("activity_en", "action_en", "scene_en", "outfit_en", "hair_en")
            for field in english_fields:
                if self._contains_cjk(detail[field]):
                    return [], f"schedule_details 第 {idx + 1} 条 {field} 必须是纯英文"

            for optional_field in ("props_en", "lighting_en"):
                value = re.sub(r"\s+", " ", str(item.get(optional_field, ""))).strip()
                if value:
                    if self._contains_cjk(value):
                        return [], f"schedule_details 第 {idx + 1} 条 {optional_field} 必须是纯英文"
                    detail[optional_field] = value

            time_conflict = self._schedule_detail_time_conflict(expected_time, detail)
            if time_conflict:
                return [], f"schedule_details 第 {idx + 1} 条时间氛围冲突: {time_conflict}"

            if prompt_activity_by_time.get(expected_time) and not detail.get("activity_en"):
                return [], f"schedule_details 第 {idx + 1} 条 activity_en 不能为空"

            normalized.append(detail)

        return normalized, ""

    @staticmethod
    def _schedule_detail_time_conflict(time_text: str, detail: dict) -> str:
        try:
            hour = int(str(time_text).split(":", 1)[0])
        except (TypeError, ValueError):
            return ""
        text = " ".join(str(detail.get(field, "")) for field in ("activity_en", "action_en", "scene_en", "props_en", "lighting_en")).lower()
        if not text:
            return ""
        if 6 <= hour < 19:
            conflict_terms = (
                " at night",
                "nighttime",
                "night life",
                "nightlife",
                " in the evening",
                "during the evening",
                "after dark",
                " at dusk",
                " at sunset",
                "neon-lit",
                "neon light",
                "street lamp",
                "streetlight",
            )
            for term in conflict_terms:
                if term in text:
                    return f"{time_text} 是白天时段，但明细包含 {term.strip()}"
        return ""

    @staticmethod
    def _caption_activity_label(activity: str, limit: int = 18) -> str:
        text = re.sub(r"\s+", "", str(activity or ""))
        text = re.sub(r"(?:，|,).*$", "", text)
        replacements = (
            ("给自己做一份", "做份"),
            ("一份", ""),
            ("水果松饼早餐", "水果松饼"),
            ("窝在沙发上看动漫新番", "窝着看会儿新番"),
            ("在阳台的摇椅上小憩打盹", "去阳台眯一小会儿"),
            ("整理房间，顺便给多肉植物浇水", "收拾下房间，给多肉浇浇水"),
            ("调一杯冰柠薄荷水", "给自己调杯冰柠薄荷水"),
            ("坐在窗边发呆看夕阳", "坐窗边看看夕阳"),
            ("打开直播和主人聊天互动，对着镜头撒娇", "开个直播聊聊天"),
            ("泡个香香的热水澡，涂上身体乳准备休息", "泡个热水澡再慢慢休息"),
        )
        for old, new in replacements:
            text = text.replace(old, new)
        text = text.replace("主人", "").replace("对着镜头撒娇", "开播互动")
        text = text.strip("，,。.!！?；;、")
        if len(text) > limit:
            return text[:limit].rstrip("，,。.!！?；;、") + "…"
        return text

    def _build_schedule_plan_caption(self, schedule: str, character_name: str = "") -> str:
        items = self._schedule_plan_items(schedule)
        if not items:
            name = character_name or "她"
            return f"{name}今天先按手边的事来，别把安排都拖到晚上，累了就给自己留点休息时间。"

        buckets = {"上午": [], "午后": [], "晚上": []}
        for time_text, activity in items:
            hour = int(time_text.split(":", 1)[0])
            label = self._caption_activity_label(activity)
            if not label:
                continue
            if hour < 12:
                buckets["上午"].append(label)
            elif hour < 19:
                buckets["午后"].append(label)
            else:
                buckets["晚上"].append(label)

        morning = buckets["上午"][0] if buckets["上午"] else ""
        noon = buckets["午后"][:2]
        evening = buckets["晚上"][0] if buckets["晚上"] else ""
        parts = []
        if morning:
            parts.append("早上" + morning)
        if noon:
            parts.append("午后" + "，再".join(noon))
        if evening:
            parts.append("晚上" + evening)
        if not parts:
            parts = [self._caption_activity_label(items[0][1], 24)]

        caption = "今天先按这个节奏来：" + "，".join(parts) + "，别把事情都拖到最后。"
        return caption[:90].rstrip("，,。.!！?；;、") + "。"

    @staticmethod
    def _caption_is_schedule_plan(caption: str) -> bool:
        text = re.sub(r"\s+", "", str(caption or ""))
        if not text:
            return False
        bad_markers = (
            "主人", "亲一口", "抱抱", "怀里", "来找我玩", "被夸",
            "美照", "自拍", "拍照", "照片", "画面", "造型", "画廊",
            "记录", "收藏", "穿得这么", "好看", "性感",
            "水珠", "叶尖", "擦亮", "像被阳光揉", "温柔照顾", "书签", "光落下来",
        )
        if any(marker in text for marker in bad_markers):
            return False
        intent_markers = ("想过", "想怎么过", "打算", "准备", "安排", "计划", "节奏", "先", "再", "然后")
        time_markers = ("一整天", "早上", "上午", "午后", "下午", "晚上")
        return any(marker in text for marker in intent_markers) and any(marker in text for marker in time_markers)

    def _get_history(self, today: date, days: int = 3) -> str:
        """获取近几天的完整穿搭历史，保留鞋包和配饰信息。"""
        all_data = self._load_schedule_data()
        items = []
        for i in range(1, days + 1):
            d = today - timedelta(days=i)
            date_str = d.isoformat()
            entry = self._daily_schedule_entry(all_data, date_str)
            if not entry:
                continue
            outfit = re.sub(r"\s+", " ", str(entry.get("outfit") or "")).strip()
            accessories = "、".join(self._accessory_features(self._entry_outfit_text(entry)).values())
            line = f"[{date_str}] 风格：{entry.get('outfit_style', '')}；穿搭：{outfit[:520]}"
            if accessories:
                line += f"；配饰特征：{accessories}"
            items.append(line)
        return "\n".join(items) if items else "（无历史记录）"

    def _favorite_outfit_context(self, limit: int = 5) -> str:
        """读取用户收藏的穿搭方案，作为 LLM 的偏好参考。"""
        path = os.path.join(self.data_dir, "favorite_outfits.json")
        if not os.path.exists(path):
            return "（无收藏穿搭偏好）"
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items", data) if isinstance(data, dict) else data
            if not isinstance(items, list):
                return "（无收藏穿搭偏好）"

            lines = []
            for item in sorted(
                [x for x in items if isinstance(x, dict)],
                key=lambda x: x.get("created_at", 0),
                reverse=True,
            )[:limit]:
                outfit = item.get("outfit") if isinstance(item.get("outfit"), dict) else {}
                parts = []
                for key in ("风格", "发型", "穿搭"):
                    value = str(outfit.get(key) or "").strip()
                    if value:
                        parts.append(f"{key}：{value[:140]}")
                if not parts:
                    continue
                meta = f"[{item.get('date', '')}] 风格：{item.get('outfit_style', '') or outfit.get('风格', '')}"
                lines.append(meta + "；" + "；".join(parts))
            return "\n".join(lines) if lines else "（无收藏穿搭偏好）"
        except Exception as e:
            logger.warning("读取收藏穿搭偏好失败: %s", e)
            return "（无收藏穿搭偏好）"

    def _load_disliked_outfits(self) -> list[dict]:
        path = os.path.join(self.data_dir, "disliked_outfits.json")
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items", data) if isinstance(data, dict) else data
            if not isinstance(items, list):
                return []
            return [item for item in items if isinstance(item, dict)]
        except Exception as e:
            logger.warning("读取不喜欢穿搭反馈失败: %s", e)
            return []

    def _disliked_outfit_context(
        self,
        limit: Optional[int] = 5,
        items: Optional[list[dict]] = None,
    ) -> str:
        """将用户不喜欢的穿搭整理为 LLM 的硬性负向参考。"""
        source_items = self._load_disliked_outfits() if items is None else items
        ordered = sorted(
            [item for item in source_items if isinstance(item, dict)],
            key=lambda item: item.get("created_at", 0),
            reverse=True,
        )
        if limit is not None:
            ordered = ordered[:max(0, limit)]

        lines = []
        for item in ordered:
            outfit = item.get("outfit") if isinstance(item.get("outfit"), dict) else {}
            parts = []
            for key in ("风格", "发型", "穿搭"):
                value = str(outfit.get(key) or "").strip()
                if value:
                    parts.append(f"{key}：{value[:180]}")
            keywords = str(item.get("outfit_keywords") or "").strip()
            if keywords:
                parts.append(f"英文单品：{keywords[:240]}")
            if not parts:
                continue
            meta = f"[{item.get('date', '')}] 风格：{item.get('outfit_style', '') or outfit.get('风格', '')}"
            lines.append(meta + "；" + "；".join(parts))
        return "\n".join(lines) if lines else "（无不喜欢穿搭反馈）"

    def _parse_llm_response(self, text: str) -> Optional[dict]:
        """从 LLM 回复中解析 JSON"""
        # 去掉可能的 markdown 代码块
        text = text.strip()
        text = text.replace("```json", "").replace("```", "").strip()

        decoder = json.JSONDecoder()
        first_dict = None
        for match in re.finditer(r"{", text):
            try:
                parsed, _end = decoder.raw_decode(text[match.start():])
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            if first_dict is None:
                first_dict = parsed
            if parsed.get("schedule") or parsed.get("outfit_style") or parsed.get("schedule_details"):
                return parsed

        if first_dict is not None:
            return first_dict

        if "{" not in text or "}" not in text:
            logger.error(f"No JSON found in LLM response: {text[:200]}")
            return None

        logger.error(f"JSON parse error: no valid object, text={text[:200]}")
        return None

    async def _repair_schedule_json(
        self,
        original_prompt: str,
        bad_text: str,
        attempt: int,
        image_path: str = "",
    ) -> Optional[dict]:
        """Ask the LLM once to regenerate strict JSON when it answered with prose."""
        bad_excerpt = llm_response_excerpt(bad_text, limit=1200)
        repair_prompt = f"""{JSON_OUTPUT_CONTRACT}

上一次回复无法被系统解析，因为它不是一个完整 JSON 对象。
请重新执行【原始任务】，不要复述任务，不要分析，不要解释，直接从 {{ 开始输出最终 JSON。

【错误回复片段】
{bad_excerpt}

【原始任务】
{original_prompt}
"""
        repair_kwargs = {"image_path": image_path} if image_path else {}
        repaired_text = await self._call_llm(
            repair_prompt,
            timeout=60,
            json_mode=True,
            temperature=0.1,
            **repair_kwargs,
        )
        if not repaired_text:
            logger.warning(f"JSON 修复请求返回为空 (attempt {attempt})")
            return None
        repaired_data = self._parse_llm_response(repaired_text)
        if not repaired_data:
            logger.warning(f"JSON 修复请求仍无法解析 (attempt {attempt})")
            return None
        logger.info(f"JSON 修复请求成功 (attempt {attempt})")
        return repaired_data

    @staticmethod
    def _season_label(today: date) -> str:
        if today.month in (3, 4, 5):
            return "春季"
        if today.month in (6, 7, 8):
            return "夏季"
        if today.month in (9, 10, 11):
            return "秋季"
        return "冬季"

    @staticmethod
    def _normalize_xiaohongshu_search_query(value: str) -> str:
        text = re.sub(r"\s+", "", str(value or "")).strip("`'\"“”‘’。，,；;：: ")
        text = re.sub(r"^(?:关键词|搜索词|keyword)[：:]?", "", text, flags=re.IGNORECASE)
        text = re.sub(r"(?:真人|全身|OOTD)+$", "", text, flags=re.IGNORECASE).strip()
        text = text[:24]
        if text and "穿搭" not in text:
            text += "穿搭"
        return text[:28]

    async def generate_xiaohongshu_search_query(self) -> str:
        """Choose one broad XHS keyword before any outfit or schedule is designed."""
        today = self._configured_today()
        day_context = self._day_context(today)
        enabled_styles = load_enabled_outfit_styles(self.config, self.data_dir)
        styles = "、".join(enabled_styles) or "自然日常风"
        history = self._get_history(today)
        prompt = f"""只输出一个 JSON 对象：{{"keyword":"搜索词"}}。
为 {today.isoformat()} 选择一个适合去小红书搜索真人穿搭照片的中文关键词。
季节：{self._season_label(today)}
日期属性：{day_context.day_type_label}
可选审美方向：{styles}
近三天穿搭（今天尽量换一种方向）：{history[:900]}

规则：
1. 此时还没有生成今日日程，也不要设计具体衣服；只选宽泛的季节、风格或场合方向。
2. 搜索词控制在 6-16 个汉字，必须包含“穿搭”，例如“夏季温柔居家穿搭”“夏日休闲通勤穿搭”。
3. 不写具体上衣、裙裤、鞋履、首饰、颜色清单，不写人物长相、身材、动作、摄影参数。
4. 只输出 JSON，不解释。"""
        text = await self._call_llm(
            prompt,
            timeout=60,
            json_mode=True,
            temperature=self._llm_config.get("schedule_temperature", 0.8),
        )
        data = self._parse_llm_response(text or "") if text else None
        keyword = self._normalize_xiaohongshu_search_query(
            data.get("keyword", "") if isinstance(data, dict) else ""
        )
        if keyword:
            logger.info("今日小红书穿搭搜索词已选择: %s", keyword)
            return keyword
        style = random.choice(enabled_styles) if enabled_styles else "自然日常风"
        fallback = self._normalize_xiaohongshu_search_query(
            f"{self._season_label(today)}{style}穿搭"
        )
        logger.warning("小红书搜索词 LLM 返回无效，使用兜底词: %s", fallback)
        return fallback

    @staticmethod
    def _xiaohongshu_validation_bool(value: object) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        normalized = str(value or "").strip().lower()
        if normalized in {"1", "true", "yes", "是", "符合"}:
            return True
        if normalized in {"0", "false", "no", "否", "不符合"}:
            return False
        return None

    async def select_xiaohongshu_outfit_image(
        self,
        contact_sheet_path: str,
        search_query: str = "",
        candidate_count: int = 1,
    ) -> dict:
        """Choose one numbered candidate only after strict vision validation."""
        candidate_count = max(1, min(4, int(candidate_count or 1)))
        prompt = f"""你是穿搭参考图质检员。附图是一张带 1-{candidate_count} 编号的候选联系表，每个编号格代表一张独立的小红书正文图片。
请在这些编号格中选择最适合直接作为图生图真人穿搭参考的一张；如果没有任何一张完全合格，selected_index 必须填 0。

搜索意图：{str(search_query or '日常真人穿搭').strip()[:80]}

被选中的单个编号格必须同时满足以下全部条件：
1. 是自然拍摄的真人照片，不是插画、商品平铺、模特截图或纯服装图；
2. 画面主体只能有一位清晰可辨的成年人，不能是多人合照；
3. 只能展示这一位主体的一套完整穿搭，不能是拼图、九宫格、前后对比或多套造型合集；
4. 从头顶到双脚/鞋子完整可见，不能是半身照、局部特写、裁掉头部或脚部；
5. 上衣、下装和鞋子轮廓清楚，没有被大面积文字、贴纸、物品或姿势遮挡；
6. 清晰度足够，且穿搭与搜索意图大致相关。
7. quality_score 必须按 0-100 给出真实综合评分；完全满足上述条件时应为 70-100，任一硬条件不满足时必须低于 70。

联系表本身的分格不算拼图；is_collage 是指被选中编号格内部是否仍是拼图或多套合集。宁可全部拒绝也不要猜测。只输出 JSON，不要解释：
{{
  "selected_index": 0,
  "is_real_photo": true,
  "is_collage": false,
  "person_count": 1,
  "single_outfit": true,
  "full_body_visible": true,
  "clothing_clear": true,
  "quality_sufficient": true,
  "keyword_match": true,
  "quality_score": 85,
  "reason": "一句中文理由"
}}"""
        text = await self._call_llm(
            prompt,
            timeout=45,
            json_mode=True,
            temperature=0.1,
            image_path=contact_sheet_path,
            require_image=True,
        )
        data = self._parse_llm_response(text or "") if text else None
        if not isinstance(data, dict):
            return {
                "accepted": False,
                "selected_index": 0,
                "quality_score": 0,
                "reason": "视觉模型没有返回有效的质检结果",
            }

        try:
            selected_index = int(data.get("selected_index") or 0)
        except (TypeError, ValueError):
            selected_index = 0
        try:
            person_count = int(data.get("person_count"))
        except (TypeError, ValueError):
            person_count = 0
        try:
            quality_score = max(0, min(100, int(float(data.get("quality_score") or 0))))
        except (TypeError, ValueError):
            quality_score = 0

        checks = {
            "is_real_photo": self._xiaohongshu_validation_bool(data.get("is_real_photo")),
            "is_collage": self._xiaohongshu_validation_bool(data.get("is_collage")),
            "single_outfit": self._xiaohongshu_validation_bool(data.get("single_outfit")),
            "full_body_visible": self._xiaohongshu_validation_bool(data.get("full_body_visible")),
            "clothing_clear": self._xiaohongshu_validation_bool(data.get("clothing_clear")),
            "quality_sufficient": self._xiaohongshu_validation_bool(data.get("quality_sufficient")),
            "keyword_match": self._xiaohongshu_validation_bool(data.get("keyword_match")),
        }
        accepted = (
            1 <= selected_index <= candidate_count
            and checks["is_real_photo"] is True
            and checks["is_collage"] is False
            and person_count == 1
            and checks["single_outfit"] is True
            and checks["full_body_visible"] is True
            and checks["clothing_clear"] is True
            and checks["quality_sufficient"] is True
            and checks["keyword_match"] is True
            and quality_score >= 70
        )
        logger.info(
            "小红书穿搭视觉质检结果: accepted=%s selected_index=%s score=%s "
            "person_count=%s checks=%s reason=%s",
            accepted,
            selected_index,
            quality_score,
            person_count,
            checks,
            str(data.get("reason") or "")[:160],
        )
        return {
            "accepted": accepted,
            "selected_index": selected_index if accepted else 0,
            "person_count": person_count,
            "quality_score": quality_score,
            "reason": str(data.get("reason") or ("符合全身穿搭参考标准" if accepted else "未通过全身穿搭参考标准"))[:160],
            **checks,
        }

    @staticmethod
    def _xiaohongshu_reference_prompt_block(search_query: str) -> str:
        return f"""

【小红书真人穿搭参考图｜最高优先级】
本次消息附带的图片，是用“{search_query or '今日真人穿搭'}”搜索后选中的真人穿搭照片。
必须先看图，再生成全天计划；图片中实际可见的服装组合、颜色、材质、版型、层次、鞋履、配饰和造型，是今日穿搭的唯一事实来源。
- 禁止自行重新设计、替换或补造另一套服装；不要让历史偏好、心情色彩、随机风格或文字模板覆盖图片里的真实搭配。
- outfit_style 选择最接近图片的已启用风格；outfit、reference_query、prompt、outfit_keywords，以及每条 schedule_details.outfit_en 都必须忠实描述同一套图中穿搭。
- 发型与配饰只描述图片中确实可见的造型；角色固有发色和脸部身份仍由角色设定及脸模决定，不从参考图复制脸或身材。
- 根据这套真实穿搭适合的场合和活动来安排日程，让活动、场景和服装自然匹配；日程可以丰富变化，但全天服装核心组合保持一致。
- caption 仍然是自然的全天计划小念头，可以让这套真人穿搭影响当天想去的场合和活动，但不要写成商品介绍或穿搭测评。
"""

    async def generate_today(
        self,
        *,
        outfit_reference_path: str = "",
        xiaohongshu_search_query: str = "",
    ) -> Optional[DailyEntry]:
        """生成今日日程"""
        today = self._configured_today()
        day_context = self._day_context(today)
        date_str = today.isoformat()

        logger.info("正在生成 %s 的日程... date_context=%s", date_str, day_context.day_type_label)

        history = self._get_history(today)
        schedule_history = self._get_schedule_history(today)
        recent_counts = self._recent_schedule_category_counts(today)
        recent_actions = self._recent_schedule_actions(today)
        recent_accessories = self._recent_outfit_accessories(today)
        disliked_items = self._load_disliked_outfits()
        disliked_context = self._disliked_outfit_context(limit=12, items=disliked_items)
        prompt = self._build_schedule_prompt(
            today,
            history,
            schedule_history,
            disliked_context=disliked_context,
        )
        compact_prompt = self._build_compact_schedule_prompt(
            today,
            history,
            schedule_history,
            disliked_context=disliked_context,
        )
        emergency_prompt = self._build_emergency_schedule_prompt(
            today,
            schedule_history,
            history,
            disliked_context=disliked_context,
        )
        visual_block = ""
        outfit_reference_path = str(outfit_reference_path or "").strip()
        xiaohongshu_search_query = self._normalize_xiaohongshu_search_query(
            xiaohongshu_search_query
        )
        if outfit_reference_path:
            visual_block = self._xiaohongshu_reference_prompt_block(
                xiaohongshu_search_query
            )
        prompt_sequence = [
            current_prompt + visual_block
            for current_prompt in (prompt, compact_prompt, emergency_prompt)
        ]
        disliked_rejection_feedback = ""
        self._last_llm_model = ""

        # 最多重试 3 次
        for attempt in range(3):
            current_prompt = prompt_sequence[attempt]
            if disliked_rejection_feedback:
                current_prompt += (
                    "\n\n【上一候选已被系统拒绝】\n"
                    + disliked_rejection_feedback
                    + "\n这次必须重新设计核心单品组合。"
                )
            if attempt == 1:
                logger.warning("完整日程 prompt 未生成可用 JSON，切换压缩日程 prompt 重试")
            elif attempt == 2:
                logger.warning("压缩日程 prompt 未生成可用 JSON，切换极简日程 prompt 重试")
            image_kwargs = {"image_path": outfit_reference_path} if outfit_reference_path else {}
            text = await self._call_llm(
                current_prompt,
                timeout=180,
                json_mode=True,
                temperature=self._llm_config.get("schedule_temperature", 0.8),
                **image_kwargs,
            )
            if not text:
                logger.warning(f"LLM 返回为空 (attempt {attempt+1})")
                continue

            data = self._parse_llm_response(text)
            if not data:
                logger.warning(f"解析失败 (attempt {attempt+1})，尝试 JSON 修复")
                data = await self._repair_schedule_json(
                    current_prompt,
                    text,
                    attempt + 1,
                    image_path=outfit_reference_path,
                )
                if not data:
                    continue

            # 提取关键词（LLM 输出优先，fallback 从 prompt 提取）
            outfit_kw = self._text_field(data.get("outfit_keywords", ""))
            scene_kw = self._text_field(data.get("scene_keywords", ""))
            photo_style_en = self._text_field(data.get("photo_style_en", "") or data.get("photo_style", ""))
            llm_prompt = self._text_field(data.get("prompt", ""))
            if not outfit_kw and llm_prompt:
                outfit_kw = self._extract_outfit_keywords(llm_prompt)
            if not scene_kw and llm_prompt:
                scene_kw = self._extract_scene_keywords(llm_prompt)

            schedule_display = self._schedule_text_field(data.get("schedule", ""))
            schedule_prompt = self._schedule_text_field(data.get("schedule_prompt", "") or data.get("schedule_en", ""), english=True)
            outfit_display = self._outfit_text_field(data.get("outfit", ""), data.get("outfit_style", ""))
            base_style = self._normalize_base_style(data.get("base_style", ""))
            reference_query = str(data.get("reference_query") or "").strip()
            if not reference_query:
                reference_query = " | ".join(
                    part.strip()
                    for part in (data.get("outfit_style", ""), outfit_display, llm_prompt)
                    if str(part or "").strip()
                )[:600]
            if not schedule_display or not schedule_prompt or not self._contains_cjk(schedule_display):
                logger.warning(f"日程字段不完整或展示日程非中文 (attempt {attempt+1})")
                continue
            forbidden_error = self._schedule_forbidden_output_error(data)
            if forbidden_error:
                logger.warning("日程触发禁词约束 (attempt %s): %s", attempt + 1, forbidden_error)
                continue
            display_items, prompt_items, alignment_error = self._validate_schedule_alignment(
                schedule_display,
                schedule_prompt,
            )
            if alignment_error:
                logger.warning(f"日程/生图日程结构不合格 (attempt {attempt+1}): {alignment_error}")
                continue
            diversity_note = self._schedule_diversity_error(
                display_items,
                recent_counts,
                recent_actions=recent_actions,
            )
            if diversity_note:
                logger.info(
                    "日程多样性建议（不拦截） (attempt %s): %s",
                    attempt + 1,
                    diversity_note,
                )
            missing_display = self._missing_required_periods(schedule_display)
            missing_prompt = self._missing_required_periods(schedule_prompt)
            if missing_display or missing_prompt:
                logger.warning(
                    f"日程缺少早中晚覆盖 (attempt {attempt+1}): "
                    f"display_missing={missing_display}, prompt_missing={missing_prompt}"
                )
                continue
            if not self._valid_display_outfit(outfit_display):
                logger.warning(f"outfit 展示字段不完整或非中文 (attempt {attempt+1})")
                continue
            candidate_outfit = dict(data)
            candidate_outfit["outfit"] = outfit_display
            candidate_outfit["prompt"] = llm_prompt
            candidate_outfit["outfit_keywords"] = outfit_kw
            accessory_repeat_error = self._outfit_accessory_repeat_error(
                candidate_outfit,
                recent_accessories,
            )
            if accessory_repeat_error:
                logger.info(
                    "穿搭配饰多样性建议（不拦截） (attempt %s): %s",
                    attempt + 1,
                    accessory_repeat_error,
                )
            schedule_details, detail_error = self._normalize_schedule_details(
                data.get("schedule_details"),
                display_items,
                prompt_items,
            )
            if detail_error:
                logger.warning(f"schedule_details 不合格 (attempt {attempt+1}): {detail_error}")
                continue
            candidate_outfit["schedule_details"] = schedule_details
            disliked_similarity_error = self._disliked_outfit_similarity_error(
                candidate_outfit,
                disliked_items,
            )
            if disliked_similarity_error:
                if outfit_reference_path:
                    logger.info(
                        "小红书真人参考图优先，忽略文本不喜欢相似度建议 (attempt %s): %s",
                        attempt + 1,
                        disliked_similarity_error,
                    )
                else:
                    disliked_rejection_feedback = disliked_similarity_error
                    logger.warning(
                        "穿搭触发不喜欢相似度硬拦截 (attempt %s): %s",
                        attempt + 1,
                        disliked_similarity_error,
                    )
                    continue
            calendar_conflicts = day_context.rest_day_conflicts(
                schedule_display,
                schedule_prompt,
                schedule_details,
                data.get("caption", ""),
            )
            calendar_error = self._calendar_conflict_message(day_context, calendar_conflicts)
            if calendar_error:
                logger.warning("真实日期约束不合格 (attempt %s): %s", attempt + 1, calendar_error)
                continue

            persona = self._runtime_persona()
            character_name = persona.get("name") or "角色"
            caption = (data.get("caption", "") or "").strip()
            if not self._caption_is_schedule_plan(caption):
                caption = self._build_schedule_plan_caption(schedule_display, character_name)

            schedule_model = str(getattr(self, "_last_llm_model", "") or "").strip()
            entry = DailyEntry(
                date=date_str,
                outfit_style=data.get("outfit_style", ""),
                base_style=base_style,
                reference_query=reference_query,
                outfit=outfit_display,
                schedule=schedule_display,
                schedule_prompt=schedule_prompt,
                schedule_details=schedule_details,
                prompt=llm_prompt,
                caption=caption,
                status="ok",
                outfit_keywords=outfit_kw,
                scene_keywords=scene_kw,
                photo_style_en=photo_style_en,
                schedule_llm_model=schedule_model,
                xiaohongshu_search_query=xiaohongshu_search_query,
            )
            logger.info(
                f"日程生成成功: model={schedule_model or '-'} | {entry.outfit_style} | "
                f"reference_query={entry.reference_query[:60]} "
                f"| outfit_kw={outfit_kw[:50]} | scene_kw={scene_kw[:50]} | photo_style={photo_style_en[:80]}"
            )
            return entry

        logger.error(f"日程生成失败: 重试 {3} 次均未成功")
        return self._build_fallback_entry(today)

    def _build_fallback_entry(self, today: date) -> DailyEntry:
        date_str = today.isoformat()
        return DailyEntry(
            date=date_str,
            outfit_style="",
            base_style="",
            reference_query="",
            outfit="",
            schedule="生成失败",
            schedule_prompt="",
            schedule_details=[],
            prompt="",
            caption="",
            status="failed",
            source="fallback",
            outfit_keywords="",
            scene_keywords="",
            photo_style_en="",
            schedule_llm_model="fallback",
        )
