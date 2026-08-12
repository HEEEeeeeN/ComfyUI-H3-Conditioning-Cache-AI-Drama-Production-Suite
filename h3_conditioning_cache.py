"""H3 conditioning cache nodes.

Nodes:
- H3SaveConditioning: serialize the full CONDITIONING output of
  MiniMaxH3ReferenceToVideo (including 'minimax_refs' reference latents) to a
  .pt file on disk. Optionally stores reference images (compressed) and
  prompt text for cloud portability and cross-resolution re-encoding.
- H3LoadConditioning: load a previously saved .pt file back into a CONDITIONING.
- H3ReencodeFromCache: load a .pt with stored reference images, re-encode the
  reference latents with VAE at a new resolution, and return a fresh
  conditioning + latent. Enables "preview at low-res, produce at high-res".

Storage format options for reference images:
  - JPEG Q95: ~0.3 MB per image (near-lossless, recommended for cloud)
  - PNG: ~1.9 MB per image (lossless, +8-10% file size)

The stock LTXV conditioning saver/loader only persists 'conditioning_data_*' and
'attention_mask_*', silently dropping 'minimax_refs' (the reference image/video
latents that H3 re-injects at every sampling step). These nodes persist the
entire conditioning structure, including NestedTensor reference latents.
"""

import os
import io
import math
import torch
import folder_paths
import comfy.model_management as model_management
import comfy.utils
import node_helpers
from comfy.nested_tensor import NestedTensor

# Constants for reference image re-encoding (mirrors nodes_minimax_h3.py)
_CANVAS_MULTIPLE = 32
_REF_IMAGE_SHORT_EDGE = 2048
_FPS = 24
_AUDIO_LATENT_FPS = 40


def _cache_dir():
    base = folder_paths.get_output_directory()
    d = os.path.join(base, "h3_cond_cache")
    os.makedirs(d, exist_ok=True)
    return d


def _search_dirs():
    out = folder_paths.get_output_directory()
    dirs = [_cache_dir(), out, folder_paths.get_input_directory()]
    out_dirs = []
    for d in dirs:
        if d not in out_dirs:
            out_dirs.append(d)
    return out_dirs


def _scan_pt_files():
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
    if isinstance(data, dict) and "conditioning" in data and "metadata" in data:
        return data["conditioning"], data["metadata"]
    return data, {}


def _compute_frame_count(duration, fps=24):
    a = float(duration)
    base = max(5, round(a * fps))
    return base + (5 - (base % 17)) % 17


# ---------------------------------------------------------------------------
# Image compression utilities
# ---------------------------------------------------------------------------

def _image_to_bytes(image_tensor, fmt="jpeg", quality=95):
    """Convert [B,H,W,C] float32 0-1 to list of compressed bytes.

    Args:
        image_tensor: ComfyUI IMAGE tensor
        fmt: "jpeg" (default, ~0.17MB/img) or "png" (~1.3MB/img)
        quality: JPEG quality (default 95, near-lossless)

    Returns: list of bytes objects (one per batch item)
    """
    from PIL import Image

    results = []
    arr = (image_tensor.clamp(0, 1) * 255).round().to(torch.uint8).cpu().numpy()
    for i in range(arr.shape[0]):
        img = Image.fromarray(arr[i])
        buf = io.BytesIO()
        if fmt.lower() == "png":
            img.save(buf, format="PNG", optimize=True)
        else:
            img.save(buf, format="JPEG", quality=quality)
        results.append(buf.getvalue())
    return results


def _bytes_to_image(bytes_list, fmt="jpeg"):
    """Convert compressed bytes back to [B,H,W,C] float32 0-1."""
    from PIL import Image
    import numpy as np

    images = []
    for b in bytes_list:
        img = Image.open(io.BytesIO(b)).convert("RGB")
        images.append(np.array(img, dtype=np.float32) / 255.0)
    return torch.from_numpy(np.stack(images, axis=0))


# ---------------------------------------------------------------------------
# Re-encoding helpers (mirror nodes_minimax_h3.py)
# ---------------------------------------------------------------------------

def _resize_image(image, width, height, crop):
    samples = image[..., :3].movedim(-1, 1)
    samples = comfy.utils.common_upscale(samples, width, height, "lanczos", crop)
    return samples.movedim(1, -1)


