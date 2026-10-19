"""Which `<lan>` are we in? Matches this machine's addresses against each LAN's `subnets`."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass

import psutil

from xml_gen_iterm2.log import log
from xml_gen_iterm2.model import Config

Address = ipaddress.IPv4Address | ipaddress.IPv6Address


@dataclass(frozen=True)
class Detection:
    lan_id: str | None
    reason: str


def local_addresses() -> list[tuple[str, Address]]:
    """(interface, address) for every non-loopback, non-link-local address on this machine."""
    out: list[tuple[str, Address]] = []
    for iface, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            try:
                ip = ipaddress.ip_address(a.address.split("%", 1)[0])
            except ValueError:
                continue
            if not (ip.is_loopback or ip.is_link_local):
                out.append((iface, ip))
    return out


def detect_lan(config: Config, addrs: list[tuple[str, Address]]) -> Detection:
    """First LAN (document order) with a subnet containing one of `addrs`."""
    matches: list[tuple[str, str]] = []
    for lan in config.lans:
        if lan.id is None:
            continue
        hit = next(
            (f"{iface} {ip} ∈ {net}" for net in lan.subnets for iface, ip in addrs if ip in net),
            None,
        )
        if hit is not None:
            matches.append((lan.id, hit))
    if not matches:
        return Detection(None, "no local address matches any lan subnets")
    if len(matches) > 1:
        log.warning(f"several lans match ({', '.join(m[0] for m in matches)}); using {matches[0][0]!r}")
    return Detection(*matches[0])
