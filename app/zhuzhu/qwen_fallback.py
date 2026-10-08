"""Optional Qwen-Image-2.1 (WIND ComfyUI) fallback for scheduled GPT outfit photos.

GPT Image stays the primary provider.  This module only runs when a caller
opted in (``--qwen-fallback``: scheduled initial photos, dynamic photo jobs and
scheduled rerolls) and ``qwen_fallback_enabled`` is on in plugin_config.json
(product default: off).  It never runs for custom, character, group, precision
edit or "generate now" requests, and the standalone Qwen image-to-image API is
untouched.

Eligibility (all must hold, otherwise a clear reason is logged and nothing is
submitted):

1. **Known primary failure.**  GPT must have failed *definitively*: an explicit
   error response (4xx, 429, 500, 503, quota, auth, no channel, unsupported
   API, no image in the response, not configured).  A timeout, dropped
   connection, Codex EOF, gateway error (408/502/504/52x) or unclassified
   exception anywhere in the GPT call means the upstream may still have
   rendered and billed an image; the synchronous Images API has no task id to
   reconcile, so this is recorded as uncertain and Qwen is NOT started.  A
   content-policy refusal is never routed to another model.
2. **Outfit + identity pair.**  Exactly two references in the gallery's
   Xiaohongshu contract order: ``[outfit, identity]``.  Text-only requests,
   single profile/identity references and >2 references are skipped (never
   truncated).  ComfyUI sizes the output latent from Image 1, so the outfit
   photo must be first: its (portrait) aspect is kept and nothing is cropped.
   Prompt roles use the "Picture 1 / Picture 2" labels and treat the Picture 1
   model as a mannequin whose face is replaced; the identity reference is
   upscaled to the ~1MP budget so it is not outweighed by the outfit photo.
   (Validated on WIND: with "Image 1/2" wording and a 512px identity, the
   outfit model's face won; with these two changes the identity held.)
3. **Time budget.**  The parent passes ``ZHUZHU_PROCESS_DEADLINE``; Qwen only
   starts when enough time remains for the bounded wait plus saving and the
   caption, so the child process is never killed mid-job.
4. **Queue limit.**  Nothing is enqueued behind more than ``max_queue_ahead``
   running/pending ComfyUI jobs.

Duplicate protection: the ComfyUI prompt id is written to
``data/qwen_fallback_jobs.json`` *before* ``POST /prompt``.  A later retry of
the same slot (same source/theme/date/time/reference bytes) first reconciles
that job: an already saved image is reused, a completed job is downloaded, a
running job blocks a new submission, and only an errored or never-enqueued
job allows a new one.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import generate_qwen as qwen_provider
from core import CONFIG_PATH, SECRETARY_GALLERY_DIR, _DATA_DIR, schedule_filename_theme
from settings import resolve_qwen_fallback_enabled

LEDGER_FILENAME = "qwen_fallback_jobs.json"
LEDGER_RETENTION_SECONDS = 48 * 3600
MIN_QWEN_BUDGET_SECONDS = 180
POST_GENERATION_RESERVE_SECONDS = 90  # save, visual caption, gallery sync
RECONCILE_WAIT_SECONDS = 60
DEFAULT_MAX_QUEUE_AHEAD = 1

AMBIGUOUS_KINDS = frozenset({"timeout", "connection", "codex_edits_eof", "error"})
AMBIGUOUS_HTTP_STATUS = frozenset({408, 502, 504, 520, 522, 524})
POLICY_KINDS = frozenset({"moderation"})

ROLE_ORDER = ("outfit", "identity")

QWEN_REFERENCE_ROLES = (
    "There are two input pictures. Picture 1 (the first input image) shows only the OUTFIT. "
    "Picture 2 (the second input image) shows the PERSON whose face and identity must appear in the result. "
    "Generate the person from Picture 2 - same face shape, eyes, eyebrows, nose, mouth, skin tone, fringe and overall "
    "likeness - wearing the exact clothes from Picture 1: garment types, cut and silhouette, length, colors and patterns, "
    "fabric texture, layering, shoes, bag and accessories. "
    "The woman in Picture 1 is only a mannequin for the clothes: replace her face and head completely with the person from "
    "Picture 2 and keep none of her facial features, expression or hairstyle. "
    "Scene, activity, pose, time of day, lighting, props and camera framing follow the description above; the hairstyle "
    "follows the description, otherwise keep Picture 2's hairstyle; body proportions follow the gallery character description."
)


# ---------------------------------------------------------------------------
# Settings and eligibility
# ---------------------------------------------------------------------------
def fallback_settings(config_path: str = CONFIG_PATH) -> dict:
    """``qwen_fallback_enabled`` (default False) and ``qwen_fallback_max_queue_ahead``."""
    data: dict = {}
    try:
        with open(config_path, encoding="utf-8") as handle:
            loaded = json.load(handle)
        data = loaded if isinstance(loaded, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    enabled = resolve_qwen_fallback_enabled(data)
    try:
        max_queue_ahead = max(0, min(4, int(data.get("qwen_fallback_max_queue_ahead", DEFAULT_MAX_QUEUE_AHEAD))))
    except (TypeError, ValueError):
        max_queue_ahead = DEFAULT_MAX_QUEUE_AHEAD
    return {"enabled": enabled, "max_queue_ahead": max_queue_ahead}


@dataclass(frozen=True)
class PrimaryFailure:
    eligible: bool
    code: str
    detail: str = ""


def _http_status(kind: str) -> int:
    if kind.startswith("http_"):
        try:
            return int(kind[5:])
        except ValueError:
            return 0
    return 0


def classify_primary_failure(report: Optional[dict]) -> PrimaryFailure:
    """Decide whether the GPT failure is known well enough to start Qwen."""
    report = report if isinstance(report, dict) else {}
    kinds = [str(k).strip().lower() for k in report.get("kinds") or [] if str(k or "").strip()]
    terminal = [str(r) for r in (report.get("terminal_reasons") or []) if r]
    if report.get("terminal_reason"):
        terminal.append(str(report["terminal_reason"]))
    terminal_text = " ".join(terminal)
    lowered = terminal_text.lower()
    if any(k in POLICY_KINDS for k in kinds) or "moderation" in lowered or "内容安全" in terminal_text:
        return PrimaryFailure(False, "primary_refused_by_content_policy", terminal_text or ",".join(kinds))
    if "reference_unavailable" in kinds or "参考图不可用" in terminal_text:
        return PrimaryFailure(False, "reference_unavailable", terminal_text)
    ambiguous = [k for k in kinds if k in AMBIGUOUS_KINDS or _http_status(k) in AMBIGUOUS_HTTP_STATUS]
    if ambiguous:
        return PrimaryFailure(
            False,
            "primary_outcome_unknown",
            "GPT may still have rendered/billed upstream (no task id to reconcile): " + ",".join(ambiguous),
        )
    if not kinds and not terminal:
        return PrimaryFailure(False, "primary_outcome_unknown", "no failure classification recorded")
    return PrimaryFailure(True, "primary_failed_definitively", terminal_text or ",".join(kinds))


@dataclass(frozen=True)
class ReferenceCheck:
    ok: bool
    code: str
    detail: str = ""
    refs: tuple = ()


def check_references(ref_image: Optional[str], ref_images: Optional[list], outfit_identity_pair: bool) -> ReferenceCheck:
    """Require exactly the [outfit, identity] pair; never drop extra references."""
    ordered: list[str] = []
    for value in ([ref_image] if ref_image else []) + list(ref_images or []):
        text = str(value or "").strip()
        if not text:
            continue
        resolved = os.path.realpath(os.path.expanduser(text))
        if resolved not in ordered:
            ordered.append(resolved)
    if not ordered:
        return ReferenceCheck(False, "text_only_request", "Qwen is image-to-image only; text-only requests are not rerouted")
    if not outfit_identity_pair:
        return ReferenceCheck(
            False,
            "outfit_identity_pair_required",
            "only the [outfit, identity] schedule contract is supported; a single profile/identity reference "
            "would make Image 1 (and the output canvas) a face crop",
        )
    if len(ordered) > len(ROLE_ORDER):
        return ReferenceCheck(False, "too_many_references", f"{len(ordered)} references; WIND limit is {qwen_provider.MAX_REFERENCES}, none are discarded")
    if len(ordered) < len(ROLE_ORDER):
        return ReferenceCheck(False, "identity_reference_missing", "the configured identity reference is required as Image 2")
    missing = [name for name, path in zip(ROLE_ORDER, ordered) if not os.path.isfile(path)]
    if missing:
        return ReferenceCheck(False, "identity_reference_missing" if "identity" in missing else "outfit_reference_missing",
                              "missing file for: " + ",".join(missing))
    return ReferenceCheck(True, "ok", refs=tuple(ordered))


def build_qwen_prompt(scene_prompt: str) -> str:
    """Scheduled description + explicit Qwen image-role contract."""
    base = str(scene_prompt or "").strip()
    return f"{base} {QWEN_REFERENCE_ROLES}".strip()


def time_budget(deadline: Optional[float], configured_timeout: int, now: Callable[[], float] = time.time) -> int:
    """Seconds Qwen may wait; never past the parent's process deadline."""
    if deadline is None:
        return int(configured_timeout)
    remaining = int(deadline - now() - POST_GENERATION_RESERVE_SECONDS)
    return max(0, min(int(configured_timeout), remaining))


