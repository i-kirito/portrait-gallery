#!/usr/bin/env python3
"""
DRY-RUN 版本 — 展示优化后的 z-image-turbo API 调用参数
不实际发送请求，只输出最终 prompt 和 payload

用法：
  python dryrun_gitee_v2.py --theme custom --prompt "描述"
  python dryrun_gitee_v2.py --theme morning
"""
import random
import json

from core import APPEARANCE, THEMES, get_image_model

# ─── 优化配置 ────────────────────────────────────────────────────────────────

NEGATIVE_PROMPT = (
    "blurry, low quality, distorted, deformed, ugly, bad anatomy, "
    "extra fingers, extra limbs, mutated hands, watermark, text, "
    "oversaturated, underexposed, noisy, grainy, cropped, worst quality, "
    "bad proportions, disfigured, poorly drawn face, fused fingers, "
    "long neck, cross-eyed"
)

GUIDANCE_SCALE = 8.0       # 默认 7.5，稍高更贴 prompt
NUM_INFERENCE_STEPS = 30   # Turbo 默认 25，加 5 步提升细节

QUALITY_BOOSTERS = (
    "masterpiece, best quality, highly detailed, 8K resolution, "
    "sharp focus, professional photography, cinematic lighting, "
    "film grain, RAW photo"
)


def build_optimized_prompt(theme: str, extra_prompt: str = None) -> str:
    """四段式结构：主体 → 风格 → 细节 → 质量（全英文）"""
    theme_cfg = THEMES.get(theme, THEMES["morning"])

    hair = random.choice(theme_cfg["hair"])
    clothing = random.choice(theme_cfg["clothing"])
    pose = random.choice(theme_cfg["pose"])
    environment = random.choice(theme_cfg.get("env", theme_cfg.get("environment")))
    lighting = random.choice(theme_cfg.get("light", theme_cfg.get("lighting")))

    if extra_prompt:
        body = extra_prompt
    else:
        body = (
            f"Her hair is {hair}. "
            f"She is {pose}. "
            f"She is wearing {clothing}. "
            f"Background: {environment}. "
            f"Lighting: {lighting}."
        )

    return f"{APPEARANCE} {body} {QUALITY_BOOSTERS}."


def dry_run(theme: str, extra_prompt: str = None):
    prompt = build_optimized_prompt(theme, extra_prompt)

    payload = {
        "model": get_image_model("gitee_model"),
        "prompt": prompt,
        "negative_prompt": NEGATIVE_PROMPT,
        "size": "1024x1024",
        "response_format": "b64_json",
        "guidance_scale": GUIDANCE_SCALE,
        "num_inference_steps": NUM_INFERENCE_STEPS,
    }

    print("=" * 70)
    print(f"🎨 Gitee dry run — theme: {theme}")
    print("=" * 70)
    print(f"\n📝 PROMPT ({len(prompt)} chars):")
    print("-" * 40)
    print(prompt)
    print(f"\n🚫 NEGATIVE PROMPT:")
    print("-" * 40)
    print(NEGATIVE_PROMPT)
    print(f"\n⚙️ PARAMETERS:")
    for k, v in payload.items():
        if k not in ("prompt", "negative_prompt"):
            print(f"  {k}: {v}")
    print(f"\n{'=' * 70}")
    print(f"✅ DRY RUN 完成 — 未发送任何 API 请求")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--theme", default="custom")
    parser.add_argument("--prompt", default=None)
    args = parser.parse_args()
    dry_run(args.theme, args.prompt)