def _align_frame_count(n):
    while n % 17 != 5:
        n += 1
    return n


def _video_latent_t(frame_count):
    return 2 if frame_count <= 5 else ((frame_count - 5) // 17) * 5 + 2


def _temporal_shape(length):
    frame_count = _align_frame_count(max(5, length))
    duration = frame_count / _FPS
    return frame_count, _video_latent_t(frame_count), round(duration * _AUDIO_LATENT_FPS)


def _empty_av_latent(width, height, length, batch_size=1):
    frame_count, latent_t, audio_t = _temporal_shape(length)
    video = torch.zeros([batch_size, 24, latent_t, height // 16, width // 16],
                        device=comfy.model_management.intermediate_device())
    audio = torch.zeros([batch_size, 32, 2, audio_t],
                        device=comfy.model_management.intermediate_device())
    return {"samples": NestedTensor((video, audio))}, frame_count


def _reencode_ref_images(ref_images_tensor, vae, width, height, ref_image_size="match"):
    """Re-encode reference images with VAE at the target resolution."""
    ref_blocks = []
    for i in range(ref_images_tensor.shape[0]):
        img = ref_images_tensor[i:i+1]
        h, w = img.shape[1], img.shape[2]
        if ref_image_size == "match":
            scale = min(1.0, math.sqrt((width * height) / (w * h)))
        else:
            scale = min(1.0, _REF_IMAGE_SHORT_EDGE / min(w, h))
        tw = max(_CANVAS_MULTIPLE, round(w * scale / _CANVAS_MULTIPLE) * _CANVAS_MULTIPLE)
        th = max(_CANVAS_MULTIPLE, round(h * scale / _CANVAS_MULTIPLE) * _CANVAS_MULTIPLE)
        resized = _resize_image(img, tw, th, "disabled")
        z = vae.encode(resized)
        ref_blocks.append({
            "kind": "image",
            "latent_h": th // 16,
            "latent_w": tw // 16,
            "latent": z,
        })
    return ref_blocks


# ---------------------------------------------------------------------------
# Node: H3EncodeConditioning (CLIP-only pre-encode, no VAE)
#
# 拆解自 MiniMaxH3ReferenceToVideo：
#   - 只执行 Qwen3VL CLIP 编码（文本 + 参考图视觉 token）→ conditioning
#   - 不做参考图 VAE 编码（minimax_refs 留空），不加载 VAE，省显存
#   - 不依赖 width/height/length（参考图用固定 "max" 短边缩放，与最终分辨率解耦）
#   - 配合 H3ReencodeFromCache：生成阶段用目标 vae + 分辨率重新编码参考图
# ---------------------------------------------------------------------------

class H3EncodeConditioning:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "clip": ("CLIP",),
                "prompt": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "tooltip": "H3 提示词全文（九分节或 Ref2VA 格式）。",
                }),
                "ref_image_short_edge": ("INT", {
                    "default": 768,
                    "min": 256, "max": 2048, "step": 128,
                    "tooltip": "参考图缩放的短边基准（像素）。768=官方默认（快）；越大视觉 token 越多、越慢（2048 约 7 倍耗时）。生成阶段由 H3ReencodeFromCache 按目标分辨率重新编码，此处仅影响 Qwen3VL 理解精度。",
                }),
            },
            "optional": {
                "ref_image_0": ("IMAGE", {
                    "tooltip": "参考图 1（主角色等）。仅用于 Qwen3VL 视觉编码；生成阶段由 H3ReencodeFromCache 重新 VAE 编码。",
                }),
                "ref_image_1": ("IMAGE", {
                    "tooltip": "参考图 2（可选）。",
                }),
                "ref_image_2": ("IMAGE", {
                    "tooltip": "参考图 3（可选）。",
                }),
                "ref_image_3": ("IMAGE", {
                    "tooltip": "参考图 4（可选）。",
                }),
            },
        }

    RETURN_TYPES = ("CONDITIONING",)
    RETURN_NAMES = ("conditioning",)
    FUNCTION = "encode"
    CATEGORY = "H3Cache"

    def encode(self, clip, prompt, ref_image_short_edge=768, ref_image_0=None,
               ref_image_1=None, ref_image_2=None, ref_image_3=None):
        # 构造 Qwen3VL 参考图条目（按短边基准缩放，默认 768 与官方一致；与生成分辨率无关）
        short_edge = max(256, int(ref_image_short_edge or 768))
        reference_items = []
        for img in (ref_image_0, ref_image_1, ref_image_2, ref_image_3):
            if img is None:
                continue
            h, w = img.shape[1], img.shape[2]
            scale = min(1.0, short_edge / min(w, h))
            tw = max(_CANVAS_MULTIPLE,
                     round(w * scale / _CANVAS_MULTIPLE) * _CANVAS_MULTIPLE)
            th = max(_CANVAS_MULTIPLE,
                     round(h * scale / _CANVAS_MULTIPLE) * _CANVAS_MULTIPLE)
            resized = _resize_image(img[:1], tw, th, "disabled")
            reference_items.append({"type": "image", "data": resized})

        tokens = clip.tokenize(prompt, minimax_ref_items=reference_items)
        conditioning = clip.encode_from_tokens_scheduled(tokens)
        print(f"[H3Cache] H3EncodeConditioning: {len(reference_items)} ref image(s), "
              f"short_edge={short_edge} (no VAE)")
        return (conditioning,)


