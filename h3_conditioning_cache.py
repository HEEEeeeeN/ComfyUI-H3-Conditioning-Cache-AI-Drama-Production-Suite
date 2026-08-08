"""H3 conditioning cache nodes.

Two nodes:
- H3SaveConditioning: serialize the full CONDITIONING output of
  MiniMaxH3ReferenceToVideo (including 'minimax_refs' reference latents) to a
  .pt file on disk.
- H3LoadConditioning: load a previously saved .pt file back into a CONDITIONING,
  so the expensive Qwen3-VL-32B + reference VAE encoding can be skipped.

The stock LTXV conditioning saver/loader only persists 'conditioning_data_*' and
'attention_mask_*', silently dropping 'minimax_refs' (the reference image/video
latents that H3 re-injects at every sampling step). These nodes persist the
entire conditioning structure, including NestedTensor reference latents.
"""

import os
import torch
import folder_paths
import comfy.model_management as model_management
from comfy.nested_tensor import NestedTensor


def _cache_dir():
    """Default cache location: ComfyUI output/h3_cond_cache."""
    base = folder_paths.get_output_directory()
    d = os.path.join(base, "h3_cond_cache")
    os.makedirs(d, exist_ok=True)
    return d


def _search_dirs():
    """Directories scanned for .pt cache files (dropdown + resolution)."""
    out = folder_paths.get_output_directory()
    dirs = [_cache_dir(), out, folder_paths.get_input_directory()]
    out_dirs = []
    for d in dirs:
        if d not in out_dirs:
            out_dirs.append(d)
    return out_dirs


def _scan_pt_files():
    """Every .pt file visible in the dropdown across all search dirs."""
    seen, files = set(), []
    for d in _search_dirs():
        try:
            for name in sorted(os.listdir(d)):
                if name.endswith(".pt") and name not in seen:
                    seen.add(name)
                    files.append(name)
        except OSError:
            continue
    return files


def _convert_to_serializable(obj):
    """Recursively lower tensors / NestedTensors / dicts to a plain structure
    that torch.save can pickle deterministically."""
    if isinstance(obj, NestedTensor):
        return {"__nested__": [_convert_to_serializable(t) for t in obj.tensors]}
    if torch.is_tensor(obj):
        return obj.detach().cpu()
    if isinstance(obj, dict):
        return {k: _convert_to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_convert_to_serializable(v) for v in obj]
    return obj


def _convert_from_serializable(obj):
    if isinstance(obj, dict):
        if "__nested__" in obj:
            return NestedTensor([_convert_from_serializable(t) for t in obj["__nested__"]])
        return {k: _convert_from_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_convert_from_serializable(v) for v in obj]
    return obj


def _move_to_device(obj, device):
    """Recursively move all tensors / NestedTensors to `device` (in place-ish,
    returns the possibly-new container)."""
    if isinstance(obj, NestedTensor):
        return NestedTensor([_move_to_device(t, device) for t in obj.tensors])
    if torch.is_tensor(obj):
        return obj.to(device)
    if isinstance(obj, dict):
        return {k: _move_to_device(v, device) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_move_to_device(v, device) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_move_to_device(v, device) for v in obj)
    return obj


def _extract_conditioning_and_meta(data):
    """Extract conditioning and metadata from a loaded .pt object.

    New format (with metadata):
        {"conditioning": <serialized conditioning>, "metadata": {...}}
    Old format (no metadata):
        <serialized conditioning> directly

    Returns (conditioning_data, metadata_dict). For old-format files,
    metadata_dict is an empty dict.
    """
    if isinstance(data, dict) and "conditioning" in data and "metadata" in data:
        return data["conditioning"], data["metadata"]
    return data, {}


def _compute_frame_count(duration, fps=24):
    """Compute H3 frame count from duration in seconds.

    Formula identical to the official H3 r2v workflow's ComfyMathExpression:
        max(5, round(a*fps)) + (5 - (max(5, round(a*fps)) % 17)) % 17
    """
    a = float(duration)
    base = max(5, round(a * fps))
    return base + (5 - (base % 17)) % 17


