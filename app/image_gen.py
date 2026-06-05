"""生图引擎包装 - 调用 zhuzhu-image-gen 脚本"""
import asyncio
import logging
import os
import subprocess
import time
from typing import Optional

logger = logging.getLogger(__name__)


class ImageGenerator:
    """调用本地 zhuzhu-image-gen 脚本生成图片"""

    def __init__(self, script_dir: str, data_dir: str):
        self.script_dir = script_dir
        self.data_dir = data_dir
        self.output_dir = os.path.join(data_dir, "images")
        os.makedirs(self.output_dir, exist_ok=True)

    async def generate(
        self,
        prompt: str,
        style: Optional[str] = None,
        engine: str = "gptimage",
        timeout: int = 300,
        ref_image: str = "",
        size: str = "",
    ) -> Optional[str]:
        """生成图片，返回图片文件名（相对路径）（异步，不阻塞事件循环）"""
        logger.info(f"开始生图: engine={engine}, style={style}, prompt={prompt[:80]}...")

        # 构建命令
        cmd = [
            "python3",
            f"{self.script_dir}/generate.py",
            "--theme", "custom",
            "--engine", engine,
        ]
        if style:
            cmd.extend(["--style", style])
        if ref_image:
            cmd.extend(["--ref-image", ref_image])
        if size:
            cmd.extend(["--size", size])
        cmd.extend(["--prompt", prompt])

        try:
            # 用 run_in_executor 避免阻塞事件循环
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout,
                    env={**os.environ, "ZHUZHU_MEDIA_DIR": self.output_dir},
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
    ) -> Optional[str]:
        """根据穿搭描述生成图片，自动选择引擎和风格"""
        style_map = {
            "冷御风": "cool",
            "甜美风": "sweet",
            "元气风": "girly",
            "温柔风": "sweet",
            "优雅风": "cool",
            "休闲风": "girly",
            "酷飒风": "cool",
            "清新风": "sweet",
            "性感风": "cool",
            "复古风": "cool",
        }
        style = style_map.get(outfit_style, None)
        return await self.generate(outfit_prompt, style=style)