# ---------------------------------------------------------------------------
# Node: H3SaveConditioning
# ---------------------------------------------------------------------------

class H3SaveConditioning:
    """Save H3 conditioning to a .pt file.

    When reference images and prompt are provided, they are stored alongside
    the conditioning as compressed bytes in the .pt. This makes the .pt
    self-contained for cloud transfer and enables cross-resolution re-encoding.

    Size impact per reference image (1280x736):
      JPEG Q95: +0.3 MB (+1%)  -- recommended, near-lossless
      PNG:      +1.9 MB (+8%)  -- lossless
    """

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
                "prompt": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "tooltip": "\u63d0\u793a\u8bcd\u6587\u672c\u3002\u5b58\u5165 .pt \u5143\u6570\u636e\uff0c\u4f9b H3ReencodeFromCache \u53c2\u8003\u3002",
                }),
                "ref_image_size": (["match", "max"], {
                    "default": "match",
                    "tooltip": "\u53c2\u8003\u56fe\u7f29\u653e\u6a21\u5f0f\u3002\u4e0e MiniMaxH3ReferenceToVideo \u7684\u8bbe\u7f6e\u4fdd\u6301\u4e00\u81f4\u3002",
                }),
                "ref_image_format": (["jpeg", "png"], {
                    "default": "jpeg",
                    "tooltip": "参考图存储格式。JPEG Q95（默认，+0.3MB/张）或 PNG（无损，+1.9MB/张）。",
                }),
                "ref_image_0": ("IMAGE", {
                    "tooltip": "\u53c2\u8003\u56fe 1\u3002\u5efa\u8bae\u4e0e MiniMaxH3ReferenceToVideo \u7684 ref_image_0 \u8fde\u63a5\u540c\u4e00\u6765\u6e90\u3002",
                }),
                "ref_image_1": ("IMAGE", {
                    "tooltip": "\u53c2\u8003\u56fe 2\uff08\u53ef\u9009\uff09\u3002",
                }),
                "ref_image_2": ("IMAGE", {
                    "tooltip": "\u53c2\u8003\u56fe 3\uff08\u53ef\u9009\uff09\u3002",
                }),
                "ref_image_3": ("IMAGE", {
                    "tooltip": "\u53c2\u8003\u56fe 4\uff08\u53ef\u9009\uff09\u3002",
                }),
            },
        }

    RETURN_TYPES = ()
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "H3Cache"

    def save(self, conditioning, filename, duration=0, width=0, height=0,
             prompt="", ref_image_size="match", ref_image_format="jpeg",
             ref_image_0=None, ref_image_1=None, ref_image_2=None, ref_image_3=None):
        safe = "".join(c for c in filename if c.isalnum() or c in ("_", "-", "."))
        if not safe:
            safe = "shot"
        path = os.path.join(_cache_dir(), f"{safe}.pt")
        if os.path.isfile(path):
            mb = os.path.getsize(path) / (1024 * 1024)
            print(f"[H3Cache] SKIP (already cached) -> {path} ({mb:.1f} MB)")
            return {}
        cond_data = _convert_to_serializable(conditioning)

        # Compress reference images
        ref_images_data = []
        ref_image_shapes = []
        for idx, img in enumerate([ref_image_0, ref_image_1, ref_image_2, ref_image_3]):
            if img is not None:
                data_list = _image_to_bytes(img, fmt=ref_image_format)
                ref_images_data.extend(data_list)
                for d in data_list:
                    ref_image_shapes.append([img.shape[1], img.shape[2]])
                kb = len(data_list[0]) / 1024
                print(f"[H3Cache] ref_image_{idx}: {img.shape[1]}x{img.shape[2]} -> "
                      f"{ref_image_format.upper()} {len(data_list)} frame(s), {kb:.0f} KB")

        metadata = {
            "duration": float(duration) if duration else 0.0,
            "width": int(width) if width else 0,
            "height": int(height) if height else 0,
            "frame_rate": 24,
            "frame_count": _compute_frame_count(duration) if duration else 0,
            "prompt": prompt,
            "ref_image_size": ref_image_size,
            "ref_image_format": ref_image_format,
            "ref_image_count": len(ref_images_data),
            "ref_image_shapes": ref_image_shapes,
        }
        wrapper = {
            "conditioning": cond_data,
            "metadata": metadata,
            "ref_images_data": ref_images_data,
        }
        torch.save(wrapper, path)
        mb = os.path.getsize(path) / (1024 * 1024)
        ref_str = f", {len(ref_images_data)} ref imgs ({ref_image_format})" if ref_images_data else ""
        meta_str = f", meta: {duration}s {width}x{height}" if duration else ""
        print(f"[H3Cache] saved conditioning -> {path} ({mb:.1f} MB{meta_str}{ref_str})")
        return {}


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _resolve_cache_path(file_name, cache_dir):
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


