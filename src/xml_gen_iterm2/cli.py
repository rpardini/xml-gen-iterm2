"""Command line entry point."""

from __future__ import annotations

import difflib
import os
import sys
import tempfile
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.syntax import Syntax

from xml_gen_iterm2 import log as logmod
from xml_gen_iterm2.kube import discover_all, sync_overlays
from xml_gen_iterm2.lima import discover_all as discover_lima
from xml_gen_iterm2.lima import ssh_config as lima_ssh_config
from xml_gen_iterm2.log import log
from xml_gen_iterm2.model import Config
from xml_gen_iterm2.parse import ConfigError, parse_file
from xml_gen_iterm2.render import render, to_json, walk_hosts
from xml_gen_iterm2.tree import build_tree
from xml_gen_iterm2.where import Detection, detect_lan, local_addresses

CONFIG_NAME = "iterm2-profiles.xml"
DEFAULT_OUTPUT = Path("~/Library/Application Support/iTerm2/DynamicProfiles/nxcp.json").expanduser()


def default_configs() -> list[tuple[Path, str]]:
    """Candidate configs in priority order, as (path, where it is). Evaluated at call time (HOME may change)."""
    home = Path.home()
    return [
        (home / "Library/CloudStorage/Dropbox" / CONFIG_NAME, "Dropbox"),
        (home / "Dropbox" / CONFIG_NAME, "Dropbox"),
        (Path.cwd() / CONFIG_NAME, "current directory"),
    ]


def find_config() -> tuple[Path, str] | None:
    """First existing default config. Warns about a lower-priority copy that differs from the one used."""
    existing = [(p, origin) for p, origin in default_configs() if p.is_file()]
    if not existing:
        return None
    chosen, origin = existing[0]
    for other, other_origin in existing[1:]:
        if other.resolve() != chosen.resolve() and other.read_bytes() != chosen.read_bytes():
            log.warning(f"ignoring {_tilde(other)} ({other_origin}): it differs from {_tilde(chosen)} ({origin})")
    return chosen, origin


def _tilde(path: Path) -> str:
    """Absolute path with $HOME shown as ~."""
    absolute = path.expanduser().absolute()
    try:
        return f"~/{absolute.relative_to(Path.home())}"
    except ValueError:
        return str(absolute)


def _where(cfg: Config, lan_override: str | None) -> Detection:
    if lan_override is None:
        return detect_lan(cfg, local_addresses())
    if lan_override == "none":
        return Detection(None, "forced via --lan none")
    ids = [lan.id for lan in cfg.lans if lan.id]
    if lan_override not in ids:
        raise ConfigError(f"--lan {lan_override!r}: no such lan; known: {', '.join(ids) or 'none'}")
    return Detection(lan_override, "forced via --lan")


def _write_atomic(path: Path, content: str) -> None:
    """iTerm2 watches the DynamicProfiles dir; never let it see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument(
    "config",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=False,
)
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=DEFAULT_OUTPUT,
    show_default=True,
    help="Dynamic Profiles JSON to write.",
)
@click.option(
    "--stdout", "to_stdout", is_flag=True, help="Print JSON to stdout instead of writing; tree/logs on stderr."
)
@click.option("--check", is_flag=True, help="Don't write; diff against OUTPUT and exit 1 if it would change.")
@click.option("--no-tree", is_flag=True, help="Don't print the tree.")
@click.option(
    "--lan",
    "lan_override",
    metavar="ID|none",
    help="Pretend we're in this lan (or in none of them) instead of detecting it from local addresses.",
)
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
def main(
    config: Path | None,
    output: Path,
    to_stdout: bool,
    check: bool,
    no_tree: bool,
    lan_override: str | None,
    verbose: bool,
) -> None:
    """Generate iTerm2 Dynamic Profiles from CONFIG (XML).

    Without CONFIG, the first existing of: Dropbox (~/Library/CloudStorage/Dropbox or ~/Dropbox), then the current
    directory, each as iterm2-profiles.xml.
    """
    console = Console(stderr=to_stdout)
    logmod.setup(console, verbose=verbose)

    if config is None:
        found = find_config()
        if found is None:
            tried = ", ".join(_tilde(p) for p, _ in default_configs())
            log.error(f"no config given and none found; tried: {escape(tried)}")
            sys.exit(1)
        config, origin = found
    else:
        origin = "given on command line"
    log.info(f"reading [bold]{escape(_tilde(config))}[/] ({origin})")

    try:
        cfg = parse_file(config)
        where = _where(cfg, lan_override)
        kube = discover_all(cfg, config.resolve().parent)
        lima = discover_lima(cfg)
        profiles = render(cfg, where.lan_id, kube, lima)
    except ConfigError as e:
        log.error(f"[bold red]{escape(str(config))}[/]: {escape(str(e))}")
        sys.exit(1)

    if not no_tree:
        tree, _ = build_tree(cfg, _tilde(config), where, kube, lima)
        console.print(tree)
    log.info(f"{len(profiles)} profiles from {sum(1 for _ in walk_hosts(cfg))} hosts")

    content = to_json(profiles)
    if to_stdout:
        sys.stdout.write(content)
        return

    if check:
        old = output.read_text(encoding="utf-8") if output.exists() else ""
        if old == content:
            log.info(f"{output} is up to date")
            return
        diff = "".join(difflib.unified_diff(old.splitlines(True), content.splitlines(True), str(output), "generated"))
        console.print(Syntax(diff, "diff", theme="ansi_dark", background_color="default"))
        log.warning(f"{output} would change")
        sys.exit(1)

    _write_atomic(output, content)
    log.info(f"wrote [bold]{output}[/]")
    if any(loc.lima or loc.colima for loc in cfg.locals):
        ssh_config = Path(cfg.defaults.lima_ssh_config).expanduser()
        _write_atomic(ssh_config, lima_ssh_config(lima))
        log.info(f"wrote [bold]{_tilde(ssh_config)}[/] (Include it from ~/.ssh/config)")
    overlay_dir = Path(cfg.defaults.kube_overlay_dir).expanduser()
    written, removed = sync_overlays(kube, overlay_dir)
    if written or removed:
        files = len({c.file for contexts in kube.values() for c in contexts})
        log.info(f"{written} kube contexts from {files} files; overlays in {overlay_dir} ({removed} stale removed)")
