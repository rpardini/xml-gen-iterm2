from __future__ import annotations

import pytest

from xml_gen_iterm2.model import Group, Host
from xml_gen_iterm2.parse import ConfigError, parse_bytes


def _xml(body: str) -> bytes:
    return f"<profiles>{body}</profiles>".encode()


def test_group_inheritance_and_tags() -> None:
    cfg = parse_bytes(
        _xml(
            '<group name="A" users="x y" tags="t1"><group name="B" port="2222" tags="t2">'
            '<host name="h" ip="10.0.0.1" tags="t3 t1"/></group></group>'
        )
    )
    group = cfg.lans[0].children[0]
    assert isinstance(group, Group)
    inner = group.children[0]
    assert isinstance(inner, Group)
    assert inner.path == "A/B"
    host = inner.children[0]
    assert isinstance(host, Host)
    assert host.users == ("x", "y")
    assert host.port == 2222
    assert host.tags == ("A/B", "t1", "t2", "t3")


def test_lans_and_loose_entries() -> None:
    cfg = parse_bytes(_xml('<host name="a" ip="1.1.1.1"/><lan id="x"><host name="b" ip="2.2.2.2"/></lan>'))
    assert [lan.id for lan in cfg.lans] == [None, "x"]


def test_nested_host_gets_group_context_not_parent_host() -> None:
    cfg = parse_bytes(
        _xml(
            '<group name="G" user="g"><host name="p" ip="1.1.1.1" user="p" port="2"><host name="c" ip="2.2.2.2"/>'
            "</host></group>"
        )
    )
    group = cfg.lans[0].children[0]
    assert isinstance(group, Group)
    parent = group.children[0]
    assert isinstance(parent, Host)
    child = parent.hosts[0]
    assert (child.users, child.port) == (("g",), 22)