# ---------------------------------------------------------------------------
# Node: H3LoadConditioning
# ---------------------------------------------------------------------------

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
                    "tooltip": "\u81ea\u5b9a\u4e49\u7f13\u5b58\u76ee\u5f55\uff08\u7edd\u5bf9\u8def\u5f84\uff09\u3002\u7559\u7a7a\u5219\u81ea\u52a8\u641c\u7d22\u3002",
                }),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(cls, **kwargs):
        """允许 combo 之外的绝对路径（浏览按钮填入），或列表内相对文件名。"""
        file_name = kwargs.get("file_name", "")
        if not file_name:
            return True
        if os.path.isabs(file_name):
            if os.path.isfile(file_name):
                return True
            return f"Cache file not found: {file_name}"
        try:
            _resolve_cache_path(file_name, kwargs.get("cache_dir", ""))
            return True
        except Exception as exc:  # noqa: BLE001
            return str(exc)

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
        device = model_management.get_torch_device()
        cond = _move_to_device(cond, device)
        mb = os.path.getsize(path) / (1024 * 1024)
        meta_str = f" meta={meta}" if meta else ""
        print(f"[H3Cache] loaded conditioning <- {path} ({mb:.1f} MB{meta_str}) -> {device}")
        return (cond,)


# ---------------------------------------------------------------------------
# Node: H3ReadMetaSingle
# ---------------------------------------------------------------------------

class H3ReadMetaSingle:
    """按单个 .pt 文件名读取元数据（时长/宽高/帧数）。

    供单链生成 JSON 使用：H3LoadConditioning 加载 conditioning 的同时，
    用本节点把 .pt 元数据（duration/width/height/frame_count）读出，
    直接喂给 EmptyMiniMaxH3LatentAV 的 width/height/length，避免手动填参。
    """

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
                    "tooltip": "\u81ea\u5b9a\u4e49\u7f13\u5b58\u76ee\u5f55\uff08\u7edd\u5bf9\u8def\u5f84\uff09\u3002\u7559\u7a7a\u5219\u81ea\u52a8\u641c\u7d22\u3002",
                }),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(cls, **kwargs):
        """允许 combo 之外的绝对路径（浏览按钮填入），或列表内相对文件名。"""
        file_name = kwargs.get("file_name", "")
        if not file_name:
            return True
        if os.path.isabs(file_name):
            if os.path.isfile(file_name):
                return True
            return f"Cache file not found: {file_name}"
        try:
            _resolve_cache_path(file_name, kwargs.get("cache_dir", ""))
            return True
        except Exception as exc:  # noqa: BLE001
            return str(exc)

    RETURN_TYPES = ("FLOAT", "INT", "INT", "INT")
    RETURN_NAMES = ("duration", "width", "height", "frame_count")
    FUNCTION = "read_meta"
    CATEGORY = "H3Cache"

    def read_meta(self, file_name, cache_dir=""):
        if cache_dir is None:
            cache_dir = ""
        path = _resolve_cache_path(file_name, cache_dir)
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

        print(f"[H3Cache] H3ReadMetaSingle {file_name}: duration={duration}s, "
              f"{width}x{height}, frames={frame_count}")
        return (duration, width, height, frame_count)


