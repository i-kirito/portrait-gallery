"""日程生成器 - 调用 LLM 生成每日穿搭+日程"""
import asyncio
import json
import logging
import os
import random
import re
from datetime import datetime, date, timedelta
from typing import Optional

from data import DailyEntry
from settings import DEFAULT_OUTFIT_STYLES, llm_request_config, load_enabled_outfit_styles, load_runtime_persona

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

BASE_STYLE_OPTIONS = {"cool", "girly", "sweet"}
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
    {"name": "noon", "label": "中", "start": "12:00", "end": "17:59"},
    {"name": "evening", "label": "晚", "start": "18:00", "end": "23:59"},
]


class DailyScheduler:
    """使用 LLM 生成每日穿搭和日程"""

    def __init__(self, config: dict, data_dir: str):
        self.config = config
        self.data_dir = data_dir
        self._llm_config = config.get("llm", {})
        self._char = config.get("character", {})

    def _runtime_persona(self) -> dict:
        return load_runtime_persona(self.config, self.data_dir)

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

    async def _call_llm(self, prompt: str, timeout: int = 60) -> Optional[str]:
        """调用 CPA LLM（异步，不阻塞事件循环）"""
        request_config = llm_request_config(self.config, self.data_dir)
        chat_url = request_config["chat_url"]
        api_key = request_config["api_key"]
        models = request_config["models"]
        if not chat_url or not models:
            logger.error("LLM config missing: chat_url/models")
            return None

        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # deepseek 模型对 system 角色有 reasoning 问题，全放 user 消息
        messages = [
            {"role": "user", "content": prompt},
        ]

        loop = asyncio.get_running_loop()

        def _do_request(url, headers, json_data, timeout):
            import requests as req
            try:
                return req.post(url, headers=headers, json=json_data, timeout=timeout)
            except Exception:
                return None

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

        for model in models:
            payload = {
                "model": model,
                "messages": messages,
                "max_tokens": 4096,
                "temperature": 0.3,
            }
            try:
                resp = await loop.run_in_executor(
                    None,
                    lambda p=payload: _do_request(chat_url, headers, p, timeout),
                )
                if resp is not None and resp.status_code == 200:
                    data = resp.json()
                    choices = data.get("choices") if isinstance(data, dict) else None
                    if not choices:
                        logger.error(
                            "LLM call returned invalid response: model=%s, detail=%s",
                            model,
                            _response_error(resp),
                        )
                        continue
                    msg = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
                    content = (msg.get("content") or msg.get("reasoning_content") or "").strip()
                    if content:
                        return content
                    logger.error(f"LLM call returned empty content: model={model}")
                else:
                    status = resp.status_code if resp is not None else "no response"
                    logger.error(
                        "LLM call failed: model=%s, status=%s, detail=%s",
                        model,
                        status,
                        _response_error(resp),
                    )
            except Exception as e:
                logger.error(f"LLM call error: model={model}, {e}")
        return None

    def _build_schedule_prompt(self, today: date, history: str) -> str:
        """构建日程生成 prompt"""
        weekday = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][today.weekday()]
        enabled_styles = load_enabled_outfit_styles(self.config, self.data_dir)
        style_list_text = ", ".join(enabled_styles)
        mood = random.choice(MOOD_COLORS)
        sched_type = random.choice(SCHEDULE_TYPES)
        persona = self._runtime_persona()
        character_name = persona.get("name") or "角色"
        user_name = persona.get("user_name") or "用户"
        persona_text = persona.get("persona") or f"你正在为「{character_name}」生成每日穿搭和心情记录。"
        caption_voice = persona.get("caption_voice") or "自然、亲切、贴近日常。"
        appearance = persona.get("appearance") or self._char.get("appearance", "")
        if not appearance:
            appearance = self._read_config_key("character_appearance")
        favorite_outfits = self._favorite_outfit_context()
        disliked_outfits = self._disliked_outfit_context()

        return f"""你正在为「{character_name}」生成每日穿搭和日程。
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

【历史穿搭参考（不要重复以下穿搭）】
{history}

【收藏穿搭偏好（用户主动收藏的审美方向；只作为发型/穿搭参考，不是日程、动作或场景参考）】
{favorite_outfits}

【不喜欢穿搭反馈（用户明确想减少的审美方向；只作为负向发型/穿搭参考，不是硬编码禁用规则）】
{disliked_outfits}

【角色外貌】
{appearance}

【任务要求】
请为今日生成一份完整的穿搭和日程计划。

⚠️ 你需要自己选择当天最合适的 outfit_style，并写出 reference_query：
- outfit_style 必须从 [{style_list_text}] 中选择一个，不要使用未启用的风格。
- reference_query 是给系统选择参考图用的自然语言提示，必须概括今天适合的参考图气质、风格、发型/脸部氛围、服装色系和场景 mood；不要写 cool/girly/sweet 三选一，不要写文件名。
- 如果存在收藏穿搭偏好，只影响 outfit_style、reference_query、outfit 和 prompt 里的发型/服装部分：参考服装气质、配色、版型、材质和搭配层次，生成相近但新的组合。
- 如果存在不喜欢穿搭反馈，请由你判断相似度并减少相近方向：避开高度相似的配色、版型、材质、发型、搭配层次和整体气质；不要机械禁用某个大类风格。
- 不要照抄收藏里的完整发型短语、单品组合或旧描述；不要参考、复用或联想收藏里的日程、动作、场景。schedule、schedule_prompt、动作、场景必须根据今日信息重新决定。

⚠️ outfit 字段必须包含以下五个部分，缺一不可：
1. 「风格：」+ 风格名（只能从 [{style_list_text}] 中选，不要使用未启用的风格）
2. 「发型：」+ 具体发型描述（15-30 个汉字，如：双马尾配蝴蝶结、慵懒低丸子头、编发侧马尾、高马尾、公主切、蛋卷头等，不要披头散发）
3. 「穿搭：」+ 详细穿搭描述（至少 70 个汉字），必须同时写清：上装、下装或裙装、鞋子、包/发饰/首饰等配饰、主色、材质、版型/廓形、一个细节亮点。不要只写“少女风造型”“精心搭配”等空泛词。
4. 「动作：」+ 当前的姿态/场景动作（20-40 个汉字，由你根据今天设定自主决定；示例只作格式参考，不要照抄：托腮趴在桌上、踮脚够书架上的书、蹲下系鞋带、靠在窗边喝咖啡等）
5. 「场景：」+ 当前中文场景描述（15-40 个汉字，由你根据今天设定自主决定；示例只作格式参考，不要照抄：晨光照进来的卧室窗边、安静咖啡馆的靠窗小桌、暖色路灯下的街角等）

⚠️ prompt 字段必须是纯英文，适合 AI 生图，必须包含：发型、服装细节、动作/姿势、场景、光影氛围

⚠️ schedule 是 WebUI 展示用，必须用中文，必须有 6-8 条，严格使用 \\n 分隔，每行一条，格式为「HH:mm 中文活动描述」：
   下面示例只展示格式，不要照抄活动内容：
   "09:00 起床整理今天的温柔穿搭\\n10:30 坐在咖啡馆窗边写手账\\n12:00 吃一份清爽午餐\\n14:00 在画室整理灵感草图\\n16:00 去公园散步拍照\\n18:00 回家做一顿简单晚餐\\n20:00 准备晚间直播\\n22:00 做睡前护肤准备休息"
   不要用"早上9点"、"下午2点"等中文时间格式，必须用 HH:mm 数字格式！每行之间必须用 \\n 换行，不要用空格或句号分隔！
   每条活动描述必须用中文写，要具体到场景/动作/道具（12-30 个汉字），不要只写"做早餐""出门""休息"等短句。
   每个时段的动作、道具和场景都由你根据今日人设、心情色彩、日程类型和穿搭自主决定；后续生图会直接采用这些日程动作，不会再用代码模板补动作。

⚠️ schedule_prompt 是生图 prompt 注入用，必须用纯英文，条数和时间必须与 schedule 一一对应：
   下面示例只展示格式，不要照抄活动内容：
   "09:00 wake up and arrange today's soft outfit\\n10:30 write diary at a window table in a cafe\\n12:00 have a light refreshing lunch\\n14:00 organize inspiration sketches in an art studio\\n16:00 take a walk and photos in the park\\n18:00 cook a simple dinner at home\\n20:00 prepare for an evening livestream\\n22:00 do skincare and get ready for bedtime"
   schedule 给用户看中文；schedule_prompt 只给生图 prompt 使用英文。
   schedule_prompt 的每条英文活动必须明确 action + scene + props/time mood，不能只写 vague daily routine。

⚠️ schedule_details 是生图链路的严格结构化明细，必须是数组，条数、顺序、time 必须与 schedule 和 schedule_prompt 完全一致：
   - 每个时间段都必须输出一个对象，不能遗漏任何一条 schedule。
   - 每个对象必须包含 time、activity_zh、activity_en、action_en、scene_en、outfit_en、hair_en，可选 props_en、lighting_en。
   - activity_zh 必须是中文，并和 schedule 当前行表达同一个活动。
   - activity_en、action_en、scene_en、outfit_en、hair_en、props_en、lighting_en 必须是纯英文。
   - action_en 必须写清人物当时正在做什么、手部/身体动作和互动对象。
   - scene_en 必须写清具体地点、周围环境、关键道具和时间氛围。
   - time 是强约束：06:00-11:59 必须是 morning/daylight 氛围；12:00-17:59 必须是 midday/afternoon daylight 氛围；不能因为“街区/打卡/散步”等活动就写成 night/evening/sunset/neon/street lamps。
   - outfit_en 必须写清上装、下装/裙装、鞋子、配饰、颜色、材质/版型；如果当天整套穿搭不变，也要在每个时间段重复写清。
   - hair_en 必须写清具体发型和发饰/整理状态；不要只写 "nice hair"、"beautiful hairstyle" 等空话。
   - 后续生图会严格采用对应 time 的 schedule_details，不再用代码随机补动作、场景、服饰或发型。

⚠️ schedule 必须覆盖早/中/晚三个时间段，每个时间段至少 1 条：
   - 早：06:00-11:59
   - 中：12:00-17:59
   - 晚：18:00-23:59
   即使只输出 6 条，也必须至少包含 1 条早、1 条中、1 条晚；不要把所有安排都集中在上午和下午。
   schedule_prompt 的时间必须和 schedule 一一对应，也要覆盖同样的早/中/晚时间段。

⚠️ caption 是 WebUI「今日穿搭方案」里的“小心思”，不是单张照片配文。
   它必须写成「{character_name}」在心里自然冒出来的全天计划小念头：
   - 像刚醒来或出门前在心里嘀咕“今天想怎么过”，轻轻带到 schedule 里的 2-4 个安排。
   - 不要用“心里把今天的节奏排了一遍”“上午先/午后留给/晚上再收尾”这类总结式模板。
   - 少用时段标签和清单感，句子要像自然想法，而不是系统概括日程。
   - 可以有一点自然期待和情绪，但不要写成自拍/画面/穿搭点评。
   - 禁止写主人互动、调情、亲一口、抱抱、被夸、等人来找、穿得好不好看等内容。
   - 禁止出现“画廊、拍照、美照、造型、穿搭很美、今天穿得”等记录或外观评价话术。
   - 输出 1-2 句中文，总长 40-90 个汉字；不要加标题、引号或 emoji。

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
            "hair_en": "specific hairstyle and hair accessory/status in English",
            "props_en": "optional relevant props in English",
            "lighting_en": "optional lighting and ambience in English"
        }}
    ],
    "prompt": "English prompt with hairstyle, outfit details, pose, scene, lighting...",
    "caption": "{character_name}自然想着今天想怎么过的小心思。",
    "outfit_keywords": "JK uniform, pleated skirt, white blouse, red ribbon, loafers",
    "scene_keywords": "coffee shop, cafe counter, warm ambient light"
}}"""

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
        if 6 <= hour < 17:
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
            return f"{name}今天想过得松一点，认真做点事，也给自己留一点发呆和慢慢休息的空隙。"

        buckets = {"上午": [], "午后": [], "晚上": []}
        for time_text, activity in items:
            hour = int(time_text.split(":", 1)[0])
            label = self._caption_activity_label(activity)
            if not label:
                continue
            if hour < 12:
                buckets["上午"].append(label)
            elif hour < 18:
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

        caption = "今天想过得松一点：" + "，".join(parts) + "，慢慢把心放下来。"
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
        )
        if any(marker in text for marker in bad_markers):
            return False
        intent_markers = ("想过", "想怎么过", "打算", "准备", "安排", "计划", "节奏", "先", "再", "然后")
        time_markers = ("一整天", "早上", "上午", "午后", "下午", "晚上")
        return any(marker in text for marker in intent_markers) and any(marker in text for marker in time_markers)

    def _get_history(self, today: date, days: int = 7) -> str:
        """获取最近几天的历史日程"""
        items = []
        for i in range(1, days + 1):
            d = today - timedelta(days=i)
            date_str = d.isoformat()
            # 尝试从持久化数据读取
            try:
                import json as j
                path = f"{self.data_dir}/schedule_data.json"
                with open(path) as f:
                    all_data = j.load(f)
                entry = all_data.get(date_str)
                if entry and entry.get("status") == "ok":
                    items.append(f"[{date_str}] 风格：{entry.get('outfit_style','')} 穿搭：{entry.get('outfit','')[:60]}")
            except Exception:
                pass
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

    def _disliked_outfit_context(self, limit: int = 5) -> str:
        """读取用户不喜欢的穿搭方案，作为 LLM 的负向偏好参考。"""
        path = os.path.join(self.data_dir, "disliked_outfits.json")
        if not os.path.exists(path):
            return "（无不喜欢穿搭反馈）"
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items", data) if isinstance(data, dict) else data
            if not isinstance(items, list):
                return "（无不喜欢穿搭反馈）"

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
            return "\n".join(lines) if lines else "（无不喜欢穿搭反馈）"
        except Exception as e:
            logger.warning("读取不喜欢穿搭反馈失败: %s", e)
            return "（无不喜欢穿搭反馈）"

    def _parse_llm_response(self, text: str) -> Optional[dict]:
        """从 LLM 回复中解析 JSON"""
        # 去掉可能的 markdown 代码块
        text = text.strip()
        text = text.replace("```json", "").replace("```", "").strip()

        # 找第一个 { 和最后一个 }
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            logger.error(f"No JSON found in LLM response: {text[:200]}")
            return None

        try:
            return json.loads(text[start:end+1])
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}, text={text[start:end+1][:200]}")
            return None

    async def generate_today(self) -> Optional[DailyEntry]:
        """生成今日日程"""
        today = date.today()
        date_str = today.isoformat()

        logger.info(f"正在生成 {date_str} 的日程...")

        history = self._get_history(today)
        prompt = self._build_schedule_prompt(today, history)

        # 最多重试 3 次
        for attempt in range(3):
            text = await self._call_llm(prompt)
            if not text:
                logger.warning(f"LLM 返回为空 (attempt {attempt+1})")
                continue

            data = self._parse_llm_response(text)
            if not data:
                logger.warning(f"解析失败 (attempt {attempt+1})")
                continue

            # 提取关键词（LLM 输出优先，fallback 从 prompt 提取）
            outfit_kw = data.get("outfit_keywords", "").strip()
            scene_kw = data.get("scene_keywords", "").strip()
            llm_prompt = data.get("prompt", "")
            if not outfit_kw and llm_prompt:
                outfit_kw = self._extract_outfit_keywords(llm_prompt)
            if not scene_kw and llm_prompt:
                scene_kw = self._extract_scene_keywords(llm_prompt)

            schedule_display = data.get("schedule", "").strip()
            schedule_prompt = (data.get("schedule_prompt", "") or data.get("schedule_en", "")).strip()
            outfit_display = data.get("outfit", "").strip()
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
            display_items, prompt_items, alignment_error = self._validate_schedule_alignment(
                schedule_display,
                schedule_prompt,
            )
            if alignment_error:
                logger.warning(f"日程/生图日程结构不合格 (attempt {attempt+1}): {alignment_error}")
                continue
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
            schedule_details, detail_error = self._normalize_schedule_details(
                data.get("schedule_details"),
                display_items,
                prompt_items,
            )
            if detail_error:
                logger.warning(f"schedule_details 不合格 (attempt {attempt+1}): {detail_error}")
                continue

            persona = self._runtime_persona()
            character_name = persona.get("name") or "角色"
            caption = (data.get("caption", "") or "").strip()
            if not self._caption_is_schedule_plan(caption):
                caption = self._build_schedule_plan_caption(schedule_display, character_name)

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
            )
            logger.info(f"日程生成成功: {entry.outfit_style} | reference_query={entry.reference_query[:60]} | outfit_kw={outfit_kw[:50]} | scene_kw={scene_kw[:50]}")
            return entry

        logger.error(f"日程生成失败: 重试 {3} 次均未成功")
        return self._build_fallback_entry(today)

    def _build_fallback_entry(self, today: date) -> DailyEntry:
        date_str = today.isoformat()
        return DailyEntry(
            date=date_str,
            outfit_style="",
            base_style="",
            outfit="生成失败",
            schedule="生成失败",
            schedule_prompt="",
            prompt="",
            caption="",
            status="failed",
            source="fallback",
            outfit_keywords="",
            scene_keywords="",
        )
