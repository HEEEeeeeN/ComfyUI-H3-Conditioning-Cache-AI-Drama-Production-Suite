"""H3 for-loop batch generation nodes.

Ported from ComfyUI_GJJ_Nodes (gjj_for_loop.py) into this plugin so the H3
conditioning-cache pipeline does not depend on the GJJ plugin.

The structure mirrors the reference `for.json` workflow:
    H3LoadConditioningList  (batch: resolve .pt file PATHS only — NO tensors loaded)
                 |
    H3ForLoopStart(total=N) -> index
                 |
    H3ConditioningIndex(paths, index) -> lazy-load ONE .pt -> CONDITIONING
                 |
          [single processing chain: model already loaded outside]
                 |
    H3ForLoopEnd(flow, ...) -> feeds next round / final output

Loops are expanded at execution time via ComfyUI's GraphBuilder + dynprompt,
so the loop body (conditioning load -> sample -> decode -> save -> free)
is a SINGLE chain that is cloned once per iteration, instead of N parallel
chains. Model / VAE / CLIP / LoRA loaders stay OUTSIDE the loop and are
shared across all iterations.

**Lazy loading (VRAM-safe)**: H3LoadConditioningList only resolves file paths
and returns them as a list — it does NOT load any .pt tensors into memory.
H3ConditioningIndex loads ONE .pt file per iteration inside the loop body,
so GPU memory usage stays constant regardless of batch size (400+ shots).
"""

from __future__ import annotations

from typing import Any

import os
import torch
import folder_paths
import comfy.model_management as model_management

try:
    from comfy_execution.graph import ExecutionBlocker
    from comfy_execution.graph_utils import GraphBuilder, is_link
except Exception:  # pragma: no cover
    ExecutionBlocker = None
    GraphBuilder = None
    is_link = None

# Reuse the cache helpers from h3_conditioning_cache.py
from .h3_conditioning_cache import (
    _convert_from_serializable,
    _extract_conditioning_and_meta,
    _compute_frame_count,
    _move_to_device,
    _resolve_cache_path,
    _scan_pt_files,
)

MAX_FLOW_NUM = 20
ANY_TYPE = "*"
H3_COND_BATCH = "H3_COND_BATCH"
H3_COND_NAMES = "H3_COND_NAMES"


class AlwaysEqualProxy(str):
    def __eq__(self, _: object) -> bool:
        return True

    def __ne__(self, _: object) -> bool:
        return False


class TautologyStr(str):
    def __ne__(self, _: object) -> bool:
        return False


class ByPassTypeTuple(tuple):
    def __getitem__(self, index):
        if isinstance(index, int) and index > 0:
            index = 0
        item = super().__getitem__(index)
        if isinstance(item, str):
            return TautologyStr(item)
        return item


any_type = AlwaysEqualProxy(ANY_TYPE)


def _require_graph_builder() -> None:
    if GraphBuilder is None or is_link is None:
        raise RuntimeError("当前 ComfyUI 缺少 GraphBuilder，无法执行 H3 循环节点。")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _candidate_node_ids(dynprompt: Any, node_id: Any) -> list[Any]:
    ids: list[Any] = []
    if node_id is not None:
        ids.append(node_id)
    try:
        display_id = dynprompt.get_display_node_id(node_id)
        if display_id not in ids:
            ids.append(display_id)
    except Exception:
        pass
    return ids


def _read_start_total(dynprompt: Any, open_node_id: Any, fallback: int = 1) -> Any:
    if dynprompt is None:
        return fallback
    for candidate_id in _candidate_node_ids(dynprompt, open_node_id):
        try:
            node = dynprompt.get_node(candidate_id)
        except Exception:
            continue
        class_type = node.get("class_type")
        inputs = node.get("inputs", {})
        if class_type in ("H3ForLoopStart", "H3ForLoopWhileStart"):
            if class_type == "H3ForLoopStart":
                total = inputs.get("total", fallback)
                if is_link is not None and is_link(total):
                    # total 接了外部链接（如 H3LoadConditioningList.count）。
                    # 循环展开图里无法引用循环外节点，终止条件会失效导致循环不终止。
                    # 这里尝试从链接上游节点的 shots 列表推断数量作为兜底。
                    try:
                        upstream = dynprompt.get_node(total[0])
                        if upstream.get("class_type") == "H3LoadConditioningList":
                            shots = upstream.get("inputs", {}).get("shots", "")
                            if isinstance(shots, str):
                                n = len([s for s in shots.split(",") if s.strip()])
                            elif isinstance(shots, (list, tuple)):
                                n = len([s for s in shots if str(s).strip()])
                            else:
                                n = 0
                            if n > 0:
                                _log("警告: total 接了 count 链接，从 shots 列表推断数量 "
                                     f"= {n}。建议直接在 total 填数值更可靠。")
                                return n
                    except Exception:
                        pass
                    _log("警告: total 是外部链接且无法解析数值，循环终止条件可能失效。"
                         "请在 H3ForLoopStart 的 total 填入实际镜头数(数值)。")
                return total
            return inputs.get("condition", fallback)
    return fallback


