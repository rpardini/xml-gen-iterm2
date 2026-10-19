from __future__ import annotations

import ipaddress

import pytest

from xml_gen_iterm2.parse import parse_bytes
from xml_gen_iterm2.where import Address, detect_lan, local_addresses

CFG = parse_bytes(
    b'<profiles><lan id="a" subnets="10.0.0.0/24 fd00:1::/64"/><lan id="b" subnets="10.0.0.0/16"/>'
    b'<lan id="c"/></profiles>'
)


def _addrs(*ips: str) -> list[tuple[str, Address]]:
    return [("en0", ipaddress.ip_address(ip)) for ip in ips]


def test_single_match() -> None:
    d = detect_lan(CFG, _addrs("192.168.1.2", "10.0.5.1"))
    assert d.lan_id == "b"
    assert d.reason == "en0 10.0.5.1 ∈ 10.0.0.0/16"


def test_no_match() -> None:
    assert detect_lan(CFG, _addrs("192.168.1.2")).lan_id is None


def test_first_of_several_wins(caplog: pytest.LogCaptureFixture) -> None:
    assert detect_lan(CFG, _addrs("10.0.0.9")).lan_id == "a"
    assert "several lans match" in caplog.text


def test_ipv6() -> None:
    assert detect_lan(CFG, _addrs("fd00:1::42")).lan_id == "a"


def test_local_addresses_smoke() -> None:
    for iface, ip in local_addresses():
        assert iface
        assert not ip.is_loopback
