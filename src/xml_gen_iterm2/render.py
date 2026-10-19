"""Model -> iTerm2 Dynamic Profiles."""

from __future__ import annotations

import json
import shlex
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from xml_gen_iterm2.model import DEFAULT_PORT, Config, Defaults, Group, Host, Kind, OciConsole, Profile, Serial
from xml_gen_iterm2.parse import ConfigError

_OCI_RSA = "-oHostKeyAlgorithms=+ssh-rsa -oPubkeyAcceptedAlgorithms=+ssh-rsa"

Chain = tuple[Host, ...]  # bastions, outermost first


def bash_c(script: str) -> str:
    """`bash -c "..."` when that's safe (keeps commands readable), otherwise fully shell-quoted."""
    if any(c in script for c in '"$`\\'):
        return f"bash -c {shlex.quote(script)}"
    return f'bash -c "{script}"'


def _proxy_opt(chain: Chain, d: Defaults) -> str:
    """`-o ProxyCommand=...` to reach the last host of `chain` (itself reached through the ones before it)."""
    if not chain:
        return ""
    bastion = chain[-1]
    parts = [
        "ssh",
        d.ssh_opts,
        # ssh expands %-tokens in ProxyCommand; escape the inner hop's so they reach the right ssh
        _proxy_opt(chain[:-1], d).replace("%", "%%"),
        _port_opt(bastion.port),
        f"-W %h:%p {bastion.user}@{bastion.address}",
    ]
    return f"-o ProxyCommand={shlex.quote(_join(parts))}"


def _port_opt(port: int) -> str:
    return f"-p {port}" if port != DEFAULT_PORT else ""


def _join(parts: list[str]) -> str:
    return " ".join(p for p in parts if p)


def ssh_command(user: str, address: str, port: int, chain: Chain, d: Defaults, *, tty: bool = False) -> str:
    return _join(["ssh", d.ssh_opts, _proxy_opt(chain, d), "-t" if tty else "", _port_opt(port), f"{user}@{address}"])


def _addresses(host: Host) -> list[tuple[str, str, tuple[str, ...]]]:
    """(address, name suffix, extra tags) per reachable address."""
    out: list[tuple[str, str, tuple[str, ...]]] = []
    if host.ip:
        out.append((host.ip, "", ()))
    if host.tailnet:
        out.append((host.tailnet, " tailnet", ("tailnet",)))
    return out


def _tags(host: Host, *extra: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*host.tags, *extra)))


def _ssh_targets(host: Host) -> list[tuple[str, str, int]]:
    """(profile base name, user, port) for each ssh login."""
    multi = len(host.users) > 1
    out = [(f"{host.name} {u}" if multi else host.name, u, host.port) for u in host.users]
    out += [(s.name, s.user, s.port) for s in host.ssh]
    return out


def _oci_command(console: OciConsole, d: Defaults) -> str:
    gateway = f"{console.connection}@instance-console.{console.region}.oci.oraclecloud.com"
    ssh = f"ssh {_OCI_RSA} -o ProxyCommand='ssh -W %h:%p -p 443 {_OCI_RSA} {gateway}' {console.instance}"
    if not d.ssh_auth_sock:
        return ssh
    return bash_c(f"export SSH_AUTH_SOCK={Path(d.ssh_auth_sock).expanduser()}; {ssh}")


def _serial_command(adapter: Host, serial: Serial, chain: Chain, d: Defaults) -> str:
    picocom = _join(["picocom", "-b", str(serial.baud), serial.args or "", shlex.quote(serial.dev)])
    ssh = ssh_command(serial.user or adapter.user, adapter.address, adapter.port, chain, d, tty=True)
    return f"{ssh} {picocom}"


def host_profiles(host: Host, chain: Chain, d: Defaults) -> list[Profile]:
    """Profiles belonging to `host` itself (not to hosts nested in it)."""
    profiles: list[Profile] = []
    for base, user, port in _ssh_targets(host):
        for address, suffix, extra in _addresses(host):
            cmd = ssh_command(user, address, port, chain, d)
            profiles.append(Profile(base + suffix, cmd, _tags(host, *extra), Kind.SSH))

    if host.mac and host.ip and not chain:
        for base, user, _port in _ssh_targets(host)[: len(host.users)]:
            wait = f"until nc -z {host.ip} {host.port}; do sleep 2; done"
            script = f"{d.wol_cmd} {host.mac}; {wait}; {ssh_command(user, host.ip, host.port, chain, d)}"
            profiles.append(Profile(f"{base} WOL", bash_c(script), _tags(host, "wol"), Kind.WOL))

    profiles.extend(
        Profile(c.name or f"{host.name} CONSOLE", _oci_command(c, d), _tags(host, "ociConsole"), Kind.CONSOLE)
        for c in host.consoles
    )

    profiles.extend(
        Profile(
            f"{s.target_name} serial via {host.name}",
            _serial_command(host, s, chain, d),
            _tags(host, "serial"),
            Kind.SERIAL,
        )
        for s in host.serials
    )
    return profiles


def walk_hosts(config: Config) -> Iterator[tuple[Host, Chain]]:
    """Every host in document order, with the bastion chain used to reach it."""

    def visit(host: Host, chain: Chain) -> Iterator[tuple[Host, Chain]]:
        yield host, chain
        for serial in host.serials:
            if serial.target is not None:
                yield from visit(serial.target, chain)  # same network as the adapter host
        for sub in host.hosts:
            yield from visit(sub, (*chain, host))

    def children(items: tuple[Group | Host, ...]) -> Iterator[tuple[Host, Chain]]:
        for item in items:
            if isinstance(item, Group):
                yield from children(item.children)
            else:
                yield from visit(item, ())

    for lan in config.lans:
        yield from children(lan.children)


def render(config: Config) -> list[Profile]:
    profiles = [p for host, chain in walk_hosts(config) for p in host_profiles(host, chain, config.defaults)]
    if dupes := sorted(name for name, n in Counter(p.name for p in profiles).items() if n > 1):
        raise ConfigError(f"duplicate profile name(s): {', '.join(map(repr, dupes))}")
    return profiles


def to_json(profiles: list[Profile]) -> str:
    doc: dict[str, Any] = {
        "Profiles": [
            {
                "Name": p.name,
                "Guid": p.guid,
                "Custom Command": "Yes",
                "Badge Text": p.name,
                "Tags": list(p.tags),
                "Command": p.command,
            }
            for p in profiles
        ]
    }
    return json.dumps(doc, indent=4, ensure_ascii=False) + "\n"
