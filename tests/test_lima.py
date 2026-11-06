from __future__ import annotations

import json
import subprocess

import pytest

from xml_gen_iterm2.lima import discover_all
from xml_gen_iterm2.parse import ConfigError, parse_bytes
from xml_gen_iterm2.render import render


def _vm(name: str, status: str = "Running", port: int = 60022) -> dict[str, object]:
    return {
        "name": name,
        "status": status,
        "sshAddress": "127.0.0.1",
        "sshLocalPort": port,
        "IdentityFile": "/lima/_config/user",
        "config": {"user": {"name": "tester"}},
    }


@pytest.fixture
def fake_limactl(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """Canned `limactl list --json`; records LIMA_HOME of each call."""
    homes: list[str | None] = []
    by_home = {
        None: [_vm("dev", port=61235), _vm("stopped", "Stopped", 0), _vm("colima-bogus"), _vm("no-port", port=0)],
        "colima": [_vm("colima"), _vm("colima-arm"), _vm("other")],
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
    assert user.command.endswith(" -o IdentitiesOnly=yes -p 61235 tester@127.0.0.1")
    assert root.command.endswith(" -i /lima/_config/user -p 61235 root@127.0.0.1")  # agent keys offered too
    for p in (user, root):
        words = p.command.split()
        assert "-F" not in words and "ControlMaster=no" in words and "ControlPath=none" in words
        assert {"-A", "-X", "-Y"} <= set(words)
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
