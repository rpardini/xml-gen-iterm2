"""Kubeconfig discovery for `<local kube="...">`: one local-shell profile per context.

kubectl/k9s/helm don't read a KUBECONTEXT variable, so each profile puts a tiny overlay file (only `current-context`)
first in KUBECONFIG: kubectl takes current-context from the first file that sets it. Real kubeconfigs are never written,
and their contents are never logged (they hold credentials).
"""

from __future__ import annotations

import glob
import hashlib
import os
import pwd
import re
import shlex
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import yaml

from xml_gen_iterm2.log import log
from xml_gen_iterm2.model import Config, Local


@dataclass(frozen=True)
class KubeContext:
    name: str  # context name as in the kubeconfig
    file: Path  # kubeconfig it comes from
    display: str  # used in the profile name; disambiguated with the file name when needed
    overlay: Path  # generated file pinning current-context
    shell: str  # login shell to start

    @property
    def command(self) -> str:
        kubeconfig = f"{self.overlay}{os.pathsep}{self.file}"
        parts = ["/usr/bin/env", f"KUBECONTEXT={self.name}", f"KUBECONFIG={kubeconfig}", self.shell, "-l"]
        return " ".join(shlex.quote(p) for p in parts)


KubeDiscovery = dict[str, tuple[KubeContext, ...]]  # <local> name -> its contexts


def login_shell() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_shell or "/bin/zsh"
    except KeyError:
        return "/bin/zsh"


def expand(patterns: tuple[str, ...], base_dir: Path) -> list[Path]:
    """Paths/globs -> existing files, in pattern order (each pattern's matches sorted), deduped."""
    out: dict[Path, None] = {}
    for pattern in patterns:
        p = Path(pattern).expanduser()
        if not p.is_absolute():
            p = base_dir / p
        matches = sorted(Path(m) for m in glob.glob(str(p)))  # noqa: PTH207 -- Path.glob cannot take absolute patterns
        if not matches:
            log.debug(f"kube: {pattern!r} matched nothing")
        for m in matches:
            if m.is_file():
                out.setdefault(m.resolve(), None)
    return list(out)


def read_contexts(path: Path) -> list[str]:
    """Context names in a kubeconfig; [] (with a warning) if unreadable. Never logs file contents."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        where = ""
        if isinstance(e, yaml.MarkedYAMLError) and e.problem_mark is not None:
            where = f" near line {e.problem_mark.line + 1}"
        log.warning(f"kube: skipping {path}: {type(e).__name__}{where}")
        return []
    contexts = doc.get("contexts") if isinstance(doc, dict) else None
    if not isinstance(contexts, list):
        log.warning(f"kube: skipping {path}: no 'contexts' list")
        return []
    return [c["name"] for c in contexts if isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"]]


def overlay_name(file: Path, context: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", context)
    digest = hashlib.sha256(f"{file}\0{context}".encode()).hexdigest()[:8]
    return f"{safe}--{digest}.yaml"


def discover(local: Local, base_dir: Path, overlay_dir: Path, shell: str) -> tuple[KubeContext, ...]:
    found = [(f, name) for f in expand(local.kube, base_dir) for name in read_contexts(f)]
    dupes = {name for name, n in Counter(name for _, name in found).items() if n > 1}
    return tuple(
        KubeContext(
            name=name,
            file=f,
            display=f"{name} ({f.name})" if name in dupes else name,
            overlay=overlay_dir / overlay_name(f, name),
            shell=shell,
        )
        for f, name in found
    )


def discover_all(config: Config, base_dir: Path) -> KubeDiscovery:
    overlay_dir = Path(config.defaults.kube_overlay_dir).expanduser()
    shell = login_shell()
    return {loc.name: discover(loc, base_dir, overlay_dir, shell) for loc in config.locals if loc.kube}


def overlay_yaml(context: str) -> str:
    return yaml.safe_dump({"apiVersion": "v1", "kind": "Config", "current-context": context}, sort_keys=False)


def sync_overlays(discovery: KubeDiscovery, overlay_dir: Path) -> tuple[int, int]:
    """Write every overlay (atomically) and delete stale *.yaml in `overlay_dir`. Returns (written, removed)."""
    wanted = {c.overlay.name: c for contexts in discovery.values() for c in contexts}
    if not wanted and not overlay_dir.is_dir():
        return 0, 0
    overlay_dir.mkdir(parents=True, exist_ok=True)
    for name, ctx in wanted.items():
        fd, tmp = tempfile.mkstemp(dir=overlay_dir, prefix=f".{name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(overlay_yaml(ctx.name))
        Path(tmp).replace(overlay_dir / name)
    stale = [p for p in overlay_dir.glob("*.yaml") if p.name not in wanted]
    for p in stale:
        p.unlink()
    return len(wanted), len(stale)
