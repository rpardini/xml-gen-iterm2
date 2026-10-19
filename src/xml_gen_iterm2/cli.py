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
from xml_gen_iterm2.log import log
from xml_gen_iterm2.parse import ConfigError, parse_file
from xml_gen_iterm2.render import render, to_json, walk_hosts
from xml_gen_iterm2.tree import build_tree

DEFAULT_OUTPUT = Path("~/Library/Application Support/iTerm2/DynamicProfiles/nxcp.json").expanduser()


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
    default="iterm2-profiles.xml",
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
@click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
def main(config: Path, output: Path, to_stdout: bool, check: bool, no_tree: bool, verbose: bool) -> None:
    """Generate iTerm2 Dynamic Profiles from CONFIG (XML)."""
    console = Console(stderr=to_stdout)
    logmod.setup(console, verbose=verbose)

    try:
        cfg = parse_file(config)
        profiles = render(cfg)
    except ConfigError as e:
        log.error(f"[bold red]{escape(str(config))}[/]: {escape(str(e))}")
        sys.exit(1)

    if not no_tree:
        tree, _ = build_tree(cfg, str(config))
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