# ---------------------------------------------------------------------------
# Node: H3LoadConditioningBatch
# ---------------------------------------------------------------------------

class H3LoadConditioningBatch:
    MAX_OUT = 24

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "shots": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "tooltip": "\u9017\u53f7\u5206\u9694\u7684\u955c\u5934\u540d\uff0c\u4f8b\u5982 A01,A02,A03\u3002\u7559\u7a7a\u5219\u52a0\u8f7d\u5168\u90e8 .pt\u3002",
                }),
            },
            "optional": {
                "cache_dir": ("STRING", {
                    "default": "",
                    "tooltip": "\u81ea\u5b9a\u4e49\u7f13\u5b58\u76ee\u5f55\u3002",
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


# ---------------------------------------------------------------------------
# Node: H3ReencodeFromCache
# ---------------------------------------------------------------------------

class H3ReencodeFromCache:
    """Load a cached .pt and re-encode reference latents at a new resolution.

    Enables the "preview at low-res, produce at high-res" workflow:
    1. Pre-encode: generate .pt at low resolution with reference images stored.
    2. User previews results and picks the best takes.
    3. Production: feed selected .pt into this node with target high resolution.
       The node re-encodes reference latents with VAE while keeping cached
       Qwen3-VL-32B embeddings (no expensive model re-run).

    Requirements:
    - .pt must have been saved with H3SaveConditioning + ref_image inputs.
    - For best cross-resolution results, encode with ref_image_size="max".
    """

    @classmethod
    def INPUT_TYPES(cls):
        files = _scan_pt_files()
        return {
            "required": {
                "file_name": (files or [""],),
                "vae": ("VAE",),
                "width": ("INT", {
                    "default": 1280,
                    "min": 32, "max": 4096, "step": 32,
                    "tooltip": "\u76ee\u6807\u751f\u6210\u5bbd\u5ea6\u3002\u53ef\u4e0d\u540c\u4e8e .pt \u7f16\u7801\u65f6\u7684\u5206\u8fa8\u7387\u3002",
                }),
                "height": ("INT", {
                    "default": 736,
                    "min": 32, "max": 4096, "step": 32,
                    "tooltip": "\u76ee\u6807\u751f\u6210\u9ad8\u5ea6\u3002\u53ef\u4e0d\u540c\u4e8e .pt \u7f16\u7801\u65f6\u7684\u5206\u8fa8\u7387\u3002",
                }),
                "length": ("INT", {
                    "default": 124,
                    "min": 5, "max": 3600, "step": 17,
                    "tooltip": "\u5e27\u6570\uff0824fps\uff09\u3002124\u22485\u79d2\uff0c175\u22487\u79d2\uff0c243\u224810\u79d2\u3002",
                }),
            },
            "optional": {
                "cache_dir": ("STRING", {
                    "default": "",
                    "tooltip": "\u81ea\u5b9a\u4e49\u7f13\u5b58\u76ee\u5f55\u3002",
                }),
                "ref_image_size": (["match", "max"], {
                    "default": "match",
                    "tooltip": "\u53c2\u8003\u56fe\u7f29\u653e\u6a21\u5f0f\u3002\u5efa\u8bae\u4e0e\u7f16\u7801\u65f6\u4fdd\u6301\u4e00\u81f4\u3002",
                }),
            },
        }

    @classmethod
    def VALIDATE_INPUTS(cls, **kwargs):
        """允许 combo 之外的绝对路径（浏览按钮填入），或列表内相对文件名。"""
        file_name = kwargs.get("file_name", "")
        if not file_name:
            return True
        if os.path.isabs(file_name):
            if os.path.isfile(file_name):
                return True
            return f"Cache file not found: {file_name}"
        try:
            _resolve_cache_path(file_name, kwargs.get("cache_dir", ""))
            return True
        except Exception as exc:  # noqa: BLE001
            return str(exc)

    RETURN_TYPES = ("CONDITIONING", "LATENT")
    RETURN_NAMES = ("positive", "latent")
    FUNCTION = "reencode"
    CATEGORY = "H3Cache"

    def reencode(self, file_name, vae, width, height, length,
                 cache_dir="", ref_image_size="match"):
        if cache_dir is None:
            cache_dir = ""
        path = _resolve_cache_path(file_name, cache_dir)

        print(f"[H3Cache] reencode: loading {path}")
        data = torch.load(path, map_location="cpu", weights_only=False)
        cond_data, meta = _extract_conditioning_and_meta(data)

        # Find stored reference images (support both new and legacy field names)
        ref_images_data = []
        if isinstance(data, dict):
            ref_images_data = data.get("ref_images_data", data.get("ref_images_png", []))

        if not ref_images_data:
            raise ValueError(
                f"[H3Cache] reencode: {file_name} has no stored reference images. "
                f"Please re-encode with H3SaveConditioning and connect ref_image inputs."
            )

        img_fmt = meta.get("ref_image_format", "png")
        ref_count = meta.get("ref_image_count", len(ref_images_data))
        print(f"[H3Cache] reencode: {ref_count} ref images ({img_fmt}), "
              f"target {width}x{height}@{length}f")

        # Decode compressed bytes back to image tensor
        ref_images_tensor = _bytes_to_image(ref_images_data, fmt=img_fmt)
        print(f"[H3Cache] reencode: decoded {ref_images_tensor.shape[0]} images, "
              f"shape: {list(ref_images_tensor.shape)}")

        # Re-encode with VAE at new resolution
        ref_blocks = _reencode_ref_images(
            ref_images_tensor, vae, width, height, ref_image_size
        )
        print(f"[H3Cache] reencode: VAE encoded {len(ref_blocks)} ref blocks")
        for i, rb in enumerate(ref_blocks):
            print(f"  ref[{i}]: {rb['latent_h']}x{rb['latent_w']} "
                  f"latent {list(rb['latent'].shape)}")

        # Reconstruct conditioning: keep Qwen3-VL embeddings, replace minimax_refs
        cond = _convert_from_serializable(cond_data)
        cond_list = cond
        if isinstance(cond_list, list) and len(cond_list) > 0:
            entry = cond_list[0]
            if isinstance(entry, list) and len(entry) >= 2:
                cond_dict = entry[1]
                if isinstance(cond_dict, dict):
                    old_refs = cond_dict.get("minimax_refs", [])
                    cond_dict["minimax_refs"] = ref_blocks
                    print(f"[H3Cache] reencode: replaced {len(old_refs)} old refs "
                          f"-> {len(ref_blocks)} new refs")
                else:
                    raise ValueError("[H3Cache] unexpected conditioning dict")
            else:
                raise ValueError("[H3Cache] unexpected conditioning entry")
        else:
            raise ValueError("[H3Cache] unexpected conditioning structure")

        # Move to device
        device = model_management.get_torch_device()
        cond = _move_to_device(cond, device)

        # Create new empty latent
        latent, frame_count = _empty_av_latent(width, height, length)
        print(f"[H3Cache] reencode: latent {width}x{height}, frames={frame_count}")

        mb = os.path.getsize(path) / (1024 * 1024)
        print(f"[H3Cache] reencode: done (source: {mb:.1f} MB)")
        return (cond, latent)


# ---------------------------------------------------------------------------
# Node: H3FreeMemory
# ---------------------------------------------------------------------------

class H3FreeMemory:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "trigger": ("*", {
                    "tooltip": "\u4efb\u610f\u8f93\u5165\uff0c\u63a5\u5230 CreateVideo \u7684 VIDEO \u8f93\u51fa\u3002",
                }),
            },
            "optional": {
                "mode": ("BOOLEAN", {
                    "default": True,
                    "label_on": "\u6e05 GPU+CPU",
                    "label_off": "\u4ec5\u6e05 CPU",
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
                print(f"[H3Cache] cleared GPU: {before/1024**2:.0f}MB -> {after/1024**2:.0f}MB "
                      f"(freed {(before-after)/1024**2:.0f}MB)")
            except Exception:
                print("[H3Cache] cleared GPU cache")
        else:
            print("[H3Cache] cleared CPU memory")
        return {}


# ---------------------------------------------------------------------------
# Node: H3SaveVideo
# ---------------------------------------------------------------------------

class H3SaveVideo:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video": ("VIDEO",),
                "save_path": ("STRING", {
                    "default": "h3_videos",
                    "tooltip": "\u4fdd\u5b58\u8def\u5f84\uff08\u76f8\u5bf9\u4e8e output\uff09\uff0c\u5982 h3_videos/A12\u3002",
                }),
                "add_counter": ("BOOLEAN", {
                    "default": True,
                    "label_on": "\u81ea\u52a8\u5e8f\u53f7 (\u4e0d\u8986\u76d6)",
                    "label_off": "\u8986\u76d6\u6a21\u5f0f",
                    "tooltip": "\u5f00\uff1a\u50cf ComfyUI \u539f\u751f\u4e00\u6837\u81ea\u52a8\u52a0 _00001_ \u5e8f\u53f7\uff0c\u91cd\u8dd1\u4e0d\u8986\u76d6\u65e7\u89c6\u9891\uff1b\u5173\uff1a\u76f4\u63a5\u8986\u76d6\u540c\u540d\u6587\u4ef6\u3002",
                }),
            },
            "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("saved_path",)
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "H3Cache"

    def save(self, video, save_path, add_counter=True, prompt=None, extra_pnginfo=None):
        if video is None:
            print("[H3Cache] H3SaveVideo: video is None, skipping")
            return {"result": ("",)}

        output_dir = folder_paths.get_output_directory()
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

        safe_name = "".join(c for c in filename if c.isalnum() or c in ("_", "-", "."))
        if not safe_name:
            safe_name = "video"
        os.makedirs(folder, exist_ok=True)

        ext = "mp4"
        fmt = None
        try:
            from comfy_api.latest import Types
            ext = Types.VideoContainer.get_extension("auto")
            fmt = Types.VideoContainer("auto")
        except Exception:
            pass

        # 自动加序号（不覆盖）：与 ComfyUI 原生 SaveVideo 行为一致
        if add_counter:
            counter = 1
            while True:
                candidate = os.path.join(folder, f"{safe_name}_{counter:05}_.{ext}")
                if not os.path.isfile(candidate):
                    break
                counter += 1
            filepath = candidate
        else:
            filepath = os.path.join(folder, f"{safe_name}.{ext}")

        metadata = None
        if extra_pnginfo and isinstance(extra_pnginfo, dict):
            metadata = dict(extra_pnginfo)
        if prompt is not None:
            if metadata is None:
                metadata = {}
            metadata["prompt"] = prompt

        saved = False
        try:
            if fmt is not None:
                video.save_to(filepath, format=fmt, codec="auto", metadata=metadata, crf=None)
                saved = True
            else:
                video.save_to(filepath)
                saved = True
        except Exception as e:
            print(f"[H3Cache] H3SaveVideo error: {e}")
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
    "H3EncodeConditioning": H3EncodeConditioning,
    "H3SaveConditioning": H3SaveConditioning,
    "H3LoadConditioning": H3LoadConditioning,
    "H3ReadMetaSingle": H3ReadMetaSingle,
    "H3LoadConditioningBatch": H3LoadConditioningBatch,
    "H3ReencodeFromCache": H3ReencodeFromCache,
    "H3FreeMemory": H3FreeMemory,
    "H3SaveVideo": H3SaveVideo,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "H3EncodeConditioning": "H3 Encode Conditioning (CLIP-only, no VAE)",
    "H3SaveConditioning": "H3 Save Conditioning (cache + ref images)",
    "H3LoadConditioning": "H3 Load Conditioning (cache)",
    "H3ReadMetaSingle": "H3 Read Meta Single (.pt \u5143\u6570\u636e)",
    "H3LoadConditioningBatch": "H3 Load Conditioning Batch (cache)",
    "H3ReencodeFromCache": "H3 Reencode From Cache (cross-resolution)",
    "H3FreeMemory": "H3 Free Memory (\u663e\u5b58/\u5185\u5b58\u6e05\u7406)",
    "H3SaveVideo": "H3 Save Video (\u65e0\u9884\u89c8)",
}
