#!/usr/bin/env python3
"""
excel_to_multi_chain_json.py
============================
提示词审阅表 Excel → 多链生产 JSON

读取用户审阅后的提示词审阅表 Excel，按主角色分组生成多链预编码 JSON。
同一图片只 Load 一次，连到多个 MiniMaxH3ReferenceToVideo（合并 LoadImage）。

用法:
    python excel_to_multi_chain_json.py <审阅表.xlsx> <输出目录> [--by-shot]

参数:
    审阅表.xlsx   — shot_md_to_excel.py 生成、用户审阅后的 Excel
    输出目录       — 多链 JSON 输出路径（如 preencode_by_char/）
    --by-shot      — 可选，按镜头顺序而非按角色分组（每镜一个JSON）
"""

import json
import sys
import os
import re
import argparse
from pathlib import Path

try:
    from openpyxl import load_workbook
except ImportError:
    print("ERROR: openpyxl not found. Install with: pip install openpyxl")
    sys.exit(1)


def sanitize_filename(name):
    """清理文件名中的非法字符，截断过长名称。

    Windows 非法字符: \\ / : * ? " < > |
    同时去除 Excel 单元格中可能混入的 Markdown 格式残留（**、| 等）。
    """
    # 去掉 Markdown 粗体标记和管道符分隔的多余内容
    name = re.sub(r'\*\*', '', name)
    # 如果名称中包含 |，只取第一部分（通常是角色名）
    if '|' in name:
        name = name.split('|')[0].strip()
    # 替换 Windows 非法字符为下划线
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    # 去除首尾空格和点
    name = name.strip().strip('.')
    # 截断过长名称
    if len(name) > 50:
        name = name[:50]
    return name if name else "unnamed"


# ── 常量 ─────────────────────────────────────────────────────────────

# MiniMaxH3ReferenceToVideo 的 ref_image 槽位对应的 input 索引
SLOT_INPUT = {0: 3, 1: 4, 2: 5, 3: 13, 4: 14, 5: 15}

# 共享节点模板
CLIP_MODEL = "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"
VIDEO_VAE = "minimax_h3_video_vae_fp16.safetensors"
AUDIO_VAE = "minimax_h3_audio_vae_fp32.safetensors"


# ── ID 管理器 ─────────────────────────────────────────────────────────

class IDGen:
    def __init__(self, start=1):
        self._next = start

    def node(self):
        n = self._next
        self._next += 1
        return n

    def link(self):
        n = self._next
        self._next += 1
        return n


# ── 节点工厂 ─────────────────────────────────────────────────────────

def make_node(nid, ntype, title, pos, size, widgets_values=None,
              inputs=None, outputs=None, color=None, bgcolor=None):
    node = {
        "id": nid,
        "type": ntype,
        "pos": list(pos),
        "size": list(size),
        "flags": {},
        "order": 0,
        "mode": 0,
        "inputs": inputs or [],
        "outputs": outputs or [],
        "properties": {"Node name for S&R": ntype},
        "widgets_values": widgets_values or [],
        "title": title,
    }
    if color:
        node["color"] = color
    if bgcolor:
        node["bgcolor"] = bgcolor
    return node


def make_output(name, dtype, links=None, slot=0):
    return {"name": name, "type": dtype, "links": links or [], "slot_index": slot}


def make_input(name, dtype, link=None, widget_name=None):
    inp = {"name": name, "type": dtype, "link": link}
    if widget_name:
        inp["widget"] = {"name": widget_name}
    return inp


# ── Excel 读取 ───────────────────────────────────────────────────────

def read_excel(xlsx_path):
    wb = load_workbook(xlsx_path, data_only=True)

    # Sheet1: 提示词审阅
    ws1 = wb["提示词审阅"]
    headers = [cell.value for cell in ws1[1]]

    shots = []
    for row in ws1.iter_rows(min_row=2, values_only=True):
        if not row[0]:
            continue
        shot = {}
        for i, h in enumerate(headers):
            if h and i < len(row):
                shot[h] = str(row[i]).strip() if row[i] else ""
        shots.append(shot)

    # Sheet2: 美术资产路径
    asset_paths = {}
    if "美术资产路径" in wb.sheetnames:
        ws2 = wb["美术资产路径"]
        for row in ws2.iter_rows(min_row=2, values_only=True):
            if not row[0] or not row[1]:
                continue
            asset_type = str(row[0]).strip()
            asset_name = str(row[1]).strip()
            input_path = str(row[2]).strip() if row[2] else ""
            if input_path:
                asset_paths[(asset_type, asset_name)] = input_path

    return shots, asset_paths


