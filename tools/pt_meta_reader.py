#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pt_meta_reader.py - H3 .pt conditioning cache 元数据读取工具

用途：
    读取 H3 .pt conditioning cache 文件的元数据，供 GUI 通过 subprocess 调用。
    GUI 侧用 ComfyUI 嵌入式 Python 执行本脚本，解析 stdout 的 JSON 即可展示
    .pt 缓存文件的时长、分辨率、帧率、帧数、提示词预览、参考图数量等信息。

.pt 文件结构（torch.save 的对象）：
    {
        "conditioning": <data>,
        "metadata": {
            "duration": float,       # 镜头时长（秒）
            "width": int,            # 视频宽度
            "height": int,           # 视频高度
            "frame_rate": int,       # 帧率（通常 24）
            "frame_count": int,      # 帧数
            "prompt": str,           # 提示词（新版本字段，可选）
            "ref_image_count": int,  # 参考图数量（新版本字段，可选）
        },
        "ref_images_data": [...]     # 参考图压缩字节（顶层，不在 metadata 内）
    }

用法：
    python pt_meta_reader.py <pt_file_or_dir> [--json]

参数：
    pt_file_or_dir  .pt 文件路径，或包含 .pt 文件的目录（递归扫描）
    --json          以紧凑单行 JSON 输出（默认为缩进格式化 JSON）

输出字段（每个 .pt 文件一个对象）：
    filename         文件名
    path             文件绝对路径
    size_mb          文件大小（MB，保留两位小数）
    duration         时长（秒），旧格式为 null
    width            宽度，旧格式为 null
    height           高度，旧格式为 null
    frame_rate       帧率，旧格式为 null
    frame_count      帧数，缺失时按公式计算
    has_prompt       是否包含 prompt 字段
    prompt_preview   prompt 前 200 字符，无则为 null
    ref_image_count  参考图数量
    error            加载失败时的错误信息（仅出错时存在）

注意：
    本脚本需要在有 torch 的 Python 环境下运行（如 ComfyUI 的嵌入式 Python）。
    GUI 通过 subprocess 调用本脚本，解析 stdout 的 JSON 获取结果。
