"""Rich tree view of the parsed config and what each host generates."""

from __future__ import annotations

from rich.markup import escape
from rich.text import Text
from rich.tree import Tree

from xml_gen_iterm2.model import DEFAULT_PORT, Config, Group, Host, Kind, Profile
from xml_gen_iterm2.render import Chain, Hypervisors, extend, host_profiles, index_hosts, is_away, reachable
from xml_gen_iterm2.where import Detection

_KIND_STYLE: dict[Kind, tuple[str, str]] = {
    Kind.SSH: ("🔑", "green"),
    Kind.CONSOLE: ("🖥 ", "magenta"),
    Kind.SERIAL: ("🔌", "yellow"),
    Kind.WOL: ("⏰", "cyan"),
    Kind.VIRSH: ("📺", "bright_magenta"),
}


def _host_label(host: Host, chain: Chain, hyps: Hypervisors, *, away: bool, serial_target: bool = False) -> str:
    users = ",".join(host.users)
    port = f":{host.port}" if host.port != DEFAULT_PORT else ""
    addrs = " ".join(a for a in (host.ip, host.tailnet and f"[dim](tailnet {host.tailnet})[/dim]") if a)
    label = f"[bold blue]{escape(host.name)}[/] {escape(users)}@{addrs}{port}"
    if chain:
        hops = " → ".join(f"{escape(h.host.name)}{' (tailnet)' if h.via_tailnet else ''}" for h in chain)
        label += f" [dim]via {hops}[/dim]"
        if not reachable(chain, away=away):
            label += " [bold yellow]⚠ unreachable from here?[/]"
    if host.hypervisor and host.hypervisor in hyps:
        label += f" [dim]🖥 on {escape(hyps[host.hypervisor][0].name)} ({escape(host.virsh_domain or '')})[/dim]"
    if serial_target:
        label = f"[yellow]⇄[/] {label}"
    if host.note:
        label += f" [dim italic]# {escape(host.note)}[/]"
    return label


def _profile_label(p: Profile) -> Text:
    icon, style = _KIND_STYLE[p.kind]
    text = Text.assemble(f"{icon} ", (p.name, f"{style} dim" if p.away else style))
    text.append(f"  [{', '.join(p.tags)}]", style="dim")
    if p.away:
        text.append("  (away)", style="dim italic")
    return text


def _add_host(
    parent: Tree,
    host: Host,
    chain: Chain,
    config: Config,
    hyps: Hypervisors,
    *,
    away: bool,
    serial_target: bool = False,
) -> int:
    node = parent.add(_host_label(host, chain, hyps, away=away, serial_target=serial_target))
    profiles = host_profiles(host, chain, config.defaults, away=away, hypervisors=hyps)
    for p in profiles:
        node.add(_profile_label(p))
    count = len(profiles)
    for serial in host.serials:
        if serial.target is not None:
            count += _add_host(node, serial.target, chain, config, hyps, away=away, serial_target=True)
    for sub in host.hosts:
        count += _add_host(node, sub, extend(chain, host, away=away), config, hyps, away=away)
    return count


def _add_items(parent: Tree, items: tuple[Group | Host, ...], config: Config, hyps: Hypervisors, *, away: bool) -> int:
    count = 0
    for item in items:
        if isinstance(item, Group):
            branch = parent.add(f"[bold]📁 {escape(item.name)}[/]")
            count += _add_items(branch, item.children, config, hyps, away=away)
        else:
            count += _add_host(parent, item, (), config, hyps, away=away)
    return count


def build_tree(config: Config, source: str, where: Detection) -> tuple[Tree, int]:
    """Returns the tree and the number of profiles it shows."""
    if where.lan_id:
        here = f"📍 you are in: [bold green]{escape(where.lan_id)}[/] [dim]({escape(where.reason)})[/dim]"
    else:
        here = f"📍 [bold yellow]away from all lans[/] [dim]({escape(where.reason)})[/dim]"
    root = Tree(f"[bold]{escape(source)}[/]  {here}")
    hyps = index_hosts(config, where.lan_id)
    count = 0
    for lan in config.lans:
        away = is_away(lan, where.lan_id)
        branch = root
        if lan.id:
            nets = " ".join(str(n) for n in lan.subnets)
            label = f"[bold red]🌐 lan {escape(lan.id)}[/]" + (f" [dim]{nets}[/dim]" if nets else "")
            if lan.id == where.lan_id:
                label += " [bold green]← here[/]"
            elif away:
                label += " [dim italic](away)[/]"
            branch = root.add(label)
        count += _add_items(branch, lan.children, config, hyps, away=away)
    return root, count