# ── 提示词改写 ───────────────────────────────────────────────────────

def rewrite_prompt(prompt, char, scene, prop, sup_chars):
    """在提示词前插入参考句，并替换角色名为图片引用。"""
    refs = []

    # 参考句
    if char and char != "(纯场景)":
        refs.append(f"使用<Picture 1>作为{char}的身份参考（驱动其身份）")
    if scene:
        refs.append(f"使用<Picture 2>作为{scene}场景环境参考")
    if char and prop and char != "(纯场景)":
        refs.append(f"使用<Picture 3>作为{prop}道具参考，图片1中的角色佩戴<图片3>中的{prop}。")

    # 配角参考句
    pic_idx = 4
    for sup in sup_chars:
        if sup:
            refs.append(f"使用<Picture {pic_idx}>作为{sup}的身份参考（驱动其身份）。")
            pic_idx += 1

    ref_str = "，".join(refs)
    if ref_str:
        ref_str += "。"

    # 简单替换：主角色名 → 图片1中的角色
    result = prompt
    if char and char != "(纯场景)":
        result = result.replace(char, "图片1中的角色")

    # 配角名 → 图片N中的{配角名}
    pic_idx = 4
    for sup in sup_chars:
        if sup and sup in result:
            result = result.replace(sup, f"图片{pic_idx}中的{sup}")
            pic_idx += 1

    return ref_str + result


# ── 多链 JSON 生成 ───────────────────────────────────────────────────

def build_shared_nodes(idgen, resolution=0.5):
    """创建循环体外共享节点，返回节点列表和输出引用。"""
    nodes = []

    # CLIPLoader
    clip_id = idgen.node()
    nodes.append(make_node(
        clip_id, "CLIPLoader", "H3 CLIP (Qwen3VL 32B)",
        [-1800, -400], [300, 82],
        widgets_values=[CLIP_MODEL, "minimax", "default"],
        outputs=[make_output("CLIP", "CLIP", links=[], slot=0)],
        color="#322", bgcolor="#533",
    ))

    # VAELoader (Video)
    vae_v_id = idgen.node()
    nodes.append(make_node(
        vae_v_id, "VAELoader", "Video VAE",
        [-1800, -300], [300, 82],
        widgets_values=[VIDEO_VAE],
        outputs=[make_output("VAE", "VAE", links=[], slot=0)],
        color="#322", bgcolor="#533",
    ))

    # VAELoader (Audio)
    vae_a_id = idgen.node()
    nodes.append(make_node(
        vae_a_id, "VAELoader", "Audio VAE",
        [-1800, -200], [300, 82],
        widgets_values=[AUDIO_VAE],
        outputs=[make_output("VAE", "VAE", links=[], slot=0)],
        color="#322", bgcolor="#533",
    ))

    # ResolutionSelector (共享，全组一个)
    res_id = idgen.node()
    nodes.append(make_node(
        res_id, "ResolutionSelector", f"Resolution (Shared, {resolution})",
        [-1800, -100], [300, 82],
        widgets_values=["16:9 (Widescreen)", float(resolution), 32],
        outputs=[
            make_output("width", "INT", links=[], slot=0),
            make_output("height", "INT", links=[], slot=1),
        ],
        color="#322", bgcolor="#533",
    ))

    refs = {
        "clip_id": clip_id, "clip_out": 0,
        "vae_v_id": vae_v_id, "vae_v_out": 0,
        "vae_a_id": vae_a_id, "vae_a_out": 0,
        "res_id": res_id, "res_w_out": 0, "res_h_out": 1,
    }
    return nodes, refs


def build_loadimage(idgen, asset_name, image_path, x, y):
    """创建一个 LoadImage 节点。"""
    nid = idgen.node()
    node = make_node(
        nid, "LoadImage", f"Load {asset_name}",
        [x, y], [300, 82],
        widgets_values=[image_path, "image"],
        outputs=[
            make_output("IMAGE", "IMAGE", links=[], slot=0),
            make_output("MASK", "MASK", links=[], slot=1),
        ],
        color="#322", bgcolor="#533",
    )
    return nid, node


