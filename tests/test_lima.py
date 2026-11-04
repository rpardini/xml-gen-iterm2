from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from xml_gen_iterm2.lima import discover_all
from xml_gen_iterm2.parse import ConfigError, parse_bytes
from xml_gen_iterm2.render import render


def _vm(tmp_path: Path, name: str, status: str = "Running", host: str | None = None) -> dict[str, str]:
    cfg = tmp_path / name / "ssh.config"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(f"# generated\nHost {host or 'lima-' + name}\n  User tester\n")
    return {"name": name, "status": status, "sshConfigFile": str(cfg)}


@pytest.fixture
def fake_limactl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str | None]:
    """Canned `limactl list --json`; records LIMA_HOME of each call."""
    homes: list[str | None] = []
    by_home = {
        None: [_vm(tmp_path, "dev"), _vm(tmp_path, "stopped", "Stopped"), _vm(tmp_path, "colima-bogus")],
        "colima": [_vm(tmp_path, "colima"), _vm(tmp_path, "colima-arm"), _vm(tmp_path, "other")],
    }

    def run(cmd: list[str], env: dict[str, str], **_: object) -> subprocess.CompletedProcess[str]:
        home = env.get("LIMA_HOME")
        homes.append(home)
        insts = by_home["colima" if home and home.endswith("/.colima/_lima") else None]
        return subprocess.CompletedProcess(cmd, 0, "\n".join(json.dumps(i) for i in insts), "")

    monkeypatch.setattr(subprocess, "run", run)
    return homes


def _cfg(attrs: str) -> str:
    return f'<profiles><local name="Local" {attrs}/></profiles>'


def test_profiles_for_running_vms(fake_limactl: list[str | None]) -> None:
    cfg = parse_bytes(_cfg('lima="true" colima="true"').encode())
    profiles = {p.name: p for p in render(cfg, None, None, discover_all(cfg))}
    assert [n for n in profiles if n != "Local"] == [
        "Local lima dev",
        "Local lima dev root",
        "Local lima colima-bogus",
        "Local lima colima-bogus root",
        "Local colima default",
        "Local colima default root",
        "Local colima arm",
        "Local colima arm root",
    ]
    user, root = profiles["Local lima dev"], profiles["Local lima dev root"]
    assert "-l" not in user.command.split()
    assert "-l root" in root.command
    for p in (user, root):
        assert "ControlMaster=no" in p.command and "ControlPath=none" in p.command
        assert {"-A", "-X", "-Y"} <= set(p.command.split())
        assert p.command.endswith(" lima-dev")
    assert "root" in root.tags and "root" not in user.tags and "lima" in user.tags


def test_off_by_default(fake_limactl: list[str | None]) -> None:
    cfg = parse_bytes(_cfg("").encode())
    assert discover_all(cfg) == {}
    assert fake_limactl == []


def test_missing_limactl_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object, **__: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    assert discover_all(parse_bytes(_cfg('lima="true"').encode())) == {}


def test_bad_bool() -> None:
    with pytest.raises(ConfigError):
        parse_bytes(_cfg('lima="yes"').encode())
