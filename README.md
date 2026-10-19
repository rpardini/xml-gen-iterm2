# xml-gen-iterm2

Generates [iTerm2 Dynamic Profiles](https://iterm2.com/documentation-dynamic-profiles.html) from a compact XML file:
ssh logins (multi-user, tailnet, bastion chains), picocom serial consoles reached through another host, Oracle OCI
instance consoles, and wake-on-LAN.

```sh
uv run xml-gen-iterm2                       # reads ./iterm2-profiles.xml, writes ~/Library/Application Support/iTerm2/DynamicProfiles/nxcp.json
uv run xml-gen-iterm2 my.xml -o out.json    # explicit paths
uv run xml-gen-iterm2 --check               # diff against the existing output, exit 1 if it'd change
uv run xml-gen-iterm2 --stdout --no-tree    # JSON to stdout
```

Every run prints a tree of what was parsed and which profiles each host produces.

## Format

See [`examples/example.xml`](examples/example.xml) for a working example of every feature.

```xml
<profiles>
  <defaults user="root" ssh-opts="..." ssh-auth-sock="~/.gnupg/S.gpg-agent.ssh" wol-cmd="wakeonlan"/>
  <lan id="home">                                 <!-- organizational only -->
    <group name="Home" users="root alice" tags="x">
      <group name="bench" port="2222">             <!-- tag "Home/bench" = iTerm2 submenu -->
        <host name="Board" ip="192.0.2.10" tailnet="100.64.0.10" mac="00:11:22:33:44:55" tags="y">
          <ssh name="Board runner" user="runner" port="22222"/>
          <serial dev="/dev/ttyUSB0" baud="1500000">
            <host name="Other board" ip="192.0.2.11"/>
          </serial>
          <host name="VM behind Board" ip="10.0.0.5"/>
          <oci-console connection="ocid1.instanceconsoleconnection..." instance="ocid1.instance..."/>
        </host>
      </group>
    </group>
  </lan>
</profiles>
```

| Element         | Attributes                                                                          | Generates                                                    |
|-----------------|-------------------------------------------------------------------------------------|--------------------------------------------------------------|
| `<defaults>`    | `user` (root), `ssh-opts`, `ssh-auth-sock` (used by OCI consoles), `wol-cmd`        | nothing                                                      |
| `<lan>`         | `id`                                                                                | nothing; grouping only (shown in the tree)                   |
| `<group>`       | `name`, `user`/`users`, `port`, `tags`                                              | nested names are joined with `/` into the first tag; the other attributes are inherited |
| `<host>`        | `name`, `ip` and/or `tailnet`, `user`/`users`, `port`, `tags`, `mac`, `ipv6`, `note` | one ssh profile per user × address                           |
| `<ssh>`         | `name`, `user`, `port`                                                              | one extra ssh profile per address                            |
| `<serial>`      | `dev`, `baud`, `user`, `args`, `name`                                               | `<target> serial via <host>`: `ssh -t host picocom …`        |
| `<oci-console>` | `connection` + `instance`, or `paste` (the raw string from OCI's web console)       | `<host> CONSOLE` (tag `ociConsole`)                          |

Rules:
- **Users**: with more than one user, the user is appended to the profile name.
- **tailnet**: generates an extra `<name> tailnet` profile, tagged `tailnet`. A host can be tailnet-only.
- **mac**: generates `<name> WOL`, which runs `wakeonlan`, waits for the ssh port, then connects (tag `wol`). This
  only happens for hosts that have a LAN `ip` and aren't behind a bastion.
- **`<host>` inside `<host>`**: the parent is used as a bastion via `ProxyCommand`, and deeper nesting chains the
  bastions. A nested host inherits its enclosing *group's* users, port and tags, not the parent host's.
- **`<serial>`**: this is the console of *another* board, cabled to this host's USB-serial adapter. picocom runs on this
  host. The serial node wraps the target board's `<host>`, which gets its own ssh profiles and can't nest further. A
  board without network uses `name=` instead.
- **GUIDs**: a uuid5 of the profile name, so they stay stable when you reorder entries. Duplicate names are an error.
- `ipv6` and `note` are metadata only. Unknown elements or attributes are errors, reported with line numbers.

## Development

```sh
uv sync
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest
UPDATE_GOLDEN=1 uv run pytest    # regenerate tests/expected/example.json after intentional output changes
```
