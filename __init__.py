import os
import sys
import asyncio
import subprocess
from pathlib import Path

from aiohttp import web
from server import PromptServer

from .h3_conditioning_cache import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
from .h3_for_loop import (
    NODE_CLASS_MAPPINGS as LOOP_NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS as LOOP_NODE_DISPLAY_NAME_MAPPINGS,
)

NODE_CLASS_MAPPINGS.update(LOOP_NODE_CLASS_MAPPINGS)
NODE_DISPLAY_NAME_MAPPINGS.update(LOOP_NODE_DISPLAY_NAME_MAPPINGS)

# Web directory for the frontend "懒" GUI launcher button
WEB_DIRECTORY = "./web"

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]

# ---------------------------------------------------------------------------
# AI剧生产套件 GUI launcher
#
# Adds a GET /h3_tools/launch route that opens h3_tools_gui.py (Tkinter) on the
# host machine. The frontend registers a "懒" menu button that calls this route.
# ---------------------------------------------------------------------------

_NODE_DIR = Path(__file__).resolve().parent

# Python interpreters that have tkinter + openpyxl installed. The GUI needs a
# full desktop Python; the ComfyUI embedded interpreter usually lacks tkinter.
_PYTHON_CANDIDATES = [
    r"C:\Users\psp75\AppData\Local\Programs\Python\Python312\python.exe",
    sys.executable,
]

# Candidate locations for h3_tools_gui.py (node-bundled tools/ preferred).
_GUI_CANDIDATES = [
    _NODE_DIR / "tools" / "h3_tools_gui.py",
    _NODE_DIR.parent / "ComfyUI-H3-Conditioning-Cache" / "tools" / "h3_tools_gui.py",
    Path(r"F:\01登黄项目资产\ComfyUI-H3-Conditioning-Cache-AI-Drama-Production-Suite\tools\h3_tools_gui.py"),
    Path(r"F:\03技术探索\ComfyUI-H3-Conditioning-Cache\tools\h3_tools_gui.py"),
]


def _find_gui_script() -> Path:
    for candidate in _GUI_CANDIDATES:
        if candidate.is_file():
            return candidate
    return _GUI_CANDIDATES[0]


def _find_python() -> str:
    for candidate in _PYTHON_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    return sys.executable


@PromptServer.instance.routes.get("/h3_tools/launch")
async def launch_h3_tools(request: web.Request) -> web.Response:
    """Launch the AI剧生产套件 Tkinter GUI on the host machine."""
    script = _find_gui_script()
    if not script.is_file():
        return web.json_response(
            {"error": f"GUI 脚本不存在: {script}"}, status=404
        )

    python = _find_python()
    try:
        proc = subprocess.Popen(
            [python, str(script)],
            cwd=str(script.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        return web.json_response({"status": "ok", "pid": proc.pid})
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"error": str(exc)}, status=500)


# ---------------------------------------------------------------------------
# Pick .pt file via native file dialog
#
# Adds a POST /h3cache/pick_pt route. The frontend "浏览 .pt 文件" button on the
# H3LoadConditioning node calls this to open a native folder/file picker on the
# host machine, so users can load a .pt from anywhere, not just the cache dirs.
# ---------------------------------------------------------------------------

def _pick_pt_file():
    """Open a native file dialog and return the chosen absolute path (or '')."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"tkinter 不可用: {exc}") from exc

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        path = filedialog.askopenfilename(
            title="选择 .pt 条件缓存文件",
            filetypes=[
                ("PyTorch 缓存", "*.pt"),
                ("PyTorch 权重", "*.pth"),
                ("所有文件", "*.*"),
            ],
        )
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
    return path or ""


@PromptServer.instance.routes.post("/h3cache/pick_pt")
async def pick_pt(request: web.Request) -> web.Response:
    """Open the native file dialog to locate a .pt conditioning cache file."""
    try:
        path = await asyncio.to_thread(_pick_pt_file)
        return web.json_response({"path": path})
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"error": str(exc)}, status=500)
