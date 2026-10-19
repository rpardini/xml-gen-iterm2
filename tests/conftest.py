from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
EXAMPLE = ROOT / "examples" / "example.xml"
EXPECTED = Path(__file__).parent / "expected" / "example.json"


@pytest.fixture(autouse=True)
def fake_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """`~` in ssh-auth-sock must expand the same on every machine."""
    monkeypatch.setenv("HOME", "/home/test")
