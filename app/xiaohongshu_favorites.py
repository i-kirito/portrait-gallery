"""Persistent Xiaohongshu outfit favorites library.

This is a collection *inside Portrait Gallery*.  It never talks to a
Xiaohongshu account, never publishes anything, and is deliberately separate
from the wardrobe's ``favorite_outfits.json`` so the two lifecycles cannot
interfere with each other.

Lifecycle of one outfit::

    save ──► favorites (available) ──reserve──► favorites (reserved)
                                                    │      │
                                         release ◄──┘      └─► mark_used
                                                                  │
                                                                  ▼
                                                              history

* ``reserve_for_date`` is how automatic scheduling takes an outfit.  A
  reservation is bookkeeping only; drafting a schedule never consumes the
  outfit.
* ``mark_used`` is the only operation that moves an outfit into history, and it
  requires the filename of a generated, recorded image.
* History items are never returned by automatic selection.  An explicit
  "wear this outfit" request goes through ``reserve_manual`` which works for
  both collections and never moves a history item back to the pool.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from store import LockedJsonDictStore


STORE_FILENAME = "xiaohongshu_favorites.json"
LOCK_FILENAME = ".xiaohongshu_favorites.lock"
IMAGE_SUBDIR = "xiaohongshu_favorites"
IMAGE_URL_PREFIX = f"/local-refs/{IMAGE_SUBDIR}"
STORE_VERSION = 1

COLLECTION_FAVORITES = "favorites"
COLLECTION_HISTORY = "history"

MODE_AUTO = "auto"
MODE_MANUAL = "manual"

# A reservation whose schedule date is older than this many days can no longer
# be completed (night-tail photos run until ~02:00 of the next day), so it is
# released instead of stranding the outfit.
STALE_RESERVATION_GRACE_DAYS = 2

MAX_USAGE_ENTRIES = 200

SEASON_KEYWORDS: dict[str, tuple[str, ...]] = {
    "spring": ("春",),
    "summer": ("夏", "短袖", "吊带"),
    "autumn": ("秋", "风衣"),
    "winter": ("冬", "羽绒", "棉服", "毛呢", "围巾", "大衣"),
}
ACTIVITY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "home": ("居家", "家居", "睡衣", "宅"),
    "commute": ("通勤", "上班", "职场", "办公", "OL"),
    "date": ("约会", "浪漫"),
    "casual": ("休闲", "日常", "逛街", "出街"),
    "sport": ("运动", "健身", "瑜伽", "户外", "骑行"),
    "travel": ("旅行", "旅游", "度假", "出游"),
    "campus": ("学院", "校园", "上学", "开学", "学生"),
    "party": ("聚会", "派对", "晚宴", "演出"),
}
_SEASON_BY_MONTH = {
    12: "winter", 1: "winter", 2: "winter",
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "autumn", 10: "autumn", 11: "autumn",
}
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_WORD = re.compile(r"[a-z0-9]{3,}")


def season_for_date(value: Any) -> str:
    """Return the northern-hemisphere season for an ISO date or ``date``."""
    try:
        parsed = value if isinstance(value, date) else date.fromisoformat(str(value))
    except ValueError:
        return ""
    return _SEASON_BY_MONTH.get(parsed.month, "")


def infer_seasons(text: str) -> list[str]:
    """Infer explicit season tags; an empty list means "no seasonal claim"."""
    text = str(text or "")
    return [
        season
        for season, words in SEASON_KEYWORDS.items()
        if any(word in text for word in words)
    ]


def infer_activities(text: str) -> list[str]:
    text = str(text or "")
    return [
        activity
        for activity, words in ACTIVITY_KEYWORDS.items()
        if any(word in text for word in words)
    ]


def _tokens(text: str) -> set[str]:
    lowered = str(text or "").lower()
    found: set[str] = set(_LATIN_WORD.findall(lowered))
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            continue
        found.update(run[i:i + 2] for i in range(len(run) - 1))
    return found


def make_outfit_key(post_id: str, image_identity: str, image_sha256: str = "") -> str:
    """Stable identity for one outfit: post + image, or the image bytes."""
    post_id = str(post_id or "").strip().lower()
    image_identity = str(image_identity or "").strip().lower()
    if post_id and image_identity:
        return f"xhs:{post_id}:{image_identity}"
    if image_sha256:
        return f"img:{str(image_sha256).strip().lower()[:40]}"
    return ""


def favorite_id_for_key(outfit_key: str) -> str:
    return "xhsfav_" + hashlib.sha1(outfit_key.encode("utf-8")).hexdigest()[:12]


def build_post_url(post_id: str, xsec_token: str = "") -> str:
    post_id = str(post_id or "").strip()
    if not post_id:
        return ""
    url = f"https://www.xiaohongshu.com/explore/{post_id}"
    token = str(xsec_token or "").strip()
    if token:
        url += f"?xsec_token={token}&xsec_source=pc_feed"
    return url


class XiaohongshuFavoriteLibrary:
    """File-locked persistence and lifecycle rules for saved outfits."""

    def __init__(
        self,
        data_dir: str,
        image_dir: str,
        *,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self.data_dir = data_dir
        self.image_dir = image_dir
        self._now = now or datetime.now
        self.store = LockedJsonDictStore(
            os.path.join(data_dir, STORE_FILENAME),
            os.path.join(data_dir, LOCK_FILENAME),
        )

    # ------------------------------------------------------------------ utils
    def _stamp(self) -> str:
        return self._now().isoformat(timespec="seconds")

    def _today(self) -> date:
        return self._now().date()

    @staticmethod
    def _items(state: dict) -> dict:
        items = state.get("items")
        if not isinstance(items, dict):
            items = {}
            state["items"] = items
        state.setdefault("version", STORE_VERSION)
        if not isinstance(state.get("settings"), dict):
            state["settings"] = {}
        return items

    @staticmethod
    def _reservations(item: dict) -> dict:
        reservations = item.get("reservations")
        if not isinstance(reservations, dict):
            reservations = {}
            item["reservations"] = reservations
        return reservations

    def image_path(self, filename: str) -> str:
        """Absolute path for a library image, rejecting path traversal."""
        name = os.path.basename(str(filename or ""))
        if not name or name != filename or name.startswith("."):
            return ""
        path = os.path.join(self.image_dir, name)
        return path if os.path.isfile(path) else ""

    # ------------------------------------------------------------------ views
    @staticmethod
    def item_state(item: dict) -> str:
        if item.get("collection") == COLLECTION_HISTORY:
            return "history"
        return "reserved" if item.get("reservations") else "available"

    def public_item(self, item: dict) -> dict:
        """API representation: never exposes the stored xsec_token."""
        images = []
        for image in item.get("images") or []:
            if not isinstance(image, dict) or not image.get("filename"):
                continue
            images.append({
                "index": int(image.get("index") or 0),
                "filename": image["filename"],
                "url": f"{IMAGE_URL_PREFIX}/{image['filename']}",
                "width": int(image.get("width") or 0),
                "height": int(image.get("height") or 0),
            })
        primary = images[0] if images else {}
        usage = []
        for entry in item.get("usage") or []:
            if not isinstance(entry, dict):
                continue
            image_filename = str(entry.get("image_filename") or "")
            usage.append({
                "schedule_date": str(entry.get("schedule_date") or ""),
                "used_at": str(entry.get("used_at") or ""),
                "mode": str(entry.get("mode") or MODE_AUTO),
                "image_filename": image_filename,
                "image_url": f"/images/{image_filename}" if image_filename else "",
            })
        reservations = [
            {
                "schedule_date": schedule_date,
                "mode": str(info.get("mode") or MODE_AUTO),
                "reserved_at": str(info.get("reserved_at") or ""),
            }
            for schedule_date, info in sorted((item.get("reservations") or {}).items())
            if isinstance(info, dict)
        ]
        return {
            "id": item.get("id", ""),
            "outfit_key": item.get("outfit_key", ""),
            "collection": item.get("collection", COLLECTION_FAVORITES),
            "state": self.item_state(item),
            "title": item.get("title", ""),
            "author": item.get("author", ""),
            "post_id": item.get("post_id", ""),
            "post_url": item.get("post_url", ""),
            "image_index": int(item.get("image_index") or 0),
            "preview_url": primary.get("url", ""),
            "images": images,
            "query": item.get("query", ""),
            "description": item.get("description", ""),
            "seasons": list(item.get("seasons") or []),
            "activities": list(item.get("activities") or []),
            "saved_at": item.get("saved_at", ""),
            "updated_at": item.get("updated_at", ""),
            "save_count": int(item.get("save_count") or 1),
            "reservations": reservations,
            "usage": usage,
            "use_count": len(usage),
            "first_used_at": item.get("first_used_at", ""),
            "last_used_at": item.get("last_used_at", ""),
        }

    def list_items(self, collection: str = "") -> list[dict]:
        self.reconcile()
        state = self.store.load()
        items = [item for item in self._items(state).values() if isinstance(item, dict)]
        if collection in {COLLECTION_FAVORITES, COLLECTION_HISTORY}:
            items = [item for item in items if item.get("collection") == collection]
        if collection == COLLECTION_HISTORY:
            items.sort(key=lambda i: (i.get("last_used_at") or "", i.get("id") or ""), reverse=True)
        else:
            items.sort(key=lambda i: (i.get("saved_at") or "", i.get("id") or ""), reverse=True)
        return [self.public_item(item) for item in items]

    def counts(self) -> dict:
        state = self.store.load()
        items = [item for item in self._items(state).values() if isinstance(item, dict)]
        favorites = [i for i in items if i.get("collection") == COLLECTION_FAVORITES]
        return {
            "favorites": len(favorites),
            "available": sum(1 for i in favorites if not i.get("reservations")),
            "reserved": sum(1 for i in favorites if i.get("reservations")),
            "history": sum(1 for i in items if i.get("collection") == COLLECTION_HISTORY),
        }

    def get(self, favorite_id: str) -> dict:
        state = self.store.load()
        item = self._items(state).get(str(favorite_id or ""))
        return dict(item) if isinstance(item, dict) else {}

    def find(self, *, outfit_key: str = "", image_sha256: str = "") -> dict:
        """Return the stored item matching an outfit identity, if any."""
        outfit_key = str(outfit_key or "").strip()
        sha = str(image_sha256 or "").strip().lower()
        state = self.store.load()
        for item in self._items(state).values():
            if not isinstance(item, dict):
                continue
            if (outfit_key and item.get("outfit_key") == outfit_key) or (
                sha and item.get("image_sha256") == sha
            ):
                return dict(item)
        return {}

    def settings(self) -> dict:
        state = self.store.load()
        self._items(state)
        return {"auto_schedule": state["settings"].get("auto_schedule") is not False}

    def set_auto_schedule(self, enabled: bool) -> dict:
        def _update(state: dict) -> dict:
            self._items(state)
            state["settings"]["auto_schedule"] = bool(enabled)
            return state

        self.store.update(_update)
        return self.settings()

    # ------------------------------------------------------------------- save
    def save(self, record: dict) -> tuple[dict, bool]:
        """Insert an outfit, or return the existing one when it is a repeat.

        A repeat never changes the collection, reservations or usage of the
        stored item, so saving a historical outfit again cannot reactivate it.
        Returns ``(item, created)``.
        """
        outfit_key = str(record.get("outfit_key") or "").strip()
        image_sha = str(record.get("image_sha256") or "").strip().lower()
        if not outfit_key:
            raise ValueError("outfit_key is required")
        result: dict[str, Any] = {}

        def _update(state: dict) -> dict:
            items = self._items(state)
            now = self._stamp()
            existing = None
            for candidate in items.values():
                if not isinstance(candidate, dict):
                    continue
                if candidate.get("outfit_key") == outfit_key or (
                    image_sha and candidate.get("image_sha256") == image_sha
                ):
                    existing = candidate
                    break
            if existing is not None:
                existing["save_count"] = int(existing.get("save_count") or 1) + 1
                existing["last_saved_at"] = now
                # Only fill gaps; never overwrite what the user already has.
                for field in ("post_url", "xsec_token", "title", "author", "creator_id", "query"):
                    if not existing.get(field) and record.get(field):
                        existing[field] = record[field]
                if not existing.get("description") and record.get("description"):
                    existing["description"] = str(record["description"])[:500]
                result.update(item=existing, created=False)
                return state

            favorite_id = favorite_id_for_key(outfit_key)
            context_text = " ".join(
                str(record.get(field) or "")
                for field in ("title", "description", "query")
            )
            item = {
                "id": favorite_id,
                "outfit_key": outfit_key,
                "image_sha256": image_sha,
                "collection": COLLECTION_FAVORITES,
                "post_id": str(record.get("post_id") or ""),
                "post_url": str(record.get("post_url") or ""),
                "xsec_token": str(record.get("xsec_token") or ""),
                "creator_id": str(record.get("creator_id") or ""),
                "title": str(record.get("title") or "小红书穿搭")[:80],
                "author": str(record.get("author") or "")[:60],
                "query": str(record.get("query") or "")[:80],
                "description": str(record.get("description") or "")[:500],
                "seasons": list(record.get("seasons") or infer_seasons(context_text)),
                "activities": list(record.get("activities") or infer_activities(context_text)),
                "image_index": int(record.get("image_index") or 0),
                "images": [dict(image) for image in record.get("images") or []],
                "reservations": {},
                "usage": [],
                "save_count": 1,
                "saved_at": now,
                "updated_at": now,
            }
            items[favorite_id] = item
            result.update(item=item, created=True)
            return state

        self.store.update(_update)
        return dict(result["item"]), bool(result["created"])

    def delete(self, favorite_id: str) -> tuple[bool, str, list[str]]:
        """Remove an unused favorite.  History and reserved items are kept.

        Returns ``(removed, reason, image_filenames_to_delete)``.
        """
        outcome: dict[str, Any] = {"removed": False, "reason": "not_found", "files": []}

        def _update(state: dict) -> dict:
            items = self._items(state)
            item = items.get(str(favorite_id or ""))
            if not isinstance(item, dict):
                return state
            if item.get("collection") == COLLECTION_HISTORY:
                outcome["reason"] = "history_protected"
                return state
            if item.get("reservations"):
                outcome["reason"] = "reserved"
                return state
            items.pop(item["id"], None)
            outcome.update(
                removed=True,
                reason="",
                files=[i.get("filename") for i in item.get("images") or [] if i.get("filename")],
            )
            return state

        self.store.update(_update)
        return outcome["removed"], outcome["reason"], outcome["files"]

    # ------------------------------------------------------------- selection
    @staticmethod
    def score_item(item: dict, *, context_text: str, season: str) -> Optional[float]:
        """Suitability of ``item`` for a context; ``None`` means unsuitable."""
        seasons = list(item.get("seasons") or [])
        score = 0.0
        if seasons and season:
            if season not in seasons:
                return None
            score += 2.0
        item_text = " ".join(
            str(item.get(field) or "") for field in ("title", "description", "query")
        )
        overlap = len(_tokens(item_text) & _tokens(context_text))
        score += min(3, overlap)
        wanted = set(infer_activities(context_text))
        have = set(item.get("activities") or [])
        if wanted and have:
            score += 1.0 if wanted & have else -1.0
        return score

    def reserve_for_date(self, schedule_date: str, *, context_text: str = "") -> dict:
        """Atomically reserve the best unused favorite for ``schedule_date``.

        Idempotent per date, so retries and restarts keep the same outfit.
        Returns ``{}`` when auto-scheduling is off or nothing suits.
        """
        self.reconcile()
        season = season_for_date(schedule_date)
        outcome: dict[str, Any] = {}

        def _update(state: dict) -> dict:
            items = self._items(state)
            if state["settings"].get("auto_schedule") is False:
                return state
            for item in items.values():
                info = (item.get("reservations") or {}).get(schedule_date) if isinstance(item, dict) else None
                if isinstance(info, dict) and info.get("mode") == MODE_AUTO:
                    outcome["item"] = item
                    return state
            best = None
            for item in items.values():
                if (
                    not isinstance(item, dict)
                    or item.get("collection") != COLLECTION_FAVORITES
                    or item.get("reservations")
                ):
                    continue
                score = self.score_item(item, context_text=context_text, season=season)
                if score is None:
                    continue
                rank = (-score, item.get("saved_at") or "", item.get("id") or "")
                if best is None or rank < best[0]:
                    best = (rank, item)
            if best is None:
                return state
            item = best[1]
            self._reservations(item)[schedule_date] = {
                "mode": MODE_AUTO,
                "reserved_at": self._stamp(),
            }
            item["updated_at"] = self._stamp()
            outcome["item"] = item
            return state

        self.store.update(_update)
        return dict(outcome.get("item") or {})

    def reserve_manual(self, favorite_id: str, schedule_date: str, *, reference_filename: str = "") -> dict:
        """Record an explicit assignment; works for favorites and history."""
        outcome: dict[str, Any] = {}

        def _update(state: dict) -> dict:
            item = self._items(state).get(str(favorite_id or ""))
            if not isinstance(item, dict):
                return state
            self._reservations(item)[schedule_date] = {
                "mode": MODE_MANUAL,
                "reserved_at": self._stamp(),
                "reference_filename": reference_filename,
            }
            item["updated_at"] = self._stamp()
            outcome["item"] = item
            return state

        self.store.update(_update)
        return dict(outcome.get("item") or {})

    def release(self, schedule_date: str, *, favorite_id: str = "", reason: str = "") -> list[str]:
        """Drop reservations for a date (optionally only one favorite's)."""
        released: list[str] = []

        def _update(state: dict) -> dict:
            for item in self._items(state).values():
                if not isinstance(item, dict):
                    continue
                if favorite_id and item.get("id") != favorite_id:
                    continue
                reservations = item.get("reservations")
                if isinstance(reservations, dict) and schedule_date in reservations:
                    reservations.pop(schedule_date, None)
                    item["updated_at"] = self._stamp()
                    released.append(item["id"])
            return state

        self.store.update(_update)
        return released

    def reconcile(self, today: Optional[date] = None) -> list[str]:
        """Release reservations that can no longer be completed."""
        cutoff = (today or self._today()) - timedelta(days=STALE_RESERVATION_GRACE_DAYS)
        released: list[str] = []
        state = self.store.load()
        stale = any(
            isinstance(item, dict)
            and any(self._is_stale(day, cutoff) for day in (item.get("reservations") or {}))
            for item in self._items(state).values()
        )
        if not stale:
            return released

        def _update(current: dict) -> dict:
            for item in self._items(current).values():
                reservations = item.get("reservations") if isinstance(item, dict) else None
                if not isinstance(reservations, dict):
                    continue
                for day in [d for d in reservations if self._is_stale(d, cutoff)]:
                    reservations.pop(day, None)
                    released.append(item["id"])
            return current

        self.store.update(_update)
        return released

    @staticmethod
    def _is_stale(schedule_date: str, cutoff: date) -> bool:
        try:
            return date.fromisoformat(str(schedule_date)) < cutoff
        except ValueError:
            return True

    # ---------------------------------------------------------------- history
    def mark_used(
        self,
        favorite_id: str,
        schedule_date: str,
        image_filename: str,
        *,
        reference_filename: str = "",
    ) -> dict:
        """Move an outfit to history after its image was generated and recorded.

        Requires a real ``image_filename``; idempotent per (date, image).  A
        manual reuse of a history item appends to ``usage`` and keeps the
        original entry.
        """
        image_filename = str(image_filename or "").strip()
        if not image_filename:
            return {}
        outcome: dict[str, Any] = {}

        def _update(state: dict) -> dict:
            item = self._items(state).get(str(favorite_id or ""))
            if not isinstance(item, dict):
                return state
            reservation = (item.get("reservations") or {}).get(schedule_date)
            mode = str((reservation or {}).get("mode") or MODE_AUTO)
            usage = item.setdefault("usage", [])
            if not any(
                e.get("schedule_date") == schedule_date and e.get("image_filename") == image_filename
                for e in usage
                if isinstance(e, dict)
            ):
                now = self._stamp()
                usage.append({
                    "schedule_date": schedule_date,
                    "used_at": now,
                    "mode": mode,
                    "image_filename": image_filename,
                    "reference_filename": reference_filename,
                })
                del usage[:-MAX_USAGE_ENTRIES]
                item["last_used_at"] = now
                item.setdefault("first_used_at", now)
            item["collection"] = COLLECTION_HISTORY
            self._reservations(item).pop(schedule_date, None)
            item["updated_at"] = self._stamp()
            outcome["item"] = item
            return state

        self.store.update(_update)
        return dict(outcome.get("item") or {})

    # -------------------------------------------------------- search exclusion
    def known_outfit(self, *, post_id: str = "", image_identity: str = "", image_sha256: str = "") -> bool:
        """True when an outfit is already in the library (any collection).

        Live search uses this so it can neither re-pick a historical outfit
        nor duplicate one that is reserved or waiting in the pool.
        """
        key = make_outfit_key(post_id, image_identity)
        sha = str(image_sha256 or "").strip().lower()
        state = self.store.load()
        for item in self._items(state).values():
            if not isinstance(item, dict):
                continue
            if key and item.get("outfit_key") == key:
                return True
            if sha and item.get("image_sha256") == sha:
                return True
        return False