def _total_status_text(total: Any, fallback: int = 1) -> str:
    if is_link is not None and is_link(total):
        return "外部输入"
    return str(max(1, _safe_int(total, fallback)))


def _log(msg: str) -> None:
    try:
        print(f"[H3Cache] {msg}")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Internal while-loop nodes (created by the start/end nodes at expansion time)
# ---------------------------------------------------------------------------
class H3ForLoopWhileStart:
    CATEGORY = "H3Cache/循环"
    DEPRECATED = True

    @classmethod
    def INPUT_TYPES(cls):
        inputs = {
            "required": {
                "condition": ("BOOLEAN", {"default": True, "display_name": "是否继续"}),
            },
            "optional": {},
        }
        for i in range(MAX_FLOW_NUM):
            inputs["optional"][f"initial_value{i}"] = (
                any_type,
                {"display_name": f"初始值 {i}"},
            )
        return inputs

    RETURN_TYPES = ByPassTypeTuple(tuple(["FLOW_CONTROL"] + [any_type] * MAX_FLOW_NUM))
    RETURN_NAMES = ByPassTypeTuple(tuple(["循环控制"] + [f"值{i}" for i in range(MAX_FLOW_NUM)]))
    FUNCTION = "while_loop_open"

    def while_loop_open(self, condition: bool, **kwargs):
        values = []
        for i in range(MAX_FLOW_NUM):
            value = kwargs.get(f"initial_value{i}", None)
            values.append(value if condition else ExecutionBlocker(None))
        return tuple(["stub"] + values)


class H3ForLoopIntAdd:
    CATEGORY = "H3Cache/循环"
    DEPRECATED = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "a": ("INT", {"default": 0, "display_name": "数值 A"}),
                "b": ("INT", {"default": 1, "display_name": "数值 B"}),
            },
            "optional": {
                "status_total": ("STRING", {"default": "", "display_name": "总轮次"}),
            },
        }

    RETURN_TYPES = ("INT",)
    RETURN_NAMES = ("结果",)
    FUNCTION = "add"

    def add(self, a: int, b: int, status_total: str = ""):
        result = _safe_int(a) + _safe_int(b)
        total_value = _safe_int(status_total, 0)
        if total_value > 0:
            _log(f"循环进度：第 {min(result + 1, total_value)} / {total_value} 轮")
        return (result,)


class H3ForLoopIntLess:
    CATEGORY = "H3Cache/循环"
    DEPRECATED = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "a": ("INT", {"default": 0, "display_name": "数值 A"}),
                "b": ("INT", {"default": 1, "display_name": "数值 B"}),
            }
        }

    RETURN_TYPES = ("BOOLEAN",)
    RETURN_NAMES = ("是否小于",)
    FUNCTION = "less"

    def less(self, a: int, b: int):
        return (_safe_int(a) < _safe_int(b),)


