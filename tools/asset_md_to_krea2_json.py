# -*- coding: utf-8 -*-
"""asset_md_to_krea2_json.py

Parse a 美术资产准备表 MD file and generate Krea2 ComfyUI workflow JSONs.

Three asset types are supported:
  - 角色 (character)  → identity_edit 锁脸多景别 JSON
  - 场景 (scene)      → txt2img_batch 文生图批量 JSON (easy promptLine)
  - 道具 (prop)       → txt2img 单图设定图 JSON

Usage:
  python asset_md_to_krea2_json.py <input_md> <output_dir>
  python asset_md_to_krea2_json.py <input_md> <output_dir> --batch
"""

import json
import os
import re
import sys
import copy
import argparse


# ──────────────────────────────────────────────
# 1. MD Parser
# ──────────────────────────────────────────────

def parse_md(md_path):
    """Parse 美术资产准备表 MD → dict with global_info + assets list."""
    with open(md_path, "r", encoding="utf-8") as f:
        text = f.read()

    result = {"global": {}, "characters": [], "scenes": [], "props": []}

    # ── Global info ──
    g_match = re.search(r"##\s*全局信息\s*\n(.*?)(?=\n##\s)", text, re.DOTALL)
    if g_match:
        for line in g_match.group(1).split("\n"):
            m = re.match(r"-\s*\*\*(.+?)\*\*\s*:\s*(.+)", line.strip())
            if m:
                result["global"][m.group(1).strip()] = m.group(2).strip()

    # ── Asset sections ──
    for asset_type, key in [("角色", "characters"), ("场景", "scenes"), ("道具", "props")]:
        pattern = rf"##\s*{asset_type}资产\s*\n(.*?)(?=\n##\s|$)"
        section_match = re.search(pattern, text, re.DOTALL)
        if not section_match:
            continue
        section = section_match.group(1)

        # Split by ### headers
        items = re.split(r"###\s+", section)
        for item in items[1:]:  # skip text before first ###
            lines = item.strip().split("\n")
            first_line = lines[0].strip()
            # Parse name from header like "角色：金止戈" or "角色: 金止戈"
            name_match = re.match(r"(?:角色|场景|道具)\s*[：:]\s*(.+)", first_line)
            name = name_match.group(1).strip() if name_match else first_line

            asset = {"name": name, "raw": item.strip()}

            # Parse fields
            for line in lines[1:]:
                m = re.match(r"-\s*\*\*(.+?)\*\*\s*[:：]\s*(.+)", line.strip())
                if m:
                    field = m.group(1).strip()
                    val = m.group(2).strip()
                    asset[field] = val

            # Parse batch prompts for scenes (lines starting with "  - ")
            if key == "scenes":
                prompts = []
                in_prompts = False
                for line in lines[1:]:
                    if "批量提示词" in line:
                        in_prompts = True
                        continue
                    if in_prompts:
                        pm = re.match(r"\s+-\s+(.+)", line)
                        if pm:
                            prompts.append(pm.group(1).strip())
                        elif line.strip() and not line.strip().startswith("-"):
                            in_prompts = False
                if prompts:
                    asset["batch_prompts"] = prompts
                # If no batch prompts, check for single prompt field
                if "提示词" in asset and not prompts:
                    asset["batch_prompts"] = [asset["提示词"]]

            # Parse angle prompts for characters (fields with / in name)
            if key == "characters":
                angles = []
                angle_list_str = asset.get("景别列表", "")
                if angle_list_str:
                    angles = [a.strip() for a in angle_list_str.split(",") if a.strip()]
                asset["angles"] = angles if angles else ["正面全身"]

                # Collect per-angle prompts
                angle_prompts = {}
                for line in lines[1:]:
                    m = re.match(r"-\s*\*\*(.+?)\s*/\s*.+?\*\*\s*[:：]\s*(.+)", line.strip())
                    if m:
                        angle_name = m.group(1).strip()
                        angle_prompts[angle_name] = m.group(2).strip()
                asset["angle_prompts"] = angle_prompts

            result[key].append(asset)

    return result


# ──────────────────────────────────────────────
# 2. JSON Builders
# ──────────────────────────────────────────────

DEFAULT_NEGATIVE = (
    "worst quality, low quality, bad quality, worst detail, sketch, censor, "
    "extra limbs, deformed fingers, bad anatomy, mutated body, lowres, blurry, "
    "text, ugly, watermark, pale, bad hands, bad proportions, poorly drawn face, "
    "poorly drawn hand, missing finger, pixelated, jpeg artifacts, signature, "
    "(deformed:1.5), (bad hand:1.3), overexposed, underexposed, mutated, "
    "extra finger, cloned face, bad eyes, earrings, "
    "鲜红血液, 鲜艳蓝天, 鲜绿, 卡通, Q版, 现代建筑, 水印, "
    "多余手指, 多余肢体, 畸形手, 坏解剖"
)


def _node(node_id, ntype, pos, title, widgets=None, inputs=None, outputs=None,
          color="#222", bgcolor="#000"):
    """Helper to build a ComfyUI node dict."""
    n = {
        "id": node_id,
        "type": ntype,
        "pos": pos,
        "size": [400, 120],
        "flags": {},
        "order": 0,
        "mode": 0,
        "inputs": inputs or [],
        "outputs": outputs or [],
        "properties": {"Node name for S&R": ntype},
        "widgets_values": widgets or [],
        "title": title,
        "color": color,
        "bgcolor": bgcolor,
    }
    return n