def build_loadaudio(idgen, asset_name, audio_path, x, y):
    """创建一个 LoadAudio 节点。"""
    nid = idgen.node()
    node = make_node(
        nid, "LoadAudio", f"Load Audio ({asset_name})",
        [x, y], [300, 82],
        widgets_values=[audio_path],
        outputs=[make_output("AUDIO", "AUDIO", links=[], slot=0)],
        color="#223", bgcolor="#335",
    )
    return nid, node


def build_primitive_float(idgen, duration, x, y):
    """创建 PrimitiveFloat 节点（时长）。"""
    nid = idgen.node()
    node = make_node(
        nid, "PrimitiveFloat", f"Duration ({duration}s)",
        [x, y], [300, 82],
        widgets_values=[float(duration)],
        outputs=[make_output("FLOAT", "FLOAT", links=[], slot=0)],
        color="#322", bgcolor="#533",
    )
    return nid, node


def build_comfy_math(idgen, float_id, float_out, x, y):
    """创建 ComfyMathExpression 节点（帧数计算）。"""
    nid = idgen.node()
    link_id = idgen.link()
    node = make_node(
        nid, "ComfyMathExpression", "Frame Count",
        [x, y], [300, 82],
        inputs=[make_input("values.a", "FLOAT", link=link_id)],
        widgets_values=["max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17"],
        outputs=[
            make_output("FLOAT", "FLOAT", links=[], slot=0),
            make_output("INT", "INT", links=[], slot=1),
            make_output("BOOL", "BOOL", links=[], slot=2),
        ],
        color="#322", bgcolor="#533",
    )
    return nid, node, link_id


def _extract_number(val, default):
    """从字符串中提取首个数字，用于时长/分辨率等含单位的值（如 '11秒' → 11.0）。"""
    if val is None:
        return default
    m = re.search(r"-?\d+(?:\.\d+)?", str(val))
    if m:
        return float(m.group(0))
    return default


