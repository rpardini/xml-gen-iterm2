from __future__ import annotations

import json
import os
import shlex

import pytest

from xml_gen_iterm2.model import Kind, Profile, Serial
from xml_gen_iterm2.parse import ConfigError, parse_bytes, parse_file
from xml_gen_iterm2.render import bash_c, picocom_script, render, to_json, virsh_script

from conftest import EXAMPLE, EXPECTED

OPTS = '<defaults ssh-opts="-q"/>'


def _render(body: str) -> dict[str, Profile]:
    return {p.name: p for p in render(parse_bytes(f"<profiles>{OPTS}{body}</profiles>".encode()))}


def test_golden_example() -> None:
    generated = to_json(render(parse_file(EXAMPLE), current="home"))
    if os.environ.get("UPDATE_GOLDEN"):
        EXPECTED.write_text(generated, encoding="utf-8")
    assert generated == EXPECTED.read_text(encoding="utf-8")


def test_users_and_tailnet_multiply() -> None:
    p = _render('<host name="h" ip="10.0.0.1" tailnet="100.64.0.1" users="a b"/>')
    assert sorted(p) == ["h a", "h a tailnet", "h b", "h b tailnet"]
    assert p["h b tailnet"].command == "ssh -q b@100.64.0.1"
    assert p["h b tailnet"].tags == ("tailnet",)


def test_wol_only_when_asked() -> None:
    p = _render(
        '<host name="h" ip="10.0.0.1" mac="00:11:22:33:44:55" wol="true" port="2222">'
        '<host name="inner" ip="10.0.0.2" mac="00:11:22:33:44:66"/></host>'
        '<host name="t" tailnet="100.64.0.1" mac="00:11:22:33:44:77"/>'
        '<host name="m" ip="10.0.0.3" mac="00:11:22:33:44:88"/>'
    )
    assert p["h WOL"].kind is Kind.WOL
    assert p["h WOL"].command == (
        'bash -c "wakeonlan 00:11:22:33:44:55; until nc -z 10.0.0.1 2222; do sleep 2; done; '
        'ssh -q -p 2222 root@10.0.0.1"'
    )
    assert "inner WOL" not in p
    assert "t tailnet WOL" not in p
    assert "t WOL" not in p
    assert "m WOL" not in p  # mac without wol="true" is just metadata


def test_wol_behind_bastion_is_an_error() -> None:
    with pytest.raises(ConfigError, match="behind a bastion"):
        _render('<host name="b" ip="1.1.1.1"><host name="i" ip="2.2.2.2" mac="00:11:22:33:44:55" wol="true"/></host>')


def test_bastion_chain_escapes_tokens() -> None:
    p = _render(
        '<host name="a" ip="1.1.1.1" port="2"><host name="b" ip="2.2.2.2"><host name="c" ip="3.3.3.3"/></host></host>'
    )
    assert p["b"].command == "ssh -q -o ProxyCommand='ssh -q -p 2 -W %h:%p root@1.1.1.1' root@2.2.2.2"
    args = shlex.split(p["c"].command)
    outer = args[args.index("-o") + 1].removeprefix("ProxyCommand=")
    assert outer.endswith("-W %h:%p root@2.2.2.2")
    assert "-W %%h:%%p root@1.1.1.1" in outer


def test_serial_target_uses_adapters_chain() -> None:
    p = _render(
        '<host name="bastion" ip="1.1.1.1"><host name="x13s" ip="2.2.2.2" users="pi root">'
        '<serial dev="/dev/serial/by-id/usb with space" baud="115200" args="--quiet">'
        '<host name="rpi" ip="3.3.3.3"/></serial></host></host>'
    )
    proxy = "-o ProxyCommand='ssh -q -W %h:%p root@1.1.1.1'"
    remote = shlex.split(p["rpi serial via x13s"].command)
    assert remote[:-1] == shlex.split(f"ssh -q {proxy} -t pi@2.2.2.2")
    assert "dev='/dev/serial/by-id/usb with space';" in remote[-1]
    assert 'picocom -b 115200 --quiet "$dev"' in remote[-1]
    assert p["rpi serial via x13s"].tags == ("serial",)
    assert p["rpi"].command == f"ssh -q {proxy} root@3.3.3.3"


