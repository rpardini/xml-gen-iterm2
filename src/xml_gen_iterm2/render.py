"""Model -> iTerm2 Dynamic Profiles."""

from __future__ import annotations

import json
import shlex
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xml_gen_iterm2.kube import KubeContext, KubeDiscovery
from xml_gen_iterm2.model import (
    DEFAULT_PORT,
    Config,
    Defaults,
    Group,
    Host,
    Hotkey,
    Kind,
    Lan,
    Local,
    OciConsole,
    Profile,
    Serial,
)
from xml_gen_iterm2.parse import ConfigError

_OCI_RSA = "-oHostKeyAlgorithms=+ssh-rsa -oPubkeyAcceptedAlgorithms=+ssh-rsa"


@dataclass(frozen=True)
class Hop:
    """A bastion and the address used to reach it from where we are."""

    host: Host
    address: str
    via_tailnet: bool


Chain = tuple[Hop, ...]  # bastions, outermost first


def resolve(host: Host, *, away: bool) -> tuple[str, bool]:
    """(address, via_tailnet) to reach `host` itself: its LAN ip at home, its tailnet address when away (if any)."""
    if host.tailnet and (away or not host.ip):
        return host.tailnet, True
    return host.address, False


def extend(chain: Chain, bastion: Host, *, away: bool) -> Chain:
    """Chain to reach hosts behind `bastion`. A tailnet hop is reachable from anywhere, so it restarts the chain."""
    address, via_tailnet = resolve(bastion, away=away)
    hop = Hop(bastion, address, via_tailnet)
    return (hop,) if via_tailnet else (*chain, hop)


def reachable(chain: Chain, *, away: bool) -> bool:
    """Whether something reached through `chain` (empty = directly) is likely reachable from where we are."""
    return not away or (bool(chain) and chain[0].via_tailnet)


def bash_c(script: str) -> str:
    """`bash -c "..."` when that's safe (keeps commands readable), otherwise fully shell-quoted."""
    if any(c in script for c in '"$`\\'):
        return f"bash -c {shlex.quote(script)}"
    return f'bash -c "{script}"'