class H3SaveConditioning:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "conditioning": ("CONDITIONING",),
                "filename": ("STRING", {"default": "shot_001"}),
            },
            "optional": {
                "duration": ("FLOAT", {
                    "default": 0,
                    "tooltip": "\u955c\u5934\u65f6\u957f\uff08\u79d2\uff09\u3002\u5199\u5165 .pt \u5143\u6570\u636e\uff0c\u751f\u6210\u9636\u6bb5\u53ef\u8bfb\u53d6\u4ee5\u81ea\u52a8\u8bbe\u7f6e\u5e27\u6570\u3002",
                }),
                "width": ("INT", {
                    "default": 0,
                    "tooltip": "\u89c6\u9891\u5bbd\u5ea6\uff08\u50cf\u7d20\uff09\u3002\u5199\u5165 .pt \u5143\u6570\u636e\u4f9b\u751f\u6210\u9636\u6bb5\u8bfb\u53d6\u3002",
                }),
                "height": ("INT", {
                    "default": 0,
                    "tooltip": "\u89c6\u9891\u9ad8\u5ea6\uff08\u50cf\u7d20\uff09\u3002\u5199\u5165 .pt \u5143\u6570\u636e\u4f9b\u751f\u6210\u9636\u6bb5\u8bfb\u53d6\u3002",
                }),
            },
        }

    RETURN_TYPES = ()
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "H3Cache"

    def save(self, conditioning, filename, duration=0, width=0, height=0):
        safe = "".join(c for c in filename if c.isalnum() or c in ("_", "-", "."))
        if not safe:
            safe = "shot"
        path = os.path.join(_cache_dir(), f"{safe}.pt")
        # Resume-friendly: if the cache file already exists, skip re-encoding.
        if os.path.isfile(path):
            mb = os.path.getsize(path) / (1024 * 1024)
            print(f"[H3Cache] SKIP (already cached) -> {path} ({mb:.1f} MB)")
            return {}
        cond_data = _convert_to_serializable(conditioning)
        metadata = {
            "duration": float(duration) if duration else 0.0,
            "width": int(width) if width else 0,
            "height": int(height) if height else 0,
            "frame_rate": 24,
            "frame_count": _compute_frame_count(duration) if duration else 0,
        }
        wrapper = {"conditioning": cond_data, "metadata": metadata}
        torch.save(wrapper, path)
        mb = os.path.getsize(path) / (1024 * 1024)
        meta_str = f", meta: {duration}s {width}x{height}" if duration else ""
        print(f"[H3Cache] saved conditioning -> {path} ({mb:.1f} MB{meta_str})")
        return {}


def _resolve_cache_path(file_name, cache_dir):
    """Locate the cache file. Priority:
    1) cache_dir (custom absolute path) + file_name
    2) file_name given as an absolute path
    3) output root + file_name
    4) output/h3_cond_cache + file_name
    5) ComfyUI input dir + file_name
    """
    if not file_name:
        raise ValueError("[H3Cache] no conditioning cache file selected")

    candidates = []
    if os.path.isabs(file_name):
        candidates.append(file_name)
    if cache_dir and isinstance(cache_dir, str) and cache_dir.strip():
        d = cache_dir.strip().strip('"').strip("'")
        if os.path.isdir(d):
            candidates.append(os.path.join(d, file_name))
    candidates.append(os.path.join(folder_paths.get_output_directory(), file_name))
    candidates.append(os.path.join(_cache_dir(), file_name))
    candidates.append(os.path.join(folder_paths.get_input_directory(), file_name))

    for p in candidates:
        if os.path.isfile(p):
            return p

    raise FileNotFoundError(
        f"[H3Cache] conditioning cache not found: '{file_name}'. Searched:\n"
        + "\n".join(f"  - {p}" for p in candidates)
    )


