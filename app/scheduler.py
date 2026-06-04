"""日程生成器 - 调用 LLM 生成每日穿搭+日程"""
import asyncio
import json
import logging
import random
from datetime import datetime, date, timedelta
from typing import Optional

from data import DailyEntry

logger = logging.getLogger(__name__)

# 穿搭风格池
OUTFIT_STYLES = [
    "冷御风", "甜美风", "元气风", "温柔风", "优雅风",
    "休闲风", "酷飒风", "清新风", "性感风", "复古风",
]

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


class DailyScheduler:
    """使用 LLM 生成每日穿搭和日程"""

    def __init__(self, config: dict, data_dir: str):
        self.config = config
        self.data_dir = data_dir
        self._llm_config = config.get("llm", {})
        self._char = config.get("character", {})

    async def _call_llm(self, prompt: str, timeout: int = 60) -> Optional[str]:
        """调用 CPA LLM（异步，不阻塞事件循环）"""
        base_url = self._llm_config.get("base_url", "http://127.0.0.1:8327/v1")
        api_key = self._llm_config.get("api_key", "")
        model = self._llm_config.get("model", "deepseek-v4-flash")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        # deepseek 模型对 system 角色有 reasoning 问题，全放 user 消息
        messages = [
            {"role": "user", "content": prompt},
        ]

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": 2048,
            "temperature": 0.3,
        }

        loop = asyncio.get_running_loop()

        def _do_request(url, headers, json_data, timeout):
            import requests as req
            try:
                return req.post(url, headers=headers, json=json_data, timeout=timeout)
            except Exception:
                return None

        try:
            resp = await loop.run_in_executor(
                None,
                lambda: _do_request(
                    f"{base_url}/chat/completions",
                    headers, payload, timeout,
                )
            )
            if resp and resp.status_code == 200:
                data = resp.json()
                msg = data["choices"][0]["message"]
                # deepseek 推理模型可能把输出放 reasoning_content
                content = (msg.get("content") or "").strip()
                if not content:
                    content = (msg.get("reasoning_content") or "").strip()
                if content:
                    return content
                return None
            else:
                status = resp.status_code if resp else "no response"
                logger.error(f"LLM call failed: {status}")
                return None
        except Exception as e:
            logger.error(f"LLM call error: {e}")
            # 尝试备用模型
            fallback = self._llm_config.get("fallback_model", "mimo-v2.5")
            if fallback and fallback != model:
                payload["model"] = fallback
                payload["temperature"] = 0.3
                try:
                    resp = await loop.run_in_executor(
                        None,
                        lambda: _do_request(
                            f"{base_url}/chat/completions",
                            headers, payload, timeout,
                        )
                    )
                    if resp and resp.status_code == 200:
                        data = resp.json()
                        msg = data["choices"][0]["message"]
                        return (msg.get("content") or msg.get("reasoning_content") or "").strip()
                except Exception:
                    pass
            return None

    def _build_schedule_prompt(self, today: date, history: str) -> str:
        """构建日程生成 prompt"""
        weekday = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][today.weekday()]
        outfit_style = random.choice(OUTFIT_STYLES)
        mood = random.choice(MOOD_COLORS)
        sched_type = random.choice(SCHEDULE_TYPES)
        appearance = self._char.get("appearance", "")
        persona = self._char.get("persona", "")

        return f"""你是一个18岁的虚拟主播，名叫猪猪，是主人的专属小宝贝。你热爱生活，情感细腻，每天都会精心打扮自己。

重要：只输出 JSON，不输出其他任何文字。不要解释，不要开头，不要结尾，只输出 JSON 对象本体。

【今日信息】
日期：{today.year}年{today.month}月{today.day}日
星期：{weekday}
随机主题：{outfit_style}
心情色彩：{mood}
日程类型：{sched_type}

【历史穿搭参考（不要重复以下穿搭）】
{history}

【角色外貌】
{appearance}

【任务要求】
请为今日生成一份完整的穿搭和日程计划。

⚠️ outfit 字段必须包含以下三个部分，缺一不可：
1. 「风格：」+ 风格名（从 [冷御风, 甜美风, 元气风, 温柔风, 优雅风, 休闲风, 酷飒风, 清新风, 性感风, 复古风] 中选）
2. 「发型：」+ 具体发型描述（如：双马尾配蝴蝶结、慵懒低丸子头、编发侧马尾、高马尾、公主切、蛋卷头等，不要披头散发）
3. 「穿搭：」+ 服装+鞋子+配饰的完整描述，包含颜色、材质、款式
4. 「动作：」+ 当前的姿态/场景动作（如：托腮趴在桌上、踮脚够书架上的书、蹲下系鞋带、靠在窗边喝咖啡等）

⚠️ prompt 字段必须是纯英文，适合 AI 生图，必须包含：发型、服装细节、动作/姿势、场景、光影氛围

⚠️ schedule 必须严格使用 \\n 分隔，每行一条，格式为「HH:mm 活动描述」：
   "09:00 起床洗漱，换上今日穿搭\\n10:30 去咖啡馆写日记\\n12:00 午餐\\n14:00 画画创作\\n16:00 出门散步\\n18:00 回家做饭\\n20:00 晚间直播\\n22:00 护肤睡觉"
   不要用"早上9点"、"下午2点"等中文时间格式，必须用 HH:mm 数字格式！每行之间必须用 \\n 换行，不要用空格或句号分隔！

caption 要用猪猪的语气，带颜文字和～波浪号，根据穿搭和日程写出今日心情。

JSON 格式（字段名固定，value 替换为实际内容）：
{{
    "outfit_style": "风格名",
    "outfit": "风格：xxx \\n发型：xxx \\n穿搭：xxx \\n动作：xxx",
    "schedule": "HH:mm 活动描述\\nHH:mm 活动描述\\n...",
    "prompt": "English prompt with hairstyle, outfit details, pose, scene, lighting...",
    "caption": "猪猪的今日心情文案～"
}}"""

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

            entry = DailyEntry(
                date=date_str,
                outfit_style=data.get("outfit_style", ""),
                outfit=data.get("outfit", ""),
                schedule=data.get("schedule", ""),
                prompt=data.get("prompt", ""),
                caption=data.get("caption", ""),
                status="ok",
            )
            logger.info(f"日程生成成功: {entry.outfit_style}")
            return entry

        logger.error(f"日程生成失败: 重试 {3} 次均未成功")
        return DailyEntry(
            date=date_str,
            outfit="生成失败",
            schedule="生成失败",
            status="failed",
        )
