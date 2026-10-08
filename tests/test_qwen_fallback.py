"""Offline tests for the optional GPT -> Qwen-Image-2.1 schedule fallback.

Every GPT and ComfyUI response here is stubbed; nothing touches the network,
the live gallery data or the WIND ComfyUI host.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
for directory in (APP_DIR, os.path.join(APP_DIR, "zhuzhu")):
    if directory not in sys.path:
        sys.path.insert(0, directory)

import generate as generate_module  # noqa: E402  (app/zhuzhu/generate.py)
import generate_scheduled as scheduled_module  # noqa: E402
import generate_gptimage as gpt  # noqa: E402
import generate_qwen as qwen  # noqa: E402
import main as main_module  # noqa: E402
import qwen_fallback as fb  # noqa: E402
from image_gen import ImageGenerator  # noqa: E402
from main import PortraitGalleryApp  # noqa: E402
from settings import (  # noqa: E402
    XIAOHONGSHU_OUTFIT_REFERENCE_MARKER,
    image_process_timeout,
    qwen_fallback_enabled,
    qwen_fallback_process_extension,
)

DEFINITE = {"kinds": ["http_503"], "terminal_reasons": [], "terminal_reason": ""}


def png_bytes(size):
    output = io.BytesIO()
    Image.new("RGB", size, "white").save(output, format="PNG")
    return output.getvalue()


class RefsMixin:
    def make_refs(self, root: Path):
        outfit = root / "xhs_outfit.png"
        face = root / "reference_face_faceonly.jpg"
        outfit.write_bytes(png_bytes((1080, 1440)))
        face.write_bytes(png_bytes((512, 512)))
        return str(outfit), str(face)


class FakeComfy:
    """Stub for generate_qwen's HTTP helpers (_json, _open, upload_reference)."""

    def __init__(self, *, queue_ahead=0, prompt_error=None, poll_error=None, history_error=False, output_size=(864, 1152)):
        self.queue_ahead = queue_ahead
        self.prompt_error = prompt_error
        self.poll_error = poll_error
        self.history_error = history_error
        self.output_size = output_size
        self.paths = []
        self.prompts = []
        self.uploads = []

    def json(self, base_url, path, body=None, timeout=15):
        self.paths.append(path)
        if path == "/system_stats":
            return {"system": {"comfyui_version": "0.37.0"}}
        if path == "/queue":
            return {"queue_running": [[0, f"r{i}"] for i in range(self.queue_ahead)], "queue_pending": []}
        if path == "/prompt":
            if self.prompt_error:
                raise self.prompt_error
            self.prompts.append(body)
            return {"prompt_id": body["prompt_id"], "number": 1, "node_errors": {}}
        if path.startswith("/history/"):
            if self.poll_error:
                raise self.poll_error
            pid = path.rsplit("/", 1)[-1]
            if self.history_error:
                return {pid: {"status": {"status_str": "error", "completed": False,
                                         "messages": [["execution_error", {"exception_message": "OOM"}]]}}}
            return {pid: {"status": {"status_str": "success", "completed": True},
                          "outputs": {"7": {"images": [{"filename": "o.png", "subfolder": "", "type": "output"}]}}}}
        raise AssertionError("unexpected path " + path)

    def open(self, base_url, path, body=None, timeout=15, **_kw):
        assert path.startswith("/view?"), path
        return io.BytesIO(png_bytes(self.output_size))

    def upload(self, base_url, data):
        with Image.open(io.BytesIO(data)) as image:
            name = f"hermes_gallery_qwen21/{image.size[0]}x{image.size[1]}.png"
        self.uploads.append(name)
        return name

    def patches(self):
        return [
            patch.object(qwen, "_json", side_effect=self.json),
            patch.object(qwen, "_open", side_effect=self.open),
            patch.object(qwen, "upload_reference", side_effect=self.upload),
            patch.object(qwen, "_base_url", return_value="http://comfy.test:8188"),
        ]


