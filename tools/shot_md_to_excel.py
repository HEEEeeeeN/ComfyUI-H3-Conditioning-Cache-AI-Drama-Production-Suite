#!/usr/bin/env python3
"""
shot_md_to_excel.py
===================
分镜头需求 MD → 提示词审阅表 Excel

读取分镜导演 Skill 生成的分镜头需求 MD 文件，
生成包含提示词审阅和美术资产路径映射的 Excel 表格。

用法:
  单文件:  python shot_md_to_excel.py <input.md> <output.xlsx>
  批量:    python shot_md_to_excel.py -o <输出目录> <file1.md> [file2.md ...]

Excel 结构:
  Sheet1 "提示词审阅" — 按镜头序号分行，列含场景/镜头调度/约束条件/镜头风格/
                        完整提示词/参演角色×3/场景设置×2/参演道具×3/修改指令
  Sheet2 "美术资产路径" — 用户在此填写角色/场景/道具的 input 路径
"""

import re
import sys
import os
import argparse
from pathlib import Path

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from openpyxl.worksheet.datavalidation import DataValidation
except ImportError:
    print("ERROR: openpyxl not found. Install with: pip install openpyxl")
    sys.exit(1)


# ── MD 解析 ──────────────────────────────────────────────────────────

def parse_md(md_path):
    """解析分镜头需求 MD 文件，返回 (global_info, shots)。"""
    text = Path(md_path).read_text(encoding="utf-8")

    global_info = {}
    g_match = re.search(r"## 全局信息\s*\n(.*?)(?=## 镜头列表)", text, re.DOTALL)
    if g_match:
        for line in g_match.group(1).strip().splitlines():
            m = re.match(r"-\s*\*\*(.+?)\*\*:\s*(.+)", line)
            if m:
                global_info[m.group(1)] = m.group(2).strip()

    shots = []
    parts = re.split(r"^### ", text, flags=re.MULTILINE)
    for part in parts:
        part = part.strip()
        if not part or part.startswith("#"):
            continue
        lines = part.splitlines()
        shot_id = lines[0].strip()
        if not re.match(r"^[A-Z]\d+", shot_id):
            continue

        shot = {"id": shot_id}
        for line in lines[1:]:
            line = line.strip()
            m = re.match(r"-\s*\*\*(.+?)\*\*:\s*(.*)", line)
            if m:
                key, val = m.group(1), m.group(2).strip()
                shot[key] = val

        prompt_match = re.search(
            r"#### 完整 H3 提示词\s*\n(.*?)(?=#### |^---|\Z)", part, re.DOTALL
        )
        if prompt_match:
            shot["完整提示词"] = prompt_match.group(1).strip()

        shots.append(shot)

    return global_info, shots


# ── Excel 生成 ───────────────────────────────────────────────────────

HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
HEADER_FILL = PatternFill(start_color="3F789E", end_color="3F789E", fill_type="solid")
HEADER_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)
THIN_BORDER = Border(
    left=Side(style="thin", color="CCCCCC"),
    right=Side(style="thin", color="CCCCCC"),
    top=Side(style="thin", color="CCCCCC"),
    bottom=Side(style="thin", color="CCCCCC"),
)

SHEET1_HEADERS = [
    "镜头编号", "时长", "分辨率", "场景", "镜头调度", "约束条件", "镜头风格",
    "完整提示词",
    "参演角色1", "参演角色2", "参演角色3",
    "场景设置1", "场景设置2",
    "参演道具1", "参演道具2", "参演道具3",
    "修改指令",
]

SHEET2_HEADERS = ["类型", "名称", "input路径"]


def _split_assets(val):
    if not val or val == "(无)":
        return []
    return [s.strip() for s in val.split(",") if s.strip() and s.strip() != "(无)"]