class H3LoadConditioning:
    @classmethod
    def INPUT_TYPES(cls):
        files = _scan_pt_files()
        return {
            "required": {
                "file_name": (files or [""],),
            },
            "optional": {
                "cache_dir": ("STRING", {
                    "default": "",
                    "tooltip": "自定义缓存目录（绝对路径）。留空则自动搜索 output、output/h3_cond_cache 与 input 目录。",
                }),
            },
        }

    RETURN_TYPES = ("CONDITIONING",)
    FUNCTION = "load"
    CATEGORY = "H3Cache"

    def load(self, file_name, cache_dir=""):
        if cache_dir is None:
            cache_dir = ""
        path = _resolve_cache_path(file_name, cache_dir)
        data = torch.load(path, map_location="cpu", weights_only=False)
        cond_data, meta = _extract_conditioning_and_meta(data)
        cond = _convert_from_serializable(cond_data)
        # Move every tensor back to the active compute device so the sampler can
        # use the reference latents (minimax_refs) directly during each step.
        device = model_management.get_torch_device()
        cond = _move_to_device(cond, device)
        mb = os.path.getsize(path) / (1024 * 1024)
        meta_str = f" meta={meta}" if meta else ""
        print(f"[H3Cache] loaded conditioning <- {path} ({mb:.1f} MB{meta_str}) -> {device}")
        return (cond,)


class H3LoadConditioningBatch:
    """Load several cached conditionings at once and expose them on separate
    outputs, mimicking the batch prompt / batch image loader nodes of other
    plugins. Each .pt is several MB, so loading a full generation batch
    (<= MAX_OUT shots) up-front is safe on GPU memory.

    `shots` may be a comma-separated list of shot stems (e.g. "A01,A02,A03");
    leave empty to load every .pt found in the cache search dirs.
    """

    MAX_OUT = 24

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "shots": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "tooltip": "逗号分隔的镜头名，例如 A01,A02,A03。留空则加载缓存目录下全部 .pt。",
                }),
            },
            "optional": {
                "cache_dir": ("STRING", {
                    "default": "",
                    "tooltip": "自定义缓存目录（绝对路径）。留空则自动搜索 output、output/h3_cond_cache 与 input 目录。",
                }),
            },
        }

    RETURN_TYPES = ("CONDITIONING",) * MAX_OUT
    RETURN_NAMES = [f"cond_{i}" for i in range(MAX_OUT)]
    FUNCTION = "load"
    CATEGORY = "H3Cache"

    def load(self, shots, cache_dir=""):
        if cache_dir is None:
            cache_dir = ""
        names = [s.strip() for s in (shots or "").split(",") if s.strip()]
        if not names:
            names = [os.path.splitext(n)[0] for n in _scan_pt_files()]
        if len(names) > self.MAX_OUT:
            print(f"[H3Cache] batch: {len(names)} shots exceed MAX_OUT={self.MAX_OUT}, truncating")
            names = names[: self.MAX_OUT]

        device = model_management.get_torch_device()
        outs = []
        for nm in names:
            fname = nm if nm.endswith(".pt") else nm + ".pt"
            path = _resolve_cache_path(fname, cache_dir)
            data = torch.load(path, map_location="cpu", weights_only=False)
            cond_data, meta = _extract_conditioning_and_meta(data)
            cond = _convert_from_serializable(cond_data)
            cond = _move_to_device(cond, device)
            mb = os.path.getsize(path) / (1024 * 1024)
            print(f"[H3Cache] batch loaded {nm} <- {path} ({mb:.1f} MB) -> {device}")
            outs.append(cond)
        while len(outs) < self.MAX_OUT:
            outs.append(None)
        return tuple(outs)