def _link(link_id, src_node, src_slot, dst_node, dst_slot, data_type):
    return [link_id, src_node, src_slot, dst_node, dst_slot, data_type]


def _get_global(parsed, key, default):
    """Get a global config value with fallback."""
    return parsed.get(key, default)


# ── 2a. Character (identity_edit 锁脸) ──

def build_character_json(asset, parsed):
    """Build a Krea2 identity_edit (锁脸) JSON for a character asset.

    Generates multi-angle character sheets using Krea2EditGroundedEncode +
    Krea2EditModelPatch, with shared LoadImage reference.
    """
    name = asset["name"]
    g = parsed["global"]

    unet = _get_global(g, "Krea2模型", "krea2/krea2_turbo_int8_convrot.safetensors")
    clip = _get_global(g, "CLIP模型", "qwen3-vl-4b-heretic_int8.safetensors")
    vae = _get_global(g, "VAE模型", "qwen_image_vae.safetensors")
    style_lora = _get_global(g, "风格LoRA", "krea2/Krea2MythD4rkL1nes.safetensors")
    identity_lora = _get_global(g, "身份LoRA", "krea2/krea2_identity_edit_v1_2.safetensors")
    negative = _get_global(g, "负面提示词", DEFAULT_NEGATIVE)

    ref_image = asset.get("参考图路径", "")
    char_lora = asset.get("角色LoRA", "")
    char_lora_strength = float(asset.get("角色LoRA权重", "0.6"))
    resolution = asset.get("分辨率", "1152x1536")
    try:
        w, h = map(int, resolution.lower().split("x"))
    except Exception:
        w, h = 1152, 1536

    angles = asset.get("angles", ["正面全身"])
    angle_prompts = asset.get("angle_prompts", {})

    nodes = []
    links = []
    nid = 0
    lid = 0

    def next_nid():
        nonlocal nid
        nid += 1
        return nid

    def next_lid():
        nonlocal lid
        lid += 1
        return lid

    # ── Shared nodes ──
    nid_unet = next_nid()
    n_unet = _node(nid_unet, "UNETLoader", [-1100, -500], "UNET - Krea2 Turbo",
                   widgets=[unet, "default"])
    n_unet["outputs"] = [{"name": "MODEL", "type": "MODEL", "slot_index": 0, "links": []}]
    nodes.append(n_unet)

    nid_clip = next_nid()
    n_clip = _node(nid_clip, "CLIPLoader", [-1100, -340], "CLIP - Krea2 (Qwen3-VL)",
                   widgets=[clip, "krea2", "default"])
    n_clip["outputs"] = [{"name": "CLIP", "type": "CLIP", "slot_index": 0, "links": []}]
    nodes.append(n_clip)

    nid_vae = next_nid()
    n_vae = _node(nid_vae, "VAELoader", [-1100, -180], "VAE Loader",
                   widgets=[vae])
    n_vae["outputs"] = [{"name": "VAE", "type": "VAE", "slot_index": 0, "links": []}]
    nodes.append(n_vae)

    # Style LoRA
    nid_style = next_nid()
    n_style = _node(nid_style, "LoraLoaderModelOnly", [-1100, -20],
                    f"LoRA - D4rkL1nes (风格)",
                    inputs=[{"name": "model", "type": "MODEL", "link": None}],
                    widgets=[style_lora, 1.0])
    n_style["outputs"] = [{"name": "MODEL", "type": "MODEL", "slot_index": 0, "links": []}]
    nodes.append(n_style)

    # Identity Edit LoRA
    nid_identity = next_nid()
    n_identity = _node(nid_identity, "LoraLoaderModelOnly", [-1100, 140],
                       "LoRA - Identity Edit (锁脸)",
                       inputs=[{"name": "model", "type": "MODEL", "link": None}],
                       widgets=[identity_lora, 1.0])
    n_identity["outputs"] = [{"name": "MODEL", "type": "MODEL", "slot_index": 0, "links": []}]
    nodes.append(n_identity)

    # Optional character LoRA
    nid_char_lora = None
    if char_lora:
        nid_char_lora = next_nid()
        n_char_lora = _node(nid_char_lora, "LoraLoaderModelOnly", [-1100, 300],
                            f"LoRA - {name} (角色)",
                            inputs=[{"name": "model", "type": "MODEL", "link": None}],
                            widgets=[char_lora, char_lora_strength])
        n_char_lora["outputs"] = [{"name": "MODEL", "type": "MODEL", "slot_index": 0, "links": []}]
        nodes.append(n_char_lora)

    # LoadImage (reference)
    nid_loadimg = next_nid()
    n_loadimg = _node(nid_loadimg, "LoadImage", [-1100, 500],
                      f"Image: SUBJECT ({name} 参考图)",
                      widgets=[ref_image, "image"])
    n_loadimg["outputs"] = [
        {"name": "IMAGE", "type": "IMAGE", "slot_index": 0, "links": []},
        {"name": "MASK", "type": "MASK", "slot_index": 1, "links": []},
    ]
    nodes.append(n_loadimg)

    # VAEEncode (reference latent)
    nid_vaeenc = next_nid()
    n_vaeenc = _node(nid_vaeenc, "VAEEncode", [-800, 500], "VAEEncode (subject)",
                     inputs=[{"name": "pixels", "type": "IMAGE", "link": None},
                             {"name": "vae", "type": "VAE", "link": None}])
    n_vaeenc["outputs"] = [{"name": "LATENT", "type": "LATENT", "slot_index": 0, "links": []}]
    nodes.append(n_vaeenc)

    # EmptySD3LatentImage
    nid_latent = next_nid()
    n_latent = _node(nid_latent, "EmptySD3LatentImage", [-800, -340],
                     f"Latent ({w}x{h})",
                     widgets=[w, h, 1])
    n_latent["outputs"] = [{"name": "LATENT", "type": "LATENT", "slot_index": 0, "links": []}]
    nodes.append(n_latent)

    # ── Connect shared nodes ──
    # UNet → Style LoRA
    l = next_lid()
    links.append(_link(l, nid_unet, 0, nid_style, 0, "MODEL"))
    n_unet["outputs"][0]["links"].append(l)
    n_style["inputs"][0]["link"] = l

    # Style LoRA → Identity LoRA
    l = next_lid()
    links.append(_link(l, nid_style, 0, nid_identity, 0, "MODEL"))
    n_style["outputs"][0]["links"].append(l)
    n_identity["inputs"][0]["link"] = l

    # Model chain end
    model_source = nid_identity
    if nid_char_lora:
        l = next_lid()
        links.append(_link(l, nid_identity, 0, nid_char_lora, 0, "MODEL"))
        n_identity["outputs"][0]["links"].append(l)
        n_char_lora["inputs"][0]["link"] = l
        model_source = nid_char_lora

    # LoadImage → VAEEncode
    l = next_lid()
    links.append(_link(l, nid_loadimg, 0, nid_vaeenc, 0, "IMAGE"))
    n_loadimg["outputs"][0]["links"].append(l)
    n_vaeenc["inputs"][0]["link"] = l

    # VAE → VAEEncode
    l = next_lid()
    links.append(_link(l, nid_vae, 0, nid_vaeenc, 1, "VAE"))
    n_vae["outputs"][0]["links"].append(l)
    n_vaeenc["inputs"][1]["link"] = l

    # ── Per-angle chains ──
    x_offset = 0
    for angle in angles:
        prompt = angle_prompts.get(angle, f"Generate a new scene featuring the character as shown in the reference image. Apply the reference character's face, hairstyle, eye color, body type and clothing. D4rkL1nes, beautiful_darkness_style, {angle}, Preserve the exact facial identity and distinguishing features of the reference character.")

        # Positive Krea2EditGroundedEncode
        nid_pos = next_nid()
        n_pos = _node(nid_pos, "Krea2EditGroundedEncode",
                      [-440 + x_offset, -500], f"Positive ({angle})",
                      inputs=[{"name": "clip", "type": "CLIP", "link": None},
                              {"name": "image", "type": "IMAGE", "link": None},
                              {"name": "image_b", "type": "IMAGE", "link": None}],
                      widgets=[prompt, 768, ""],
                      color="#226", bgcolor="#114")
        n_pos["outputs"] = [{"name": "CONDITIONING", "type": "CONDITIONING", "slot_index": 0, "links": []}]
        nodes.append(n_pos)

        # Negative Krea2EditGroundedEncode
        nid_neg = next_nid()
        n_neg = _node(nid_neg, "Krea2EditGroundedEncode",
                      [-440 + x_offset, -300], f"Negative ({angle})",
                      inputs=[{"name": "clip", "type": "CLIP", "link": None},
                              {"name": "image", "type": "IMAGE", "link": None},
                              {"name": "image_b", "type": "IMAGE", "link": None}],
                      widgets=[negative, 768, ""],
                      color="#622", bgcolor="#411")
        n_neg["outputs"] = [{"name": "CONDITIONING", "type": "CONDITIONING", "slot_index": 0, "links": []}]
        nodes.append(n_neg)

        # Krea2EditModelPatch
        nid_patch = next_nid()
        n_patch = _node(nid_patch, "Krea2EditModelPatch",
                        [-440 + x_offset, -80], f"Edit Patch ({angle})",
                        inputs=[{"name": "model", "type": "MODEL", "link": None},
                                {"name": "source_latent", "type": "LATENT", "link": None},
                                {"name": "source_latent_b", "type": "LATENT", "link": None},
                                {"name": "ref_boost_mask", "type": "MASK", "link": None},
                                {"name": "vae", "type": "VAE", "link": None},
                                {"name": "source_image", "type": "IMAGE", "link": None},
                                {"name": "source_image_b", "type": "IMAGE", "link": None},
                                {"name": "target_latent", "type": "LATENT", "link": None}],
                        widgets=[2.0, 1.0, "fit"])
        n_patch["outputs"] = [{"name": "MODEL", "type": "MODEL", "slot_index": 0, "links": []}]
        nodes.append(n_patch)

        # KSampler
        nid_ks = next_nid()
        n_ks = _node(nid_ks, "KSampler", [40 + x_offset, -500], f"KSampler ({angle})",
                     inputs=[{"name": "model", "type": "MODEL", "link": None},
                             {"name": "positive", "type": "CONDITIONING", "link": None},
                             {"name": "negative", "type": "CONDITIONING", "link": None},
                             {"name": "latent_image", "type": "LATENT", "link": None}],
                     widgets=[0, 10, 1, "euler", "simple", 1])
        n_ks["outputs"] = [{"name": "LATENT", "type": "LATENT", "slot_index": 0, "links": []}]
        nodes.append(n_ks)

        # VAEDecode
        nid_vaedec = next_nid()
        n_vaedec = _node(nid_vaedec, "VAEDecode", [460 + x_offset, -500],
                         f"VAE Decode ({angle})",
                         inputs=[{"name": "samples", "type": "LATENT", "link": None},
                                 {"name": "vae", "type": "VAE", "link": None}])
        n_vaedec["outputs"] = [{"name": "IMAGE", "type": "IMAGE", "slot_index": 0, "links": []}]
        nodes.append(n_vaedec)

        # SaveImage
        nid_save = next_nid()
        safe_name = re.sub(r'[^\w\u4e00-\u9fff]', '_', name)
        safe_angle = re.sub(r'[^\w\u4e00-\u9fff]', '_', angle)
        n_save = _node(nid_save, "SaveImage", [880 + x_offset, -500],
                       f"Save ({angle})",
                       inputs=[{"name": "images", "type": "IMAGE", "link": None}],
                       widgets=[f"krea2_锁脸/{safe_name}/{safe_angle}"])
        nodes.append(n_save)

        # ── Connect per-angle ──
        # CLIP → Positive
        l = next_lid()
        links.append(_link(l, nid_clip, 0, nid_pos, 0, "CLIP"))
        n_clip["outputs"][0]["links"].append(l)
        n_pos["inputs"][0]["link"] = l

        # LoadImage → Positive
        l = next_lid()
        links.append(_link(l, nid_loadimg, 0, nid_pos, 1, "IMAGE"))
        n_loadimg["outputs"][0]["links"].append(l)
        n_pos["inputs"][1]["link"] = l

        # CLIP → Negative
        l = next_lid()
        links.append(_link(l, nid_clip, 0, nid_neg, 0, "CLIP"))
        n_clip["outputs"][0]["links"].append(l)
        n_neg["inputs"][0]["link"] = l

        # LoadImage → Negative
        l = next_lid()
        links.append(_link(l, nid_loadimg, 0, nid_neg, 1, "IMAGE"))
        n_loadimg["outputs"][0]["links"].append(l)
        n_neg["inputs"][1]["link"] = l

        # Model → Patch
        l = next_lid()
        links.append(_link(l, model_source, 0, nid_patch, 0, "MODEL"))
        nodes_model_out = [n for n in nodes if n["id"] == model_source][0]
        nodes_model_out["outputs"][0]["links"].append(l)
        n_patch["inputs"][0]["link"] = l

        # VAEEncode → Patch (source_latent)
        l = next_lid()
        links.append(_link(l, nid_vaeenc, 0, nid_patch, 1, "LATENT"))
        n_vaeenc["outputs"][0]["links"].append(l)
        n_patch["inputs"][1]["link"] = l

        # VAE → Patch
        l = next_lid()
        links.append(_link(l, nid_vae, 0, nid_patch, 4, "VAE"))
        n_vae["outputs"][0]["links"].append(l)
        n_patch["inputs"][4]["link"] = l

        # LoadImage → Patch (source_image)
        l = next_lid()
        links.append(_link(l, nid_loadimg, 0, nid_patch, 5, "IMAGE"))
        n_loadimg["outputs"][0]["links"].append(l)
        n_patch["inputs"][5]["link"] = l

        # EmptyLatent → Patch (target_latent)
        l = next_lid()
        links.append(_link(l, nid_latent, 0, nid_patch, 7, "LATENT"))
        n_latent["outputs"][0]["links"].append(l)
        n_patch["inputs"][7]["link"] = l

        # Patch → KSampler (model)
        l = next_lid()
        links.append(_link(l, nid_patch, 0, nid_ks, 0, "MODEL"))
        n_patch["outputs"][0]["links"].append(l)
        n_ks["inputs"][0]["link"] = l

        # Positive → KSampler
        l = next_lid()
        links.append(_link(l, nid_pos, 0, nid_ks, 1, "CONDITIONING"))
        n_pos["outputs"][0]["links"].append(l)
        n_ks["inputs"][1]["link"] = l

        # Negative → KSampler
        l = next_lid()
        links.append(_link(l, nid_neg, 0, nid_ks, 2, "CONDITIONING"))
        n_neg["outputs"][0]["links"].append(l)
        n_ks["inputs"][2]["link"] = l

        # EmptyLatent → KSampler (latent_image)
        l = next_lid()
        links.append(_link(l, nid_latent, 0, nid_ks, 3, "LATENT"))
        n_latent["outputs"][0]["links"].append(l)
        n_ks["inputs"][3]["link"] = l

        # KSampler → VAEDecode
        l = next_lid()
        links.append(_link(l, nid_ks, 0, nid_vaedec, 0, "LATENT"))
        n_ks["outputs"][0]["links"].append(l)
        n_vaedec["inputs"][0]["link"] = l

        # VAE → VAEDecode
        l = next_lid()
        links.append(_link(l, nid_vae, 0, nid_vaedec, 1, "VAE"))
        n_vae["outputs"][0]["links"].append(l)
        n_vaedec["inputs"][1]["link"] = l

        # VAEDecode → SaveImage
        l = next_lid()
        links.append(_link(l, nid_vaedec, 0, nid_save, 0, "IMAGE"))
        n_vaedec["outputs"][0]["links"].append(l)
        n_save["inputs"][0]["link"] = l

        x_offset += 500

    # Note node
    nid_note = next_nid()
    n_note = _node(nid_note, "Note", [-1100, 700], f"使用说明 - {name}锁脸",
                   widgets=[
                       f"《登黄》{name} - 锁脸角色定妆工作流\n"
                       f"====================================================================\n"
                       f"【结构】共享 LoadImage(SUBJECT) → VAEEncode 提供 source_latent；\n"
                       f"  每个景别 = Positive/Negative GroundedEncode + EditModelPatch + KSampler + VAEDecode + SaveImage。\n\n"
                       f"【锁脸参考图】{ref_image}\n\n"
                       f"【景别】{', '.join(angles)}\n\n"
                       f"【参数】euler/simple/10步/CFG1 | {w}x{h}\n"
                       f"LoRA: D4rkL1nes(1.0) + Identity Edit(1.0)"
                       + (f" + {name}({char_lora_strength})" if char_lora else "")
                       + f"\n\n【输出】ComfyUI/output/krea2_锁脸/{safe_name}/",
                       "",
                       '<div style=""></div>'
                   ],
                   color="#444", bgcolor="#222")
    nodes.append(n_note)

    wf = {
        "id": f"denghuang-krea2-asset-角色_{name}_锁脸",
        "revision": 0,
        "last_node_id": nid,
        "last_link_id": lid,
        "nodes": nodes,
        "links": links,
        "groups": [],
        "config": {},
        "extra": {
            "note": f"Krea2 角色定妆锁脸工作流：{name} {len(angles)}景别\n"
                    f"技术栈：{unet} + D4rkL1nes(1.0) + Identity Edit(1.0)"
                    + (f" + {name} LoRA({char_lora_strength})" if char_lora else "")
                    + f"\n共享SUBJECT参考图({ref_image})经VAEEncode提供source_latent。\n"
                    f"景别：{', '.join(angles)}",
            "ds": {"scale": 0.8, "offset": [1000, 100]},
            "frontendVersion": "1.45.20"
        },
        "version": 0.4
    }
    return wf


