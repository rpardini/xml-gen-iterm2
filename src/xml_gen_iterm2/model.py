"""Parsed, inheritance-resolved configuration model."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import IPv4Network, IPv6Network
from typing import Literal

DEFAULT_SSH_OPTS = (
    "-o GlobalKnownHostsFile=/dev/null -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -A -X -Y"
)
DEFAULT_PORT = 22
GUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/rpardini/xml-gen-iterm2")

_OCI_CONNECTION_RE = re.compile(r"ocid1\.instanceconsoleconnection\.oc1\.([a-z0-9-]+)\.[a-z0-9]+")
_OCI_INSTANCE_RE = re.compile(r"ocid1\.instance\.oc1\.[a-z0-9-]+\.[a-z0-9]+")


HotkeyStyle = Literal["quake", "window"]


@dataclass(frozen=True)
class Hotkey:
    """A global iTerm2 hotkey, encoded the way iTerm2 stores it (see iTermShortcut.m)."""

    label: str  # as written, e.g. "cmd+F12"
    key_code: int  # macOS virtual key code
    char: str  # NSFunctionKey character, e.g. U+F70F for F12
    modifiers: int  # NSEvent modifier flags (cmd/opt/ctrl/shift only)
    style: HotkeyStyle = "quake"


@dataclass(frozen=True)
class Defaults:
    user: str = "root"
    ssh_opts: str = DEFAULT_SSH_OPTS
    ssh_auth_sock: str = ""
    wol_cmd: str = "wakeonlan"
    kube_overlay_dir: str = "~/.kube/iterm2"


@dataclass(frozen=True)
class SshEntry:
    """An explicit extra `<ssh>` profile on a host."""

    user: str
    port: int
    name: str


@dataclass(frozen=True)
class OciConsole:
    connection: str
    instance: str
    name: str | None = None

    @property
    def region(self) -> str:
        match = _OCI_CONNECTION_RE.fullmatch(self.connection)
        if match is None:  # validated at parse time
            raise ValueError(f"bad OCI console connection OCID: {self.connection}")
        return match.group(1)


@dataclass(frozen=True)
class Serial:
    """Console of another board, wired to the enclosing host's USB-serial adapter."""

    dev: str
    baud: int
    user: str | None = None
    args: str | None = None
    name: str | None = None
    target: Host | None = None

    @property
    def target_name(self) -> str:
        if self.name:
            return self.name
        if self.target is not None:
            return self.target.name
        raise ValueError("serial without name or target")  # validated at parse time


@dataclass(frozen=True)
class Host:
    name: str
    users: tuple[str, ...]
    port: int
    tags: tuple[str, ...]
    ip: str | None = None
    tailnet: str | None = None
    mac: str | None = None
    wol: bool = False  # emit "<name> WOL" profiles (needs mac + ip)
    id: str | None = None  # for references from other hosts (e.g. hypervisor=)
    hypervisor: str | None = None  # id of the host running this VM under libvirt
    virsh_domain: str | None = None
    hotkey: Hotkey | None = None
    hotkey_user: str | None = None  # which user's profile gets the hotkey (default: first user)
    ipv6: str | None = None
    note: str | None = None
    ssh: tuple[SshEntry, ...] = ()
    serials: tuple[Serial, ...] = ()
    consoles: tuple[OciConsole, ...] = ()
    hosts: tuple[Host, ...] = ()  # reached using this host as bastion

    @property
    def address(self) -> str:
        """Primary address: LAN ip if there is one, otherwise the tailnet address."""
        addr = self.ip or self.tailnet
        if addr is None:  # validated at parse time
            raise ValueError(f"host {self.name} has no address")
        return addr

    @property
    def user(self) -> str:
        return self.users[0]


@dataclass(frozen=True)
class Group:
    name: str
    path: str
    children: tuple[Group | Host, ...]


@dataclass(frozen=True)
class Lan:
    """Purely organizational wrapper (for now). `id` is None for stuff outside any `<lan>`."""

    id: str | None
    children: tuple[Group | Host, ...]
    subnets: tuple[IPv4Network | IPv6Network, ...] = ()


@dataclass(frozen=True)
class Config:
    defaults: Defaults
    lans: tuple[Lan, ...]
    locals: tuple[Local, ...] = ()


@dataclass(frozen=True)
class Local:
    """A plain local shell profile (no custom command)."""

    name: str
    tags: tuple[str, ...]
    hotkey: Hotkey | None = None
    kube: tuple[str, ...] = ()  # kubeconfig paths/globs; one extra profile per context found


class Kind(StrEnum):
    SSH = "ssh"
    CONSOLE = "console"
    SERIAL = "serial"
    WOL = "wol"
    VIRSH = "virsh"
    LOCAL = "local"
    KUBE = "kube"


@dataclass(frozen=True)
class Profile:
    name: str
    command: str
    tags: tuple[str, ...]
    kind: Kind
    away: bool = False  # likely unreachable from the current LAN; only affects the tree, not the JSON
    hotkey: Hotkey | None = None
    badge: str | None = None  # defaults to the name

    @property
    def guid(self) -> str:
        """Stable across reorders; changes only when the profile is renamed."""
        return str(uuid.uuid5(GUID_NAMESPACE, self.name))


def oci_connection_ok(value: str) -> bool:
    return _OCI_CONNECTION_RE.fullmatch(value) is not None


def oci_instance_ok(value: str) -> bool:
    return _OCI_INSTANCE_RE.fullmatch(value) is not None


def oci_extract(paste: str) -> tuple[str, str] | None:
    """Pull (connection, instance) OCIDs out of the raw ssh string OCI's web console gives you."""
    conn = _OCI_CONNECTION_RE.search(paste)
    inst = _OCI_INSTANCE_RE.search(paste)
    if conn is None or inst is None:
        return None
    return conn.group(0), inst.group(0)
