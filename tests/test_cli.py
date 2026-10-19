from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from xml_gen_iterm2.cli import main

from conftest import EXAMPLE


def test_stdout_prints_json_and_tree() -> None:
    result = CliRunner().invoke(main, [str(EXAMPLE), "--stdout", "--lan", "home"])
    assert result.exit_code == 0, result.output
    assert "you are in: home" in result.stderr
    assert len(json.loads(result.stdout)["Profiles"]) == 29


def test_write_then_check(tmp_path: Path) -> None:
    out = tmp_path / "sub" / "profiles.json"
    runner = CliRunner()
    assert runner.invoke(main, [str(EXAMPLE), "-o", str(out), "--no-tree", "--lan", "home"]).exit_code == 0
    assert json.loads(out.read_text())["Profiles"]
    assert runner.invoke(main, [str(EXAMPLE), "-o", str(out), "--check", "--lan", "home"]).exit_code == 0
    out.write_text("{}\n")
    result = runner.invoke(main, [str(EXAMPLE), "-o", str(out), "--check", "--lan", "home"])
    assert result.exit_code == 1
    assert out.read_text() == "{}\n"
    assert not list(out.parent.glob(".*.tmp"))


def test_bad_config_exits_1(tmp_path: Path) -> None:
    bad = tmp_path / "bad.xml"
    bad.write_text('<profiles><host name="h"/></profiles>')
    result = CliRunner().invoke(main, [str(bad), "--stdout"])
    assert result.exit_code == 1
    assert "needs 'ip'" in result.stderr
    assert result.stdout == ""


def test_unknown_lan_exits_1() -> None:
    result = CliRunner().invoke(main, [str(EXAMPLE), "--stdout", "--lan", "mars"])
    assert result.exit_code == 1
    assert "no such lan" in result.stderr


def test_lan_none_dims_everything() -> None:
    result = CliRunner().invoke(main, [str(EXAMPLE), "--stdout", "--lan", "none"])
    assert result.exit_code == 0
    assert "away from all lans" in result.stderr
