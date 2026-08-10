# Local Model Cards

This is the current working inventory for FruitcakeAI and FruitcakeImageLab.
It separates upstream model facts from local configuration and local verification.

Last reviewed: 2026-07-13

## How To Read This Document

- **Configured** means the model appears in the current Fruitcake configuration or ImageLab workflow registry.
- **Verified** means the current project has exercised that path successfully.
- **Needs local confirmation** means the model is configured or referenced, but the exact installed quantization, capabilities, or checkpoint hash still needs to be checked on the host.
- Ollama tags are runtime aliases. The upstream Hugging Face card may describe the base model, not the exact Ollama quantization or template.

## Fruitcake Chat Models

### Qwen2.5 14B Instruct

- **Fruitcake name:** `ollama_chat/qwen2.5:14b`
- **Role:** small local task model; selectable local chat model
- **Model family:** Qwen2.5 instruction-tuned text generation
- **Capabilities:** text chat, summarization, structured reasoning; tool use depends on the Ollama/LiteLLM chat path and the prompt/tool schema
- **Vision:** no; use the configured Qwen2.5-VL model instead
- **Quantization:** Ollama-managed; exact local quantization is not recorded in Fruitcake configuration
- **Upstream card:** [Qwen2.5-14B-Instruct](https://huggingface.co/Qwen/Qwen2.5-14B-Instruct)
- **Local guidance:** useful for inexpensive task steps and lightweight transformations. It is not the preferred model for large-document synthesis or complex multi-tool orchestration.

### Qwen2.5 32B Instruct

- **Fruitcake name:** `ollama_chat/qwen2.5:32b`
- **Role:** selectable local chat model and historical large-model default
- **Model family:** Qwen2.5 instruction-tuned text generation
- **Capabilities:** text chat, document work, coding, and tool use when the Ollama chat interface supports the supplied schema
- **Vision:** no
- **Quantization:** Ollama-managed; exact local quantization is not recorded in Fruitcake configuration
- **Upstream card:** [Qwen2.5-32B-Instruct](https://huggingface.co/Qwen/Qwen2.5-32B-Instruct)
- **Local guidance:** requires substantially more memory than the 14B model. It is a reasonable quality step up for local text work, but context size, tool definitions, and long document results still consume runtime memory.

### Qwen3.6 35B-A3B

- **Fruitcake name:** `ollama_chat/qwen3.6:35b`
- **Role:** current large local task model and document-summary model
- **Model family:** Qwen3.6 mixture-of-experts model with approximately 35B total parameters and approximately 3B active parameters per token, according to the model naming and upstream card
- **Capabilities:** text generation, coding, long-context work, and multimodal input in the upstream model family; actual Ollama capabilities depend on the installed package
- **Vision:** upstream Qwen3.6-35B-A3B is documented as image-text-to-text, but Fruitcake currently routes image description through `ollama_chat/qwen2.5vl:7b` instead
- **Tool use:** supported in Fruitcake's normal path, with targeted Qwen/Ollama guardrails retained for known malformed tool-call cases
- **Quantization:** Ollama-managed; exact local quantization is not recorded in Fruitcake configuration
- **Upstream card:** [Qwen3.6-35B-A3B](https://huggingface.co/Qwen/Qwen3.6-35B-A3B)
- **Local guidance:** preferred local model for higher-quality synthesis and document summaries. Large tool payloads, long histories, and repeated tool turns remain the main stability risks.

### Qwen3.6 27B Heretic / NEO-CODE GGUF

- **Fruitcake name:** `ollama_chat/qwen36-heretic:q4km`
- **Role:** selectable experimental local model
- **Model family:** community fine-tune of Qwen3.6-27B with a GGUF Q4_K_M quantization imported through Ollama
- **Capabilities:** text generation, coding, creative writing, and image-text input according to the community card; Fruitcake currently treats it as text-only through `LOCAL_TOOL_TEXT_ONLY_MODELS` when configured that way
- **Vision:** the upstream community card includes multimodal examples, but this local Ollama import should not be assumed to support vision until the installed Modelfile and runtime are verified
- **Tool use:** this model previously reported that it did not support tools; keep it in text-only mode unless a deliberate compatibility test proves otherwise
- **License/source:** the community card identifies the model as Apache-2.0 and links its base/fine-tune lineage
- **Upstream card:** [DavidAU Qwen3.6-27B Heretic NEO-CODE GGUF](https://huggingface.co/DavidAU/Qwen3.6-27B-Heretic-Uncensored-FINETUNE-NEO-CODE-Di-IMatrix-MAX-GGUF)
- **Local guidance:** experimental only. The community model card makes strong performance and safety claims that are not independently verified here. Treat it as a separate model with its own prompt, refusal, tool, and content-quality behavior.

## Fruitcake Vision Model

### Qwen2.5-VL 7B Instruct

- **Fruitcake name:** `ollama_chat/qwen2.5vl:7b`
- **Role:** configured `IMAGE_VISION_MODEL` for `describe_image`
- **Model family:** Qwen2.5 vision-language model
- **Capabilities:** image description, visual question answering, OCR-like visual interpretation, and image-plus-text reasoning
- **Tool use:** not the default role; the vision path should remain focused on describing supplied images
- **Upstream card:** [Qwen2.5-VL-7B-Instruct](https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct)
- **Local guidance:** this is the current explicit vision choice. Do not infer vision support from a model's name alone; the Ollama package and API adapter must accept image content.

## FruitcakeImageLab Image Workflows

ImageLab's workflow registry is not a model download manifest. The checkpoint filenames below are the files the configured ComfyUI workflows expect; the exact installed files and hashes should be recorded separately when reproducibility matters.

### SDXL Basic

- **ImageLab workflow:** `sdxl_basic`
- **Role:** default ImageLab workflow
- **Checkpoint:** no fixed override; it uses the globally selected ComfyUI checkpoint
- **Family:** SDXL-compatible text-to-image workflow
- **Default workflow:** 1024x1024, 8 steps, Euler sampler, `sgm_uniform` scheduler, CFG 2.0
- **Capabilities:** text-to-image, image-to-image, and SDXL face-reference/IPAdapter conditioning when the selected checkpoint and required nodes are available
- **Upstream family card:** [Stable Diffusion XL Base 1.0](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0)
- **Local guidance:** this is a workflow contract, not one fixed model card. Its output quality and prompting behavior depend on the checkpoint selected in ComfyUI. Record that checkpoint separately for reproducible results.

### Juggernaut XL Lightning

- **ImageLab workflow:** `juggernaut_lightning`
- **Checkpoint:** `juggernaut_xl_lightning.safetensors`
- **Family:** SDXL Lightning-distilled photorealistic checkpoint
- **Default workflow:** 1024x1024, 6 steps, DPM++ SDE, Karras scheduler, CFG 1.8
- **Capabilities:** fast SDXL text-to-image, image-to-image, and face-reference/IPAdapter conditioning through the shared SDXL graph
- **Upstream card:** [RunDiffusion/Juggernaut-XL-Lightning](https://huggingface.co/RunDiffusion/Juggernaut-XL-Lightning)
- **License note:** the upstream card identifies CreativeML Open RAIL-M and says commercial API deployment requires explicit licensing. Verify commercial terms before offering generated-image services.
- **Local guidance:** use the low-step Lightning settings. Applying ordinary 25- or 30-step SDXL settings can reduce quality or waste time.

### Lustify APEX V8

- **ImageLab workflow:** `lustify`
- **Checkpoint:** `lustifySDXLNSFW_apexV8.safetensors`
- **Family:** SDXL community checkpoint focused on photorealistic adult-oriented generation
- **Default workflow:** 1024x1024, 30 steps, DPM++ 2M SDE, Karras scheduler, CFG 3.5
- **Capabilities:** SDXL text-to-image, image-to-image, and face-reference/IPAdapter conditioning through the shared SDXL graph
- **Upstream reference:** [LUSTIFY! APEX v8 model listing](https://tensor.art/models/989656629584639086)
- **License/source note:** the repository does not yet record the original checkpoint source, hash, or license terms. Treat this as an experimental/private workflow until those details are documented.
- **Local guidance:** keep this workflow separated from general-purpose or family-safe presets. Its model and output policy should be explicit before any multiuser or customer deployment.

### Stable Diffusion 3.5 Large

- **ImageLab workflow:** `sd3_5_large`
- **Checkpoint:** `sd3.5_large.safetensors`
- **Family:** Stability AI Stable Diffusion 3.5 Large, an approximately 8B text-to-image diffusion model
- **Default workflow:** 1024x1024, 20 steps, Euler sampler, `sgm_uniform` scheduler, CFG 4.01
- **Text encoders:** `clip_l.safetensors`, `clip_g.safetensors`, and `t5xxl_fp16.safetensors`
- **Capabilities:** text-to-image; the current ImageLab workflow does not advertise SDXL face-reference/IPAdapter support for this model
- **Prompt guidance:** natural descriptive sentences with subject, composition, lighting, materials, and style
- **Upstream card:** [stabilityai/stable-diffusion-3.5-large](https://huggingface.co/stabilityai/stable-diffusion-3.5-large)
- **Local guidance:** high-memory and slow on Apple Silicon. This is the quality-oriented image workflow in the current registry.

### DreamShaper 8

- **ImageLab workflow:** `dreamshaper_8`
- **Checkpoint:** `DreamShaper_8_pruned.safetensors`
- **Family:** Stable Diffusion 1.5 fine-tune
- **Default workflow:** 512x512, 25 steps, DPM++ 2M Karras, CFG 7.0
- **Capabilities:** general text-to-image generation with a smaller SD1.5 memory footprint
- **Upstream card:** [Lykon/dreamshaper-8](https://huggingface.co/Lykon/dreamshaper-8)
- **Local guidance:** use SD1.5-compatible prompting and resolutions. It is a good fast baseline but should not be expected to follow SD3.5 prompt conventions exactly.

### MoP Flexible v7.1

- **ImageLab workflow:** `mop_flexible`
- **Checkpoint:** `mopMixtureOfPerverts_v71Flexible.safetensors`
- **Family:** locally installed SDXL 1.0-based community merge/fine-tune
- **Default workflow:** 1024x1024, 12 steps, Euler Ancestral, `sgm_uniform` scheduler, CFG 2.0
- **Capabilities:** SDXL text-to-image; the workflow is marked as supporting SDXL face-reference/IPAdapter conditioning
- **Upstream source:** the source card is not currently recorded in the repository; the workflow comment identifies the checkpoint by filename/hash research rather than a stable upstream URL
- **Local guidance:** add the exact source URL, hash, license, and recommended trigger words to this card when the installed checkpoint is next verified.

### Wan 2.1 VACE 1.3B

- **ImageLab workflow:** `wan_vace_1_3b`
- **Diffusion model:** `diffusion_models/wan2.1_vace_1.3B_fp16.safetensors`
- **Additional files:** `text_encoders/umt5_xxl_fp16.safetensors`, `vae/wan_2.1_vae.safetensors`
- **Family:** Wan 2.1 VACE video-conditioning model
- **Default workflow:** 832x480, 20 steps, UniPC sampler, 2-second output at 16 FPS
- **Capabilities:** image-to-video; requires an input image and produces MP4 output
- **Upstream card:** [Wan-AI/Wan2.1-VACE-1.3B](https://huggingface.co/Wan-AI/Wan2.1-VACE-1.3B)
- **Local guidance:** intended for short, approximately 480p clips. It is compute-intensive on Apple Silicon and should be treated as a heavy workflow.

## Embedding And Retrieval Models

### BGE Small English v1.5

- **Fruitcake setting:** `EMBEDDING_MODEL=BAAI/bge-small-en-v1.5`
- **Role:** local document embeddings for library/RAG retrieval
- **Embedding dimension:** 384 in the current configuration
- **Capabilities:** text embedding and semantic retrieval; it is not a chat or generation model
- **Important constraint:** changing the embedding model after documents are indexed requires reindexing because vector dimensions and embedding space must remain consistent
- **Configuration reference:** [LLM backends and embedding guidance](LLM_BACKENDS.md)

## Capability Summary

| Model/workflow | Text | Vision input | Tool use | Image output | Video output | Primary role |
|---|---:|---:|---:|---:|---:|---|
| Qwen2.5 14B | Yes | No | Conditional | No | No | Small task/local chat model |
| Qwen2.5 32B | Yes | No | Conditional | No | No | Larger local text model |
| Qwen3.6 35B-A3B | Yes | Upstream supports it; local path unverified | Yes with guardrails | No | No | Large local synthesis and summaries |
| Qwen3.6 27B Heretic | Yes | Upstream card claims it; local path unverified | Text-only recommended | No | No | Experimental coding/text model |
| Qwen2.5-VL 7B | Yes | Yes | Not primary role | No | No | Image description |
| SDXL Basic | No | No | No | Yes | No | Default SDXL workflow using selected checkpoint |
| Juggernaut XL Lightning | No | No | No | Yes | No | Fast photorealistic SDXL workflow |
| Lustify APEX V8 | No | No | No | Yes | No | Experimental adult-oriented SDXL workflow |
| SD3.5 Large | No | No | No | Yes | No | High-quality text-to-image |
| DreamShaper 8 | No | No | No | Yes | No | Fast SD1.5 image generation |
| MoP Flexible v7.1 | No | Reference conditioning | No | Yes | No | SDXL image generation |
| Wan 2.1 VACE 1.3B | No | Image conditioning | No | No | Yes | Short image-to-video |
| BGE Small English v1.5 | No | No | No | No | No | Document embeddings |

## Local Verification Checklist

The following commands should be run on the host where Ollama and ComfyUI actually run. The Ollama CLI currently crashes during model enumeration on the development Mac because of an MLX/Metal initialization failure, so the installed model list was not treated as authoritative during this review.

```bash
ollama list
ollama show qwen2.5:14b
ollama show qwen2.5:32b
ollama show qwen3.6:35b
ollama show qwen36-heretic:q4km
ollama show qwen2.5vl:7b
```

Record the following for each installed Ollama model:

- exact model tag
- quantization
- context length
- capabilities reported by `ollama show`
- whether tools work in an isolated Fruitcake test
- whether image input works through the configured API path

For ImageLab, record the exact checkpoint hash and source URL for each installed ComfyUI file, especially `mopMixtureOfPerverts_v71Flexible.safetensors`, whose upstream source is not currently documented in the project.