class ProviderDualReferenceTests(RefsMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.outfit, self.face = self.make_refs(Path(self.tmp.name))

    def run_provider(self, comfy, **kwargs):
        for p in comfy.patches():
            p.start()
            self.addCleanup(p.stop)
        info = {}
        result = qwen.generate_image_bytes("scene", size="1536x2048", request_info=info,
                                           ref_image=self.outfit, ref_images=[self.outfit, self.face], **kwargs)
        return result, info

    def test_outfit_is_image_1_identity_is_image_2_and_canvas_follows_outfit(self):
        comfy = FakeComfy()
        events = []
        (data, _elapsed), info = self.run_provider(comfy, on_submit=lambda pid, state: events.append((state, len(comfy.prompts))))

        graph = comfy.prompts[0]["prompt"]
        self.assertEqual(["hermes_gallery_qwen21/864x1152.png", "hermes_gallery_qwen21/512x512.png"], comfy.uploads)
        self.assertEqual("hermes_gallery_qwen21/864x1152.png", graph["20"]["inputs"]["image"])
        self.assertEqual("hermes_gallery_qwen21/512x512.png", graph["21"]["inputs"]["image"])
        self.assertEqual(["20", 0], graph["4"]["inputs"]["images.image_1"])
        self.assertEqual(["21", 0], graph["4"]["inputs"]["images.image_2"])
        self.assertEqual(["4", 2], graph["5"]["inputs"]["latent_image"])  # latent sized from Image 1
        self.assertEqual("864x1152", info["resolved_size"])  # 3:4 outfit aspect, ~1MP, no crop
        self.assertEqual(qwen.MODEL_NAME, info["model_name"])
        # id persisted BEFORE the POST, then confirmed after it
        self.assertEqual([("submitting", 0), ("queued", 1)], events)
        self.assertTrue(data)

    def test_identity_is_upscaled_only_when_requested_and_canvas_is_unchanged(self):
        comfy = FakeComfy()
        (_data, _), info = self.run_provider(comfy, upscale_secondary_references=True)
        self.assertEqual(["hermes_gallery_qwen21/864x1152.png", "hermes_gallery_qwen21/1024x1024.png"], comfy.uploads)
        self.assertEqual("864x1152", info["resolved_size"])  # Image 1 still defines the canvas
        self.assertEqual(["864x1152", "1024x1024"], info["reference_sizes"])

    def test_small_outfit_image_is_never_upscaled_so_the_canvas_policy_is_unchanged(self):
        small = Path(self.tmp.name) / "small_outfit.png"
        small.write_bytes(png_bytes((600, 800)))
        comfy = FakeComfy(output_size=(576, 800))
        for p in comfy.patches():
            p.start()
            self.addCleanup(p.stop)
        info = {}
        qwen.generate_image_bytes("scene", size="1536x2048", request_info=info, ref_image=str(small),
                                  ref_images=[str(small), self.face], upscale_secondary_references=True)
        self.assertEqual("576x800", info["resolved_size"])  # Image 1 keeps its own size (multiple of 32)
        self.assertEqual(["576x800", "1024x1024"], info["reference_sizes"])

    def test_queue_limit_refuses_before_any_upload_or_submit(self):
        comfy = FakeComfy(queue_ahead=2)
        with self.assertRaises(qwen.QwenBusyError):
            self.run_provider(comfy, max_queue_ahead=1)
        self.assertEqual([], comfy.uploads)
        self.assertEqual([], comfy.prompts)

    def test_lost_enqueue_response_is_ambiguous_and_not_resubmitted(self):
        comfy = FakeComfy(prompt_error=qwen.QwenError("connection reset"))
        events = []
        with self.assertRaises(qwen.QwenError) as ctx:
            self.run_provider(comfy, on_submit=lambda pid, state: events.append(state))
        self.assertNotIsInstance(ctx.exception, qwen.QwenJobFailed)
        self.assertIn("不要重复提交", str(ctx.exception))
        self.assertEqual(["submitting"], events)
        self.assertEqual(1, comfy.paths.count("/prompt"))

    def test_poll_failure_is_ambiguous_and_job_error_is_definite(self):
        with self.assertRaises(qwen.QwenError) as poll:
            self.run_provider(FakeComfy(poll_error=qwen.QwenError("timeout")))
        self.assertNotIsInstance(poll.exception, qwen.QwenJobFailed)
        with self.assertRaises(qwen.QwenJobFailed):
            self.run_provider(FakeComfy(history_error=True))

    def test_excess_references_are_rejected_not_truncated(self):
        third = Path(self.tmp.name) / "third.png"
        third.write_bytes(png_bytes((600, 800)))
        with self.assertRaises(ValueError):
            qwen.reference_paths(self.outfit, [self.outfit, self.face, str(third)])

    def test_standalone_generate_keeps_signature_and_records_real_model(self):
        comfy = FakeComfy()
        for p in comfy.patches():
            p.start()
            self.addCleanup(p.stop)
        saved = {}
        save_calls = []
        with patch.object(qwen, "save_image", side_effect=lambda *a, **k: save_calls.append(k) or ("/tmp/x.png", "x.png", 1)), \
             patch.object(qwen, "update_metadata", side_effect=lambda *a, **k: saved.update(k["extra_metadata"])):
            path = qwen.generate("custom", "make it blue", "auto", "custom", self.outfit, [self.outfit, self.face])
        self.assertEqual("/tmp/x.png", path)
        self.assertEqual(qwen.MODEL_NAME, saved["model_name"])
        self.assertEqual("qwen21_q8_edit", save_calls[0]["filename_theme"])  # standalone naming unchanged
        self.assertFalse(saved["fallback_used"])
        self.assertNotIn("identity_reference_upscaled", saved)  # standalone never upscales


class EligibilityTests(unittest.TestCase):
    def test_definite_failures_are_eligible(self):
        for report in (
            {"kinds": ["http_503"]},
            {"kinds": ["http_429"]},
            {"kinds": ["unavailable_channel"]},
            {"kinds": ["no_image"]},
            {"kinds": ["not_configured"]},
            {"kinds": [], "terminal_reason": "GPT Image 图片账号额度已用完"},
        ):
            self.assertTrue(fb.classify_primary_failure(report).eligible, report)

    def test_ambiguous_or_unknown_outcomes_never_fall_back(self):
        for kinds in (["timeout"], ["connection"], ["codex_edits_eof"], ["error"], ["http_504"], ["http_502"], []):
            verdict = fb.classify_primary_failure({"kinds": kinds})
            self.assertFalse(verdict.eligible, kinds)
            self.assertEqual("primary_outcome_unknown", verdict.code)

    def test_timeout_hidden_behind_a_definite_face_only_retry_still_blocks(self):
        self.assertFalse(fb.classify_primary_failure({"kinds": ["timeout", "http_503"]}).eligible)

    def test_content_policy_refusal_is_never_routed_to_qwen(self):
        verdict = fb.classify_primary_failure({"kinds": ["moderation"], "terminal_reason": "GPT Image 内容安全拦截（moderation）"})
        self.assertEqual("primary_refused_by_content_policy", verdict.code)

    def test_gpt_client_keeps_every_failure_kind_of_one_call(self):
        def fake_direct(prompt, ref_image, size, **kwargs):
            gpt._note_image_failure_kind("timeout" if len(kwargs.get("ref_images") or []) > 1 else "http_503")
            return None

        with tempfile.TemporaryDirectory() as tmpdir:
            outfit, face = Path(tmpdir) / "o.png", Path(tmpdir) / "f.png"
            outfit.write_bytes(png_bytes((300, 400)))
            face.write_bytes(png_bytes((200, 200)))
            with patch.object(gpt, "_generate_via_direct_gpt", side_effect=fake_direct), \
                 patch.object(gpt, "resolve_reference_output_size", return_value="1536x2048"):
                path = gpt.generate("custom", prompt_override="p", prompt_is_final=True,
                                    ref_image=str(outfit), ref_images=[str(outfit), str(face)], sync_gallery=False)
        self.assertIsNone(path)
        report = gpt.last_failure_report()
        self.assertEqual(["timeout", "http_503"], report["kinds"])  # face-only retry did not hide the timeout
        self.assertFalse(fb.classify_primary_failure(report).eligible)


class ReferenceAndPromptTests(RefsMixin, unittest.TestCase):
    def test_reference_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outfit, face = self.make_refs(Path(tmpdir))
            extra = Path(tmpdir) / "x.png"
            extra.write_bytes(png_bytes((400, 400)))
            self.assertEqual("text_only_request", fb.check_references("", None, False).code)
            self.assertEqual("outfit_identity_pair_required", fb.check_references(face, None, False).code)
            self.assertEqual("identity_reference_missing", fb.check_references(outfit, [outfit], True).code)
            self.assertEqual("identity_reference_missing",
                             fb.check_references(outfit, [outfit, str(Path(tmpdir) / "gone.jpg")], True).code)
            self.assertEqual("too_many_references", fb.check_references(outfit, [outfit, face, str(extra)], True).code)
            ok = fb.check_references(outfit, [outfit, face], True)
            self.assertTrue(ok.ok)
            self.assertEqual((os.path.realpath(outfit), os.path.realpath(face)), ok.refs)

    def test_qwen_prompt_assigns_explicit_roles_and_keeps_schedule_context(self):
        gpt_prompt = (
            f"Woman reading in a lakeside library at 09:00, sitting by the window. {XIAOHONGSHU_OUTFIT_REFERENCE_MARKER} "
            f"{generate_module.XIAOHONGSHU_GPT_REFERENCE_ROLES} Daylight only."
        )
        scene = generate_module._strip_gpt_reference_roles(gpt_prompt)
        prompt = fb.build_qwen_prompt(scene)

        self.assertNotIn("Strict ordered reference roles", prompt)
        self.assertNotIn(XIAOHONGSHU_OUTFIT_REFERENCE_MARKER, prompt)
        self.assertIn("lakeside library at 09:00, sitting by the window", prompt)
        self.assertIn("Daylight only.", prompt)
        self.assertIn("Picture 1 (the first input image) shows only the OUTFIT", prompt)
        self.assertIn("Picture 2 (the second input image) shows the PERSON whose face and identity", prompt)
        self.assertIn("garment types, cut and silhouette", prompt)
        self.assertIn("replace her face and head completely with the person from Picture 2", prompt)
        self.assertIn("keep none of her facial features", prompt)
        self.assertLess(prompt.index("lakeside library"), prompt.index("There are two input pictures"))


class FakeProvider:
    """Provider double for run_fallback; raises the real provider exceptions."""

    def __init__(self, *, state="absent", raise_on_generate=None, emit_submit=True):
        self.state = state
        self.raise_on_generate = raise_on_generate
        self.emit_submit = emit_submit
        self.generate_calls = []
        self.resume_calls = []
        self.saved = []

    def _configured_timeout(self):
        return 900

    def _base_url(self):
        return "http://comfy.test:8188"

    def prompt_state(self, base_url, prompt_id):
        return self.state

    def resume_image_bytes(self, prompt_id, **kwargs):
        self.resume_calls.append(prompt_id)
        return b"img", 1.0

    def generate_image_bytes(self, prompt, **kwargs):
        self.generate_calls.append({"prompt": prompt, **kwargs})
        info = kwargs["request_info"]
        info.update(width=864, height=1152, resolved_size="864x1152", model_name="qwen-image-2.1-int8-convrot",
                    comfy_base_url="http://comfy.test:8188")
        if self.emit_submit:
            info["comfy_prompt_id"] = f"pid-{len(self.generate_calls)}"
            kwargs["on_submit"](info["comfy_prompt_id"], "submitting")
        if self.raise_on_generate:
            raise self.raise_on_generate
        kwargs["on_submit"](info["comfy_prompt_id"], "queued")
        return b"img", 12.5

    def save_generated(self, data, elapsed, info, theme, prompt, source="custom", **kwargs):
        self.saved.append({"info": dict(info), "prompt": prompt, "source": source, **kwargs})
        path = os.path.join(self.out_dir, f"cron_qwen_{len(self.saved)}.png")
        Path(path).write_bytes(data)
        return path, os.path.basename(path), str(info.get("model_name") or "")


class RunFallbackTests(RefsMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.outfit, self.face = self.make_refs(root)
        self.ledger = fb.JobLedger(str(root / "jobs.json"))

    def run_fb(self, provider, *, report=DEFINITE, enabled=True, deadline=None, pair=True, refs=None, now=None):
        provider.out_dir = self.tmp.name
        kwargs = dict(
            theme="morning", scene_prompt="Reading at the lakeside library at 09:00.",
            ref_image=self.outfit, ref_images=refs if refs is not None else [self.outfit, self.face],
            outfit_identity_pair=pair, source="cron", requested_size="1536x2048",
            schedule_date="2026-10-08", schedule_time="09:00 湖边图书馆看书",
            primary_report=report, deadline=deadline,
            settings={"enabled": enabled, "max_queue_ahead": 1}, ledger=self.ledger, provider=provider,
        )
        if now is not None:
            kwargs["now"] = now
        return fb.run_fallback(**kwargs)

    def key(self):
        return fb.request_key(source="cron", theme="morning", schedule_date="2026-10-08",
                              schedule_time="09:00 湖边图书馆看书",
                              refs=(os.path.realpath(self.outfit), os.path.realpath(self.face)))

    def test_disabled_or_ineligible_makes_no_provider_calls(self):
        for kwargs in ({"enabled": False}, {"report": {"kinds": ["timeout"]}}, {"pair": False},
                       {"refs": []}, {"deadline": 1000.0, "now": lambda: 900.0}):
            provider = FakeProvider()
            outcome = self.run_fb(provider, **kwargs)
            self.assertEqual([], provider.generate_calls, kwargs)
            self.assertFalse(outcome.path, kwargs)
            self.assertFalse(outcome.uncertain, kwargs)

    def test_success_uses_ordered_pair_bounded_wait_and_records_fallback_metadata(self):
        provider = FakeProvider()
        outcome = self.run_fb(provider, deadline=1700.0, now=lambda: 1000.0)  # 700s left < qwen_timeout

        call = provider.generate_calls[0]
        self.assertEqual(os.path.realpath(self.outfit), call["ref_image"])
        self.assertEqual([os.path.realpath(self.outfit), os.path.realpath(self.face)], call["ref_images"])
        self.assertEqual(700 - fb.POST_GENERATION_RESERVE_SECONDS, call["wait_timeout"])  # never past the deadline
        self.assertEqual(1, call["max_queue_ahead"])
        self.assertIn("Picture 2 (the second input image) shows the PERSON", call["prompt"])
        self.assertTrue(call["upscale_secondary_references"])
        saved = provider.saved[0]
        meta = saved["extra_metadata"]
        self.assertEqual(("gptimage", "qwen", True), (meta["fallback_from"], meta["fallback_to"], meta["fallback_used"]))
        self.assertEqual("primary_failed_definitively", meta["fallback_reason_code"])
        self.assertEqual(["outfit", "identity"], meta["reference_roles"])
        self.assertTrue(meta["identity_reference_upscaled"])
        self.assertEqual("1536x2048", meta["primary_requested_size"])
        # same schedule-aware filename rule as the GPT path, so the gallery time slot is preserved
        self.assertEqual(fb.schedule_filename_theme("morning", "09:00 湖边图书馆看书"), saved["filename_theme"])
        self.assertEqual("qwen-image-2.1-int8-convrot", outcome.model_name)
        self.assertEqual("saved", self.ledger.get(self.key())["status"])

    def test_retry_after_success_reuses_the_saved_image_without_a_new_job(self):
        provider = FakeProvider()
        first = self.run_fb(provider)
        second = self.run_fb(provider)
        self.assertEqual(1, len(provider.generate_calls))
        self.assertEqual(first.path, second.path)
        self.assertTrue(second.reused)

    def test_ambiguous_enqueue_blocks_duplicates_until_reconciled(self):
        provider = FakeProvider(raise_on_generate=qwen.QwenError("connection reset after POST"))
        first = self.run_fb(provider)
        self.assertTrue(first.uncertain)
        self.assertEqual("qwen_outcome_unknown", first.code)
        self.assertEqual("unknown", self.ledger.get(self.key())["status"])
        self.assertEqual("pid-1", self.ledger.get(self.key())["prompt_id"])

        running = FakeProvider(state="running")
        blocked = self.run_fb(running)
        self.assertEqual("previous_qwen_job_still_running", blocked.code)
        self.assertEqual([], running.generate_calls)

        completed = FakeProvider(state="completed")
        reconciled = self.run_fb(completed)
        self.assertEqual(["pid-1"], completed.resume_calls)
        self.assertEqual([], completed.generate_calls)  # no new ComfyUI job
        self.assertTrue(reconciled.path and reconciled.reused)
        self.assertEqual(1, len(completed.saved))

    def test_never_enqueued_or_errored_job_allows_one_new_submission(self):
        for state in ("absent", "error"):
            self.ledger.put(self.key(), prompt_id="old", status="unknown")
            provider = FakeProvider(state=state)
            outcome = self.run_fb(provider)
            self.assertEqual(1, len(provider.generate_calls), state)
            self.assertTrue(outcome.path, state)
            self.ledger.put(self.key(), status="cleared", prompt_id="")

    def test_definite_qwen_failure_and_busy_queue_are_not_uncertain(self):
        failed = self.run_fb(FakeProvider(raise_on_generate=qwen.QwenJobFailed("OOM")))
        self.assertEqual("qwen_job_failed", failed.code)
        self.assertFalse(failed.uncertain)
        self.assertEqual("failed", self.ledger.get(self.key())["status"])
        busy = self.run_fb(FakeProvider(raise_on_generate=qwen.QwenBusyError("busy"), emit_submit=False))
        self.assertEqual("comfyui_queue_busy", busy.code)
        self.assertFalse(busy.uncertain)


class GenerateIntegrationTests(RefsMixin, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.outfit, self.face = self.make_refs(Path(self.tmp.name))
        self.qwen_path = str(Path(self.tmp.name) / "morning_qwen.png")
        Path(self.qwen_path).write_bytes(png_bytes((864, 1152)))

    def call(self, *, qwen_fallback, outcome):
        run = Mock(return_value=outcome)
        sync = Mock()
        gitee_gen = Mock(return_value=None)
        with patch.object(generate_module, "generate_with_gptimage", return_value=None), \
             patch.object(generate_module, "gpt_last_failure_report", return_value=DEFINITE), \
             patch.object(generate_module.qwen_fallback_module, "run_fallback", run), \
             patch.object(generate_module, "generate_with_gitee", gitee_gen), \
             patch("core.sync_to_gallery", sync):
            path = generate_module.generate(
                "custom", "gptimage", False, "在湖边图书馆看书", prompt_final=True, source="cron",
                ref_image=self.outfit, ref_images=[self.outfit, self.face], size="1536x2048",
                xiaohongshu_outfit_reference=True, qwen_fallback=qwen_fallback,
            )
        return path, run, sync, gitee_gen

    def test_opt_in_fallback_records_qwen_model_and_one_gallery_entry(self):
        outcome = fb.FallbackOutcome(path=self.qwen_path, model_name="qwen-image-2.1-int8-convrot",
                                     code="qwen_fallback_succeeded", prompt="QWEN PROMPT")
        path, run, sync, _ = self.call(qwen_fallback=True, outcome=outcome)

        self.assertEqual(self.qwen_path, path)
        kwargs = run.call_args.kwargs
        self.assertNotIn("Strict ordered reference roles", kwargs["scene_prompt"])
        self.assertIn("在湖边图书馆看书", kwargs["scene_prompt"])
        self.assertEqual([self.outfit, self.face], kwargs["ref_images"])
        self.assertTrue(kwargs["outfit_identity_pair"])
        sync.assert_called_once()
        self.assertEqual("qwen-image-2.1-int8-convrot", sync.call_args.kwargs["model_name"])
        self.assertTrue(sync.call_args.kwargs["fallback_used"])
        self.assertEqual("QWEN PROMPT", sync.call_args.kwargs["prompt"])

    def test_without_opt_in_the_existing_flow_is_unchanged(self):
        path, run, sync, gitee_gen = self.call(qwen_fallback=False, outcome=fb.FallbackOutcome())
        self.assertIsNone(path)
        run.assert_not_called()
        sync.assert_not_called()
        gitee_gen.assert_not_called()

    def test_uncertain_qwen_outcome_blocks_further_fallbacks(self):
        outcome = fb.FallbackOutcome(code="qwen_outcome_unknown", uncertain=True)
        path, _run, sync, gitee_gen = self.call(qwen_fallback=True, outcome=outcome)
        self.assertIsNone(path)
        gitee_gen.assert_not_called()
        sync.assert_not_called()

    def test_no_gitee_after_disabled_skipped_or_failed_qwen(self):
        for code in ("fallback_disabled", "missing_reference_pair", "comfyui_queue_busy", "qwen_job_failed"):
            with self.subTest(code=code):
                path, run, sync, gitee_gen = self.call(qwen_fallback=True, outcome=fb.FallbackOutcome(code=code))
                self.assertIsNone(path)
                run.assert_called_once()
                sync.assert_not_called()
                gitee_gen.assert_not_called()

    def test_gemini_failure_does_not_invoke_gitee_or_unclassified_qwen(self):
        with patch.object(generate_module, "_generate_with_gemini_cpa", return_value=None), \
             patch.object(generate_module, "generate_with_gitee") as gitee_gen, \
             patch.object(generate_module.qwen_fallback_module, "run_fallback") as run:
            path = generate_module.generate(
                "custom", "gemini", False, "在湖边图书馆看书", prompt_final=True,
                source="cron", ref_images=[self.outfit, self.face], qwen_fallback=True,
            )
        self.assertIsNone(path)
        gitee_gen.assert_not_called()
        run.assert_not_called()

    def test_scheduled_entrypoint_uses_shared_fallback_with_references(self):
        def backend(*args, **kwargs):
            print("CAPTION:湖边读书")
            return self.qwen_path

        with patch.object(scheduled_module, "generate_image", side_effect=backend) as generate:
            path, caption = scheduled_module.generate(
                "morning", ref_images=[self.outfit, self.face],
                xiaohongshu_outfit_reference=True,
                schedule_date="2026-10-08", schedule_time="08:30",
            )
        self.assertEqual((self.qwen_path, "湖边读书"), (path, caption))
        generate.assert_called_once_with(
            "morning", send=False, caption=True, source="cron", qwen_fallback=True,
            ref_images=[self.outfit, self.face], xiaohongshu_outfit_reference=True,
            schedule_date="2026-10-08", schedule_time="08:30",
        )

    def test_scheduled_cli_loads_without_external_pythonpath(self):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, str(Path(APP_DIR, "zhuzhu", "generate_scheduled.py")), "--help"],
            env=env, cwd=self.tmp.name, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("--ref-images", result.stdout)


class CallerWiringTests(RefsMixin, unittest.IsolatedAsyncioTestCase):
    def enable(self, data_dir, enabled=True):
        Path(data_dir, "plugin_config.json").write_text(json.dumps({"qwen_fallback_enabled": enabled}), encoding="utf-8")

    def test_setting_defaults_off_and_extension_follows_qwen_timeout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertFalse(qwen_fallback_enabled(tmpdir))
            self.assertEqual(0, qwen_fallback_process_extension({}, tmpdir))
            self.enable(tmpdir)
            Path(tmpdir, "api_keys_config.json").write_text(json.dumps({"qwen_timeout": 600}), encoding="utf-8")
            self.assertEqual(780, qwen_fallback_process_extension({}, tmpdir))

    def test_old_switch_migration_matches_child_and_parent_timeouts(self):
        cases = [
            ({"gitee_fallback_enabled": True}, True),
            ({"gitee_fallback_enabled": "true"}, True),
            ({"gitee_fallback_enabled": False}, False),
            ({"gitee_fallback_enabled": "false"}, False),
            ({"gitee_fallback_enabled": True, "qwen_fallback_enabled": False}, False),
            ({"gitee_fallback_enabled": False, "qwen_fallback_enabled": True}, True),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir, "plugin_config.json")
            Path(tmpdir, "api_keys_config.json").write_text(json.dumps({"qwen_timeout": 600}), encoding="utf-8")
            for config, enabled in cases:
                with self.subTest(config=config):
                    path.write_text(json.dumps(config), encoding="utf-8")
                    self.assertEqual(enabled, qwen_fallback_enabled(tmpdir))
                    self.assertEqual(enabled, fb.fallback_settings(str(path))["enabled"])
                    self.assertEqual(780 if enabled else 0, qwen_fallback_process_extension({}, tmpdir))

    async def test_image_gen_opt_in_flag_timeout_and_deadline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "generate.py").write_text("# stub", encoding="utf-8")
            out = Path(tmpdir) / "out.png"
            out.write_bytes(b"png")
            self.enable(tmpdir)
            generator = ImageGenerator(tmpdir, tmpdir, default_engine="gptimage")
            done = subprocess.CompletedProcess(args=[], returncode=0, stdout=f"SUCCESS:{out}\n", stderr="")
            with patch("image_gen.subprocess.run", return_value=done) as run:
                await generator.generate("p", ref_image="a.png", ref_images=["a.png", "b.png"], qwen_fallback=True,
                                         xiaohongshu_outfit_reference=True)
                cmd, kwargs = run.call_args.args[0], run.call_args.kwargs
                self.assertIn("--qwen-fallback", cmd)
                base = image_process_timeout({}, with_reference_fallback=True)
                self.assertEqual(base + 900 + 180, kwargs["timeout"])
                self.assertIn("ZHUZHU_PROCESS_DEADLINE", kwargs["env"])
                await generator.generate("p", ref_image="a.png", precise_edit=True, qwen_fallback=True)
                self.assertNotIn("--qwen-fallback", run.call_args.args[0])
                await generator.generate("p", ref_image="a.png", engine="qwen", qwen_fallback=True)
                self.assertNotIn("--qwen-fallback", run.call_args.args[0])
                await generator.generate("custom text only")
                self.assertNotIn("--qwen-fallback", run.call_args.args[0])

    def photo_app(self, data_dir, *, outfit=None, face=None):
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app.config = {"image_gen": {"metadata_size": "1536x2048"}}
        app.data_dir = data_dir
        app._photo_job_schedule_meta = {}
        app._slot_key_for_schedule_time = lambda _value: ("", "", "")
        app._is_photo_quiet_now = lambda: False
        app._today_schedule_entry = lambda: {"date": "2026-10-08", "outfit_style": "通勤风"}
        app._select_reference_for_generation = AsyncMock(return_value={})
        reference = {"path": outfit, "filename": os.path.basename(outfit), "label": "今日穿搭",
                     "selection_mode": "xiaohongshu_schedule"} if outfit else {}
        app.web_server = SimpleNamespace(
            ensure_xiaohongshu_schedule_reference=AsyncMock(return_value=reference),
            _preferred_xiaohongshu_identity_reference=lambda _current="": face or "",
        )
        app.image_gen = SimpleNamespace(python_executable=sys.executable, generate_script="/tmp/generate.py",
                                        script_dir="/tmp", build_env=lambda: {})
        app._photo_image_path = lambda value: value
        app._gallery_caption_for_image = lambda _path, caption: caption
        app._delivery_enabled = lambda: False
        app._set_gallery_delivery_status = Mock()
        app._failed_photo_jobs = {}
        return app

    async def test_photo_job_opts_in_with_pair_and_extends_the_stale_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outfit, face = self.make_refs(Path(tmpdir))
            self.enable(tmpdir)
            app = self.photo_app(tmpdir, outfit=outfit, face=face)
            done = SimpleNamespace(returncode=0, stdout="", stderr="")
            with patch.object(main_module.subprocess, "run", return_value=done) as run:
                await app.photo_job("morning")
            cmd, kwargs = run.call_args.args[0], run.call_args.kwargs
            self.assertIn("--qwen-fallback", cmd)
            self.assertEqual(f"{outfit},{face}", cmd[cmd.index("--ref-images") + 1])
            self.assertEqual(app._photo_job_process_timeout(), kwargs["timeout"])
            self.assertIn("ZHUZHU_PROCESS_DEADLINE", kwargs["env"])
            self.assertGreaterEqual(app._photo_job_stale_seconds(), kwargs["timeout"])

    async def test_text_only_photo_job_never_requests_qwen(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.enable(tmpdir)
            app = self.photo_app(tmpdir)
            done = SimpleNamespace(returncode=0, stdout="", stderr="")
            with patch.object(main_module.subprocess, "run", return_value=done) as run:
                await app.photo_job("morning")
            cmd = run.call_args.args[0]
            self.assertNotIn("--qwen-fallback", cmd)
            self.assertIn("--no-auto-style", cmd)


if __name__ == "__main__":
    unittest.main()
