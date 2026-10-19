"""XML -> model: validation and inheritance resolution."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.parsers import expat

from xml_gen_iterm2.model import (
    DEFAULT_PORT,
    Config,
    Defaults,
    Group,
    Host,
    Hotkey,
    Lan,
    Local,
    OciConsole,
    Serial,
    SshEntry,
    oci_connection_ok,
    oci_extract,
    oci_instance_ok,
)

_MAC_RE = re.compile(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}")

_HOST_ATTRS = frozenset(
    {"name", "ip", "tailnet", "user", "users", "port", "tags", "mac", "wol", "ipv6", "note"}
    | {"id", "hypervisor", "virsh-domain", "hotkey", "hotkey-user", "style"}
)

# macOS virtual key codes (Carbon kVK_F1..kVK_F20)
_FKEY_CODES = (122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111, 105, 107, 113, 106, 64, 79, 80, 90)
_NS_F1_FUNCTION_KEY = 0xF704  # NSF1FunctionKey; F(n) = this + n - 1
# NSEventModifierFlag* values
_MODIFIERS = {"shift": 1 << 17, "ctrl": 1 << 18, "opt": 1 << 19, "cmd": 1 << 20}
_ATTRS: dict[str, frozenset[str]] = {
    "profiles": frozenset(),
    "local": frozenset({"name", "hotkey", "style", "tags", "kube"}),
    "defaults": frozenset({"user", "ssh-opts", "ssh-auth-sock", "wol-cmd", "kube-overlay-dir"}),
    "lan": frozenset({"id", "subnets"}),
    "group": frozenset({"name", "user", "users", "port", "tags"}),
    "host": _HOST_ATTRS,
    "ssh": frozenset({"name", "user", "port"}),
    "serial": frozenset({"dev", "baud", "user", "args", "name"}),
    "oci-console": frozenset({"connection", "instance", "paste", "name"}),
}


class ConfigError(Exception):
    """A problem in the user's XML; message is meant to be shown as-is."""


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    line: int
    children: list[_Node] = field(default_factory=list)

    def where(self) -> str:
        label = self.attrs.get("name") or self.attrs.get("id") or self.attrs.get("dev")
        return f"<{self.tag}{f' {label!r}' if label else ''}> (line {self.line})"

    def fail(self, msg: str) -> ConfigError:
        return ConfigError(f"{self.where()}: {msg}")


@dataclass(frozen=True)
class _Ctx:
    """What groups hand down to their children."""

    path: tuple[str, ...]
    users: tuple[str, ...]
    port: int
    tags: tuple[str, ...]


def _read_tree(data: bytes) -> _Node:
    parser = expat.ParserCreate()
    stack: list[_Node] = []
    root: list[_Node] = []

    def start(tag: str, attrs: dict[str, str]) -> None:
        node = _Node(tag, attrs, parser.CurrentLineNumber)
        (stack[-1].children if stack else root).append(node)
        stack.append(node)

    def end(_tag: str) -> None:
        stack.pop()

    def text(data: str) -> None:
        if data.strip():
            where = stack[-1].where() if stack else "document"
            raise ConfigError(f"{where}: unexpected text {data.strip()!r} (line {parser.CurrentLineNumber})")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    try:
        parser.Parse(data, True)
    except expat.ExpatError as e:
        raise ConfigError(f"XML syntax error: {e}") from e
    return root[0]


def _check(node: _Node, allowed_children: frozenset[str] = frozenset()) -> None:
    allowed = _ATTRS[node.tag]
    if unknown := sorted(set(node.attrs) - allowed):
        raise node.fail(f"unknown attribute(s) {', '.join(unknown)}; allowed: {', '.join(sorted(allowed)) or 'none'}")
    for child in node.children:
        if child.tag not in allowed_children:
            raise child.fail(f"not allowed inside <{node.tag}>; allowed: {', '.join(sorted(allowed_children))}")


def _required(node: _Node, attr: str) -> str:
    value = node.attrs.get(attr, "").strip()
    if not value:
        raise node.fail(f"missing required attribute {attr!r}")
    return value


