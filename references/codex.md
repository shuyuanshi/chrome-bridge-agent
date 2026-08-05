# Codex setup

Chrome Bridge uses the open Agent Skills format and its ordinary Python CLI;
it does not require a Codex-specific `SKILL.md` or an MCP conversion.

## Install

Ask Codex's `$skill-installer` to install the full GitHub repository as the
`chrome-bridge-agent` user skill. The full tree is required because a Python
wheel contains the client and relay modules but not the unpacked Chrome
extension. A newly installed skill becomes available to Codex on the next turn.

On macOS, `.codex` is hidden in Finder and in Chrome's **Load unpacked**
picker. Press **Command-Shift-G**, enter
`~/.codex/skills/chrome-bridge-agent/extension`, and choose that directory.
Use the equivalent path under `$CODEX_HOME` if it is customized.

After installation, resolve `ROOT` to the installed skill directory and create
its isolated runtime:

```bash
ROOT="${CODEX_HOME:-$HOME/.codex}/skills/chrome-bridge-agent"

# Preferred when uv is available.
uv sync --project "$ROOT" --frozen --no-dev

# Or, without uv:
python3 -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/python" -m pip install -e "$ROOT"
```

Run one setup method, not both.

Codex can call these entry points directly:

```bash
"$ROOT/.venv/bin/chrome-bridge" status
"$ROOT/.venv/bin/chrome-bridge-server" --no-watch
```

Adding those entry points to `PATH` is optional. Keep Codex-specific UI metadata
in `agents/openai.yaml`; keep the canonical workflow in `SKILL.md` portable for
Claude Code and other Agent Skills runtimes.

The relay speaks the project's own authenticated WebSocket protocol, not MCP.
Do not register its `ws://localhost:9333` URL as a Codex MCP server.
