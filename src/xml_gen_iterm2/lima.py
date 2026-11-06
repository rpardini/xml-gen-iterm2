"""Lima and Colima VM discovery for `<local lima="true" colima="true">`: ssh profiles for every VM.

Colima is a Lima instance living in its own LIMA_HOME, so both are found with `limactl list --json`. Each VM gets a
login as its normal user and a direct login as root, built from the JSON (address, port, key, user) rather than Lima's
ssh.config, whose `IdentitiesOnly yes` would hide agent keys from root. Connections are never multiplexed
(ControlMaster would silently ignore the root login and reuse the user's session).
"""

from __future__ import annotations

import getpass
import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from xml_gen_iterm2.log import log
from xml_gen_iterm2.model import Config

COLIMA_HOME = "~/.colima/_lima"
# Host key handling as in Lima's ssh.config (the key changes whenever a VM is recreated); never multiplexed.
_SSH_OPTS = (
    *("-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"),
    *("-o", "NoHostAuthenticationForLocalhost=yes", "-o", "ControlMaster=no", "-o", "ControlPath=none"),
    *("-A", "-X", "-Y"),
)


@dataclass(frozen=True)
class LimaVm:
    name: str  # instance name as in `limactl list`
    display: str  # used in the profile name
    flavor: str  # "lima" or "colima"
    address: str  # sshAddress, usually 127.0.0.1
    port: int  # sshLocalPort
    identity: Path  # Lima's own key, authorized for `user`
    user: str

    def command(self, *, root: bool) -> str:
        # The user only gets Lima's key; root also gets agent keys (eg. from ssh-import-id), so no IdentitiesOnly.
        ident = ["-i", str(self.identity), *([] if root else ["-o", "IdentitiesOnly=yes"])]
        login = "root" if root else self.user
        parts = ["/usr/bin/ssh", *_SSH_OPTS, *ident, "-p", str(self.port), f"{login}@{self.address}"]
        return " ".join(shlex.quote(p) for p in parts)


LimaDiscovery = dict[str, tuple[LimaVm, ...]]  # <local> name -> its VMs


def _user(inst: dict[str, object]) -> str:
    """`config.user.name`; Lima defaults it to the local user name."""
    cfg = inst.get("config")
    user = cfg.get("user") if isinstance(cfg, dict) else None
    name = user.get("name") if isinstance(user, dict) else None
    return name if isinstance(name, str) and name else getpass.getuser()


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
        name = inst.get("name")
        if not isinstance(name, str):
            continue
        if colima and not (name == "colima" or name.startswith("colima-")):
            continue
        # Any status: a stopped (or still starting) VM reports its configured port, so a VM with a fixed ssh port
        # keeps its profiles across reboots. One with a random port (localPort 0, eg. Colima's) has none until it runs.
        address, port, identity = inst.get("sshAddress"), inst.get("sshLocalPort"), inst.get("IdentityFile")
        if not (isinstance(address, str) and isinstance(port, int) and port > 0 and isinstance(identity, str)):
            status = inst.get("status")
            if status == "Running":
                log.warning(f"lima: no ssh address/port/key for {name}; skipping")
            else:
                log.info(f"lima: {name} is {status} and has no fixed ssh port; skipping")
            continue
        display = (name.removeprefix("colima-") if name != "colima" else "default") if colima else name
        vms.append(LimaVm(name, display, flavor, address, port, Path(identity), _user(inst)))
    return tuple(vms)


def discover_all(config: Config) -> LimaDiscovery:
    out: LimaDiscovery = {}
    for loc in config.locals:
        vms = (*(discover_flavor("lima") if loc.lima else ()), *(discover_flavor("colima") if loc.colima else ()))
        if vms:
            out[loc.name] = vms
    return out