def _int(node: _Node, attr: str, lo: int, hi: int) -> int | None:
    if attr not in node.attrs:
        return None
    raw = node.attrs[attr]
    try:
        value = int(raw)
    except ValueError:
        raise node.fail(f"{attr}={raw!r} is not an integer") from None
    if not lo <= value <= hi:
        raise node.fail(f"{attr}={value} out of range {lo}..{hi}")
    return value


def _parse_hotkey(node: _Node) -> Hotkey | None:
    """`hotkey="[mod+...]F<n>"` (+ optional `style="quake|window"`) -> Hotkey."""
    raw = node.attrs.get("hotkey", "").strip()
    style = node.attrs.get("style", "").strip()
    if not raw:
        if style:
            raise node.fail("'style' without 'hotkey'")
        return None
    if style not in {"", "quake", "window"}:
        raise node.fail(f"style={style!r} must be 'quake' or 'window'")
    *mods, key = [part.strip().lower() for part in raw.split("+")]
    match = re.fullmatch(r"f(\d{1,2})", key)
    if match is None or not 1 <= int(match.group(1)) <= len(_FKEY_CODES):
        raise node.fail(f"hotkey={raw!r}: key must be F1..F{len(_FKEY_CODES)}")
    if unknown := [m for m in mods if m not in _MODIFIERS]:
        raise node.fail(f"hotkey={raw!r}: unknown modifier(s) {', '.join(unknown)}; use {', '.join(_MODIFIERS)}")
    n = int(match.group(1))
    return Hotkey(
        label=raw,
        key_code=_FKEY_CODES[n - 1],
        char=chr(_NS_F1_FUNCTION_KEY + n - 1),
        modifiers=sum({_MODIFIERS[m] for m in mods}),
        style="window" if style == "window" else "quake",
    )


def _parse_local(node: _Node) -> Local:
    _check(node)
    return Local(
        name=node.attrs.get("name", "").strip() or "Local",
        tags=_dedupe(("local", *node.attrs.get("tags", "").split())),
        hotkey=_parse_hotkey(node),
        kube=tuple(node.attrs.get("kube", "").split()),
    )


def _bool(node: _Node, attr: str) -> bool:
    raw = node.attrs.get(attr, "false").strip().lower()
    if raw not in {"true", "false"}:
        raise node.fail(f"{attr}={raw!r} must be 'true' or 'false'")
    return raw == "true"


def _users(node: _Node) -> tuple[str, ...] | None:
    if "user" in node.attrs and "users" in node.attrs:
        raise node.fail("use either 'user' or 'users', not both")
    raw = node.attrs.get("user") or node.attrs.get("users")
    if raw is None:
        return None
    users = tuple(raw.split())
    if not users:
        raise node.fail("empty user list")
    return users


def _ctx_for(node: _Node, ctx: _Ctx) -> _Ctx:
    """Apply a group's/host's inheritable attributes on top of the inherited context."""
    path = (*ctx.path, _required(node, "name")) if node.tag == "group" else ctx.path
    return _Ctx(
        path=path,
        users=_users(node) or ctx.users,
        port=_int(node, "port", 1, 65535) or ctx.port,
        tags=(*ctx.tags, *node.attrs.get("tags", "").split()),
    )