class H3FreeMemory:
    """Clear GPU/CPU memory after a shot finishes generating.

    Place this node at the tail of each generation chain (feed it the
    CreateVideo VIDEO output) so it runs right after that shot is saved and
    frees the VRAM / RAM that the sampler+decoders accumulated, keeping long
    batch runs from growing memory without bound and OOM-ing.

    mode=True  -> torch.cuda.synchronize + empty_cache + gc.collect
    mode=False -> only gc.collect (CPU)
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "trigger": ("*", {
                    "tooltip": "任意输入，接到本镜头 CreateVideo 的 VIDEO 输出；该镜头生成成功保存后即触发本节点清理显存/内存。",
                }),
            },
            "optional": {
                "mode": ("BOOLEAN", {
                    "default": True,
                    "label_on": "清 GPU+CPU",
                    "label_off": "仅清 CPU",
                }),
            },
        }

    RETURN_TYPES = ()
    FUNCTION = "free"
    OUTPUT_NODE = True
    CATEGORY = "H3Cache"

    def free(self, trigger=None, mode=True):
        import gc
        gc.collect()
        if mode and torch.cuda.is_available():
            try:
                torch.cuda.synchronize()
            except Exception:
                pass
            try:
                before = torch.cuda.memory_allocated()
                torch.cuda.empty_cache()
                gc.collect()
                after = torch.cuda.memory_allocated()
                print(
                    f"[H3Cache] cleared GPU cache: allocated "
                    f"{before / 1024 ** 2:.0f}MB -> {after / 1024 ** 2:.0f}MB "
                    f"(freed {(before - after) / 1024 ** 2:.0f}MB)"
                )
            except Exception:
                print("[H3Cache] cleared GPU cache (no stats available)")
        else:
            print("[H3Cache] cleared CPU memory")
        return {}


class H3SaveVideo:
    """Save a VIDEO to disk without UI preview.

    Designed for batch loop workflows where only file output is needed.
    Takes the VIDEO object from CreateVideo and a save_path string (e.g.
    "h3_videos/A12") and saves directly as A12.mp4 in the output folder.
    No preview is generated, avoiding duplicate video files in loop expansion.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video": ("VIDEO",),
                "save_path": ("STRING", {
                    "default": "h3_videos",
                    "tooltip": "保存路径（相对于 output 目录），如 h3_videos/A12。视频将保存为 A12.mp4。",
                }),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("saved_path",)
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "H3Cache"

    def save(self, video, save_path):
        if video is None:
            print("[H3Cache] H3SaveVideo: video is None, skipping")
            return {"result": ("",)}

        output_dir = folder_paths.get_output_directory()

        # Parse save_path: "h3_videos/A12" -> folder="h3_videos", filename="A12"
        save_path = (save_path or "").strip().strip("/")
        parts = [p for p in save_path.split("/") if p]

        if len(parts) > 1:
            folder = os.path.join(output_dir, *parts[:-1])
            filename = parts[-1]
        elif len(parts) == 1:
            folder = output_dir
            filename = parts[0]
        else:
            folder = output_dir
            filename = "video"

        # Sanitize filename
        safe_name = "".join(c for c in filename if c.isalnum() or c in ("_", "-", "."))
        if not safe_name:
            safe_name = "video"

        os.makedirs(folder, exist_ok=True)

        # Determine extension and format
        ext = "mp4"
        fmt = None
        try:
            from comfy_api.latest import Types
            ext = Types.VideoContainer.get_extension("auto")
            fmt = Types.VideoContainer("auto")
        except Exception:
            pass

        filepath = os.path.join(folder, f"{safe_name}.{ext}")

        # Save the video using ComfyUI's Video API
        saved = False
        try:
            if fmt is not None:
                video.save_to(filepath, format=fmt, codec="auto", metadata=None, crf=None)
                saved = True
            else:
                video.save_to(filepath)
                saved = True
        except Exception as e:
            print(f"[H3Cache] H3SaveVideo save_to error: {e}")
            try:
                video.save_to(filepath)
                saved = True
            except Exception as e2:
                print(f"[H3Cache] H3SaveVideo fallback error: {e2}")

        if saved:
            mb = os.path.getsize(filepath) / (1024 * 1024) if os.path.isfile(filepath) else 0
            print(f"[H3Cache] saved video -> {filepath} ({mb:.1f} MB)")
        else:
            print(f"[H3Cache] H3SaveVideo: failed to save {filepath}")

        return {"result": (filepath,)}


NODE_CLASS_MAPPINGS = {
    "H3SaveConditioning": H3SaveConditioning,
    "H3LoadConditioning": H3LoadConditioning,
    "H3LoadConditioningBatch": H3LoadConditioningBatch,
    "H3FreeMemory": H3FreeMemory,
    "H3SaveVideo": H3SaveVideo,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "H3SaveConditioning": "H3 Save Conditioning (cache)",
    "H3LoadConditioning": "H3 Load Conditioning (cache)",
    "H3LoadConditioningBatch": "H3 Load Conditioning Batch (cache)",
    "H3FreeMemory": "H3 Free Memory (显存/内存清理)",
    "H3SaveVideo": "H3 Save Video (无预览)",
}