def test_serial_user_override_and_name() -> None:
    p = _render('<host name="h" ip="1.1.1.1"><serial dev="/dev/ttyUSB1" baud="9600" user="me" name="board"/></host>')
    assert p["board serial via h"].command.startswith("ssh -q -t me@1.1.1.1 'dev=/dev/ttyUSB1; ")


def test_oci_without_auth_sock_is_plain_ssh() -> None:
    p = _render(
        '<host name="o" ip="1.1.1.1"><oci-console connection="ocid1.instanceconsoleconnection.oc1.r-1.c"'
        ' instance="ocid1.instance.oc1.r-1.i"/></host>'
    )
    cmd = p["o CONSOLE"].command
    assert cmd.startswith("ssh -oHostKeyAlgorithms=+ssh-rsa")
    assert "-p 443 -oHostKeyAlgorithms=+ssh-rsa -oPubkeyAcceptedAlgorithms=+ssh-rsa" in cmd
    assert "c@instance-console.r-1.oci.oraclecloud.com" in cmd
    assert p["o CONSOLE"].tags == ("ociConsole",)


def test_duplicate_names_rejected() -> None:
    with pytest.raises(ConfigError, match="duplicate profile name"):
        _render('<host name="h" ip="1.1.1.1"/><host name="h" ip="2.2.2.2"/>')


def test_guid_is_stable_uuid_of_name() -> None:
    a = Profile("x", "c1", (), Kind.SSH)
    b = Profile("x", "c2", ("t",), Kind.SERIAL)
    assert a.guid == b.guid
    assert a.guid != Profile("y", "c1", (), Kind.SSH).guid


def test_bash_c_quoting() -> None:
    assert bash_c("echo hi; ls") == 'bash -c "echo hi; ls"'
    assert bash_c('echo "$HOME"') == "bash -c 'echo \"$HOME\"'"


LANS = (
    '<lan id="home" subnets="10.0.0.0/24"><host name="h" ip="10.0.0.5"/></lan>'
    '<lan id="beach" subnets="10.9.0.0/24">'
    '<host name="gw" ip="10.9.0.1" tailnet="100.64.0.1">'
    '<host name="router" ip="10.9.0.2"><host name="nas" ip="10.9.0.3" tailnet="100.64.0.3"/></host>'
    '<serial dev="/dev/ttyUSB0" baud="115200" name="board"/>'
    "</host>"
    '<host name="tv" ip="10.9.0.4" mac="00:11:22:33:44:55" wol="true"/>'
    "</lan>"
)


def _render_at(current: str | None) -> dict[str, Profile]:
    cfg = parse_bytes(f"<profiles>{OPTS}{LANS}</profiles>".encode())
    return {p.name: p for p in render(cfg, current)}


def test_names_do_not_depend_on_location() -> None:
    assert list(_render_at("home")) == list(_render_at("beach")) == list(_render_at(None))


def test_at_home_lan_uses_lan_addresses() -> None:
    p = _render_at("beach")
    assert p["router"].command == "ssh -q -o ProxyCommand='ssh -q -W %h:%p root@10.9.0.1' root@10.9.0.2"
    assert p["board serial via gw"].command.startswith("ssh -q -t root@10.9.0.1 'dev=/dev/ttyUSB0; ")
    assert [n for n, x in p.items() if x.away] == ["h"]  # only the other lan's host


def test_away_bastion_goes_through_tailnet() -> None:
    p = _render_at("home")
    assert p["router"].command == "ssh -q -o ProxyCommand='ssh -q -W %h:%p root@100.64.0.1' root@10.9.0.2"
    assert not p["router"].away
    # deeper hop still chains through the tailnet-reached gateway
    assert "root@100.64.0.1" in p["nas"].command
    assert p["board serial via gw"].command.startswith("ssh -q -t root@100.64.0.1 'dev=/dev/ttyUSB0; ")
    assert not p["board serial via gw"].away
    # direct LAN-ip profiles are kept, but flagged
    assert p["gw"].command == "ssh -q root@10.9.0.1"
    assert p["gw"].away
    assert p["tv"].away
    assert p["tv WOL"].away
    assert not p["gw tailnet"].away
    assert not p["h"].away


