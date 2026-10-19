# AGENTS.md

Generates iTerm2 Dynamic Profiles (`~/Library/Application Support/iTerm2/DynamicProfiles/nxcp.json`) from a compact
XML file. The format reference is in `README.md`, and `examples/example.xml` exercises every feature.

## Commands
- Run: `./run.sh` (= `uv run xml-gen-iterm2`). Add `--stdout` or `--check` for a dry run, and `--lan ID|none` to fake
  your location.
- Check everything: `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest`.
- After an intentional output change: `UPDATE_GOLDEN=1 uv run pytest`, then review the diff of
  `tests/expected/example.json`.

## Layout (`src/xml_gen_iterm2/`)
- `parse.py`: XML → frozen dataclasses (`model.py`).
  - It's built on expat, to get line numbers for `ConfigError`.
  - Strict: unknown attributes or elements are errors.
- `render.py`: model → `Profile`s → JSON. Covers bastion chains (ProxyCommand), LAN/tailnet resolution, and the
  serial, virsh, WOL and hotkey JSON.
- `where.py`: detects the current `<lan>` from local IPs (psutil).
- `kube.py`: kubeconfig discovery and the overlay files in `~/.kube/iterm2/`.
- `tree.py`: Rich tree output. `cli.py`: Click, config discovery, atomic writes.

## Invariants that are easy to break
- **GUID = uuid5(profile name).** Names must never depend on location or ordering; only commands may vary by
  location.
- **Nested `%h:%p`** in ProxyCommand chains must be escaped as `%%` per level (`_proxy_opt`).
- **Remote scripts** (picocom, virsh) are POSIX `sh` and must run on busybox. Keep them free of single quotes so
  they stay one quoting level.
- **iTerm2 magic numbers** come from its source (`sources/Settings/Profiles/ITAddressBookMgr.h`, `iTermShortcut.m`).
  Verify them there with `gh api`; don't guess.
- **`--stdout` and `--check` must have no side effects.** Kube overlays are written only in the write path.

## Privacy (this repo is public)
- The real config is in Dropbox (`~/Library/CloudStorage/Dropbox/iterm2-profiles.xml`). Never commit it, and never
  copy its IPs or OCIDs into examples or tests; use documentation ranges (192.0.2.0/24 etc.).
- Never print or log kubeconfig contents (they hold credentials); only context names.