# ── 2b. Scene (txt2img_batch with easy promptLine) ──

def build_scene_json(asset, parsed):
    """Build a Krea2 txt2img batch JSON for a scene asset.

    Uses easy promptLine node for batch prompt expansion,
    similar to the 2511 workflow's batch mechanism.
    """
    name = asset["name"]
    g = parsed["global"]

    unet = _get_global(g, "Krea2模型", "krea2_turbo_fp8.safetensors")
    clip = _get_global(g, "CLIP模型", "qwen3vl_4b_fp8_scaled.safetensors")
    vae = _get_global(g, "VAE模型", "qwen_image_vae.safetensors")
    style_lora = _get_global(g, "风格LoRA", "krea2/Krea2MythD4rkL1nes.safetensors")
    negative = _get_global(g, "负面提示词", DEFAULT_NEGATIVE)

    resolution = asset.get("分辨率", "1K 16:9")
    batch_prompts = asset.get("batch_prompts", [])

    # If only one prompt, use simple txt2img (no promptLine)
    use_batch = len(batch_prompts) > 1

    nodes = []
    links = []
    nid = 0
    lid = 0

    def next_nid():
        nonlocal nid
        nid += 1
        return nid

    def next_lid():
        nonlocal lid
        lid += 1
        return lid

    # UNETLoader
    nid_unet = next_nid()
    n_unet = _node(nid_unet, "UNETLoader", [-900, -340], "UNET Loader - Krea2",
                   widgets=[unet, "default"], color="#2a2", bgcolor="#040")
    n_unet["outputs"] = [{"name": "MODEL", "type": "MODEL", "slot_index": 0, "links": []}]
    nodes.append(n_unet)

    # CLIPLoader
    nid_clip = next_nid()
    n_clip = _node(nid_clip, "CLIPLoader", [-900, -180], "CLIP Loader - Krea2",
                   widgets=[clip, "krea2", "default"], color="#2a2", bgcolor="#040")
    n_clip["outputs"] = [{"name": "CLIP", "type": "CLIP", "slot_index": 0, "links": []}]
    nodes.append(n_clip)

    # VAELoader
    nid_vae = next_nid()
    n_vae = _node(nid_vae, "VAELoader", [-900, -20], "VAE Loader",
                  widgets=[vae], color="#2a2", bgcolor="#040")
    n_vae["outputs"] = [{"name": "VAE", "type": "VAE", "slot_index": 0, "links": []}]
    nodes.append(n_vae)

    # Krea2SizePreset
    nid_size = next_nid()
    # Parse resolution: "1K 16:9" or "1360x768"
    if "x" in resolution.lower():
        parts = resolution.lower().split("x")
        try:
            rw, rh = int(parts[0]), int(parts[1])
            # Use EmptySD3LatentImage for custom resolution
            nid_size = next_nid()
            n_size = _node(nid_size, "EmptySD3LatentImage", [-900, 140],
                           f"Latent ({rw}x{rh})", widgets=[rw, rh, 1],
                           color="#2a2", bgcolor="#040")
            n_size["outputs"] = [{"name": "LATENT", "type": "LATENT", "slot_index": 0, "links": []}]
            nodes.append(n_size)
        except Exception:
            n_size = _node(nid_size, "Krea2SizePreset", [-900, 140],
                           f"Size - {resolution}", widgets=["1K", "16:9", 1],
                           color="#2a2", bgcolor="#040")
            n_size["outputs"] = [{"name": "latent", "type": "LATENT", "slot_index": 0, "links": []}]
            nodes.append(n_size)
    else:
        parts = resolution.split()
        preset = parts[0] if parts else "1K"
        ratio = parts[1] if len(parts) > 1 else "16:9"
        n_size = _node(nid_size, "Krea2SizePreset", [-900, 140],
                       f"Size - {preset} {ratio}", widgets=[preset, ratio, 1],
                       color="#2a2", bgcolor="#040")
        n_size["outputs"] = [{"name": "latent", "type": "LATENT", "slot_index": 0, "links": []}]
        nodes.append(n_size)

    # LoraLoaderModelOnly (D4rkL1nes)
    nid_lora = next_nid()
    n_lora = _node(nid_lora, "LoraLoaderModelOnly", [-480, -340], "Lora - D4rkL1nes",
                   inputs=[{"name": "model", "type": "MODEL", "link": None}],
                   widgets=[style_lora, 1.0], color="#2a2", bgcolor="#040")
    n_lora["outputs"] = [{"name": "MODEL", "type": "MODEL", "slot_index": 0, "links": []}]
    nodes.append(n_lora)

    # easy promptLine (batch prompts) or direct CLIPTextEncode
    prompt_text = "\n".join(batch_prompts) if batch_prompts else "D4rkL1nes, scene"

    if use_batch:
        nid_promptline = next_nid()
        n_promptline = _node(nid_promptline, "easy promptLine", [-440, -500],
                             "Batch Prompts (easy promptLine)",
                             widgets=[prompt_text, 0, 1000, True],
                             color="#2a2", bgcolor="#040")
        n_promptline["outputs"] = [{"name": "STRING", "type": "STRING", "slot_index": 0, "links": []}]
        nodes.append(n_promptline)

    # Positive CLIPTextEncode
    nid_pos = next_nid()
    pos_inputs = [{"name": "clip", "type": "CLIP", "link": None}]
    if use_batch:
        pos_inputs.append({"name": "text", "type": "STRING", "link": None,
                           "widget": {"name": "text"}})
    n_pos = _node(nid_pos, "CLIPTextEncode", [-440, -340], "Positive",
                  inputs=pos_inputs,
                  widgets=[prompt_text] if not use_batch else [prompt_text],
                  color="#2a2", bgcolor="#040")
    n_pos["outputs"] = [{"name": "CONDITIONING", "type": "CONDITIONING", "slot_index": 0, "links": []}]
    nodes.append(n_pos)

    # Negative CLIPTextEncode (const)
    nid_neg = next_nid()
    n_neg = _node(nid_neg, "CLIPTextEncode", [-440, 180], "Negative (const)",
                  inputs=[{"name": "clip", "type": "CLIP", "link": None}],
                  widgets=[negative], color="#a44", bgcolor="#400")
    n_neg["outputs"] = [{"name": "CONDITIONING", "type": "CONDITIONING", "slot_index": 0, "links": []}]
    nodes.append(n_neg)

    # ConditioningZeroOut
    nid_zero = next_nid()
    n_zero = _node(nid_zero, "ConditioningZeroOut", [-10, 60], "Negative (Zero Out)",
                   inputs=[{"name": "conditioning", "type": "CONDITIONING", "link": None}],
                   color="#2a2", bgcolor="#040")
    n_zero["outputs"] = [{"name": "CONDITIONING", "type": "CONDITIONING", "slot_index": 0, "links": []}]
    nodes.append(n_zero)

    # KSampler
    nid_ks = next_nid()
    n_ks = _node(nid_ks, "KSampler", [420, -340], "KSampler - 8steps",
                 inputs=[{"name": "model", "type": "MODEL", "link": None},
                         {"name": "positive", "type": "CONDITIONING", "link": None},
                         {"name": "negative", "type": "CONDITIONING", "link": None},
                         {"name": "latent_image", "type": "LATENT", "link": None}],
                 widgets=[0, 8, 1, "euler_ancestral", "simple", 1],
                 color="#2a2", bgcolor="#040")
    n_ks["outputs"] = [{"name": "LATENT", "type": "LATENT", "slot_index": 0, "links": []}]
    nodes.append(n_ks)

    # VAEDecode
    nid_vaedec = next_nid()
    n_vaedec = _node(nid_vaedec, "VAEDecode", [840, -340], "VAE Decode",
                     inputs=[{"name": "samples", "type": "LATENT", "link": None},
                             {"name": "vae", "type": "VAE", "link": None}],
                     color="#2a2", bgcolor="#040")
    n_vaedec["outputs"] = [{"name": "IMAGE", "type": "IMAGE", "slot_index": 0, "links": []}]
    nodes.append(n_vaedec)

    # SaveImage
    nid_save = next_nid()
    safe_name = re.sub(r'[^\w\u4e00-\u9fff]', '_', name)
    n_save = _node(nid_save, "SaveImage", [1260, -340], "Save Asset",
                   inputs=[{"name": "images", "type": "IMAGE", "link": None}],
                   widgets=[f"assets/场景_{safe_name}"],
                   color="#2a2", bgcolor="#040")
    nodes.append(n_save)

    # ── Connect nodes ──
    # UNet → LoRA
    l = next_lid()
    links.append(_link(l, nid_unet, 0, nid_lora, 0, "MODEL"))
    n_unet["outputs"][0]["links"].append(l)
    n_lora["inputs"][0]["link"] = l

    # CLIP → Positive
    l = next_lid()
    links.append(_link(l, nid_clip, 0, nid_pos, 0, "CLIP"))
    n_clip["outputs"][0]["links"].append(l)
    n_pos["inputs"][0]["link"] = l

    # CLIP → Negative
    l = next_lid()
    links.append(_link(l, nid_clip, 0, nid_neg, 0, "CLIP"))
    n_clip["outputs"][0]["links"].append(l)
    n_neg["inputs"][0]["link"] = l

    # promptLine → Positive (text) if batch
    if use_batch:
        l = next_lid()
        links.append(_link(l, nid_promptline, 0, nid_pos, 1, "STRING"))
        n_promptline["outputs"][0]["links"].append(l)
        n_pos["inputs"][1]["link"] = l

    # Negative → ConditioningZeroOut
    l = next_lid()
    links.append(_link(l, nid_neg, 0, nid_zero, 0, "CONDITIONING"))
    n_neg["outputs"][0]["links"].append(l)
    n_zero["inputs"][0]["link"] = l

    # LoRA → KSampler (model)
    l = next_lid()
    links.append(_link(l, nid_lora, 0, nid_ks, 0, "MODEL"))
    n_lora["outputs"][0]["links"].append(l)
    n_ks["inputs"][0]["link"] = l

    # Positive → KSampler
    l = next_lid()
    links.append(_link(l, nid_pos, 0, nid_ks, 1, "CONDITIONING"))
    n_pos["outputs"][0]["links"].append(l)
    n_ks["inputs"][1]["link"] = l

    # ZeroOut → KSampler (negative)
    l = next_lid()
    links.append(_link(l, nid_zero, 0, nid_ks, 2, "CONDITIONING"))
    n_zero["outputs"][0]["links"].append(l)
    n_ks["inputs"][2]["link"] = l

    # Size → KSampler (latent)
    l = next_lid()
    links.append(_link(l, nid_size, 0, nid_ks, 3, "LATENT"))
    n_size["outputs"][0]["links"].append(l)
    n_ks["inputs"][3]["link"] = l

    # KSampler → VAEDecode
    l = next_lid()
    links.append(_link(l, nid_ks, 0, nid_vaedec, 0, "LATENT"))
    n_ks["outputs"][0]["links"].append(l)
    n_vaedec["inputs"][0]["link"] = l

    # VAE → VAEDecode
    l = next_lid()
    links.append(_link(l, nid_vae, 0, nid_vaedec, 1, "VAE"))
    n_vae["outputs"][0]["links"].append(l)
    n_vaedec["inputs"][1]["link"] = l

    # VAEDecode → SaveImage
    l = next_lid()
    links.append(_link(l, nid_vaedec, 0, nid_save, 0, "IMAGE"))
    n_vaedec["outputs"][0]["links"].append(l)
    n_save["inputs"][0]["link"] = l

    batch_info = f" | easy promptLine 批量({len(batch_prompts)}条)" if use_batch else " | 单图"

    wf = {
        "id": f"denghuang-krea2-asset-场景_{name}",
        "revision": 0,
        "last_node_id": nid,
        "last_link_id": lid,
        "nodes": nodes,
        "links": links,
        "groups": [],
        "config": {},
        "extra": {
            "note": f"Krea2 资产生成工作流：场景 - {name}\n"
                    f"技术栈：{unet} + D4rkL1nes LoRA(1.0) + 8步 euler_ancestral simple CFG1 + {resolution}。"
                    f"{batch_info}\n"
                    f"输出到 ComfyUI/output/assets/场景_{safe_name}/",
            "ds": {"scale": 0.8, "offset": [900, 700]},
            "frontendVersion": "1.45.20"
        },
        "version": 0.4
    }
    return wf


