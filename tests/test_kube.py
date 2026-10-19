from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from xml_gen_iterm2.cli import main
from xml_gen_iterm2.kube import discover_all, overlay_yaml, sync_overlays
from xml_gen_iterm2.model import Kind
from xml_gen_iterm2.parse import parse_bytes
from xml_gen_iterm2.render import render, to_json


def _kubeconfig(path: Path, *contexts: str) -> Path:
    doc = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": contexts[0] if contexts else "",
        "contexts": [{"name": c, "context": {"cluster": c, "user": c}} for c in contexts],
        "users": [{"name": c, "user": {"token": "SECRET-TOKEN"}} for c in contexts],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc))
    return path


def _cfg(tmp_path: Path, kube: str) -> str:
    return (
        f'<profiles><defaults kube-overlay-dir="{tmp_path}/overlays"/>'
        f'<local name="Local" hotkey="F13" kube="{kube}"/></profiles>'
    )


@pytest.fixture
def kubedir(tmp_path: Path) -> Path:
    d = tmp_path / "kube"
    _kubeconfig(d / "config", "alpha", "beta")
    _kubeconfig(d / "b.yaml", "gamma")
    _kubeconfig(d / "a.yaml", "beta")  # duplicate context name, other file
    (d / "broken.yaml").write_text("contexts: [unclosed\n  token: SECRET-TOKEN\n")
    return d


def test_discovery_order_dedupe_and_duplicates(tmp_path: Path, kubedir: Path, caplog: pytest.LogCaptureFixture) -> None:
    cfg = parse_bytes(_cfg(tmp_path, "kube/config kube/*.yaml kube/config").encode())
    found = discover_all(cfg, tmp_path)["Local"]
    assert [(c.file.name, c.display) for c in found] == [
        ("config", "alpha"),
        ("config", "beta (config)"),
        ("a.yaml", "beta (a.yaml)"),
        ("b.yaml", "gamma"),
    ]
    assert "skipping" in caplog.text
    assert "broken.yaml" in caplog.text
    assert "SECRET" not in caplog.text  # never leak kubeconfig contents


def test_missing_patterns_are_fine(tmp_path: Path) -> None:
    cfg = parse_bytes(_cfg(tmp_path, "~/nope/*.yaml nothing-here").encode())
    assert discover_all(cfg, tmp_path) == {"Local": ()}


def test_profiles_and_command(tmp_path: Path, kubedir: Path) -> None:
    cfg = parse_bytes(_cfg(tmp_path, "kube/b.yaml").encode())
    kube = discover_all(cfg, tmp_path)
    profiles = {p.name: p for p in render(cfg, None, kube)}
    assert list(profiles) == ["Local", "Local k8s context gamma"]
    assert profiles["Local"].hotkey is not None
    ctx = profiles["Local k8s context gamma"]
    assert ctx.hotkey is None
    assert ctx.kind is Kind.KUBE
    assert ctx.tags == ("local", "k8s")
    overlay = kube["Local"][0].overlay
    args = shlex.split(ctx.command)
    assert args[:3] == ["/usr/bin/env", "KUBECONTEXT=gamma", f"KUBECONFIG={overlay}:{(kubedir / 'b.yaml').resolve()}"]
    assert args[4] == "-l"
    doc = {x["Name"]: x for x in json.loads(to_json(list(profiles.values())))["Profiles"]}
    assert doc["Local k8s context gamma"]["Badge Text"] == "gamma"
    assert doc["Local k8s context gamma"]["Custom Command"] == "Yes"


def test_sync_overlays(tmp_path: Path, kubedir: Path) -> None:
    cfg = parse_bytes(_cfg(tmp_path, "kube/config").encode())
    kube = discover_all(cfg, tmp_path)
    out = tmp_path / "overlays"
    out.mkdir()
    (out / "stale.yaml").write_text("x")
    (out / "keep.txt").write_text("x")
    assert sync_overlays(kube, out) == (2, 1)
    assert sorted(p.name for p in out.iterdir()) == sorted([c.overlay.name for c in kube["Local"]] + ["keep.txt"])
    assert yaml.safe_load(kube["Local"][0].overlay.read_text()) == {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "alpha",
    }


def test_overlay_yaml_quotes_odd_names() -> None:
    assert yaml.safe_load(overlay_yaml("a: b@c"))["current-context"] == "a: b@c"


def test_stdout_does_not_write_overlays(tmp_path: Path, kubedir: Path) -> None:
    xml = tmp_path / "p.xml"
    xml.write_text(_cfg(tmp_path, "kube/config"))
    result = CliRunner().invoke(main, [str(xml), "--stdout", "--lan", "none"])
    assert result.exit_code == 0, result.output
    assert "Local k8s context alpha" in result.stdout
    assert not (tmp_path / "overlays").exists()


def test_write_syncs_overlays(tmp_path: Path, kubedir: Path) -> None:
    xml = tmp_path / "p.xml"
    xml.write_text(_cfg(tmp_path, "kube/config"))
    result = CliRunner().invoke(main, [str(xml), "-o", str(tmp_path / "out.json"), "--lan", "none", "--no-tree"])
    assert result.exit_code == 0, result.output
    assert len(list((tmp_path / "overlays").glob("*.yaml"))) == 2
