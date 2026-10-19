"""Rich-backed logging; everything user-facing goes through here."""

from __future__ import annotations

import logging

from rich.console import Console
from rich.logging import RichHandler

log = logging.getLogger("xml_gen_iterm2")


def setup(console: Console, *, verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=verbose, markup=True)],
        force=True,
    )
