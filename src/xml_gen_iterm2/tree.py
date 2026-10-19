"""Rich tree view of the parsed config and what each host generates."""

from __future__ import annotations

from rich.markup import escape
from rich.text import Text
from rich.tree import Tree

from xml_gen_iterm2.model import DEFAULT_PORT, Config, Group, Host, Kind, Profile
from xml_gen_iterm2.render import Chain, host_profiles

_KIND_STYLE: dict[Kind, tuple[str, str]] = {
    Kind.SSH: ("🔑", "green"),
    Kind.CONSOLE: ("🖥 ", "magenta"),
    Kind.SERIAL: ("🔌", "yellow"),
    Kind.WOL: ("⏰", "cyan"),
}


def _host_label(host: Host, chain: Chain, *, serial_target: bool = False) -> str:
    users = ",".join(host.users)
    port = f":{host.port}" if host.port != DEFAULT_PORT else ""
    addrs = " ".join(a for a in (host.ip, host.tailnet and f"[dim](tailnet {host.tailnet})[/dim]") if a)
    label = f"[bold blue]{escape(host.name)}[/] {escape(users)}@{addrs}{port}"
    if chain:
        label += f" [dim]via {' → '.join(escape(b.name) for b in chain)}[/dim]"
    if serial_target:
        label = f"[yellow]⇄[/] {label}"
    if host.note:
        label += f" [dim italic]# {escape(host.note)}[/]"
    return label


def _profile_label(p: Profile) -> Text:
    icon, style = _KIND_STYLE[p.kind]
    text = Text.assemble(f"{icon} ", (p.name, style))
    text.append(f"  [{', '.join(p.tags)}]", style="dim")
    return text


def _add_host(parent: Tree, host: Host, chain: Chain, config: Config, *, serial_target: bool = False) -> int:
    node = parent.add(_host_label(host, chain, serial_target=serial_target))
    profiles = host_profiles(host, chain, config.defaults)
    for p in profiles:
        node.add(_profile_label(p))
    count = len(profiles)
    for serial in host.serials:
        if serial.target is not None:
            count += _add_host(node, serial.target, chain, config, serial_target=True)
    for sub in host.hosts:
        count += _add_host(node, sub, (*chain, host), config)
    return count


def _add_items(parent: Tree, items: tuple[Group | Host, ...], config: Config) -> int:
    count = 0
    for item in items:
        if isinstance(item, Group):
            count += _add_items(parent.add(f"[bold]📁 {escape(item.name)}[/]"), item.children, config)
        else:
            count += _add_host(parent, item, (), config)
    return count


def build_tree(config: Config, source: str) -> tuple[Tree, int]:
    """Returns the tree and the number of profiles it shows."""
    root = Tree(f"[bold]{escape(source)}[/]")
    count = 0
    for lan in config.lans:
        branch = root.add(f"[bold red]🌐 lan {escape(lan.id)}[/]") if lan.id else root
        count += _add_items(branch, lan.children, config)
    return root, count