class H3ForLoopWhileEnd:
    CATEGORY = "H3Cache/循环"
    DEPRECATED = True

    @classmethod
    def INPUT_TYPES(cls):
        inputs = {
            "required": {
                "flow": ("FLOW_CONTROL", {"rawLink": True, "display_name": "循环控制"}),
                "condition": ("BOOLEAN", {"display_name": "是否继续"}),
            },
            "optional": {},
            "hidden": {
                "dynprompt": "DYNPROMPT",
                "unique_id": "UNIQUE_ID",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }
        for i in range(MAX_FLOW_NUM):
            inputs["optional"][f"initial_value{i}"] = (
                any_type,
                {"display_name": f"循环值 {i}"},
            )
        return inputs

    RETURN_TYPES = ByPassTypeTuple(tuple([any_type] * MAX_FLOW_NUM))
    RETURN_NAMES = ByPassTypeTuple(tuple([f"值{i}" for i in range(MAX_FLOW_NUM)]))
    FUNCTION = "while_loop_close"

    def explore_dependencies(self, node_id: Any, dynprompt: Any, upstream: dict) -> None:
        """从 node_id 向上遍历，建立 parent -> [children] 反向邻接表。

        只记录真实存在的节点，不做任何输出节点/复合 id 的伪造扩展，
        避免把循环外的共享上游节点（H3LoadConditioningList、模型加载器等）
        误收进循环体导致每轮克隆翻倍。
        """
        node_info = dynprompt.get_node(node_id)
        if "inputs" not in node_info:
            return
        for value in node_info["inputs"].values():
            if is_link(value):
                parent_id = value[0]
                if parent_id not in upstream:
                    upstream[parent_id] = []
                    self.explore_dependencies(parent_id, dynprompt, upstream)
                upstream[parent_id].append(node_id)

    def collect_contained(self, node_id: Any, upstream: dict, contained: dict) -> None:
        """从 open_node（WhileStart）向下遍历，只收集循环体内部节点。

        循环体 = WhileStart 下游、WhileEnd 上游的节点。共享上游节点
        （如 H3LoadConditioningList、ResolutionSelector、模型加载器）不是
        WhileStart 的子节点，天然不会被收集，因此不会被克隆、不会逐轮翻倍。
        """
        if node_id not in upstream:
            return
        for child_id in upstream[node_id]:
            if child_id not in contained:
                contained[child_id] = True
                self.collect_contained(child_id, upstream, contained)

    def while_loop_close(self, flow, condition, dynprompt=None, unique_id=None, **kwargs):
        _require_graph_builder()
        if not condition:
            return tuple(kwargs.get(f"initial_value{i}", None) for i in range(MAX_FLOW_NUM))

        upstream: dict[Any, list] = {}
        self.explore_dependencies(unique_id, dynprompt, upstream)

        contained = {}
        open_node = flow[0]
        self.collect_contained(open_node, upstream, contained)
        contained[unique_id] = True
        contained[open_node] = True

        graph = GraphBuilder()
        for node_id in contained:
            original_node = dynprompt.get_node(node_id)
            node = graph.node(original_node["class_type"], "Recurse" if node_id == unique_id else node_id)
            node.set_override_display_id(node_id)
        for node_id in contained:
            original_node = dynprompt.get_node(node_id)
            node = graph.lookup_node("Recurse" if node_id == unique_id else node_id)
            for key, value in original_node["inputs"].items():
                if is_link(value) and value[0] in contained:
                    parent = graph.lookup_node(value[0])
                    node.set_input(key, parent.out(value[1]))
                else:
                    node.set_input(key, value)

        new_open = graph.lookup_node(open_node)
        for i in range(MAX_FLOW_NUM):
            key = f"initial_value{i}"
            new_open.set_input(key, kwargs.get(key, None))
        my_clone = graph.lookup_node("Recurse")
        return {
            "result": tuple(my_clone.out(i) for i in range(MAX_FLOW_NUM)),
            "expand": graph.finalize(),
        }


# ---------------------------------------------------------------------------
# Public for-loop nodes
# ---------------------------------------------------------------------------
class H3ForLoopStart:
    CATEGORY = "H3Cache/循环"
    DESCRIPTION = "H3 For 循环开始节点。默认只显示一组初始值/值输出；值 1 连线后自动扩展值 2。"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "total": ("INT", {"default": 1, "min": 1, "max": 100000, "step": 1, "display_name": "总循环次数"}),
            },
            "optional": {
                f"initial_value{i}": (any_type, {"display_name": f"初始值 {i}"})
                for i in range(1, MAX_FLOW_NUM)
            },
            "hidden": {
                "initial_value0": (any_type,),
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
                "unique_id": "UNIQUE_ID",
            },
        }

    RETURN_TYPES = ByPassTypeTuple(tuple(["FLOW_CONTROL", "INT"] + [any_type] * (MAX_FLOW_NUM - 1)))
    RETURN_NAMES = ByPassTypeTuple(tuple(["循环控制", "当前序号"] + [f"值 {i}" for i in range(1, MAX_FLOW_NUM)]))
    FUNCTION = "for_loop_start"

    def for_loop_start(self, total: int, prompt=None, extra_pnginfo=None, unique_id=None, **kwargs):
        _require_graph_builder()
        total = max(1, _safe_int(total, 1))
        index = _safe_int(kwargs.get("initial_value0", 0), 0)
        _log(f"For循环开始：第 {index + 1} / {total} 轮")

        initial_values = {f"initial_value{i}": kwargs.get(f"initial_value{i}", None) for i in range(1, MAX_FLOW_NUM)}
        graph = GraphBuilder()
        graph.node("H3ForLoopWhileStart", condition=total, initial_value0=index, **initial_values)
        outputs = [kwargs.get(f"initial_value{i}", None) for i in range(1, MAX_FLOW_NUM)]
        return {
            "result": tuple(["stub", index] + outputs),
            "expand": graph.finalize(),
        }