# ── 2c. Prop (txt2img 设定图) ──

def build_prop_json(asset, parsed):
    """Build a Krea2 txt2img JSON for a prop asset (concept art / 设定图)."""
    name = asset["name"]
    g = parsed["global"]

    unet = _get_global(g, "Krea2模型", "krea2_turbo_fp8.safetensors")
    clip = _get_global(g, "CLIP模型", "qwen3vl_4b_fp8_scaled.safetensors")
    vae = _get_global(g, "VAE模型", "qwen_image_vae.safetensors")
    style_lora = _get_global(g, "风格LoRA", "krea2/Krea2MythD4rkL1nes.safetensors")
    negative = _get_global(g, "负面提示词", DEFAULT_NEGATIVE)

    resolution = asset.get("分辨率", "1K 16:9")
    prompt = asset.get("提示词", f"D4rkL1nes, prop design sheet, {name}, pure white background")

    # Reuse scene builder structure but with single prompt
    asset_copy = copy.deepcopy(asset)
    asset_copy["batch_prompts"] = [prompt]
    asset_copy["name"] = name

    wf = build_scene_json(asset_copy, parsed)

    # Override IDs and save path
    wf["id"] = f"denghuang-krea2-asset-道具_{name}"
    safe_name = re.sub(r'[^\w\u4e00-\u9fff]', '_', name)

    # Update SaveImage filename_prefix
    for node in wf["nodes"]:
        if node["type"] == "SaveImage":
            node["widgets_values"] = [f"assets/道具_{safe_name}"]
            node["title"] = "Save Asset"

    wf["extra"]["note"] = (
        f"Krea2 资产生成工作流：道具 - {name}\n"
        f"技术栈：{unet} + D4rkL1nes LoRA(1.0) + 8步 euler_ancestral simple CFG1 + {resolution}。\n"
        f"单图生成，Seed 可调。输出到 ComfyUI/output/assets/道具_{safe_name}/"
    )

    return wf


