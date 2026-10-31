"""Lima and Colima VM discovery for `<local lima="true" colima="true">`: ssh profiles for every running VM.

Colima is a Lima instance living in its own LIMA_HOME, so both are found with `limactl list --json`. Each VM gets a
login as its normal user and a direct login as root. Connections are never multiplexed (ControlMaster would silently
ignore `-l root` and reuse the user's session). Root login needs the Lima key authorized for root inside the guest.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from xml_gen_iterm2.log import log
from xml_gen_iterm2.model import Config

COLIMA_HOME = "~/.colima/_lima"
_SSH_OPTS = ("-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "BatchMode=no")


@dataclass(frozen=True)
class LimaVm:
    name: str  # instance name as in `limactl list`
    display: str  # used in the profile name
    flavor: str  # "lima" or "colima"
    ssh_config: Path
    host: str  # Host alias inside ssh_config

    def command(self, *, root: bool) -> str:
        parts = ["/usr/bin/ssh", "-F", str(self.ssh_config), *_SSH_OPTS, *(["-l", "root"] if root else []), self.host]
        return " ".join(shlex.quote(p) for p in parts)


LimaDiscovery = dict[str, tuple[LimaVm, ...]]  # <local> name -> its VMs


def ssh_host(ssh_config: Path) -> str | None:
    """The first `Host` alias in a Lima-generated ssh.config."""
    try:
        for line in ssh_config.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].lower() == "host":
                return parts[1]
    except (OSError, UnicodeDecodeError):
        pass
    return None


def list_instances(lima_home: str | None) -> list[dict[str, object]]:
    """Instances from `limactl list --json` (one JSON object per line); [] (with a warning) when unavailable."""
    env = dict(os.environ)
    env.pop("LIMA_HOME", None)
    if lima_home:
        env["LIMA_HOME"] = str(Path(lima_home).expanduser())
    try:
        res = subprocess.run(["limactl", "list", "--json"], env=env, capture_output=True, text=True, check=True)
        return [d for d in map(json.loads, res.stdout.splitlines()) if isinstance(d, dict)]
    except FileNotFoundError:
        log.warning("lima: limactl not found")
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        log.warning(f"lima: `limactl list` failed: {type(e).__name__}")
    return []


def discover_flavor(flavor: str) -> tuple[LimaVm, ...]:
    colima = flavor == "colima"
    vms = []
    for inst in list_instances(COLIMA_HOME if colima else None):
        name, cfg = inst.get("name"), inst.get("sshConfigFile")
        if not isinstance(name, str) or not isinstance(cfg, str) or inst.get("status") != "Running":
            continue
        if colima and not (name == "colima" or name.startswith("colima-")):
            continue
        host = ssh_host(Path(cfg))
        if host is None:
            log.warning(f"lima: no Host in {cfg}; skipping {name}")
            continue
        display = (name.removeprefix("colima-") if name != "colima" else "default") if colima else name
        vms.append(LimaVm(name, display, flavor, Path(cfg), host))
    return tuple(vms)


def discover_all(config: Config) -> LimaDiscovery:
    out: LimaDiscovery = {}
    for loc in config.locals:
        vms = (*(discover_flavor("lima") if loc.lima else ()), *(discover_flavor("colima") if loc.colima else ()))
        if vms:
            out[loc.name] = vms
    return out