"""

import argparse
import json
import os
import sys


# ── 帧数计算 ─────────────────────────────────────────────────────────

def _calculate_frame_count(duration, fps):
    """根据时长和帧率计算帧数。

    公式: max(5, round(duration*fps)) + (5 - (max(5, round(duration*fps)) % 17)) % 17
    确保 frame_count >= 5 且 frame_count % 17 == 5（与 H3 模型对齐要求一致）。
    """
    base = max(5, round(duration * fps))
    adjustment = (5 - (base % 17)) % 17
    return base + adjustment


# ── metadata 提取 ────────────────────────────────────────────────────

def _extract_conditioning_and_meta(data):
    """从 torch.load 加载的数据中提取 metadata。

    逻辑：
    - 如果 data 是 dict 且同时包含 "conditioning" 和 "metadata" 键，
      返回 data["metadata"]。
    - 否则返回 None（旧格式 .pt 无 metadata 结构）。
    """
    if isinstance(data, dict) and "conditioning" in data and "metadata" in data:
        return data["metadata"]
    return None


# ── 单文件元数据读取 ─────────────────────────────────────────────────

def _read_pt_metadata(pt_path):
    """读取单个 .pt 文件的元数据。

    返回包含元数据信息的字典。如果加载失败，返回包含 error 字段的字典，
    不会抛出异常。
    """
    result = {
        "filename": os.path.basename(pt_path),
        "path": os.path.abspath(pt_path),
    }

    # 文件大小
    try:
        size_bytes = os.path.getsize(pt_path)
        result["size_mb"] = round(size_bytes / (1024 * 1024), 2)
    except Exception:
        result["size_mb"] = None

    # 加载 .pt 文件（torch 延迟导入，避免无 torch 环境下脚本无法启动）
    try:
        import torch
        data = torch.load(pt_path, map_location="cpu", weights_only=False)
    except Exception as e:
        result["error"] = f"加载失败: {e}"
        return result

    # 提取 metadata
    try:
        metadata = _extract_conditioning_and_meta(data)
    except Exception as e:
        result["error"] = f"提取 metadata 失败: {e}"
        return result

    if metadata is None:
        # 旧格式 .pt，无 metadata 结构
        result["duration"] = None
        result["width"] = None
        result["height"] = None
        result["frame_rate"] = None
        result["frame_count"] = None
        result["has_prompt"] = False
        result["prompt_preview"] = None
        result["ref_image_count"] = 0
        return result

    # 确保 metadata 是 dict
    if not isinstance(metadata, dict):
        result["error"] = f"metadata 类型异常: {type(metadata).__name__}"
        return result

    # 提取基本字段
    duration = metadata.get("duration")
    width = metadata.get("width")
    height = metadata.get("height")
    frame_rate = metadata.get("frame_rate")
    frame_count = metadata.get("frame_count")

    # 如果 metadata 中没有 frame_count，用公式计算
    if frame_count is None and duration is not None and frame_rate is not None:
        try:
            duration_f = float(duration)
            fps = int(frame_rate)
            frame_count = _calculate_frame_count(duration_f, fps)
        except (ValueError, TypeError):
            pass

    # prompt 相关
    has_prompt = "prompt" in metadata and metadata["prompt"] is not None
    prompt_preview = None
    if has_prompt:
        prompt_str = str(metadata["prompt"])
        prompt_preview = prompt_str[:200]

    # 参考图数量：优先读 metadata["ref_image_count"]，
    # 其次读 metadata["ref_images"]（可能是 list），
    # 最后读顶层 data["ref_images_data"] 的长度
    ref_image_count = 0
    if "ref_image_count" in metadata and metadata["ref_image_count"] is not None:
        try:
            ref_image_count = int(metadata["ref_image_count"])
        except (ValueError, TypeError):
            ref_image_count = 0
    elif "ref_images" in metadata and metadata["ref_images"] is not None:
        ref_images = metadata["ref_images"]
        if isinstance(ref_images, list):
            ref_image_count = len(ref_images)
        else:
            try:
                ref_image_count = int(ref_images)
            except (ValueError, TypeError):
                ref_image_count = 0
    elif isinstance(data, dict) and "ref_images_data" in data:
        ref_images_data = data["ref_images_data"]
        if isinstance(ref_images_data, list):
            ref_image_count = len(ref_images_data)

    result["duration"] = duration
    result["width"] = width
    result["height"] = height
    result["frame_rate"] = frame_rate
    result["frame_count"] = frame_count
    result["has_prompt"] = has_prompt
    result["prompt_preview"] = prompt_preview
    result["ref_image_count"] = ref_image_count

    return result


# ── 目录扫描 ─────────────────────────────────────────────────────────

def _scan_pt_files(directory):
    """递归扫描目录下的所有 .pt 文件，返回排序后的路径列表。"""
    pt_files = []
    for root, _dirs, files in os.walk(directory):
        for f in files:
            if f.lower().endswith(".pt"):
                pt_files.append(os.path.join(root, f))
    pt_files.sort()
    return pt_files


# ── 主入口 ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="读取 H3 .pt conditioning cache 文件的元数据"
    )
    parser.add_argument(
        "path",
        help=".pt 文件路径或包含 .pt 文件的目录",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以紧凑单行 JSON 输出（默认为缩进格式化 JSON）",
    )
    args = parser.parse_args()

    target = args.path

    # 判断是文件还是目录
    if os.path.isdir(target):
        pt_files = _scan_pt_files(target)
    elif os.path.isfile(target):
        pt_files = [target]
    else:
        # 路径不存在
        error_result = [{
            "filename": os.path.basename(target),
            "path": os.path.abspath(target),
            "error": f"路径不存在: {target}",
        }]
        print(json.dumps(error_result, ensure_ascii=False))
        sys.exit(1)

    if not pt_files:
        # 目录下没有 .pt 文件
        print(json.dumps([], ensure_ascii=False))
        return

    # 读取每个文件的元数据
    results = []
    for pt_file in pt_files:
        meta = _read_pt_metadata(pt_file)
        results.append(meta)

    # 输出 JSON
    if args.json:
        print(json.dumps(results, ensure_ascii=False))
    else:
        print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
