# Models Reference

This plugin only *saves/loads* conditioning; it does not bundle any weights.
You must install the MiniMax H3 model and its core ComfyUI nodes separately.
The example workflows reference the exact filenames below.

## 1. MiniMax H3 core nodes

Install the official MiniMax H3 ComfyUI integration (provides
`MiniMaxH3ReferenceToVideo`, `EmptyMiniMaxH3LatentAV`, `VAEDecodeAudio`,
`CreateVideo`, the H3 sampler, etc.):

- https://github.com/MiniMax-AI/MiniMax-H3-ComfyUI

## 2. Required model files

Place under `ComfyUI/models/` using the standard subfolders. The examples
were made with these exact files:

| Role | File | Subfolder | Notes |
| --- | --- | --- | --- |
| Text encoder (CLIP) | `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` | `text_encoders/` | Qwen3-VL 32B, H3-specialized, int8. ~25 GB. |
| UNet (base) | `minimax_h3_ref2va_pruned_int8_convrot.safetensors` | `diffusion_models/` | H3 ref2va pruned int8. |
| Turbo LoRA | `minimax_h3_turbo_4step_pruned-convertedByAIEverything.safetensors` | `loras/` | 4-step distilled LoRA. |
| Video VAE | `minimax_h3_video_vae_fp16.safetensors` | `vae/` | Decodes video latent. |
| Audio VAE | `minimax_h3_audio_vae_fp32.safetensors` | `vae/` | Decodes audio latent. |

## 3. Hardware notes

- The 32B text encoder is architecturally required; it cannot be swapped for a
  smaller Qwen. It is the dominant VRAM consumer in the **pre-encode** phase.
- int8 quantization of the encoder needs roughly 25 GB VRAM. Community
  `nvfp4_awq` quantizations can bring this down to ~14.6 GB, allowing an 8 GB
  GPU to run with ComfyUI's CPU offloading (32 GB+ system RAM recommended).
- The **generate** phase (the for-loop) loads only the UNet + LoRA + VAEs, so
  it is far lighter than the pre-encode phase.

## 4. Reference images (not committed)

The pre-encode example `preencode_multi_chain_黑猫.json` references local
images that are part of private artistic assets and are **not** included:

- `h3_ref/角色/黑猫沈天然/沈天然黑猫定妆照.png` (character)
- `B05_场景.png`, `A28_场景.png` (scene)

Replace these with your own character/scene reference images before running.
The conditioning that is saved is image-independent; once a `.pt` is written,
the generate phase no longer needs the images.

## 5. Output

- `.pt` conditioning caches are written to `output/h3_cond_cache/`.
- Generated videos are saved to `output/h3_videos/`.