def deadline_from_env() -> Optional[float]:
    try:
        value = float(os.environ.get("ZHUZHU_PROCESS_DEADLINE", "") or 0)
    except ValueError:
        return None
    return value or None


# ---------------------------------------------------------------------------
# Job ledger
# ---------------------------------------------------------------------------
def request_key(*, source: str, theme: str, schedule_date: str, schedule_time: str, refs: tuple) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps([source, theme, schedule_date, schedule_time], ensure_ascii=False).encode("utf-8"))
    for path in refs:
        with open(path, "rb") as handle:
            digest.update(hashlib.sha256(handle.read()).digest())
    return digest.hexdigest()[:32]


class JobLedger:
    """Small file-locked JSON ledger of fallback jobs (prompt ids and outcomes)."""

    def __init__(self, path: str, now: Callable[[], float] = time.time):
        self.path = path
        self.lock_path = path + ".lock"
        self._now = now

    def _read(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def get(self, key: str) -> dict:
        entry = self._read().get(key)
        if not isinstance(entry, dict):
            return {}
        if self._now() - float(entry.get("updated_at") or 0) > LEDGER_RETENTION_SECONDS:
            return {}
        return dict(entry)

    def put(self, key: str, **fields: Any) -> dict:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.lock_path, "a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                data = self._read()
                cutoff = self._now() - LEDGER_RETENTION_SECONDS
                data = {
                    k: v for k, v in data.items()
                    if isinstance(v, dict) and float(v.get("updated_at") or 0) >= cutoff
                }
                entry = dict(data.get(key) or {})
                entry.update(fields)
                entry["updated_at"] = self._now()
                entry.setdefault("created_at", entry["updated_at"])
                data[key] = entry
                tmp = f"{self.path}.{os.getpid()}.tmp"
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
                return dict(entry)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def default_ledger() -> JobLedger:
    return JobLedger(os.path.join(str(_DATA_DIR), LEDGER_FILENAME))


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
@dataclass
class FallbackOutcome:
    path: str = ""
    model_name: str = ""
    code: str = ""
    detail: str = ""
    submitted: bool = False
    # True when a ComfyUI job may still produce an image: callers must not
    # start any further generation for this request.
    uncertain: bool = False
    reused: bool = False
    prompt: str = ""
    info: dict = field(default_factory=dict)


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _existing_output(entry: dict) -> str:
    for candidate in (entry.get("path"), os.path.join(SECRETARY_GALLERY_DIR, str(entry.get("filename") or ""))):
        if candidate and entry.get("filename") and os.path.isfile(candidate):
            return candidate
    return ""


def run_fallback(
    *,
    theme: str,
    scene_prompt: str,
    ref_image: Optional[str],
    ref_images: Optional[list],
    outfit_identity_pair: bool,
    source: str,
    requested_size: str,
    schedule_date: str,
    schedule_time: str,
    primary_report: Optional[dict],
    deadline: Optional[float] = None,
    settings: Optional[dict] = None,
    ledger: Optional[JobLedger] = None,
    provider: Any = qwen_provider,
    now: Callable[[], float] = time.time,
) -> FallbackOutcome:
    """Try the Qwen fallback once; never submits when the outcome is uncertain."""
    settings = settings if settings is not None else fallback_settings()
    if not settings.get("enabled"):
        return FallbackOutcome(code="fallback_disabled")

    primary = classify_primary_failure(primary_report)
    if not primary.eligible:
        _log(f"QWEN_FALLBACK_SKIPPED reason={primary.code} detail={primary.detail}")
        return FallbackOutcome(code=primary.code, detail=primary.detail)

    refs = check_references(ref_image, ref_images, outfit_identity_pair)
    if not refs.ok:
        _log(f"QWEN_FALLBACK_SKIPPED reason={refs.code} detail={refs.detail}")
        return FallbackOutcome(code=refs.code, detail=refs.detail)

    configured_timeout = provider._configured_timeout()
    budget = time_budget(deadline, configured_timeout, now)
    if budget < MIN_QWEN_BUDGET_SECONDS:
        detail = f"{budget}s left before the parent deadline (minimum {MIN_QWEN_BUDGET_SECONDS}s)"
        _log(f"QWEN_FALLBACK_SKIPPED reason=insufficient_time_budget detail={detail}")
        return FallbackOutcome(code="insufficient_time_budget", detail=detail)

    ledger = ledger or default_ledger()
    key = request_key(source=source, theme=theme, schedule_date=schedule_date,
                      schedule_time=schedule_time, refs=refs.refs)
    qwen_prompt = build_qwen_prompt(scene_prompt)
    fallback_metadata = {
        "requested_engine": "gptimage",
        "fallback_used": True,
        "fallback_from": "gptimage",
        "fallback_to": "qwen",
        "fallback_reason": primary.detail,
        "fallback_reason_code": primary.code,
        "primary_failure_kinds": list((primary_report or {}).get("kinds") or []),
        "reference_roles": list(ROLE_ORDER),
        "identity_reference_upscaled": True,
        "primary_requested_size": requested_size or "",
        "size_policy": "preserve_first_reference_aspect (outfit image), ~1MP WIND budget",
        "schedule_date": schedule_date,
        "schedule_time": schedule_time,
        "qwen_fallback_key": key,
    }
    filename_theme = schedule_filename_theme(theme, schedule_time)

    def _save(data: bytes, elapsed: float, info: dict, reused_prompt: bool) -> FallbackOutcome:
        metadata = dict(fallback_metadata, qwen_fallback_reconciled=reused_prompt)
        path, filename, model_name = provider.save_generated(
            data, elapsed, info, theme, qwen_prompt, source,
            ref_image=refs.refs[0], filename_theme=filename_theme, extra_metadata=metadata,
        )
        ledger.put(key, status="saved", path=path, filename=filename, model_name=model_name,
                   prompt_id=str(info.get("comfy_prompt_id") or ""))
        _log(f"QWEN_FALLBACK_SAVED file={filename} prompt_id={info.get('comfy_prompt_id')} reconciled={reused_prompt}")
        return FallbackOutcome(path=path, model_name=model_name, code="qwen_fallback_succeeded",
                               detail=primary.detail, submitted=not reused_prompt, reused=reused_prompt,
                               prompt=qwen_prompt, info=info)

    # 1) Reconcile an earlier attempt for the same slot before submitting.
    previous = ledger.get(key)
    if previous.get("status") == "saved":
        existing = _existing_output(previous)
        if existing:
            _log(f"QWEN_FALLBACK_REUSED file={previous.get('filename')} prompt_id={previous.get('prompt_id')}")
            return FallbackOutcome(path=existing, model_name=str(previous.get("model_name") or ""),
                                   code="qwen_fallback_reused_saved_output", reused=True, prompt=qwen_prompt,
                                   info={"comfy_prompt_id": previous.get("prompt_id", "")})
    previous_id = str(previous.get("prompt_id") or "")
    if previous_id and previous.get("status") in {"submitting", "queued", "unknown"}:
        try:
            state = provider.prompt_state(provider._base_url(), previous_id)
        except qwen_provider.QwenError as exc:
            detail = f"previous prompt_id={previous_id} could not be checked: {exc}"
            _log(f"QWEN_FALLBACK_SKIPPED reason=cannot_reconcile_previous_job detail={detail}")
            return FallbackOutcome(code="cannot_reconcile_previous_job", detail=detail, uncertain=True)
        if state in {"running", "pending"}:
            detail = f"prompt_id={previous_id} is still {state}; not submitting a duplicate"
            _log(f"QWEN_FALLBACK_SKIPPED reason=previous_qwen_job_still_running detail={detail}")
            return FallbackOutcome(code="previous_qwen_job_still_running", detail=detail, uncertain=True)
        if state == "completed":
            info: dict = {}
            try:
                data, elapsed = provider.resume_image_bytes(
                    previous_id, width=previous.get("width") or 0, height=previous.get("height") or 0,
                    request_info=info, wait_timeout=min(RECONCILE_WAIT_SECONDS, budget),
                )
            except qwen_provider.QwenError as exc:
                ledger.put(key, status="unknown", last_error=str(exc)[:500])
                return FallbackOutcome(code="cannot_reconcile_previous_job", detail=str(exc)[:500], uncertain=True)
            info.update({k: previous[k] for k in ("model_name", "resolved_size", "width", "height") if previous.get(k)})
            return _save(data, elapsed, info, True)
        ledger.put(key, status="failed" if state == "error" else "absent")

    # 2) Submit one new job (prompt id persisted before POST /prompt).
    info = {}

    def _on_submit(prompt_id: str, state: str) -> None:
        ledger.put(key, prompt_id=prompt_id, status=state, source=source, theme=theme,
                   schedule_date=schedule_date, schedule_time=schedule_time,
                   width=info.get("width"), height=info.get("height"),
                   resolved_size=info.get("resolved_size"), model_name=info.get("model_name"),
                   base_url=info.get("comfy_base_url"))

    _log(f"GPT Image failed, falling back to Qwen (reason={primary.detail[:200]})")
    try:
        data, elapsed = provider.generate_image_bytes(
            qwen_prompt, size=requested_size or "", request_info=info,
            ref_image=refs.refs[0], ref_images=list(refs.refs),
            wait_timeout=budget, on_submit=_on_submit, max_queue_ahead=settings.get("max_queue_ahead"),
            upscale_secondary_references=True,
        )
    except qwen_provider.QwenBusyError as exc:
        _log(f"QWEN_FALLBACK_SKIPPED reason=comfyui_queue_busy detail={exc}")
        return FallbackOutcome(code="comfyui_queue_busy", detail=str(exc))
    except qwen_provider.QwenJobFailed as exc:
        if info.get("comfy_prompt_id"):
            ledger.put(key, status="failed", last_error=str(exc)[:500])
        _log(f"QWEN_FALLBACK_FAILED reason=qwen_job_failed detail={exc}")
        return FallbackOutcome(code="qwen_job_failed", detail=str(exc)[:500], submitted=bool(info.get("comfy_prompt_id")))
    except qwen_provider.QwenError as exc:
        submitted = bool(info.get("comfy_prompt_id"))
        if submitted:
            ledger.put(key, status="unknown", last_error=str(exc)[:500])
        code = "qwen_outcome_unknown" if submitted else "qwen_unavailable"
        _log(f"QWEN_FALLBACK_FAILED reason={code} prompt_id={info.get('comfy_prompt_id', '')} detail={exc}")
        return FallbackOutcome(code=code, detail=str(exc)[:500], submitted=submitted, uncertain=submitted)
    except (ValueError, OSError) as exc:
        _log(f"QWEN_FALLBACK_SKIPPED reason=invalid_reference detail={exc}")
        return FallbackOutcome(code="invalid_reference", detail=str(exc)[:500])
    return _save(data, elapsed, info, False)
