# 官方 H3 提示词编写工作流与输出规则

> 本文件浓缩自 MiniMax 官方 `h3-prompt-writing` Skill 的工作流与输出规则正文。完整格式细节（三核心字段、摄影机运动表、对白格式、Ref2VA 六段式）见 `04_项目规范` 下 `06_H3提示词指南/h3_base_prompt_guide.md` 与 `h3_ref_prompt_guide.md`。

---

## 1. Workflow（官方标准流程）

1. **识别输入模式**：T2VA、I2VA、FL2VA、L2VA，或全参考 Ref2VA。
2. **基础文本/关键帧模式**：读取 `h3_base_prompt_guide.md`，遵循其最终提示词结构。
3. **全参考模式（Ref2VA）**：读取 `h3_ref_prompt_guide.md`，遵循其六段式改写格式。
4. **严格保留**：所选指南中的字段名、段落顺序、标签、时间记法。

## 2. Base Modes（四种基础模式）

| 模式 | 定义 | 使用字段 |
|------|------|---------|
| T2VA | 从文本构建完整音视频时间线 | `integrated_multimodal_description` / `overall_soundscape` / `non_diegetic_music` |
| I2VA | 从首帧出发向前发展 | 同上 + 首帧指令 |
| FL2VA | 描述首尾帧之间的连续路径 | 同上 + 首尾帧对齐指令 |
| L2VA | 推断合理开场并收敛到末帧 | 同上 + 末帧对齐指令 |

三核心字段须按 `base-en.txt` 中所示顺序输出。

## 3. Full-Reference Mode（Ref2VA 全参考模式）

改写使用六个部分，顺序固定：

`subject_definitions` → `summary` → `retention_analysis` → `detailed_description` → `overall_soundscape` → `non_diegetic_music`

参考标签（`<Subject N>` / `<Picture N>` / `<Video N>` / `<Audio N>`）在所有部分保持一致含义。

## 4. Output Rules（输出规则）

- 改写部分用英文书写；对白、歌词、画面可见场景文字保留原语言。
- 逐镜描述构图、主体、环境、动作、镜头、声音，以及参考内容实际出现/生效的确切时间点。
- **避免**：情节概述、未解析的参考标签、与请求时长不匹配的时间记法。

---

## 5. 项目落地要点

- 本项目主要使用 **Ref2VA** 模式：角色定妆照 + 场景参考图驱动，对应六段式结构。
- 三核心字段中，`integrated_multimodal_description`（或 Ref2VA 的 `detailed_description`）为正文主体，描述随 12.2 节 MD 格式输出。
- `non_diegetic_music` 本项目统一为 `N/A`（禁止任何配乐/音乐），仅保留环境音与音效。
- 对白用稳定说话人 ID `(S1)/(S2)` + `<d>[语言] 内容</d>`；画外音用 `says in an off-screen voiceover` 并标注嘴唇闭合。
- 摄影机运动按"运动类型 + 幅度 + 速度"三维度写为自然英文动作，不堆叠标签。