def build_shot_chain(idgen, shot, shared_refs, load_nodes, asset_paths, x_offset, y_offset):
    """为单个镜头创建一条链：easy promptLine → MiniMaxH3ReferenceToVideo → H3SaveConditioning。"""
    nodes = []
    links = []

    shot_id = shot.get("镜头编号", "unknown")
    duration = _extract_number(shot.get("时长", "5"), 5.0)

    # 获取参演资产
    char = shot.get("参演角色1", "")
    scene = shot.get("场景设置1", "") or shot.get("场景设置2", "")
    prop = shot.get("参演道具1", "")
    sup_chars = []
    for i in [2, 3]:
        sc = shot.get(f"参演角色{i}", "")
        if sc:
            sup_chars.append(sc)

    # ── Per-shot PrimitiveFloat (duration) ──
    dur_id = idgen.node()
    dur_node = make_node(
        dur_id, "PrimitiveFloat", f"Duration ({shot_id}, {duration}s)",
        [x_offset - 600, y_offset], [300, 82],
        widgets_values=[float(duration)],
        outputs=[make_output("FLOAT", "FLOAT", links=[], slot=0)],
        color="#322", bgcolor="#533",
    )
    nodes.append(dur_node)

    # ── Per-shot ComfyMathExpression (frame count) ──
    math_id = idgen.node()
    math_link_id = idgen.link()
    math_node = make_node(
        math_id, "ComfyMathExpression", f"Frame Count ({shot_id})",
        [x_offset - 600, y_offset + 100], [300, 82],
        inputs=[make_input("values.a", "FLOAT", link=math_link_id)],
        widgets_values=["max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17"],
        outputs=[
            make_output("FLOAT", "FLOAT", links=[], slot=0),
            make_output("INT", "INT", links=[], slot=1),
            make_output("BOOL", "BOOL", links=[], slot=2),
        ],
        color="#322", bgcolor="#533",
    )
    nodes.append(math_node)
    links.append([math_link_id, dur_id, 0, math_id, 0, "FLOAT"])

    # ── easy promptLine ──
    pl_id = idgen.node()
    raw_prompt = shot.get("完整提示词", "")
    rewritten = rewrite_prompt(raw_prompt, char, scene, prop, sup_chars)

    pl_node = make_node(
        pl_id, "easy promptLine", f"H3 Prompt ({shot_id})",
        [x_offset - 300, y_offset + 520], [500, 300],
        inputs=[
            make_input("prompt", "STRING", link=None),
            make_input("start_index", "INT", link=None),
            make_input("max_rows", "INT", link=None),
            make_input("remove_empty_lines", "BOOLEAN", link=None),
        ],
        outputs=[
            make_output("STRING", "STRING", links=[], slot=0),
            make_output("COMBO", "COMBO", links=[], slot=1),
        ],
        widgets_values=[rewritten, 0, 1000, True],
    )
    nodes.append(pl_node)

    # ── MiniMaxH3ReferenceToVideo ──
    r2v_id = idgen.node()

    # 构建 ref_image 输入
    r2v_inputs = []
    # ref_image_0 (主角色)
    if char and char != "(纯场景)":
        ln = load_nodes.get(("char", char))
        if ln:
            lid = idgen.link()
            r2v_inputs.append(make_input("ref_images.ref_image_0", "IMAGE", link=lid))
            links.append([lid, ln, 0, r2v_id, SLOT_INPUT[0], "IMAGE"])
        else:
            r2v_inputs.append(make_input("ref_images.ref_image_0", "IMAGE", link=None))
    else:
        r2v_inputs.append(make_input("ref_images.ref_image_0", "IMAGE", link=None))

    # ref_image_1 (场景)
    if scene:
        ln = load_nodes.get(("scene", scene))
        if ln:
            lid = idgen.link()
            r2v_inputs.append(make_input("ref_images.ref_image_1", "IMAGE", link=lid))
            links.append([lid, ln, 0, r2v_id, SLOT_INPUT[1], "IMAGE"])
        else:
            r2v_inputs.append(make_input("ref_images.ref_image_1", "IMAGE", link=None))
    else:
        r2v_inputs.append(make_input("ref_images.ref_image_1", "IMAGE", link=None))

    # ref_image_2 (道具)
    if prop:
        ln = load_nodes.get(("prop", prop))
        if ln:
            lid = idgen.link()
            r2v_inputs.append(make_input("ref_images.ref_image_2", "IMAGE", link=lid))
            links.append([lid, ln, 0, r2v_id, SLOT_INPUT[2], "IMAGE"])
        else:
            r2v_inputs.append(make_input("ref_images.ref_image_2", "IMAGE", link=None))
    else:
        r2v_inputs.append(make_input("ref_images.ref_image_2", "IMAGE", link=None))

    # ref_video_0 (参考视频)
    ref_video = shot.get("参考视频", "")
    if ref_video:
        ln = load_nodes.get(("video", ref_video))
        if ln:
            lid = idgen.link()
            r2v_inputs.append(make_input("ref_videos.ref_video_0", "IMAGE", link=lid))
            links.append([lid, ln, 0, r2v_id, 6, "IMAGE"])
        else:
            r2v_inputs.append(make_input("ref_videos.ref_video_0", "IMAGE", link=None))
    else:
        r2v_inputs.append(make_input("ref_videos.ref_video_0", "IMAGE", link=None))

    # ref_video_audio_0 (参考视频音频 — 留空，用户可在 ComfyUI 手动连接)
    r2v_inputs.append(make_input("ref_video_audios.ref_video_audio_0", "AUDIO", link=None))

    # ref_audio_0 (参考音频1)
    ref_audio1 = shot.get("参考音频1", "")
    if ref_audio1:
        ln = load_nodes.get(("audio", ref_audio1))
        if ln:
            lid = idgen.link()
            r2v_inputs.append(make_input("ref_audios.ref_audio_0", "AUDIO", link=lid))
            links.append([lid, ln, 0, r2v_id, 8, "AUDIO"])
        else:
            r2v_inputs.append(make_input("ref_audios.ref_audio_0", "AUDIO", link=None))
    else:
        r2v_inputs.append(make_input("ref_audios.ref_audio_0", "AUDIO", link=None))

    # ref_audio_1 (参考音频2)
    ref_audio2 = shot.get("参考音频2", "")
    if ref_audio2:
        ln = load_nodes.get(("audio", ref_audio2))
        if ln:
            lid = idgen.link()
            r2v_inputs.append(make_input("ref_audios.ref_audio_1", "AUDIO", link=lid))
            links.append([lid, ln, 0, r2v_id, 9, "AUDIO"])
        else:
            r2v_inputs.append(make_input("ref_audios.ref_audio_1", "AUDIO", link=None))
    else:
        r2v_inputs.append(make_input("ref_audios.ref_audio_1", "AUDIO", link=None))

    # ref_image_3+ (配角)
    pic_idx = 3
    for sup in sup_chars:
        ln = load_nodes.get(("char", sup))
        if ln and pic_idx in SLOT_INPUT:
            lid = idgen.link()
            r2v_inputs.append(make_input(f"ref_images.ref_image_{pic_idx}", "IMAGE", link=lid))
            links.append([lid, ln, 0, r2v_id, SLOT_INPUT[pic_idx], "IMAGE"])
        else:
            r2v_inputs.append(make_input(f"ref_images.ref_image_{pic_idx}", "IMAGE", link=None))
        pic_idx += 1

    # CLIP 连接
    clip_lid = idgen.link()
    r2v_inputs.insert(0, make_input("clip", "CLIP", link=clip_lid))
    links.append([clip_lid, shared_refs["clip_id"], shared_refs["clip_out"],
                  r2v_id, 0, "CLIP"])

    # VAE Video 连接
    vae_lid = idgen.link()
    r2v_inputs.insert(1, make_input("vae", "VAE", link=vae_lid))
    links.append([vae_lid, shared_refs["vae_v_id"], shared_refs["vae_v_out"],
                  r2v_id, 1, "VAE"])

    # Audio VAE 连接
    audio_vae_lid = idgen.link()
    r2v_inputs.insert(2, make_input("audio_vae", "VAE", link=audio_vae_lid))
    links.append([audio_vae_lid, shared_refs["vae_a_id"], shared_refs["vae_a_out"],
                  r2v_id, 2, "VAE"])

    # width 连接 (shared ResolutionSelector)
    w_lid = idgen.link()
    r2v_inputs.append(make_input("width", "INT", link=w_lid, widget_name="width"))
    links.append([w_lid, shared_refs["res_id"], shared_refs["res_w_out"],
                  r2v_id, 1, "INT"])

    # height 连接 (shared ResolutionSelector)
    h_lid = idgen.link()
    r2v_inputs.append(make_input("height", "INT", link=h_lid, widget_name="height"))
    links.append([h_lid, shared_refs["res_id"], shared_refs["res_h_out"],
                  r2v_id, 2, "INT"])

    # prompt 连接
    prompt_lid = idgen.link()
    r2v_inputs.append(make_input("prompt", "STRING", link=prompt_lid))
    links.append([prompt_lid, pl_id, 0, r2v_id, 2, "STRING"])

    # length 连接 (per-shot ComfyMathExpression INT output)
    length_lid = idgen.link()
    r2v_inputs.append(make_input("length", "INT", link=length_lid, widget_name="length"))
    links.append([length_lid, math_id, 1, r2v_id, 12, "INT"])

    r2v_outputs = [
        make_output("positive", "CONDITIONING", links=[], slot=0),
        make_output("negative", "CONDITIONING", links=[], slot=1),
        make_output("LATENT", "LATENT", links=[], slot=2),
    ]

    r2v_node = make_node(
        r2v_id, "MiniMaxH3ReferenceToVideo", f"Ref2V ({shot_id})",
        [x_offset, y_offset], [400, 300],
        inputs=r2v_inputs,
        outputs=r2v_outputs,
        widgets_values=[],
    )
    nodes.append(r2v_node)

    # ── H3SaveConditioning ──
    save_id = idgen.node()
    save_lid = idgen.link()
    links.append([save_lid, r2v_id, 0, save_id, 0, "CONDITIONING"])

    # duration 连接 (per-shot PrimitiveFloat)
    save_dur_lid = idgen.link()
    links.append([save_dur_lid, dur_id, 0, save_id, 1, "FLOAT"])

    # width/height 连接 (shared ResolutionSelector)
    save_w_lid = idgen.link()
    links.append([save_w_lid, shared_refs["res_id"], shared_refs["res_w_out"],
                  save_id, 2, "INT"])
    save_h_lid = idgen.link()
    links.append([save_h_lid, shared_refs["res_id"], shared_refs["res_h_out"],
                  save_id, 3, "INT"])

    save_node = make_node(
        save_id, "H3SaveConditioning", f"Save ({shot_id}.pt)",
        [x_offset + 500, y_offset], [300, 120],
        inputs=[
            make_input("conditioning", "CONDITIONING", link=save_lid),
            make_input("duration", "FLOAT", link=save_dur_lid, widget_name="duration"),
            make_input("width", "INT", link=save_w_lid, widget_name="width"),
            make_input("height", "INT", link=save_h_lid, widget_name="height"),
        ],
        outputs=[],
        widgets_values=[shot_id],
        color="#232", bgcolor="#353",
    )
    nodes.append(save_node)

    return nodes, links