def _dedupe(items: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _parse_ssh(node: _Node, host_name: str, ctx: _Ctx) -> SshEntry:
    _check(node)
    user = node.attrs.get("user", "").strip() or ctx.users[0]
    port = _int(node, "port", 1, 65535) or ctx.port
    default_name = f"{host_name} {user}" + (f" :{port}" if port != DEFAULT_PORT else "")
    return SshEntry(user=user, port=port, name=node.attrs.get("name", "").strip() or default_name)


def _parse_oci(node: _Node) -> OciConsole:
    _check(node)
    paste = node.attrs.get("paste")
    if paste is not None:
        if "connection" in node.attrs or "instance" in node.attrs:
            raise node.fail("use either 'paste' or 'connection'+'instance', not both")
        extracted = oci_extract(paste)
        if extracted is None:
            raise node.fail("could not find console-connection and instance OCIDs in 'paste'")
        connection, instance = extracted
    else:
        connection, instance = _required(node, "connection"), _required(node, "instance")
    if not oci_connection_ok(connection):
        raise node.fail(f"bad console connection OCID {connection!r}")
    if not oci_instance_ok(instance):
        raise node.fail(f"bad instance OCID {instance!r}")
    return OciConsole(connection=connection, instance=instance, name=node.attrs.get("name", "").strip() or None)


def _parse_serial(node: _Node, ctx: _Ctx) -> Serial:
    _check(node, frozenset({"host"}))
    if len(node.children) > 1:
        raise node.fail("can wrap at most one <host> (the board on the other end of the cable)")
    baud = _int(node, "baud", 50, 20_000_000)
    if baud is None:
        raise node.fail("missing required attribute 'baud'")
    target = _parse_host(node.children[0], ctx, leaf=True) if node.children else None
    name = node.attrs.get("name", "").strip() or None
    if name is None and target is None:
        raise node.fail("needs either a 'name' or a nested <host> for the target board")
    return Serial(
        dev=_required(node, "dev"),
        baud=baud,
        user=node.attrs.get("user", "").strip() or None,
        args=node.attrs.get("args", "").strip() or None,
        name=name,
        target=target,
    )


def _parse_host(node: _Node, ctx: _Ctx, *, leaf: bool = False) -> Host:
    """`ctx` is the enclosing group's context; nested hosts get that same context, not the parent host's."""
    _check(node, frozenset({"ssh"}) if leaf else frozenset({"ssh", "serial", "oci-console", "host"}))
    name = _required(node, "name")
    own = _ctx_for(node, ctx)
    ip = node.attrs.get("ip", "").strip() or None
    tailnet = node.attrs.get("tailnet", "").strip() or None
    if ip is None and tailnet is None:
        raise node.fail("needs 'ip' and/or 'tailnet'")
    mac = node.attrs.get("mac", "").strip() or None
    if mac is not None and not _MAC_RE.fullmatch(mac):
        raise node.fail(f"bad mac address {mac!r}")
    wol = _bool(node, "wol")
    if wol and mac is None:
        raise node.fail("wol=\"true\" needs a 'mac'")
    if wol and ip is None:
        raise node.fail("wol=\"true\" needs a LAN 'ip' (magic packets don't cross the tailnet)")
    hypervisor = node.attrs.get("hypervisor", "").strip() or None
    virsh_domain = node.attrs.get("virsh-domain", "").strip() or None
    if (hypervisor is None) != (virsh_domain is None):
        raise node.fail("'hypervisor' and 'virsh-domain' go together")
    hotkey = _parse_hotkey(node)
    hotkey_user = node.attrs.get("hotkey-user", "").strip() or None
    if hotkey_user is not None:
        if hotkey is None:
            raise node.fail("'hotkey-user' without 'hotkey'")
        if hotkey_user not in own.users:
            raise node.fail(f"hotkey-user={hotkey_user!r} is not one of this host's users ({', '.join(own.users)})")

    tags = _dedupe(("/".join(own.path),) if own.path else ()) + _dedupe(own.tags)
    by_tag: dict[str, list[_Node]] = {}
    for child in node.children:
        by_tag.setdefault(child.tag, []).append(child)

    return Host(
        name=name,
        users=own.users,
        port=own.port,
        tags=_dedupe(tags),
        ip=ip,
        tailnet=tailnet,
        mac=mac,
        wol=wol,
        id=node.attrs.get("id", "").strip() or None,
        hypervisor=hypervisor,
        virsh_domain=virsh_domain,
        hotkey=hotkey,
        hotkey_user=hotkey_user,
        ipv6=node.attrs.get("ipv6", "").strip() or None,
        note=node.attrs.get("note", "").strip() or None,
        ssh=tuple(_parse_ssh(c, name, own) for c in by_tag.get("ssh", [])),
        serials=tuple(_parse_serial(c, ctx) for c in by_tag.get("serial", [])),
        consoles=tuple(_parse_oci(c) for c in by_tag.get("oci-console", [])),
        hosts=tuple(_parse_host(c, ctx) for c in by_tag.get("host", [])),
    )


def _parse_children(node: _Node, ctx: _Ctx) -> tuple[Group | Host, ...]:
    out: list[Group | Host] = []
    for child in node.children:
        if child.tag == "group":
            _check(child, frozenset({"group", "host"}))
            sub = _ctx_for(child, ctx)
            out.append(Group(name=sub.path[-1], path="/".join(sub.path), children=_parse_children(child, sub)))
        else:
            out.append(_parse_host(child, ctx))
    return tuple(out)


def _subnets(node: _Node) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    out: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in node.attrs.get("subnets", "").split():
        try:
            out.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            raise node.fail(f"bad subnet {raw!r} (want CIDR, e.g. 192.168.1.0/24)") from None
    return tuple(dict.fromkeys(out))


def _host_nodes(node: _Node) -> list[_Node]:
    out = [node] if node.tag == "host" else []
    for child in node.children:
        out += _host_nodes(child)
    return out


def _check_refs(root: _Node) -> None:
    """Host ids are unique across the file; `hypervisor=` must name another host's id."""
    by_id: dict[str, _Node] = {}
    hosts = _host_nodes(root)
    for node in hosts:
        host_id = node.attrs.get("id", "").strip()
        if not host_id:
            continue
        if host_id in by_id:
            raise node.fail(f"duplicate host id {host_id!r} (already used at line {by_id[host_id].line})")
        by_id[host_id] = node
    for node in hosts:
        ref = node.attrs.get("hypervisor", "").strip()
        if not ref:
            continue
        if ref not in by_id:
            raise node.fail(f"hypervisor={ref!r}: no host with that id; known: {', '.join(sorted(by_id)) or 'none'}")
        if by_id[ref] is node:
            raise node.fail("a host can't be its own hypervisor")


def parse_bytes(data: bytes) -> Config:
    root = _read_tree(data)
    if root.tag != "profiles":
        raise root.fail("root element must be <profiles>")
    _check_refs(root)
    _check(root, frozenset({"defaults", "local", "lan", "group", "host"}))

    defaults_nodes = [c for c in root.children if c.tag == "defaults"]
    if len(defaults_nodes) > 1:
        raise defaults_nodes[1].fail("only one <defaults> allowed")
    defaults = Defaults()
    if defaults_nodes:
        d = defaults_nodes[0]
        _check(d)
        defaults = Defaults(
            user=d.attrs.get("user", defaults.user).strip() or defaults.user,
            ssh_opts=d.attrs.get("ssh-opts", defaults.ssh_opts).strip(),
            ssh_auth_sock=d.attrs.get("ssh-auth-sock", defaults.ssh_auth_sock).strip(),
            wol_cmd=d.attrs.get("wol-cmd", defaults.wol_cmd).strip() or defaults.wol_cmd,
            kube_overlay_dir=d.attrs.get("kube-overlay-dir", "").strip() or defaults.kube_overlay_dir,
        )

    base = _Ctx(path=(), users=(defaults.user,), port=DEFAULT_PORT, tags=())
    lans: list[Lan] = []
    loose: list[_Node] = []
    seen_ids: set[str] = set()
    seen_nets: dict[ipaddress.IPv4Network | ipaddress.IPv6Network, str] = {}

    def flush_loose() -> None:
        if loose:
            lans.append(Lan(id=None, children=_parse_children(_Node("profiles", {}, 0, list(loose)), base)))
            loose.clear()

    for child in root.children:
        if child.tag == "lan":
            flush_loose()
            _check(child, frozenset({"group", "host"}))
            lan_id = _required(child, "id")
            if lan_id in seen_ids:
                raise child.fail(f"duplicate lan id {lan_id!r}")
            seen_ids.add(lan_id)
            subnets = _subnets(child)
            for net in subnets:
                if net in seen_nets:
                    raise child.fail(f"subnet {net} already belongs to lan {seen_nets[net]!r}")
                seen_nets[net] = lan_id
            lans.append(Lan(id=lan_id, children=_parse_children(child, base), subnets=subnets))
        elif child.tag not in {"defaults", "local"}:
            loose.append(child)
    flush_loose()
    locals_ = tuple(_parse_local(c) for c in root.children if c.tag == "local")
    return Config(defaults=defaults, lans=tuple(lans), locals=locals_)


def parse_file(path: Path) -> Config:
    return parse_bytes(path.read_bytes())
