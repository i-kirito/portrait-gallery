"""生图引擎包装 - 调用 zhuzhu-image-gen 脚本"""
import asyncio
import logging
import os
import re
import subprocess
import sys
from typing import Optional

from settings import build_child_env, configured_python, image_process_timeout, resolve_image_dir

logger = logging.getLogger(__name__)


class ImageGenerator:
    """调用本地 zhuzhu-image-gen 脚本生成图片"""

    def __init__(
        self,
        script_dir: str,
        data_dir: str,
        config: Optional[dict] = None,
        config_path: str = "",
        python_executable: str = "",
        default_engine: str = "",
    ):
        self.script_dir = os.path.abspath(os.path.expanduser(script_dir))
        self.data_dir = data_dir
        self.config = config or {}
        self.config_path = config_path
        self.python_executable = python_executable or configured_python(self.config) or sys.executable
        self.default_engine = default_engine or self.config.get("image_gen", {}).get("default_engine", "gptimage")
        self.output_dir = resolve_image_dir(self.config, data_dir)
        os.makedirs(self.output_dir, exist_ok=True)

    @property
    def generate_script(self) -> str:
        return os.path.join(self.script_dir, "generate.py")

    def set_output_dir(self, output_dir: str):
        self.output_dir = os.path.abspath(os.path.expanduser(output_dir))
        os.makedirs(self.output_dir, exist_ok=True)

    def build_env(self, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        merged = {"ZHUZHU_MEDIA_DIR": self.output_dir}
        if extra:
            merged.update(extra)
        return build_child_env(self.config, self.config_path, self.data_dir, merged)

    @staticmethod
    def _fallback_transition(stderr: str) -> Optional[tuple[str, str]]:
        text = str(stderr or "")
        markers = (
            ("GPT Image failed, falling back to Gitee", "gptimage", "gitee"),
            ("Gemini CPA failed, falling back to Gitee", "gemini", "gitee"),
            ("Gitee failed, falling back to GPT Image", "gitee", "gptimage"),
        )
        for marker, failed_engine, actual_engine in markers:
            if marker in text:
                return failed_engine, actual_engine
        return None

    @staticmethod
    def _fallback_reason(stderr: str, failed_engine: str = "") -> str:
        text = str(stderr or "")
        lower = text.lower()
        engine_label = {
            "gptimage": "GPT Image",
            "gitee": "Gitee",
            "gemini": "Gemini",
        }.get(str(failed_engine or "").strip().lower(), "生图")
        reasons = []
        if "insufficient_quota" in lower or "额度已用完" in lower:
            reasons.append(f"{engine_label} 图片账号额度已用完")
        if "deactivated_workspace" in lower or "workspace" in lower and "deactivat" in lower:
            reasons.append(f"{engine_label} 工作区已停用")
        if "no available compatible accounts" in lower:
            reasons.append(f"{engine_label} 没有可用的兼容账号")
        if "unexpected eof" in lower:
            reasons.append("上游连接意外中断")
        if any(
            marker in lower
            for marker in (
                "no available channel",
                "no available provider",
                "没有可用",
                "无可用渠道",
                "model_not_found",
            )
        ):
            reasons.append(f"上游没有可用的 {engine_label} 渠道")
        if any(marker in lower for marker in ("content_policy_violation", "safety system", "content policy")):
            reasons.append("上游内容安全策略拒绝了请求")
        if any(marker in lower for marker in ("timed out", "timeout", "read timeout")):
            reasons.append("上游请求超时")
        if reasons:
            return "；".join(dict.fromkeys(reasons))

        for raw_line in reversed(text.splitlines()):
            line = re.sub(r"\s+", " ", raw_line).strip()
            low = line.lower()
            if not line or "falling back" in low:
                continue
            if any(marker in low for marker in ("error", "failed", "failure", "unsupported")):
                return line[:500]
        return "原生图引擎未返回图片"

    async def generate(
        self,
        prompt: str,
        style: Optional[str] = None,
        engine: str = "",
        timeout: int = 0,
        ref_image: str = "",
        ref_images: Optional[list] = None,
        size: str = "",
        source: str = "custom",
        prompt_final: bool = False,
        no_auto_style: bool = False,
        theme: str = "custom",
        schedule_time: str = "",
        caption: bool = False,
        image_model: str = "",
        precise_edit: bool = False,
    ) -> Optional[str]:
        """生成图片，返回图片文件名（相对路径）（异步，不阻塞事件循环）"""
        engine = engine or self.default_engine
        if not timeout:
            timeout = image_process_timeout(self.config, with_reference_fallback=bool(style or ref_image or ref_images))
        model_label = image_model or "-"
        logger.info(
            f"开始生图: theme={theme}, engine={engine}, model={model_label}, "
            f"style={style}, size={size or '-'}, prompt={prompt}"
        )

        generate_script = self.generate_script
        if not os.path.isfile(generate_script):
            logger.error(f"生图脚本不存在: {generate_script}")
            return None

        # 构建命令
        cmd = [
            self.python_executable,
            generate_script,
            "--theme", theme or "custom",
            "--engine", engine,
            "--source", source,
        ]
        if caption:
            cmd.append("--caption")
        if style:
            cmd.extend(["--style", style])
        if ref_image:
            cmd.extend(["--ref-image", ref_image])
        if ref_images:
            cleaned = [str(x).strip() for x in ref_images if str(x or "").strip()]
            if cleaned:
                cmd.extend(["--ref-images", ",".join(cleaned)])
        if size:
            cmd.extend(["--size", size])
        if schedule_time:
            cmd.extend(["--schedule-time", schedule_time])
        if prompt_final:
            cmd.append("--prompt-final")
        if no_auto_style:
            cmd.append("--no-auto-style")
        if precise_edit:
            cmd.append("--precise-edit")
        if prompt:
            cmd.extend(["--prompt", prompt])

        try:
            child_env_extra = {"ZHUZHU_MEDIA_DIR": self.output_dir}
            if image_model and engine == "gptimage":
                child_env_extra["GPT_IMAGE_MODEL"] = image_model
            # 用 run_in_executor 避免阻塞事件循环
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout,
                    cwd=self.script_dir,
                    env=self.build_env(child_env_extra),
                )
            )
            # 检查输出路径
            output_path = None
            for line in result.stdout.split("\n"):
                line = line.strip()
                if line.startswith("SUCCESS:") or (not line.startswith("ERROR") and ".png" in line.lower()):
                    output_path = line.replace("SUCCESS:", "").strip()
                    break
                if ".png" in line or ".jpg" in line:
                    output_path = line.strip()

            fallback_transition = self._fallback_transition(result.stderr)
            if output_path and os.path.exists(output_path):
                # 复制到 gallery 目录
                filename = os.path.basename(output_path)
                dest = os.path.join(self.output_dir, filename)
                if output_path != dest:
                    import shutil
                    shutil.copy2(output_path, dest)
                if fallback_transition:
                    failed_engine, actual_engine = fallback_transition
                    logger.warning(
                        "生图引擎自动回退: requested=%s, failed=%s, actual=%s, reason=%s",
                        engine,
                        failed_engine,
                        actual_engine,
                        self._fallback_reason(result.stderr, failed_engine),
                    )
                logger.info(f"生图成功: {filename}")
                return filename
            else:
                logger.error(f"生图失败: stdout={result.stdout[:300]}, stderr={result.stderr[:300]}")
                return None

        except subprocess.TimeoutExpired:
            logger.error(f"生图超时 ({timeout}s)")
            return None
        except Exception as e:
            logger.error(f"生图异常: {e}")
            return None

    async def generate_for_outfit(
        self,
        outfit_prompt: str,
        outfit_style: str,
        base_style: str = "",
        ref_image: str = "",
        no_auto_style: bool = False,
        size: str = "",
    ) -> Optional[str]:
        """根据穿搭描述生成图片，参考图由上层选择器决定。"""
        return await self.generate(
            outfit_prompt,
            ref_image=ref_image,
            no_auto_style=no_auto_style,
            size=size,
            source="cron",
        )
