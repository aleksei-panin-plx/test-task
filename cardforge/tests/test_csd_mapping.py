"""Mathematical equivalence of CSD's CLIP visual mapping; tiny synthetic CPU model only."""

from __future__ import annotations

import pytest


class UnsafeCheckpoint:
    def __reduce__(self):
        return eval, ("1+1",)


def test_csd_checkpoint_accepts_legacy_numpy_metadata(tmp_path):
    import argparse
    import zipfile

    np = pytest.importorskip("numpy")
    torch = pytest.importorskip("torch")
    from cardforge.evaluate.csd_metric import load_checkpoint

    modern, legacy = tmp_path / "modern.pt", tmp_path / "legacy.pt"
    torch.save({"value": np.float64(0.5), "args": argparse.Namespace(example=1)}, modern)
    with zipfile.ZipFile(modern) as source, zipfile.ZipFile(legacy, "w") as target:
        changed = False
        for name in source.namelist():
            data = source.read(name)
            if name.endswith("data.pkl"):
                assert b"numpy._core.multiarray" in data
                data = data.replace(b"numpy._core.multiarray", b"numpy.core.multiarray")
                changed = True
            target.writestr(name, data)
        assert changed
    loaded = load_checkpoint(legacy)
    assert loaded["value"] == 0.5 and loaded["args"].example == 1


def test_csd_checkpoint_does_not_enable_arbitrary_pickle_execution(tmp_path):
    import pickle

    torch = pytest.importorskip("torch")
    from cardforge.evaluate.csd_metric import load_checkpoint

    path = tmp_path / "untrusted.pt"
    torch.save({"unexpected": UnsafeCheckpoint()}, path)
    with pytest.raises(pickle.UnpicklingError):
        load_checkpoint(path)


def test_csd_mapping_matches_independent_clip_forward():
    torch = pytest.importorskip("torch")
    from torch.nn import functional as f
    from transformers import CLIPVisionConfig, CLIPVisionModel

    from cardforge.evaluate.csd_metric import visual_weights

    torch.manual_seed(43)
    width, heads = 8, 2
    state = {
        "backbone.conv1.weight": torch.randn(width, 3, 14, 14) * 0.1,
        "backbone.class_embedding": torch.randn(width) * 0.1,
        "backbone.positional_embedding": torch.randn(5, width) * 0.1,
    }
    for name in (
        "ln_pre",
        "ln_post",
        "transformer.resblocks.0.ln_1",
        "transformer.resblocks.0.ln_2",
    ):
        state[f"backbone.{name}.weight"] = torch.ones(width)
        state[f"backbone.{name}.bias"] = torch.randn(width) * 0.1
    block = "backbone.transformer.resblocks.0."
    for name, shape in {
        "attn.in_proj": (3 * width, width),
        "attn.out_proj": (width, width),
        "mlp.c_fc": (4 * width, width),
        "mlp.c_proj": (width, 4 * width),
    }.items():
        separator = "_" if name == "attn.in_proj" else "."
        state[block + name + separator + "weight"] = torch.randn(*shape) * 0.1
        state[block + name + separator + "bias"] = torch.randn(shape[0]) * 0.1
    model = CLIPVisionModel(
        CLIPVisionConfig(
            hidden_size=width,
            intermediate_size=width * 4,
            num_hidden_layers=1,
            num_attention_heads=heads,
            image_size=28,
            patch_size=14,
            hidden_act="quick_gelu",
            layer_norm_eps=1e-5,
            attention_dropout=0.0,
        )
    ).eval()
    model.load_state_dict(visual_weights(state), strict=True)
    inputs = torch.randn(2, 3, 28, 28)

    def ln(x, prefix):
        return f.layer_norm(x, (width,), state[prefix + ".weight"], state[prefix + ".bias"], 1e-5)

    x = f.conv2d(inputs, state["backbone.conv1.weight"], stride=14).flatten(2).transpose(1, 2)
    x = torch.cat([state["backbone.class_embedding"].expand(2, 1, width), x], dim=1)
    x = ln(x + state["backbone.positional_embedding"], "backbone.ln_pre")
    q, k, v = f.linear(
        ln(x, block + "ln_1"),
        state[block + "attn.in_proj_weight"],
        state[block + "attn.in_proj_bias"],
    ).chunk(3, dim=-1)
    q, k, v = [t.reshape(2, 5, heads, width // heads).transpose(1, 2) for t in (q, k, v)]
    attention = (q @ k.transpose(-1, -2) / (width // heads) ** 0.5).softmax(dim=-1)
    combined = (attention @ v).transpose(1, 2).reshape(2, 5, width)
    x = x + f.linear(
        combined, state[block + "attn.out_proj.weight"], state[block + "attn.out_proj.bias"]
    )
    hidden = f.linear(
        ln(x, block + "ln_2"), state[block + "mlp.c_fc.weight"], state[block + "mlp.c_fc.bias"]
    )
    hidden = hidden * torch.sigmoid(1.702 * hidden)
    x = x + f.linear(hidden, state[block + "mlp.c_proj.weight"], state[block + "mlp.c_proj.bias"])
    expected = ln(x[:, 0, :], "backbone.ln_post")
    with torch.inference_mode():
        actual = model(pixel_values=inputs).pooler_output
    assert torch.allclose(actual, expected, atol=1e-5)