# ──────────────────────────────────────────────
# 3. Main Processing
# ──────────────────────────────────────────────

def process_single(md_path, output_dir):
    """Process a single MD file and generate all JSON workflows."""
    parsed = parse_md(md_path)
    os.makedirs(output_dir, exist_ok=True)

    results = {"characters": 0, "scenes": 0, "props": 0, "files": []}

    # Generate character JSONs
    for char in parsed["characters"]:
        wf = build_character_json(char, parsed)
        safe_name = re.sub(r'[^\w\u4e00-\u9fff]', '_', char["name"])
        filename = f"krea2_资产生成_角色_{safe_name}_锁脸.json"
        out_path = os.path.join(output_dir, filename)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(wf, f, ensure_ascii=False, indent=1)
        results["characters"] += 1
        results["files"].append(out_path)
        print(f"  [角色] {char['name']} → {filename}")

    # Generate scene JSONs
    for scene in parsed["scenes"]:
        wf = build_scene_json(scene, parsed)
        safe_name = re.sub(r'[^\w\u4e00-\u9fff]', '_', scene["name"])
        filename = f"krea2_资产生成_场景_{safe_name}.json"
        out_path = os.path.join(output_dir, filename)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(wf, f, ensure_ascii=False, indent=1)
        results["scenes"] += 1
        results["files"].append(out_path)
        print(f"  [场景] {scene['name']} → {filename}")

    # Generate prop JSONs
    for prop in parsed["props"]:
        wf = build_prop_json(prop, parsed)
        safe_name = re.sub(r'[^\w\u4e00-\u9fff]', '_', prop["name"])
        filename = f"krea2_资产生成_道具_{safe_name}.json"
        out_path = os.path.join(output_dir, filename)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(wf, f, ensure_ascii=False, indent=1)
        results["props"] += 1
        results["files"].append(out_path)
        print(f"  [道具] {prop['name']} → {filename}")

    total = results["characters"] + results["scenes"] + results["props"]
    print(f"\n  总计: {total} 个 JSON (角色 {results['characters']}, 场景 {results['scenes']}, 道具 {results['props']})")
    return results