def test_oci_paste_extracts_ocids() -> None:
    cfg = parse_bytes(
        _xml(
            '<host name="o" ip="1.1.1.1"><oci-console paste="ssh -o ProxyCommand=\'ssh -W %h:%p -p 443 '
            "ocid1.instanceconsoleconnection.oc1.eu-amsterdam-1.abc@instance-console.eu-amsterdam-1.oci.oraclecloud.com'"
            ' ocid1.instance.oc1.eu-amsterdam-1.def"/></host>'
        )
    )
    host = cfg.lans[0].children[0]
    assert isinstance(host, Host)
    console = host.consoles[0]
    assert console.region == "eu-amsterdam-1"
    assert console.instance == "ocid1.instance.oc1.eu-amsterdam-1.def"


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ('<host name="h"/>', "needs 'ip' and/or 'tailnet'"),
        ('<host name="h" ip="1" bogus="1"/>', "unknown attribute"),
        ('<host name="h" ip="1"><wat/></host>', "not allowed inside <host>"),
        ('<host name="h" ip="1" user="a" users="b"/>', "either 'user' or 'users'"),
        ('<host name="h" ip="1" port="99999"/>', "out of range"),
        ('<host name="h" ip="1" mac="nope"/>', "bad mac"),
        ('<host name="h" ip="1" wol="yes"/>', "must be 'true' or 'false'"),
        ('<host name="h" ip="1" hotkey="F21"/>', "key must be F1..F20"),
        ('<host name="h" ip="1" hotkey="x"/>', "key must be F1..F20"),
        ('<host name="h" ip="1" hotkey="hyper+F1"/>', "unknown modifier"),
        ('<host name="h" ip="1" style="quake"/>', "'style' without 'hotkey'"),
        ('<host name="h" ip="1" hotkey="F1" style="huge"/>', "must be 'quake' or 'window'"),
        ('<host name="h" ip="1" hotkey-user="root"/>', "'hotkey-user' without 'hotkey'"),
        ('<host name="h" ip="1" hotkey="F1" hotkey-user="bob"/>', "not one of this host's users"),
        ('<lan id="a"><local name="x"/></lan>', "not allowed inside <lan>"),
        ('<local bogus="1"/>', "unknown attribute"),
        ('<host id="a" name="h" ip="1"/><host id="a" name="i" ip="2"/>', "duplicate host id 'a'"),
        ('<host name="h" ip="1" hypervisor="nope" virsh-domain="d"/>', "no host with that id"),
        ('<host id="a" name="h" ip="1" hypervisor="a" virsh-domain="d"/>', "its own hypervisor"),
        ('<host id="a" name="h" ip="1"/><host name="v" ip="2" hypervisor="a"/>', "go together"),
        ('<host name="v" ip="2" virsh-domain="d"/>', "go together"),
        ('<host name="h" ip="1" wol="true"/>', "needs a 'mac'"),
        ('<host name="h" tailnet="100.64.0.1" mac="00:11:22:33:44:55" wol="true"/>', "needs a LAN 'ip'"),
        ('<host name="h" ip="1"><serial dev="/dev/x"/></host>', "'baud'"),
        ('<host name="h" ip="1"><serial dev="/dev/x" baud="fast"/></host>', "not an integer"),
        ('<host name="h" ip="1"><serial dev="/dev/x" baud="9600"/></host>', "needs either a 'name'"),
        (
            '<host name="h" ip="1"><serial dev="/d" baud="9600"><host name="t" ip="2"><host name="x" ip="3"/>'
            "</host></serial></host>",
            "not allowed inside <host>",
        ),
        (
            '<host name="h" ip="1"><serial dev="/d" baud="9600"><host name="t" ip="2"/><host name="u" ip="3"/>'
            "</serial></host>",
            "at most one",
        ),
        ('<host name="h" ip="1"><oci-console connection="x" instance="y"/></host>', "bad console connection"),
        ('<lan id="a"/><lan id="a"/>', "duplicate lan id"),
        ('<lan id="a" subnets="10.0.0.0/33"/>', "bad subnet"),
        ('<lan id="a" subnets="10.0.0.0/24"/><lan id="b" subnets="10.0.0.1/24"/>', "already belongs to lan 'a'"),
        ("<defaults/><defaults/>", "only one <defaults>"),
        ('<host name="h" ip="1">text</host>', "unexpected text"),
    ],
)
def test_errors(body: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        parse_bytes(_xml(body))


def test_error_has_line_number() -> None:
    with pytest.raises(ConfigError, match=r"<host 'h'> \(line 3\)"):
        parse_bytes(b'<profiles>\n<group name="g">\n<host name="h"/>\n</group></profiles>')


def test_bad_root_and_syntax() -> None:
    with pytest.raises(ConfigError, match="root element"):
        parse_bytes(b"<putty/>")
    with pytest.raises(ConfigError, match="XML syntax error"):
        parse_bytes(b"<profiles>")


def test_lan_subnets() -> None:
    cfg = parse_bytes(_xml('<lan id="a" subnets="10.0.0.7/24 fd00::/64 10.0.0.0/24"/>'))
    assert [str(n) for n in cfg.lans[0].subnets] == ["10.0.0.0/24", "fd00::/64"]


@pytest.mark.parametrize(
    ("raw", "code", "char", "mods"),
    [
        ("F1", 122, 0xF704, 0),
        ("F12", 111, 0xF70F, 0),
        ("F13", 105, 0xF710, 0),
        ("F20", 90, 0xF717, 0),
        ("cmd+opt+F5", 96, 0xF708, (1 << 20) | (1 << 19)),
        ("Shift+Ctrl+f2", 120, 0xF705, (1 << 17) | (1 << 18)),
    ],
)
def test_hotkey_encoding(raw: str, code: int, char: int, mods: int) -> None:
    cfg = parse_bytes(_xml(f'<host name="h" ip="1" hotkey="{raw}"/>'))
    host = cfg.lans[0].children[0]
    assert isinstance(host, Host)
    assert host.hotkey is not None
    assert (host.hotkey.key_code, ord(host.hotkey.char), host.hotkey.modifiers) == (code, char, mods)
    assert host.hotkey.style == "quake"


def test_locals() -> None:
    cfg = parse_bytes(_xml('<local/><local name="Work" hotkey="F14" style="window" tags="w"/>'))
    assert [(loc.name, loc.tags) for loc in cfg.locals] == [("Local", ("local",)), ("Work", ("local", "w"))]
    assert cfg.locals[1].hotkey is not None
    assert cfg.locals[1].hotkey.style == "window"