def test_nested_tailnet_profile_is_direct() -> None:
    for current in ("home", "beach"):
        assert _render_at(current)["nas tailnet"].command == "ssh -q root@100.64.0.3"


def test_away_from_everything() -> None:
    p = _render_at(None)
    assert p["h"].away
    assert "root@100.64.0.1" in p["router"].command


def test_lan_without_subnets_is_never_away() -> None:
    cfg = parse_bytes(f'<profiles>{OPTS}<lan id="cloud"><host name="vps" ip="203.0.113.1"/></lan></profiles>'.encode())
    assert not render(cfg, None)[0].away


def test_serial_profiles_close_on_end() -> None:
    profiles = list(_render('<host name="h" ip="1.1.1.1"><serial dev="/dev/x" baud="9600" name="b"/></host>').values())
    by_name = {p["Name"]: p for p in json.loads(to_json(profiles))["Profiles"]}
    assert by_name["b serial via h"]["Close Sessions On End"] == 1
    assert "Close Sessions On End" not in by_name["h"]


def test_picocom_script_refuses_busy_port_and_pauses_on_failure() -> None:
    script = picocom_script(Serial(dev="/dev/ttyUSB0", baud=1500000, args="--quiet"))
    assert 'pids=$(fuser "$dev" 2>/dev/null)' in script
    assert 'picocom -b 1500000 --quiet "$dev" && exit' in script
    assert script.endswith("read -r _")
    assert "'" not in script  # stays a single, readable level of quoting in the iTerm2 command


def test_virsh_console_runs_on_hypervisor() -> None:
    p = _render(
        '<host id="hv" name="hv" ip="10.0.0.1" users="admin root"/>'
        '<host name="vm" ip="10.0.0.9" users="root human" hypervisor="hv" virsh-domain="vmaster.fritz.box"/>'
    )
    console = p["vm console via hv"]
    assert console.kind is Kind.VIRSH
    assert console.tags == ("virsh",)
    args = shlex.split(console.command)
    assert args[:-1] == ["ssh", "-q", "-t", "admin@10.0.0.1"]
    assert args[-1] == virsh_script("vmaster.fritz.box")
    assert [n for n in p if "console" in n] == ["vm console via hv"]  # one per VM, not per user
    assert p["vm root"].command == "ssh -q root@10.0.0.9"  # VM ssh stays direct
    doc = {x["Name"]: x for x in json.loads(to_json(list(p.values())))["Profiles"]}
    assert doc["vm console via hv"]["Close Sessions On End"] == 1


def test_virsh_hypervisor_reached_like_any_host() -> None:
    lans = (
        '<lan id="home" subnets="10.0.0.0/24"/>'
        '<lan id="beach" subnets="10.9.0.0/24">'
        '<host name="gw" ip="10.9.0.1" tailnet="100.64.0.1"><host id="hv" name="hv" ip="10.9.0.2"/></host>'
        '<host name="vm" ip="10.9.0.9" hypervisor="hv" virsh-domain="d"/>'
        "</lan>"
    )
    cfg = parse_bytes(f"<profiles>{OPTS}{lans}</profiles>".encode())
    at_home = {x.name: x for x in render(cfg, "home")}["vm console via hv"]
    assert "-o ProxyCommand='ssh -q -W %h:%p root@100.64.0.1' -t root@10.9.0.2" in at_home.command
    assert not at_home.away
    at_beach = {x.name: x for x in render(cfg, "beach")}["vm console via hv"]
    assert "root@10.9.0.1' -t root@10.9.0.2" in at_beach.command


def test_virsh_script() -> None:
    assert (
        virsh_script("a b")
        == "dom='a b'; virsh console \"$dom\" && exit; " + 'printf "\\nPress Enter to close... "; read -r _'
    )
