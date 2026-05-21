from __future__ import annotations

from pathlib import Path

from agentflow_computer_mcp.driver.presets import DEFAULT_PRESETS_PATH, load_presets


def test_default_presets_load_and_have_required_fields() -> None:
    presets = load_presets()
    assert len(presets) >= 10, "expected ≥10 shipped presets"
    for p in presets:
        assert p.get("label"), p
        assert p.get("task"), p
        assert len(p["label"]) <= 60


def test_default_presets_path_exists() -> None:
    assert DEFAULT_PRESETS_PATH.exists(), f"missing {DEFAULT_PRESETS_PATH}"


def test_load_presets_handles_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    assert load_presets(missing) == []


def test_load_presets_inline_value(tmp_path: Path) -> None:
    f = tmp_path / "p.yaml"
    f.write_text(
        "- label: \"Quick\"\n  task: \"do thing\"\n- label: \"Two\"\n  task: \"another\"\n",
        encoding="utf-8",
    )
    out = load_presets(f)
    assert len(out) == 2
    assert out[0] == {"label": "Quick", "task": "do thing"}
    assert out[1] == {"label": "Two", "task": "another"}


def test_load_presets_block_scalar(tmp_path: Path) -> None:
    f = tmp_path / "p.yaml"
    f.write_text(
        "- label: \"Block\"\n  task: |\n    line one\n    line two\n",
        encoding="utf-8",
    )
    out = load_presets(f)
    assert len(out) == 1
    assert out[0]["label"] == "Block"
    assert "line one" in out[0]["task"]
    assert "line two" in out[0]["task"]