class H3ForLoopEnd:
    CATEGORY = "H3Cache/循环"
    DESCRIPTION = "H3 For 循环结束节点。接收本轮更新后的 value，并决定是否展开下一轮。"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "flow": ("FLOW_CONTROL", {"rawLink": True, "display_name": "循环控制"}),
            },
            "optional": {
                f"initial_value{i}": (any_type, {"rawLink": True, "display_name": f"值 {i}"})
                for i in range(1, MAX_FLOW_NUM)
            },
            "hidden": {
                "dynprompt": "DYNPROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
                "unique_id": "UNIQUE_ID",
            },
        }

    RETURN_TYPES = ByPassTypeTuple(tuple([any_type] * (MAX_FLOW_NUM - 1)))
    RETURN_NAMES = ByPassTypeTuple(tuple([f"值 {i}" for i in range(1, MAX_FLOW_NUM)]))
    FUNCTION = "for_loop_end"

    def for_loop_end(self, flow, dynprompt=None, extra_pnginfo=None, unique_id=None, **kwargs):
        _require_graph_builder()
        graph = GraphBuilder()
        while_open = flow[0]
        total = _read_start_total(dynprompt, while_open, 1)
        total_text = _total_status_text(total, 1)

        next_index = graph.node(
            "H3ForLoopIntAdd",
            a=[while_open, 1],
            b=1,
            status_total=str(total if not (is_link is not None and is_link(total)) else ""),
        )
        condition = graph.node("H3ForLoopIntLess", a=next_index.out(0), b=total)
        input_values = {f"initial_value{i}": kwargs.get(f"initial_value{i}", None) for i in range(1, MAX_FLOW_NUM)}
        while_close = graph.node(
            "H3ForLoopWhileEnd",
            flow=flow,
            condition=condition.out(0),
            initial_value0=next_index.out(0),
            **input_values,
        )

        _log(f"For循环回传：准备判断下一轮，总循环 {total_text} 轮")
        return {
            "result": tuple(while_close.out(i) for i in range(1, MAX_FLOW_NUM)),
            "expand": graph.finalize(),
        }


# ---------------------------------------------------------------------------
# Batch load all .pt into a single list, then index into it inside the loop
# ---------------------------------------------------------------------------
class H3LoadConditioningList:
    """Resolve selected .pt file paths into a list (does NOT load tensors into
    memory). Pair with H3ConditioningIndex inside a for loop to load one shot
    at a time, preventing VRAM exhaustion on large batches (400+ shots).
    
    Returns file PATHS (strings) in cond_list, not loaded conditioning tensors.
    The actual tensor loading happens lazily in H3ConditioningIndex per iteration.
    Select .pt files from the multi-select dropdown; leave empty to resolve
    every .pt found in the cache dirs."""

    @classmethod
    def INPUT_TYPES(cls):
        files = _scan_pt_files()
        return {
            "required": {
                "shots": ("COMBO", {
                    "multiselect": True,
                    "default": [],
                    "options": files or [""],
                    "tooltip": "选择要加载的 .pt 文件（可多选，支持搜索过滤）。留空则加载缓存目录下全部 .pt。",
                }),
            },
            "optional": {
                "cache_dir": ("STRING", {
                    "default": "",
                    "tooltip": "自定义缓存目录（绝对路径）。留空则自动搜索 output、output/h3_cond_cache 与 input 目录。",
                }),
            },
        }

    RETURN_TYPES = (H3_COND_BATCH, H3_COND_NAMES, "INT")
    RETURN_NAMES = ("cond_list", "shot_names", "count")
    FUNCTION = "load"
    CATEGORY = "H3Cache"

    def load(self, shots, cache_dir=""):
        if cache_dir is None:
            cache_dir = ""
        # shots 可能是多选列表、逗号字符串或 None
        if isinstance(shots, str):
            names = [s.strip() for s in shots.split(",") if s.strip()]
        elif isinstance(shots, (list, tuple)):
            names = [str(s).strip() for s in shots if str(s).strip()]
        else:
            names = []
        if not names:
            names = [os.path.splitext(n)[0] for n in _scan_pt_files()]

        # 只解析文件路径，不加载张量到显存
        # cond_list 存储的是文件路径字符串，而非 conditioning 张量
        # 实际张量加载在 H3ConditioningIndex 中按索引懒加载
        paths = []
        for nm in names:
            fname = nm if nm.endswith(".pt") else nm + ".pt"
            path = _resolve_cache_path(fname, cache_dir)
            paths.append(path)
            mb = os.path.getsize(path) / (1024 * 1024)
            _log(f"resolved {nm} <- {path} ({mb:.1f} MB)")
        _log(f"resolved {len(paths)} shot(s) (lazy: paths only, no tensors loaded)")
        return (paths, names, len(paths))