def _proxy_opt(chain: Chain, d: Defaults) -> str:
    """`-o ProxyCommand=...` to reach the last host of `chain` (itself reached through the ones before it)."""
    if not chain:
        return ""
    hop = chain[-1]
    parts = [
        "ssh",
        d.ssh_opts,
        # ssh expands %-tokens in ProxyCommand; escape the inner hop's so they reach the right ssh
        _proxy_opt(chain[:-1], d).replace("%", "%%"),
        _port_opt(hop.host.port),
        f"-W %h:%p {hop.host.user}@{hop.address}",
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


# keep the tab open after a failure so the error stays readable (serial/virsh profiles close on end)
_PAUSE = 'printf "\\nPress Enter to close... "; read -r _'


def picocom_script(serial: Serial) -> str:
    """POSIX sh run on the adapter host. picocom locks the port, so a second session would die instantly (and the tab
    closes on end): refuse up front if the device is held, showing by whom, and pause on any picocom failure."""
    picocom = _join(["picocom", "-b", str(serial.baud), serial.args or "", '"$dev"'])
    return (
        f"dev={shlex.quote(serial.dev)}; "
        'pids=$(fuser "$dev" 2>/dev/null); '
        'if [ -n "$pids" ]; then echo "$dev is in use by:"; ps -fp $pids 2>/dev/null || echo "PIDs:$pids"; '
        f"else {picocom} && exit; fi; {_PAUSE}"
    )


def virsh_script(domain: str) -> str:
    """POSIX sh run on the hypervisor. virsh fails if another session holds the console or the domain isn't running."""
    return f'dom={shlex.quote(domain)}; virsh console "$dom" && exit; {_PAUSE}'


Located = tuple[Host, Chain, bool]  # host, bastion chain to it, whether its LAN is away
Hypervisors = Mapping[str, Located]


def _remote_tty(on: Located, user: str | None, script: str, d: Defaults) -> tuple[str, bool]:
    """(`ssh -t ... '<script>'` on host `on`, likely-unreachable-from-here flag)."""
    host, chain, away = on
    address, via_tailnet = resolve(host, away=away)
    if via_tailnet:
        chain = ()
    ssh = ssh_command(user or host.user, address, host.port, chain, d, tty=True)
    return f"{ssh} {shlex.quote(script)}", not via_tailnet and not reachable(chain, away=away)


def _serial_profile(adapter: Host, serial: Serial, chain: Chain, d: Defaults, *, away: bool) -> Profile:
    cmd, dim = _remote_tty((adapter, chain, away), serial.user, picocom_script(serial), d)
    name = f"{serial.target_name} serial via {adapter.name}"
    return Profile(name, cmd, _tags(adapter, "serial"), Kind.SERIAL, away=dim)


def _virsh_profile(vm: Host, hypervisors: Hypervisors, d: Defaults) -> Profile:
    assert vm.hypervisor is not None and vm.virsh_domain is not None
    if vm.hypervisor not in hypervisors:  # validated at parse time
        raise ConfigError(f"host {vm.name!r}: unknown hypervisor {vm.hypervisor!r}")
    hyp = hypervisors[vm.hypervisor]
    cmd, dim = _remote_tty(hyp, None, virsh_script(vm.virsh_domain), d)
    return Profile(f"{vm.name} console via {hyp[0].name}", cmd, _tags(vm, "virsh"), Kind.VIRSH, away=dim)


def index_hosts(config: Config, current: str | None = None) -> dict[str, Located]:
    """Hosts with an `id`, located (chain + away) as seen from `current`."""
    return {host.id: (host, chain, away) for host, chain, away in walk_hosts(config, current) if host.id}


def host_profiles(
    host: Host, chain: Chain, d: Defaults, *, away: bool = False, hypervisors: Hypervisors | None = None
) -> list[Profile]:
    """Profiles belonging to `host` itself (not to hosts nested in it). `away`: its LAN isn't the current one."""
    profiles: list[Profile] = []
    hotkey_user = host.hotkey_user or host.user
    hotkey = host.hotkey  # goes on the first ssh profile of hotkey_user (first address), then is used up
    for base, user, port in _ssh_targets(host):
        for address, suffix, extra in _addresses(host):
            if suffix:  # tailnet: reachable from anywhere, no bastions needed
                cmd, dim = ssh_command(user, address, port, (), d), False
            else:
                cmd, dim = ssh_command(user, address, port, chain, d), not reachable(chain, away=away)
            hk = hotkey if user == hotkey_user else None
            if hk is not None:
                hotkey = None
            profiles.append(Profile(base + suffix, cmd, _tags(host, *extra), Kind.SSH, away=dim, hotkey=hk))

    if host.wol and chain:
        raise ConfigError(f'host {host.name!r}: wol="true" on a host behind a bastion; magic packets can\'t get there')
    if host.wol and host.mac and host.ip:
        for base, user, _port in _ssh_targets(host)[: len(host.users)]:
            wait = f"until nc -z {host.ip} {host.port}; do sleep 2; done"
            script = f"{d.wol_cmd} {host.mac}; {wait}; {ssh_command(user, host.ip, host.port, chain, d)}"
            profiles.append(Profile(f"{base} WOL", bash_c(script), _tags(host, "wol"), Kind.WOL, away=away))

    profiles.extend(
        Profile(c.name or f"{host.name} CONSOLE", _oci_command(c, d), _tags(host, "ociConsole"), Kind.CONSOLE)
        for c in host.consoles
    )

    profiles.extend(_serial_profile(host, s, chain, d, away=away) for s in host.serials)
    if host.virsh_domain:
        profiles.append(_virsh_profile(host, hypervisors or {}, d))
    return profiles


def is_away(lan: Lan, current: str | None) -> bool:
    """A lan without subnets (e.g. cloud stuff with public IPs) is reachable from anywhere, so never away."""
    return bool(lan.subnets) and lan.id != current


def walk_hosts(config: Config, current: str | None = None) -> Iterator[tuple[Host, Chain, bool]]:
    """Every host in document order, with the bastion chain used to reach it and whether its LAN is away."""

    def visit(host: Host, chain: Chain, away: bool) -> Iterator[tuple[Host, Chain, bool]]:
        yield host, chain, away
        for serial in host.serials:
            if serial.target is not None:
                yield from visit(serial.target, chain, away)  # same network as the adapter host
        for sub in host.hosts:
            yield from visit(sub, extend(chain, host, away=away), away)

    def children(items: tuple[Group | Host, ...], away: bool) -> Iterator[tuple[Host, Chain, bool]]:
        for item in items:
            if isinstance(item, Group):
                yield from children(item.children, away)
            else:
                yield from visit(item, (), away)

    for lan in config.lans:
        yield from children(lan.children, is_away(lan, current))


def local_profiles(loc: Local, contexts: tuple[KubeContext, ...] = ()) -> list[Profile]:
    """The plain local shell, then one profile per discovered kube context (pinned via KUBECONFIG overlay)."""
    tags = tuple(dict.fromkeys((*loc.tags, "k8s")))
    return [
        Profile(loc.name, "", loc.tags, Kind.LOCAL, hotkey=loc.hotkey),
        *(Profile(f"{loc.name} k8s context {c.display}", c.command, tags, Kind.KUBE, badge=c.name) for c in contexts),
    ]


def render(config: Config, current: str | None = None, kube: KubeDiscovery | None = None) -> list[Profile]:
    """`current`: id of the LAN we're in (None = away from all of them). `kube`: discovered contexts per <local>."""
    d = config.defaults
    hyps = index_hosts(config, current)
    profiles = [
        p
        for host, chain, away in walk_hosts(config, current)
        for p in host_profiles(host, chain, d, away=away, hypervisors=hyps)
    ]
    profiles += [p for loc in config.locals for p in local_profiles(loc, (kube or {}).get(loc.name, ()))]
    if dupes := sorted(name for name, n in Counter(p.name for p in profiles).items() if n > 1):
        raise ConfigError(f"duplicate profile name(s): {', '.join(map(repr, dupes))}")
    by_key: dict[tuple[int, int], Profile] = {}
    for p in profiles:
        if p.hotkey is None:
            continue
        key = (p.hotkey.key_code, p.hotkey.modifiers)
        if key in by_key:
            raise ConfigError(f"hotkey {p.hotkey.label!r} used by both {by_key[key].name!r} and {p.name!r}")
        by_key[key] = p
    return profiles


# iTerm2's iTermSessionEndAction (ITAddressBookMgr.h): 0 = no action, 1 = close, 2 = restart
_SESSION_END_CLOSE = 1


# iTerm2's iTermWindowType (ITAddressBookMgr.h): full-width window attached to the top edge, sized in percent
_WINDOW_TYPE_TOP_PERCENTAGE = 2
_QUAKE_HEIGHT_PERCENT = 40


def _hotkey_json(hk: Hotkey) -> dict[str, Any]:
    out: dict[str, Any] = {
        "Has Hotkey": True,
        "HotKey Activated By Modifier": False,
        "HotKey Key Code": hk.key_code,
        "HotKey Characters": hk.char,
        "HotKey Characters Ignoring Modifiers": hk.char,
        "HotKey Modifier Flags": hk.modifiers,
        "HotKey Window AutoHides": True,
        "HotKey Window Animates": True,
        "HotKey Window Floats": False,
    }
    if hk.style == "quake":
        out |= {
            "Window Type": _WINDOW_TYPE_TOP_PERCENTAGE,
            "Width Percentage": 100,
            "Height Percentage": _QUAKE_HEIGHT_PERCENT,
        }
    return out


def _profile_json(p: Profile) -> dict[str, Any]:
    out: dict[str, Any] = {"Name": p.name, "Guid": p.guid}
    if p.kind is Kind.LOCAL:  # plain login shell
        out |= {"Custom Command": "No", "Tags": list(p.tags)}
    else:
        out |= {"Custom Command": "Yes", "Badge Text": p.badge or p.name, "Tags": list(p.tags), "Command": p.command}
    if p.kind in {Kind.SERIAL, Kind.VIRSH}:  # quitting the console means "done"; don't restart like the parent
        out["Close Sessions On End"] = _SESSION_END_CLOSE
    if p.hotkey is not None:
        out |= _hotkey_json(p.hotkey)
    return out


def to_json(profiles: list[Profile]) -> str:
    doc: dict[str, Any] = {"Profiles": [_profile_json(p) for p in profiles]}
    return json.dumps(doc, indent=4, ensure_ascii=False) + "\n"
