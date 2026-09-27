"""Image-to-image-only Qwen-Image-2.1 Q8 provider using WIND ComfyUI."""
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from PIL import Image, ImageOps

from core import get_image_model, save_image, update_metadata

MODEL_NAME = "qwen-image-2.1-Q8_0"
UNET_NAME = "qwen_image_2.1-Q8_0.gguf"
CLIP_NAME = "qwen3vl_8b_nvfp4_heretic.safetensors"
VAE_NAME = "qwen_image_2.1_vae_bf16.safetensors"
MAX_PIXELS = 1024 * 1024
MAX_REFERENCES = 2
MAX_REFERENCE_BYTES = 32 * 1024 * 1024


class QwenError(RuntimeError):
    pass


def resolve_size(size="", reference_size=None):
    """Keep the first reference's aspect; size is an optional pixel budget."""
    value = str(size or "").strip().lower()
    budget = MAX_PIXELS
    if value not in {"", "auto"}:
        match = re.fullmatch(r"(\d+)x(\d+)", value)
        if not match:
            raise ValueError("Qwen size 必须为 auto 或 WIDTHxHEIGHT；输出沿用首张参考图比例。")
        requested_width, requested_height = map(int, match.groups())
        if not (256 <= requested_width <= 8192 and 256 <= requested_height <= 8192):
            raise ValueError("Qwen 请求宽高须在 256–8192 之间，输出不超过约 1MP。")
        budget = min(budget, requested_width * requested_height)
    if reference_size is None:
        raise ValueError("Qwen 图生图需要参考图尺寸，不能创建空白画布。")
    width, height = reference_size
    if min(width, height) < 32 or width * height > 64 * 1024 * 1024:
        raise ValueError("参考图边长至少32像素，总像素不得超过64MP。")
    if max(width, height) > min(width, height) * 4:
        raise ValueError("Qwen 当前参考图宽高比支持 1:4 至 4:1。")
    scale = min(1.0, math.sqrt(budget / (width * height)))
    return max(32, int(width * scale) // 32 * 32), max(32, int(height * scale) // 32 * 32)


def validate_settings(prompt, seed=None, steps=25):
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Qwen 编辑指令不能为空。")
    if isinstance(seed, bool) or isinstance(steps, bool):
        raise ValueError("seed 和 steps 必须是整数。")
    seed = secrets.randbelow(2**32) if seed is None else int(seed)
    steps = int(steps)
    if not 0 <= seed < 2**64 or not 1 <= steps <= 50:
        raise ValueError("Qwen seed 须为 uint64，steps 须为 1–50。")
    return seed, steps


def reference_paths(ref_image=None, ref_images=None):
    if ref_images is not None and not isinstance(ref_images, (list, tuple)):
        raise ValueError("ref_images 必须是参考图路径数组。")
    paths = []
    for value in ([ref_image] if ref_image else []) + list(ref_images or []):
        if not str(value or "").strip():
            continue
        path = Path(value).expanduser().resolve()
        if path not in paths:
            paths.append(path)
    if not paths:
        raise ValueError("Qwen 只支持图生图，请提供 ref_image 或 ref_images；不会改成文生图。")
    if len(paths) > MAX_REFERENCES:
        raise ValueError(f"WIND 12GB 图生图目前最多接收 {MAX_REFERENCES} 张参考图，不会丢弃多余图片。")
    for path in paths:
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_REFERENCE_BYTES:
            raise ValueError(f"参考图不存在、为空或超过32MiB：{path.name}")
    return paths


def prepare_reference(path, size=""):
    """Decode locally, honor EXIF rotation, and send only normalized image pixels."""
    with Image.open(path) as source:
        source.verify()
    with Image.open(path) as source:
        if getattr(source, "is_animated", False):
            raise ValueError("请使用静态参考图，不能静默取动图的第一帧。")
        image = ImageOps.exif_transpose(source)
        target = resolve_size(size, image.size)
        image = image.convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB")
        if image.size != target:
            image = image.resize(target, Image.Resampling.LANCZOS)
        image.info.clear()
        output = io.BytesIO()
        image.save(output, format="PNG")
    return output.getvalue(), target


def _base_url():
    value = str(os.getenv("QWEN_COMFYUI_URL") or get_image_model("qwen_base_url", "")).strip().rstrip("/")
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise QwenError("请在 image_gen.qwen_base_url 配置 WIND ComfyUI 的 HTTP 地址。")
    return value


def _open(base_url, path, body=None, timeout=15, *, raw_data=None, content_type="application/json"):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = raw_data if raw_data is not None else (None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8"))
    request = urllib.request.Request(base_url + path, data=data, headers={"Content-Type": content_type})
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read(4000).decode("utf-8", errors="replace")
        raise QwenError(f"WIND ComfyUI HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise QwenError(f"WIND ComfyUI 不可达或响应超时（{base_url}）：{exc}；请检查后台任务与局域网，不要重复提交。") from exc


def _json(base_url, path, body=None, timeout=15):
    with _open(base_url, path, body, timeout) as response:
        return json.load(response)


def upload_reference(base_url, data):
    boundary = "qwen_" + uuid.uuid4().hex
    filename = "reference_" + uuid.uuid4().hex + ".png"
    parts = []
    for name, value in (("type", "input"), ("subfolder", "hermes_gallery_qwen21"), ("overwrite", "false")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\nContent-Type: image/png\r\n\r\n'.encode(), data, f'\r\n--{boundary}--\r\n'.encode()])
    with _open(base_url, "/upload/image", timeout=60, raw_data=b"".join(parts), content_type=f"multipart/form-data; boundary={boundary}") as response:
        result = json.load(response)
    name = str(result.get("name") or "")
    subfolder = str(result.get("subfolder") or "")
    if not name or "/" in name or "\\" in name or subfolder != "hermes_gallery_qwen21" or result.get("type") != "input":
        raise QwenError(f"WIND 参考图上传响应异常：{result}")
    return subfolder + "/" + name


def health():
    base_url = _base_url()
    stats = _json(base_url, "/system_stats", timeout=5)
    info = _json(base_url, "/object_info/UnetLoaderGGUF", timeout=5)
    names = info.get("UnetLoaderGGUF", {}).get("input", {}).get("required", {}).get("unet_name", [[]])[0]
    if UNET_NAME not in names:
        raise QwenError(f"WIND 未加载所需 Q8 模型：{UNET_NAME}")
    encode = _json(base_url, "/object_info/TextEncodeQwenImage21", timeout=5)
    if "images" not in encode.get("TextEncodeQwenImage21", {}).get("input", {}).get("required", {}):
        raise QwenError("WIND 缺少 Qwen-Image-2.1 图像条件输入。")
    queue = _json(base_url, "/queue", timeout=5)
    return {"status": "ok", "engine": "qwen", "model": MODEL_NAME, "base_url": base_url,
            "comfyui_version": stats.get("system", {}).get("comfyui_version"),
            "queue_running": len(queue.get("queue_running", [])), "queue_pending": len(queue.get("queue_pending", [])),
            "max_pixels": MAX_PIXELS, "max_references": MAX_REFERENCES,
            "text_to_image": False, "image_to_image": True, "reference_required": True,
            "size_policy": "preserve_first_reference_aspect"}


def build_workflow(prompt, size="", seed=None, steps=25, *, reference_images=None, reference_size=None):
    seed, steps = validate_settings(prompt, seed, steps)
    if not isinstance(reference_images, (list, tuple)) or not 1 <= len(reference_images) <= MAX_REFERENCES or not all(reference_images):
        raise ValueError("Qwen 图生图工作流必须包含1–2张已上传的参考图。")
    width, height = resolve_size(size, reference_size)
    graph = {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": UNET_NAME}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP_NAME, "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VAE_NAME}},
        "4": {"class_type": "TextEncodeQwenImage21", "inputs": {"clip": ["2", 0], "vae": ["3", 0], "prompt": prompt, "negative_prompt": "", "resolution": 0}},
        "8": {"class_type": "QwenImage21Cache", "inputs": {"model": ["1", 0], "device": "auto", "dtype": "default"}},
        "5": {"class_type": "KSampler", "inputs": {"model": ["8", 0], "seed": seed, "steps": steps, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "positive": ["4", 0], "negative": ["4", 1], "latent_image": ["4", 2], "denoise": 1.0}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["3", 0]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "Hermes_Gallery_Qwen21/img2img"}},
    }
    for index, name in enumerate(reference_images, 1):
        node_id = str(19 + index)
        graph[node_id] = {"class_type": "LoadImage", "inputs": {"image": name}}
        graph["4"]["inputs"][f"images.image_{index}"] = [node_id, 0]
    return graph, width, height


def generate_image_bytes(prompt, size="", request_info=None, seed=None, steps=25, *, ref_image=None, ref_images=None):
    paths = reference_paths(ref_image, ref_images)
    seed, steps = validate_settings(prompt, seed, steps)
    # Validate every source before uploading anything or submitting a task.
    prepared = [prepare_reference(path, size) for path in paths]
    base_url = _base_url()
    _json(base_url, "/system_stats", timeout=5)
    uploaded = [upload_reference(base_url, data) for data, _ in prepared]
    graph, width, height = build_workflow(prompt, seed=seed, steps=steps, reference_images=uploaded, reference_size=prepared[0][1])
    timeout = max(60, min(1800, int(get_image_model("qwen_timeout", "900"))))
    info = request_info if request_info is not None else {}
    info.update(submitted_prompt=prompt, requested_size=size or "auto", resolved_size=f"{width}x{height}",
                width=width, height=height, seed=seed, steps=steps, generation_mode="img2img",
                quantization="Q8_0", text_encoder=CLIP_NAME, comfy_base_url=base_url,
                ref_image=str(paths[0]), ref_image_path=str(paths[0]), ref_images=[str(p) for p in paths],
                reference_count=len(paths), upstream_references=uploaded,
                reference_sha256=[hashlib.sha256(data).hexdigest() for data, _ in prepared],
                size_policy="preserve_first_reference_aspect")
    prompt_id = str(uuid.uuid4())
    info["comfy_prompt_id"] = prompt_id
    started = time.monotonic()
    try:
        queued = _json(base_url, "/prompt", {"prompt": graph, "prompt_id": prompt_id, "client_id": str(uuid.uuid4())}, timeout=30)
    except QwenError as exc:
        raise QwenError(f"Qwen 提交响应失败，任务 {prompt_id} 可能已入队；先查询该ID，不要重复提交。{exc}") from exc
    if queued.get("node_errors") or not queued.get("prompt_id"):
        raise QwenError(f"Qwen 工作流校验失败：{queued}")
    prompt_id = queued["prompt_id"]
    info["comfy_prompt_id"] = prompt_id
    print(f"QWEN_QUEUED:{prompt_id} mode=img2img references={len(paths)} size={width}x{height}", file=sys.stderr, flush=True)
    while time.monotonic() - started < timeout:
        try:
            item = _json(base_url, "/history/" + prompt_id, timeout=15).get(prompt_id)
        except QwenError as exc:
            raise QwenError(f"Qwen 查询失败，任务 {prompt_id} 可能仍在运行；不要重新提交。{exc}") from exc
        if item:
            status = item.get("status", {})
            if status.get("status_str") == "error":
                details = [m[1] for m in status.get("messages", []) if m[0] in {"execution_error", "execution_interrupted"}]
                raise QwenError(f"Qwen 生成失败，prompt_id={prompt_id}: {json.dumps(details, ensure_ascii=False)[:3500]}")
            if status.get("completed"):
                outputs = [image for node in item.get("outputs", {}).values() for image in node.get("images", []) if image.get("type") == "output"]
                if not outputs:
                    raise QwenError(f"Qwen 任务 {prompt_id} 完成但没有保存图片。")
                params = {key: outputs[0].get(key, "") for key in ("filename", "subfolder", "type")}
                with _open(base_url, "/view?" + urllib.parse.urlencode(params), timeout=60) as response:
                    data = response.read(64 * 1024 * 1024 + 1)
                if len(data) > 64 * 1024 * 1024:
                    raise QwenError("Qwen 输出超过 64 MiB，未保存。")
                with Image.open(io.BytesIO(data)) as image:
                    if image.size != (width, height):
                        raise QwenError(f"Qwen 输出尺寸异常：{image.size}，预期 {(width, height)}")
                    image.verify()
                info["upstream_output"] = params
                return data, round(time.monotonic() - started, 3)
        time.sleep(2)
    raise QwenError(f"Qwen 等待超过 {timeout} 秒，prompt_id={prompt_id}，任务可能仍在运行；超时不自动取消或重复提交。")


def generate(theme, prompt, size="", source="custom", ref_image=None, ref_images=None):
    info = {}
    data, elapsed = generate_image_bytes(prompt, size=size, request_info=info, ref_image=ref_image, ref_images=ref_images)
    path, filename, created_at = save_image(data, theme, MODEL_NAME, target_size=info["resolved_size"], filename_theme="qwen21_q8_edit")
    update_metadata(filename, theme, prompt, MODEL_NAME, created_at, elapsed, extra_metadata={
        **info, "model_name": MODEL_NAME, "size": info["resolved_size"], "source": source,
        "engine": "qwen", "generation_mode": "img2img", "requested_generation_mode": "img2img",
        "custom_ref_mode": "reference", "requested_ref_image": str(ref_image or ""),
        "fallback_used": False, "fallback_from": "", "fallback_to": "",
    })
    return path