class H3ShotNameByIndex:
    """Pull the shot name at `index` out of an H3_COND_NAMES list. Feed the
    loop's `当前序号` output into `index` to get the current shot's name, and
    connect the result to SaveVideo's filename_prefix for per-shot output."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "shot_names": (H3_COND_NAMES,),
                "index": ("INT", {"default": 0, "min": 0, "display_name": "索引"}),
            },
            "optional": {
                "prefix": ("STRING", {
                    "default": "",
                    "display_name": "保存前缀",
                    "tooltip": "可选子目录前缀，例如 video/h3，会拼成 video/h3/镜头名。留空则直接输出镜头名。",
                }),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("save_path",)
    FUNCTION = "shot"
    CATEGORY = "H3Cache"

    def shot(self, shot_names, index, prefix=""):
        if shot_names is None:
            raise ValueError("[H3Cache] shot_names 为空")
        idx = _safe_int(index, 0)
        if idx < 0 or idx >= len(shot_names):
            raise IndexError(f"[H3Cache] 索引 {idx} 超出 shot_names 长度 {len(shot_names)}")
        name = str(shot_names[idx])
        prefix = (prefix or "").strip()
        if prefix:
            return (f"{prefix.rstrip('/')}/{name}",)
        return (name,)


class H3ConditioningIndex:
    """Lazy-load ONE .pt file at the given index and return its conditioning.
    
    Takes a list of file paths (from H3LoadConditioningList) and an index,
    loads only that single .pt file into GPU memory. This prevents VRAM
    exhaustion when processing large batches (400+ shots): only one
    conditioning is in GPU memory at any given time, and it is freed after
    the downstream sampler consumes it.
    
    Feed the loop's `当前序号` output into `index` to process one shot per iteration."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "cond_list": (H3_COND_BATCH,),
                "index": ("INT", {"default": 0, "min": 0, "display_name": "索引"}),
            },
        }

    RETURN_TYPES = ("CONDITIONING",)
    RETURN_NAMES = ("conditioning",)
    FUNCTION = "index"
    CATEGORY = "H3Cache"

    def index(self, cond_list, index):
        if cond_list is None:
            raise ValueError("[H3Cache] cond_list 为空")
        idx = _safe_int(index, 0)
        if idx < 0 or idx >= len(cond_list):
            raise IndexError(f"[H3Cache] 索引 {idx} 超出 cond_list 长度 {len(cond_list)}")

        path = cond_list[idx]

        # 兼容旧工作流：如果列表里存的是已加载的 conditioning 张量（dict），
        # 直接返回；否则按路径懒加载单个 .pt 文件
        if isinstance(path, dict):
            _log(f"legacy: cond_list[{idx}] is pre-loaded tensor, returning directly")
            return (path,)

        # 懒加载：只加载当前索引对应的单个 .pt 文件
        device = model_management.get_torch_device()
        data = torch.load(path, map_location="cpu", weights_only=False)
        cond_data, meta = _extract_conditioning_and_meta(data)
        cond = _convert_from_serializable(cond_data)
        cond = _move_to_device(cond, device)

        # 释放原始数据，只保留 conditioning 张量
        del data

        name = os.path.splitext(os.path.basename(path))[0]
        _log(f"lazy loaded [{idx}] {name} <- {path} -> {device}")
        return (cond,)


