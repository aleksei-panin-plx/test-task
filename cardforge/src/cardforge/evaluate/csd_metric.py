"""Local CSD ViT-L fallback using the authors' checkpoint and a strict CLIP mapping.

The authors' learned 1024x768 style head follows the CLIP visual tower. Architecture
and preprocessing: https://github.com/learn2phoenix/CSD (MIT). Checkpoint attribution:
Somepalli et al., University of Maryland, 2024; CC-BY-4.0. No remote code is executed.
"""

from __future__ import annotations

import json
from pathlib import Path

from cardforge.evaluate.common import sha256


def visual_weights(state: dict) -> dict:
    """Convert OpenAI CLIP visual keys to Transformers; missing/extra model keys are fatal."""
    result = {}
    simple = {
        "class_embedding": "embeddings.class_embedding",
        "positional_embedding": "embeddings.position_embedding.weight",
        "conv1.weight": "embeddings.patch_embedding.weight",
    }
    for key, value in state.items():
        if not key.startswith("backbone."):
            continue
        name = key.removeprefix("backbone.")
        if name in simple:
            result[simple[name]] = value
        elif name.startswith(("ln_pre.", "ln_post.")):
            target = name.replace("ln_pre.", "pre_layrnorm.").replace("ln_post.", "post_layernorm.")
            result[target] = value
        elif name.startswith("transformer.resblocks."):
            layer, suffix = name.removeprefix("transformer.resblocks.").split(".", 1)
            prefix = f"encoder.layers.{layer}."
            if suffix.startswith("attn.in_proj_"):
                kind = suffix.removeprefix("attn.in_proj_")
                for part, chunk in zip(("q", "k", "v"), value.chunk(3, dim=0), strict=True):
                    result[prefix + f"self_attn.{part}_proj.{kind}"] = chunk
            else:
                target = suffix.replace("ln_1.", "layer_norm1.").replace("ln_2.", "layer_norm2.")
                target = target.replace("attn.out_proj.", "self_attn.out_proj.")
                target = target.replace("mlp.c_fc.", "mlp.fc1.").replace("mlp.c_proj.", "mlp.fc2.")
                result[prefix + target] = value
        else:
            raise ValueError(f"unexpected CSD visual parameter: {name}")
    return result


def load_checkpoint(path: Path):
    import argparse

    import numpy as np
    import torch

    allowed = [
        argparse.Namespace,
        np.dtype,
        np._core.multiarray.scalar,
        (np._core.multiarray.scalar, "numpy.core.multiarray.scalar"),
        np.dtypes.Float64DType,
        np.dtypes.Int64DType,
    ]
    with torch.serialization.safe_globals(allowed):
        return torch.load(path, map_location="cpu", mmap=True, weights_only=True)


_MODELS: dict[str, tuple] = {}


def _model(folder: Path):
    """(model, head), verified and loaded once per process: per-card scoring reuses it."""
    import torch
    from transformers import CLIPVisionConfig, CLIPVisionModel

    key = str(folder.resolve())
    if key in _MODELS:
        return _MODELS[key]
    manifest = json.loads((folder / "source.json").read_text())
    for name, expected in manifest["sha256"].items():
        if sha256(folder / name) != expected:
            raise ValueError(f"changed CSD file: {name}")
    torch.set_num_threads(4)
    # The checkpoint also contains training metadata. Permit only the exact standard-library
    # metadata types advertised by the authors' artifact; keep weights_only=True.
    checkpoint = load_checkpoint(folder / "pytorch_model.bin")
    source = checkpoint.get("model_state_dict", checkpoint)
    state = {k.removeprefix("module."): v for k, v in source.items()}
    head = state["last_layer_style"].float()
    if tuple(head.shape) != (1024, 768):
        raise ValueError("unexpected CSD style head")
    config = CLIPVisionConfig(
        hidden_size=1024,
        intermediate_size=4096,
        num_hidden_layers=24,
        num_attention_heads=16,
        image_size=224,
        patch_size=14,
        hidden_act="quick_gelu",
        layer_norm_eps=1e-5,
        attention_dropout=0.0,
    )
    model = CLIPVisionModel(config).float().eval().to("cpu")
    model.load_state_dict(visual_weights(state), strict=True)
    _MODELS[key] = (model, head)
    return _MODELS[key]


def embed(paths: list[Path], folder: Path) -> list[list[float]]:
    import numpy as np
    import torch
    from PIL import Image

    model, head = _model(folder)
    mean = np.asarray([0.48145466, 0.4578275, 0.40821073], dtype="float32")
    std = np.asarray([0.26862954, 0.26130258, 0.27577711], dtype="float32")

    def preprocess(path):
        image = Image.open(path).convert("RGB")
        w, h = image.size
        size = (224, int(224 * h / w)) if w <= h else (int(224 * w / h), 224)
        image = image.resize(size, Image.Resampling.BICUBIC)
        left, top = round((size[0] - 224) / 2), round((size[1] - 224) / 2)
        pixels = np.asarray(image.crop((left, top, left + 224, top + 224)), dtype="float32") / 255
        return torch.from_numpy(((pixels - mean) / std).transpose(2, 0, 1).copy())

    vectors = []
    with torch.inference_mode():
        for start in range(0, len(paths), 4):
            inputs = torch.stack([preprocess(p) for p in paths[start : start + 4]])
            values = model(pixel_values=inputs).pooler_output @ head
            vectors.extend(torch.nn.functional.normalize(values, dim=1).tolist())
    return vectors