def process_batch(md_files, output_dir):
    """Process multiple MD files."""
    total = {"characters": 0, "scenes": 0, "props": 0, "files": []}
    for md_path in md_files:
        print(f"\n{'='*60}")
        print(f"Processing: {os.path.basename(md_path)}")
        print(f"{'='*60}")
        r = process_single(md_path, output_dir)
        for k in total:
            total[k] += r[k]
    return total


# ──────────────────────────────────────────────
# 4. CLI Entry
# ──────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="美术资产准备表 MD → Krea2 JSON")
    parser.add_argument("input", help="Input MD file path (or directory for batch)")
    parser.add_argument("output", help="Output directory for JSON files")
    parser.add_argument("--batch", action="store_true", help="Batch process all MD files in input directory")
    args = parser.parse_args()

    if args.batch or os.path.isdir(args.input):
        md_files = []
        for f in sorted(os.listdir(args.input)):
            if f.endswith(".md") and ("美术资产准备表" in f or "asset_prep" in f.lower()):
                md_files.append(os.path.join(args.input, f))
        if not md_files:
            print(f"No 美术资产准备表 MD files found in {args.input}")
            sys.exit(1)
        print(f"Found {len(md_files)} MD files")
        process_batch(md_files, args.output)
    else:
        process_single(args.input, args.output)