def _col_letter(col_idx):
    if col_idx <= 26:
        return chr(64 + col_idx)
    return chr(64 + (col_idx - 1) // 26) + chr(65 + (col_idx - 1) % 26)


def generate_excel(global_info, shots, output_path):
    """生成 Excel 审阅表。返回 (n_shots, n_assets) 元组。"""
    wb = Workbook()

    # ── Sheet1: 提示词审阅 ──
    ws1 = wb.active
    ws1.title = "提示词审阅"

    for col, header in enumerate(SHEET1_HEADERS, 1):
        cell = ws1.cell(row=1, column=col, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGN
        cell.border = THIN_BORDER

    for row_idx, shot in enumerate(shots, 2):
        characters = _split_assets(shot.get("参演角色", ""))
        scene_refs = _split_assets(shot.get("场景参考", ""))
        props = _split_assets(shot.get("道具参考", ""))

        if not characters and scene_refs:
            characters = ["(纯场景)"]

        row_data = [
            shot["id"],
            shot.get("时长", ""),
            shot.get("分辨率", "0.5"),
            shot.get("场景 / Scene", shot.get("场景", "")),
            shot.get("镜头调度 / Camera", shot.get("镜头调度", "")),
            shot.get("约束条件 / Constraints", shot.get("约束条件", "")),
            shot.get("镜头风格 / Style", shot.get("镜头风格", "")),
            shot.get("完整提示词", ""),
            characters[0] if len(characters) > 0 else "",
            characters[1] if len(characters) > 1 else "",
            characters[2] if len(characters) > 2 else "",
            scene_refs[0] if len(scene_refs) > 0 else "",
            scene_refs[1] if len(scene_refs) > 1 else "",
            props[0] if len(props) > 0 else "",
            props[1] if len(props) > 1 else "",
            props[2] if len(props) > 2 else "",
            "",
        ]

        for col, value in enumerate(row_data, 1):
            cell = ws1.cell(row=row_idx, column=col, value=value)
            cell.border = THIN_BORDER
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    col_widths = {
        1: 10, 2: 8, 3: 10, 4: 30, 5: 30, 6: 25, 7: 20,
        8: 60, 9: 12, 10: 12, 11: 12,
        12: 15, 13: 15, 14: 12, 15: 12, 16: 12, 17: 30,
    }
    for col, width in col_widths.items():
        ws1.column_dimensions[_col_letter(col)].width = width
    ws1.freeze_panes = "A2"

    # 分辨率列下拉验证 (C列, 0.4 / 0.5)
    res_dv = DataValidation(type="list", formula1='"0.4,0.5"', allow_blank=False)
    res_dv.error = "请选择 0.4 或 0.5"
    res_dv.errorTitle = "无效分辨率"
    res_dv.prompt = "H3分辨率系数: 0.5=高清, 0.4=经济"
    res_dv.promptTitle = "分辨率选择"
    ws1.add_data_validation(res_dv)
    for row_idx in range(2, len(shots) + 2):
        res_dv.add(ws1.cell(row=row_idx, column=3))

    # ── Sheet2: 美术资产路径 ──
    ws2 = wb.create_sheet("美术资产路径")
    for col, header in enumerate(SHEET2_HEADERS, 1):
        cell = ws2.cell(row=1, column=col, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGN
        cell.border = THIN_BORDER

    asset_set = set()
    for shot in shots:
        for c in _split_assets(shot.get("参演角色", "")):
            asset_set.add(("角色", c))
        for s in _split_assets(shot.get("场景参考", "")):
            asset_set.add(("场景", s))
        for p in _split_assets(shot.get("道具参考", "")):
            asset_set.add(("道具", p))

    type_order = {"角色": 0, "场景": 1, "道具": 2}
    sorted_assets = sorted(asset_set, key=lambda x: (type_order.get(x[0], 9), x[1]))

    for row_idx, (asset_type, asset_name) in enumerate(sorted_assets, 2):
        ws2.cell(row=row_idx, column=1, value=asset_type).border = THIN_BORDER
        ws2.cell(row=row_idx, column=2, value=asset_name).border = THIN_BORDER
        ws2.cell(row=row_idx, column=3, value="").border = THIN_BORDER

    ws2.column_dimensions["A"].width = 10
    ws2.column_dimensions["B"].width = 30
    ws2.column_dimensions["C"].width = 70
    ws2.freeze_panes = "A2"

    # ── Sheet3: 说明 ──
    ws3 = wb.create_sheet("说明")
    notes = [
        "提示词审阅表使用说明",
        "",
        "1. Sheet1「提示词审阅」",
        "   - 每行一镜，从分镜头需求 MD 自动提取",
        "   - 时长 列可审阅修改（单位: 秒，直接编辑数值）",
        "   - 分辨率 列下拉选择 0.4 或 0.5（对应 H3 节点分辨率系数，0.5=高清, 0.4=经济）",
        "   - 场景/镜头调度/约束条件/镜头风格 列从 H3 提示词要素提取",
        "   - 完整提示词 列为 H3 三核心字段原文",
        "   - 参演角色1-3 / 场景设置1-2 / 参演道具1-3 从美术资产需求提取",
        "   - 修改指令 列留空，供审阅时填写修改意见",
        "",
        "2. Sheet2「美术资产路径」",
        "   - 自动收集所有镜头涉及的资产（角色/场景/道具），去重排列",
        "   - 在 input路径 列填写对应的 ComfyUI input 目录相对路径",
        "   - 示例: h3_ref/角色/黑猫沈天然/沈天然黑猫定妆照.png",
        "",
        "3. 审阅完成后",
        "   - 保存 Excel，运行 excel_to_multi_chain_json.py 生成多链生产 JSON",
        "   - 用法: python excel_to_multi_chain_json.py <审阅表.xlsx> <输出目录>",
    ]
    for row_idx, note in enumerate(notes, 1):
        ws3.cell(row=row_idx, column=1, value=note)
    ws3.column_dimensions["A"].width = 80

    wb.save(output_path)
    return len(shots), len(sorted_assets)


def collect_assets_from_md(md_path):
    """从单个 MD 文件收集资产清单，返回 dict: {type: set(names)}。
    type 为 "角色"/"场景"/"道具"。
    """
    _, shots = parse_md(md_path)
    assets = {"角色": set(), "场景": set(), "道具": set()}
    for shot in shots:
        for c in _split_assets(shot.get("参演角色", "")):
            assets["角色"].add(c)
        for s in _split_assets(shot.get("场景参考", "")):
            assets["场景"].add(s)
        for p in _split_assets(shot.get("道具参考", "")):
            assets["道具"].add(p)
    return assets


def collect_assets_batch(md_files):
    """从多个 MD 文件收集全局资产清单。
    返回 dict: {"global": {type: set}, "episodes": {stem: {type: set}}}
    """
    global_assets = {"角色": set(), "场景": set(), "道具": set()}
    episodes = {}
    for md_path in md_files:
        stem = Path(md_path).stem
        ep_assets = collect_assets_from_md(md_path)
        episodes[stem] = ep_assets
        for t in global_assets:
            global_assets[t] |= ep_assets[t]
    return {"global": global_assets, "episodes": episodes}


def save_asset_mapping(mapping, output_path):
    """将资产映射保存为 JSON 文件。
    mapping 格式: {"角色": {"黑猫": "path/to.png", ...}, "场景": {...}, "道具": {...}}
    """
    import json
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)


def process_single(md_path, output_path, log=print):
    """处理单个 MD 文件，返回 True/False。"""
    if not Path(md_path).exists():
        log(f"ERROR: file not found: {md_path}")
        return False

    global_info, shots = parse_md(md_path)
    if not shots:
        log(f"ERROR: no shots parsed from {md_path}, check MD format")
        return False

    log(f"  解析: {len(shots)} shots")
    if global_info:
        log(f"  画幅: {global_info.get('画幅', '?')}, "
            f"总时长: {global_info.get('总时长', '?')}, "
            f"模式: {global_info.get('提示词模式', '?')}")

    n_shots, n_assets = generate_excel(global_info, shots, output_path)
    log(f"  生成: {output_path} ({n_shots} shots, {n_assets} assets)")
    return True


def process_batch(md_files, output_dir, log=print):
    """批量处理多个 MD 文件。每个 MD 生成同名 .xlsx 到 output_dir。"""
    os.makedirs(output_dir, exist_ok=True)
    success, fail = 0, 0

    for md_path in md_files:
        stem = Path(md_path).stem
        out_name = stem.replace("分镜头需求", "提示词审阅表") + ".xlsx"
        out_path = os.path.join(output_dir, out_name)
        log(f"\n[{success + fail + 1}/{len(md_files)}] {Path(md_path).name}")

        if process_single(md_path, out_path, log=log):
            success += 1
        else:
            fail += 1

    log(f"\n批量完成: {success} 成功, {fail} 失败, 共 {len(md_files)} 文件")
    return success, fail


def main():
    parser = argparse.ArgumentParser(
        description="分镜头需求 MD → 提示词审阅表 Excel"
    )
    parser.add_argument("files", nargs="+", help="MD 文件路径（一个或多个）")
    parser.add_argument("-o", "--output", help="输出目录（批量模式）或输出文件路径（单文件模式）")
    args = parser.parse_args()

    md_files = args.files

    # 单文件模式: python shot_md_to_excel.py input.md output.xlsx
    if len(md_files) == 1 and args.output and not os.path.isdir(args.output):
        if not args.output.endswith("/"):
            process_single(md_files[0], args.output)
            return
        # output 是目录，走批量
        output_dir = args.output
    elif len(md_files) == 1 and not args.output:
        # 没有指定输出，同名 .xlsx
        stem = Path(md_files[0]).stem
        out_name = stem.replace("分镜头需求", "提示词审阅表") + ".xlsx"
        process_single(md_files[0], out_name)
        return
    else:
        # 批量模式
        output_dir = args.output or os.path.dirname(md_files[0]) or "."

    process_batch(md_files, output_dir)


if __name__ == "__main__":
    main()
