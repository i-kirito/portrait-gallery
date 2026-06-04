from typing import Optional
#!/usr/bin/env python3
"""Gitee image generation backend for zhuzhu-image-gen."""
import argparse
import base64
import sys
import time

from core import (
    MAX_RETRIES,
    REQUEST_SESSION,
    RETRYABLE_STATUS,
    RETRY_DELAY_SECONDS,
    build_caption,
    build_prompt,
    sync_to_gallery,
    get_gitee_key,
    save_image,
    send_photo,
    update_metadata,
)

ENGINE_URL = "https://ai.gitee.com/v1/images/generations"
MODEL_NAME = "z-image-turbo"



def generate_image_bytes(prompt: str):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {get_gitee_key()}"}
    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "size": "1536x2048",
        "response_format": "b64_json",
    }

    start = time.time()
    for attempt in range(MAX_RETRIES):
        try:
            resp = REQUEST_SESSION.post(ENGINE_URL, headers=headers, json=payload, timeout=90, verify=False)
            if resp.status_code in RETRYABLE_STATUS:
                time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                continue
            if resp.status_code != 200:
                print(f"Gitee API error {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
                return None
            img_data = base64.b64decode(resp.json()["data"][0]["b64_json"])
            return img_data, round(time.time() - start, 2)
        except Exception as e:
            print(f"Gitee attempt {attempt + 1} error: {e}", file=sys.stderr)
            time.sleep(RETRY_DELAY_SECONDS)
    return None



def generate(theme: str, send: bool = False, caption: bool = False, prompt_override: Optional[str] = None):
    prompt = build_prompt(theme, prompt_override)
    result = generate_image_bytes(prompt)
    if not result:
        return None

    img_data, gen_time = result
    path, filename, ts = save_image(img_data, theme, MODEL_NAME)
    update_metadata(filename, theme, prompt, MODEL_NAME, ts, gen_time)
    sync_to_gallery(path, filename, theme, prompt=prompt, model_name=MODEL_NAME, source="cron")

    caption_text = None
    if caption:
        caption_text = build_caption(theme)

    if send:
        send_photo(path, caption_text)

    print(f"SUCCESS:{path}")
    if caption_text:
        print(f"CAPTION:{caption_text}")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gitee z-image-turbo 生图")
    parser.add_argument("--theme", choices=["morning", "noon", "evening", "bedtime", "sexy", "custom"], default="sexy")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--caption", action="store_true")
    parser.add_argument("--prompt", type=str, default=None, help="自定义完整 prompt（自动注入前缀+外貌）")
    args = parser.parse_args()
    path = generate(args.theme, args.send, args.caption, args.prompt)
    if not path:
        print("ERROR: Gitee generation failed", file=sys.stderr)
        sys.exit(1)
