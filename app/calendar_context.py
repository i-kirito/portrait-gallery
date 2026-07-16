"""Calendar context for date-aware daily schedule generation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import json
import logging
import re
from typing import Iterable


WEEKDAY_LABELS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
logger = logging.getLogger(__name__)
_WARNED_MISSING_CALENDAR_YEARS: set[int] = set()
BUILTIN_COMPLETE_CALENDAR_YEARS = {2026}

WORKDAY_SCHEDULE_TYPES = [
    "工作日",
    "学习日",
    "创作日",
    "运动日",
    "社交日",
    "购物日",
    "约会日",
    "宅家日",
    "放松日",
]

REST_DAY_SCHEDULE_TYPES = [
    "假期日",
    "宅家日",
    "购物日",
    "运动日",
    "社交日",
    "旅行日",
    "创作日",
    "放松日",
    "约会日",
]

HOLIDAY_TYPE_HINTS = {
    "元旦": ("新年假期", "适合轻松整理、见朋友、逛街、短途散步或宅家休息。"),
    "春节": ("春节假期", "适合团圆、拜年、年味小吃、整理红包/礼物、休息或短途走亲访友。"),
    "清明节": ("清明假期", "适合安静踏青、整理花束、轻户外散步或宅家休息；语气避免过度热闹。"),
    "劳动节": ("劳动节假期", "适合小旅行、逛展、野餐、购物、朋友聚会或充分休息。"),
    "端午节": ("端午假期", "适合粽子、香囊、清爽出游、河畔散步或在家慢慢休息。"),
    "中秋节": ("中秋假期", "适合月饼、团圆饭、傍晚赏月、送礼或轻松约会。"),
    "国庆节": ("国庆假期", "适合假期出游、看展、聚会、城市漫步或宅家放松。"),
}

# State Council 2026 public holiday schedule, including make-up workdays.
# Future-year official holiday tables can be supplied through config overrides.
OFFICIAL_PUBLIC_HOLIDAYS_2026 = {
    **{(date(2026, 1, 1) + timedelta(days=i)).isoformat(): "元旦" for i in range(3)},
    **{(date(2026, 2, 15) + timedelta(days=i)).isoformat(): "春节" for i in range(9)},
    **{(date(2026, 4, 4) + timedelta(days=i)).isoformat(): "清明节" for i in range(3)},
    **{(date(2026, 5, 1) + timedelta(days=i)).isoformat(): "劳动节" for i in range(5)},
    **{(date(2026, 6, 19) + timedelta(days=i)).isoformat(): "端午节" for i in range(3)},
    **{(date(2026, 9, 25) + timedelta(days=i)).isoformat(): "中秋节" for i in range(3)},
    **{(date(2026, 10, 1) + timedelta(days=i)).isoformat(): "国庆节" for i in range(7)},
}

OFFICIAL_MAKEUP_WORKDAYS_2026 = {
    "2026-01-04": "元旦调休上班",
    "2026-02-14": "春节调休上班",
    "2026-02-28": "春节调休上班",
    "2026-05-09": "劳动节调休上班",
    "2026-09-20": "国庆节调休上班",
    "2026-10-10": "国庆节调休上班",
}

FIXED_FESTIVALS = {
    "01-01": "元旦",
    "02-14": "情人节",
    "03-08": "妇女节",
    "05-01": "劳动节",
    "06-01": "儿童节",
    "10-01": "国庆节",
    "12-24": "平安夜",
    "12-25": "圣诞节",
}

REST_DAY_FORBIDDEN_ZH = (
    "上班",
    "通勤",
    "上学",
    "上课",
    "早八",
    "补课",
    "办公室",
    "公司",
    "工位",
    "开会",
    "会议",
    "加班",
    "考试",
    "写作业",
    "赶作业",
)

REST_DAY_FORBIDDEN_EN = (
    "go to work",
    "goes to work",
    "commute to work",
    "office meeting",
    "at the office",
    "company office",
    "work shift",
    "overtime",
    "go to school",
    "school class",
    "attend class",
    "classroom lesson",
    "homework",
    "exam",
)


@dataclass(frozen=True)
class DayContext:
    """Derived facts about the target date."""

    date_value: date
    weekday_label: str
    is_weekend: bool
    is_public_holiday: bool
    is_makeup_workday: bool
    holiday_name: str = ""
    festival_name: str = ""
    makeup_workday_name: str = ""
    official_calendar_available: bool = True

    @property
    def is_rest_day(self) -> bool:
        return (self.is_weekend or self.is_public_holiday) and not self.is_makeup_workday

    @property
    def date_label(self) -> str:
        return f"{self.date_value.isoformat()} {self.weekday_label}"

    @property
    def day_type_label(self) -> str:
        if self.is_makeup_workday:
            return "调休上班日"
        if self.is_public_holiday:
            return "法定节假日"
        if self.is_weekend:
            return "周末休息日"
        return "普通工作日"

    @property
    def holiday_or_festival(self) -> str:
        return self.holiday_name or self.festival_name or "无"

    def schedule_type_pool(self, base_types: Iterable[str]) -> list[str]:
        """Return date-appropriate schedule types while preserving configured names."""
        base = [str(item).strip() for item in base_types if str(item).strip()]
        if self.is_rest_day:
            allowed = [item for item in base if item not in {"工作日", "学习日"}]
            pool = allowed or REST_DAY_SCHEDULE_TYPES
            if self.holiday_name in HOLIDAY_TYPE_HINTS:
                holiday_type = HOLIDAY_TYPE_HINTS[self.holiday_name][0]
                pool = [holiday_type] + [item for item in pool if item != holiday_type]
            elif "假期日" not in pool:
                pool = ["假期日"] + pool
            return pool
        if self.is_makeup_workday:
            preferred = ["工作日", "学习日", "创作日", "运动日", "放松日"]
            return [item for item in preferred if item in base] or WORKDAY_SCHEDULE_TYPES
        return base or WORKDAY_SCHEDULE_TYPES

    def prompt_block(self, schedule_type: str) -> str:
        """Human-readable guidance injected into LLM prompts."""
        holiday_hint = ""
        if self.holiday_name in HOLIDAY_TYPE_HINTS:
            holiday_hint = HOLIDAY_TYPE_HINTS[self.holiday_name][1]
        elif self.festival_name and self.festival_name != self.holiday_name:
            holiday_hint = f"今天有 {self.festival_name} 节日氛围，可以轻轻带到日程或小心思里。"

        if self.is_rest_day:
            restriction = (
                "今天按休息/假期处理。日程类型不能写工作日或学习日；"
                "schedule、schedule_prompt、schedule_details、caption 都不要出现上班、通勤、上课、上学、办公室会议、考试、作业、加班等安排。"
            )
            preferred = "活动应偏休息、生活、出游、聚会、兴趣创作、购物、运动或节日相关。"
        elif self.is_makeup_workday:
            restriction = "今天虽然可能是周末，但官方调休为上班日，可以安排工作、学习、通勤或普通工作日节奏。"
            preferred = "如果写工作/学习，也要保持生活化，不要整天只有工作任务。"
        else:
            restriction = "今天是普通工作日；可以安排工作、学习或生活事项，也可以选择轻松日程，但要符合星期与真实日期。"
            preferred = "日程要保留早中晚生活节奏，不要完全随机跳到节假日活动。"

        lines = [
            f"真实日期：{self.date_label}",
            f"日期性质：{self.day_type_label}",
            f"节日/假期：{self.holiday_or_festival}",
            f"本次日程类型：{schedule_type}",
            restriction,
            preferred,
        ]
        if not self.official_calendar_available:
            lines.append(
                f"警告：尚未配置 {self.date_value.year} 年官方节假日和调休表；"
                "当前只能可靠判断星期和固定公历节日，不要自行猜测春节等农历假期或调休。"
            )
        if holiday_hint:
            lines.append(holiday_hint)
        return "\n".join(lines)

    def rest_day_conflicts(self, *values) -> list[str]:
        """Return short conflict labels when rest-day schedules contain work/school items."""
        if not self.is_rest_day:
            return []
        text = "\n".join(_stringify(value) for value in values if value is not None)
        compact = re.sub(r"\s+", " ", text).strip()
        if not compact:
            return []

        conflicts = []
        for term in REST_DAY_FORBIDDEN_ZH:
            if term in compact:
                conflicts.append(term)
        lower = compact.lower()
        for term in REST_DAY_FORBIDDEN_EN:
            if term in lower:
                conflicts.append(term)
        return list(dict.fromkeys(conflicts))[:8]


def build_day_context(target_date: date, config: dict | None = None) -> DayContext:
    holiday_overrides = dict(OFFICIAL_PUBLIC_HOLIDAYS_2026)
    makeup_overrides = dict(OFFICIAL_MAKEUP_WORKDAYS_2026)
    config = config or {}
    _merge_calendar_config(holiday_overrides, makeup_overrides, config)

    complete_years = BUILTIN_COMPLETE_CALENDAR_YEARS | _configured_complete_years(config)
    official_calendar_available = target_date.year in complete_years
    if not official_calendar_available and target_date.year not in _WARNED_MISSING_CALENDAR_YEARS:
        _WARNED_MISSING_CALENDAR_YEARS.add(target_date.year)
        logger.warning(
            "缺少 %s 年完整官方节假日/调休数据；配置 holidays、makeup_workdays 后还需把年份加入 complete_years",
            target_date.year,
        )

    key = target_date.isoformat()
    month_day = target_date.strftime("%m-%d")
    holiday_name = holiday_overrides.get(key, "")
    makeup_name = makeup_overrides.get(key, "")
    festival_name = holiday_name or FIXED_FESTIVALS.get(month_day, "")
    return DayContext(
        date_value=target_date,
        weekday_label=WEEKDAY_LABELS[target_date.weekday()],
        is_weekend=target_date.weekday() >= 5,
        is_public_holiday=bool(holiday_name),
        is_makeup_workday=bool(makeup_name),
        holiday_name=holiday_name,
        festival_name=festival_name,
        makeup_workday_name=makeup_name,
        official_calendar_available=official_calendar_available,
    )


def _merge_calendar_config(holidays: dict[str, str], makeup_workdays: dict[str, str], config: dict):
    calendar = config.get("schedule", {}).get("calendar", {}) if isinstance(config, dict) else {}
    if not isinstance(calendar, dict):
        return
    for key in ("holidays", "public_holidays"):
        holidays.update(_normalize_calendar_map(calendar.get(key)))
    for key in ("makeup_workdays", "workdays"):
        makeup_workdays.update(_normalize_calendar_map(calendar.get(key)))


def _configured_complete_years(config: dict) -> set[int]:
    calendar = config.get("schedule", {}).get("calendar", {}) if isinstance(config, dict) else {}
    if not isinstance(calendar, dict):
        return set()
    raw = calendar.get("complete_years", calendar.get("official_years", []))
    if isinstance(raw, (str, int)):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return set()
    years = set()
    for value in raw:
        try:
            year = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if 2000 <= year <= 2200:
            years.add(year)
    return years


def _normalize_calendar_map(value) -> dict[str, str]:
    if not value:
        return {}
    if isinstance(value, dict):
        return {
            str(day).strip(): str(name or "假期").strip()
            for day, name in value.items()
            if _looks_like_date(day)
        }
    if isinstance(value, list):
        result = {}
        for item in value:
            if isinstance(item, str) and _looks_like_date(item):
                result[item.strip()] = "假期"
            elif isinstance(item, dict):
                day = item.get("date") or item.get("day")
                name = item.get("name") or item.get("label") or item.get("holiday") or "假期"
                if _looks_like_date(day):
                    result[str(day).strip()] = str(name).strip()
        return result
    return {}


def _looks_like_date(value) -> bool:
    try:
        date.fromisoformat(str(value).strip())
        return True
    except ValueError:
        return False


def _stringify(value) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)
