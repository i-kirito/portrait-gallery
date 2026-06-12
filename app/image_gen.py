"""生图引擎包装 - 调用 zhuzhu-image-gen 脚本"""
import asyncio
import logging
import os
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

    async def generate(
        self,
        prompt: str,
        style: Optional[str] = None,
        engine: str = "",
        timeout: int = 0,
        ref_image: str = "",
        size: str = "",
        source: str = "custom",
        prompt_final: bool = False,
    ) -> Optional[str]:
        """生成图片，返回图片文件名（相对路径）（异步，不阻塞事件循环）"""
        engine = engine or self.default_engine
        if not timeout:
            timeout = image_process_timeout(self.config, with_reference_fallback=bool(style or ref_image))
        logger.info(f"开始生图: engine={engine}, style={style}, size={size or '-'}, prompt={prompt[:80]}...")

        generate_script = self.generate_script
        if not os.path.isfile(generate_script):
            logger.error(f"生图脚本不存在: {generate_script}")
            return None

        # 构建命令
        cmd = [
            self.python_executable,
            generate_script,
            "--theme", "custom",
            "--engine", engine,
            "--source", source,
        ]
        if style:
            cmd.extend(["--style", style])
        if ref_image:
            cmd.extend(["--ref-image", ref_image])
        if size:
            cmd.extend(["--size", size])
        if prompt_final:
            cmd.append("--prompt-final")
        cmd.extend(["--prompt", prompt])

        try:
            # 用 run_in_executor 避免阻塞事件循环
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout,
                    cwd=self.script_dir,
                    env=self.build_env({"ZHUZHU_MEDIA_DIR": self.output_dir}),
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

            if output_path and os.path.exists(output_path):
                # 复制到 gallery 目录
                filename = os.path.basename(output_path)
                dest = os.path.join(self.output_dir, filename)
                if output_path != dest:
                    import shutil
                    shutil.copy2(output_path, dest)
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
    ) -> Optional[str]:
        """根据穿搭描述生成图片，使用 LLM 选出的当天底模。"""
        style_value = (base_style or "").strip().lower()
        style = style_value if style_value in {"cool", "girly", "sweet"} else None
        return await self.generate(outfit_prompt, style=style)
