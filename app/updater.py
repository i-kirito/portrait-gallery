#!/usr/bin/env python3
"""在线更新：git pull + 重启"""
import subprocess
import sys
import os


def _restart_process():
    """Restart the current process without relying on Unix-only execv."""
    if sys.platform.startswith("win"):
        subprocess.Popen([sys.executable] + sys.argv, close_fds=True)
        os._exit(0)
    os.execv(sys.executable, [sys.executable] + sys.argv)


def update():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        ["git", "pull", "origin", "main"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {"success": False, "error": result.stderr}
    return {"success": True, "output": result.stdout}


def restart():
    """重启当前进程"""
    _restart_process()