class H3ReadConditioningMeta:
    """Read metadata (duration, width, height, frame_count) from cached .pt files.

    Designed for the for-loop workflow: takes the same shot_names list and index
    as H3ShotNameByIndex / H3ConditioningIndex, resolves the corresponding .pt
    file, and returns its metadata.

    Typical connections in a for-loop:
        H3LoadConditioningList.shot_names -> H3ReadConditioningMeta.shot_names
        H3ForLoopStart.index             -> H3ReadConditioningMeta.index
        H3ReadConditioningMeta.duration  -> ComfyMathExpression (or EmptyLatent.length)
        H3ReadConditioningMeta.width     -> EmptyMiniMaxH3LatentAV.width
        H3ReadConditioningMeta.height    -> EmptyMiniMaxH3LatentAV.height

    This allows mixing shots of different durations in a single for-loop,
    eliminating the need to group shots by duration.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "shot_names": (H3_COND_NAMES,),
                "index": ("INT", {"default": 0, "min": 0, "display_name": "索引"}),
            },
            "optional": {
                "cache_dir": ("STRING", {
                    "default": "",
                    "tooltip": "自定义缓存目录（绝对路径）。留空则自动搜索 output、output/h3_cond_cache 与 input 目录。",
                }),
            },
        }

    RETURN_TYPES = ("FLOAT", "INT", "INT", "INT")
    RETURN_NAMES = ("duration", "width", "height", "frame_count")
    FUNCTION = "read_meta"
    CATEGORY = "H3Cache"

    def read_meta(self, shot_names, index, cache_dir=""):
        if cache_dir is None:
            cache_dir = ""
        if shot_names is None:
            raise ValueError("[H3Cache] shot_names is empty")
        idx = _safe_int(index, 0)
        if idx < 0 or idx >= len(shot_names):
            raise IndexError(f"[H3Cache] index {idx} out of range {len(shot_names)}")

        name = str(shot_names[idx])
        fname = name if name.endswith(".pt") else name + ".pt"
        path = _resolve_cache_path(fname, cache_dir)

        data = torch.load(path, map_location="cpu", weights_only=False)
        _, meta = _extract_conditioning_and_meta(data)

        duration = float(meta.get("duration", 0))
        width = int(meta.get("width", 0))
        height = int(meta.get("height", 0))
        fps = int(meta.get("frame_rate", 24))

        if meta.get("frame_count"):
            frame_count = int(meta["frame_count"])
        elif duration > 0:
            frame_count = _compute_frame_count(duration, fps)
        else:
            frame_count = 0

        _log(f"meta read {name}: duration={duration}s, {width}x{height}, frames={frame_count}")
        return (duration, width, height, frame_count)


NODE_CLASS_MAPPINGS = {
    "H3ForLoopStart": H3ForLoopStart,
    "H3ForLoopEnd": H3ForLoopEnd,
    "H3ForLoopWhileStart": H3ForLoopWhileStart,
    "H3ForLoopWhileEnd": H3ForLoopWhileEnd,
    "H3ForLoopIntAdd": H3ForLoopIntAdd,
    "H3ForLoopIntLess": H3ForLoopIntLess,
    "H3LoadConditioningList": H3LoadConditioningList,
    "H3ConditioningIndex": H3ConditioningIndex,
    "H3ShotNameByIndex": H3ShotNameByIndex,
    "H3ReadConditioningMeta": H3ReadConditioningMeta,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "H3ForLoopStart": "H3 For循环开始",
    "H3ForLoopEnd": "H3 For循环结束",
    "H3ForLoopWhileStart": "H3 循环开始（内部引用）",
    "H3ForLoopWhileEnd": "H3 循环结束（内部引用）",
    "H3ForLoopIntAdd": "H3 整数加法（内部引用）",
    "H3ForLoopIntLess": "H3 整数小于（内部引用）",
    "H3LoadConditioningList": "H3 批量加载 .pt 为 list",
    "H3ConditioningIndex": "H3 按索引取 conditioning",
    "H3ShotNameByIndex": "H3 按索引取镜头名",
    "H3ReadConditioningMeta": "H3 读取 .pt 元数据",
}