def generate_group_json(group_name, group_shots, asset_paths, output_dir):
    """为一个角色组生成多链 JSON。"""
    idgen = IDGen(start=1)
    all_nodes = []
    all_links = []

    # 从第一个镜头提取分辨率（全组共享）
    resolution = _extract_number(group_shots[0].get("分辨率", "0.5"), 0.5)

    # 共享节点
    shared_nodes, shared_refs = build_shared_nodes(idgen, resolution)
    all_nodes.extend(shared_nodes)

    # ── 合并 LoadImage ──
    # 收集本组所有用到的资产，去重
    load_nodes = {}  # (type, name) → node_id
    load_x = -1300
    load_y = 0

    # 主角色（全组共享）
    main_char = group_shots[0].get("参演角色1", "")
    if main_char and main_char != "(纯场景)":
        path = asset_paths.get(("角色", main_char), f"{main_char}.png")
        nid, node = build_loadimage(idgen, main_char, path, load_x, load_y)
        all_nodes.append(node)
        load_nodes[("char", main_char)] = nid
        load_y += 100

    # 各场景（去重）
    seen_scenes = set()
    for shot in group_shots:
        for sk in ["场景设置1", "场景设置2"]:
            sname = shot.get(sk, "")
            if sname and sname not in seen_scenes:
                seen_scenes.add(sname)
                path = asset_paths.get(("场景", sname), f"{sname}.png")
                nid, node = build_loadimage(idgen, sname, path, load_x, load_y)
                all_nodes.append(node)
                load_nodes[("scene", sname)] = nid
                load_y += 100

    # 各道具（去重）
    seen_props = set()
    for shot in group_shots:
        for pk in ["参演道具1", "参演道具2", "参演道具3"]:
            pname = shot.get(pk, "")
            if pname and pname not in seen_props:
                seen_props.add(pname)
                path = asset_paths.get(("道具", pname), f"{pname}.png")
                nid, node = build_loadimage(idgen, pname, path, load_x, load_y)
                all_nodes.append(node)
                load_nodes[("prop", pname)] = nid
                load_y += 100

    # 配角（去重）
    seen_sups = set()
    for shot in group_shots:
        for ck in ["参演角色2", "参演角色3"]:
            cname = shot.get(ck, "")
            if cname and cname != main_char and cname not in seen_sups:
                seen_sups.add(cname)
                path = asset_paths.get(("角色", cname), f"{cname}.png")
                nid, node = build_loadimage(idgen, cname, path, load_x, load_y)
                all_nodes.append(node)
                load_nodes[("char", cname)] = nid
                load_y += 100

    # 参考音频（去重）
    seen_audios = set()
    for shot in group_shots:
        for ak in ["参考音频1", "参考音频2"]:
            aname = shot.get(ak, "")
            if aname and aname not in seen_audios:
                seen_audios.add(aname)
                path = asset_paths.get(("音频", aname), "")
                nid, node = build_loadaudio(idgen, aname, path, load_x, load_y)
                all_nodes.append(node)
                load_nodes[("audio", aname)] = nid
                load_y += 100

    # 参考视频（去重）
    seen_videos = set()
    for shot in group_shots:
        vname = shot.get("参考视频", "")
        if vname and vname not in seen_videos:
            seen_videos.add(vname)
            path = asset_paths.get(("视频", vname), "")
            nid, node = build_loadimage(idgen, vname, path, load_x, load_y)
            all_nodes.append(node)
            load_nodes[("video", vname)] = nid
            load_y += 100

    # ── 逐镜创建链 ──
    for i, shot in enumerate(group_shots):
        x_off = -500 + (i % 4) * 700
        y_off = -420 + (i // 4) * 500
        chain_nodes, chain_links = build_shot_chain(
            idgen, shot, shared_refs, load_nodes, asset_paths, x_off, y_off
        )
        all_nodes.extend(chain_nodes)
        all_links.extend(chain_links)

    # 更新共享节点的输出 links 列表
    # CLIPLoader 输出
    for node in all_nodes:
        if node["type"] == "CLIPLoader" and node["outputs"]:
            clip_links = [l[0] for l in all_links if l[1] == node["id"] and l[2] == 0]
            node["outputs"][0]["links"] = clip_links
        elif node["type"] == "VAELoader" and node["title"] == "Video VAE":
            vae_links = [l[0] for l in all_links if l[1] == node["id"] and l[2] == 0]
            node["outputs"][0]["links"] = vae_links
        elif node["type"] == "VAELoader" and node["title"] == "Audio VAE":
            vae_links = [l[0] for l in all_links if l[1] == node["id"] and l[2] == 0]
            node["outputs"][0]["links"] = vae_links
        elif node["type"] == "ResolutionSelector":
            w_links = [l[0] for l in all_links if l[1] == node["id"] and l[2] == 0]
            h_links = [l[0] for l in all_links if l[1] == node["id"] and l[2] == 1]
            node["outputs"][0]["links"] = w_links
            node["outputs"][1]["links"] = h_links
        elif node["type"] == "LoadImage":
            img_links = [l[0] for l in all_links if l[1] == node["id"] and l[2] == 0]
            node["outputs"][0]["links"] = img_links
        elif node["type"] == "LoadAudio":
            aud_links = [l[0] for l in all_links if l[1] == node["id"] and l[2] == 0]
            node["outputs"][0]["links"] = aud_links
        elif node["type"] == "PrimitiveFloat":
            fl_links = [l[0] for l in all_links if l[1] == node["id"] and l[2] == 0]
            node["outputs"][0]["links"] = fl_links
        elif node["type"] == "ComfyMathExpression":
            math_float_links = [l[0] for l in all_links if l[1] == node["id"] and l[2] == 0]
            math_int_links = [l[0] for l in all_links if l[1] == node["id"] and l[2] == 1]
            node["outputs"][0]["links"] = math_float_links
            node["outputs"][1]["links"] = math_int_links
        elif node["type"] == "easy promptLine":
            pl_links = [l[0] for l in all_links if l[1] == node["id"] and l[2] == 0]
            if node["outputs"]:
                node["outputs"][0]["links"] = pl_links

    # 构建 JSON
    wf = {
        "id": f"denghuang-h3-preencode-{group_name}",
        "revision": 0,
        "last_node_id": idgen._next - 1,
        "last_link_id": idgen._next - 1,
        "nodes": all_nodes,
        "links": all_links,
        "groups": [
            {
                "id": 1,
                "title": f"{group_name} ({len(group_shots)}镜)",
                "bounding": [-1850, -480, 2200, 1200],
                "color": "#3f789e",
                "flags": {},
            }
        ],
        "config": {},
        "extra": {
            "ds": {"scale": 0.6, "offset": [800, 400]},
            "note": f"H3 多链预编码 | {group_name} | {len(group_shots)}镜 | 共享Load合并 | "
                    f"每镜独立分辨率+时长 | 元数据连接",
        },
        "version": 0.4,
    }

    safe_name = sanitize_filename(group_name)
    output_path = os.path.join(output_dir, f"{safe_name}.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(wf, f, ensure_ascii=False, indent=1)

    return output_path, len(group_shots), len(load_nodes)


# ── 资产映射 ─────────────────────────────────────────────────────────

def load_asset_mapping(mapping_path):
    """从 JSON 文件加载资产映射，返回 {(type, name): path} dict。
    JSON 格式: {"角色": {"黑猫": "path.png", ...}, "场景": {...}, "道具": {...}}
    """
    if not os.path.exists(mapping_path):
        return {}
    with open(mapping_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    mapping = {}
    for asset_type, items in data.items():
        if isinstance(items, dict):
            for name, path in items.items():
                if path:
                    mapping[(asset_type, name)] = path
    return mapping


def merge_asset_paths(excel_paths, mapping_paths):
    """合并 Excel Sheet2 路径和外部映射路径。
    Excel Sheet2 优先，映射补充空缺。
    """
    merged = dict(mapping_paths)
    merged.update(excel_paths)
    return merged


# ── 单文件 / 批量处理 ───────────────────────────────────────────────

def process_single(xlsx_path, output_dir, by_shot=False, log=print,
                   asset_mapping=None):
    """处理单个 Excel 文件，返回 (n_groups, n_shots) 或 None。
    asset_mapping: 可选，{(type, name): path} dict，补充 Excel Sheet2 的空缺。
    """
    if not Path(xlsx_path).exists():
        log(f"ERROR: file not found: {xlsx_path}")
        return None

    os.makedirs(output_dir, exist_ok=True)
    shots, excel_paths = read_excel(xlsx_path)

    if not shots:
        log(f"ERROR: no shots in {xlsx_path}")
        return None

    # 合并资产路径：Excel Sheet2 优先，映射补充
    if asset_mapping:
        asset_paths = merge_asset_paths(excel_paths, asset_mapping)
        log(f"  读取: {len(shots)} 镜, Excel {len(excel_paths)} 路径 + 映射 {len(asset_mapping)} 路径")
    else:
        asset_paths = excel_paths
        log(f"  读取: {len(shots)} 镜, {len(asset_paths)} 个资产路径")

    # 分组
    if by_shot:
        groups = {}
        for shot in shots:
            sid = shot.get("镜头编号", "unknown")
            groups[sid] = [shot]
    else:
        groups = {}
        for shot in shots:
            char = shot.get("参演角色1", "(纯场景)")
            if not char:
                char = "(纯场景)"
            groups.setdefault(char, []).append(shot)

    log(f"  分组: {len(groups)} 组")
    for name, group in groups.items():
        path, n_shots, n_loads = generate_group_json(name, group, asset_paths, output_dir)
        log(f"    {name}: {n_shots}镜, {n_loads}个Load -> {os.path.basename(path)}")

    return len(groups), len(shots)


def process_batch(xlsx_files, output_dir, by_shot=False, log=print,
                  asset_mapping=None):
    """批量处理多个 Excel 文件。每个 Excel 生成到 output_dir 下的子目录。"""
    success, fail = 0, 0

    for xlsx_path in xlsx_files:
        stem = Path(xlsx_path).stem
        sub_dir = os.path.join(output_dir, stem)
        log(f"\n[{success + fail + 1}/{len(xlsx_files)}] {Path(xlsx_path).name}")

        result = process_single(xlsx_path, sub_dir, by_shot=by_shot, log=log,
                                asset_mapping=asset_mapping)
        if result:
            success += 1
        else:
            fail += 1

    log(f"\n批量完成: {success} 成功, {fail} 失败, 共 {len(xlsx_files)} 文件")
    return success, fail


# ── 主入口 ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="提示词审阅表 → 多链生产 JSON")
    parser.add_argument("files", nargs="+", help="Excel 文件路径（一个或多个）")
    parser.add_argument("-o", "--output", required=True, help="输出目录")
    parser.add_argument("--by-shot", action="store_true",
                        help="按镜头顺序分组（每镜一个JSON）")
    parser.add_argument("-m", "--mapping", help="资产映射 JSON 文件路径（补充 Excel Sheet2 空缺）")
    args = parser.parse_args()

    xlsx_files = args.files
    asset_mapping = None
    if args.mapping:
        asset_mapping = load_asset_mapping(args.mapping)
        if asset_mapping:
            print(f"加载资产映射: {len(asset_mapping)} 条 from {args.mapping}")
        else:
            print(f"WARNING: 资产映射为空或文件不存在: {args.mapping}")

    if len(xlsx_files) == 1:
        result = process_single(xlsx_files[0], args.output, by_shot=args.by_shot,
                                asset_mapping=asset_mapping)
        if result is None:
            sys.exit(1)
    else:
        process_batch(xlsx_files, args.output, by_shot=args.by_shot,
                      asset_mapping=asset_mapping)


if __name__ == "__main__":
    main()
