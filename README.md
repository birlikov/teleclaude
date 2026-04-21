# TeleClaude

A Telegram bot that gives you remote access to [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and a project shell — right from your phone.

Point it at any local project directory, start a Claude Code session, run shell commands, manage Git, create PRs, and control Docker Compose — all through Telegram.

## Features

- **Claude Code sessions** — interactive AI coding sessions with conversation context preserved across messages
- **Streaming shell** — run commands with live-updating output and inline Cancel button
- **Git shortcuts** — branches, git commands, push & create GitHub PRs or GitLab MRs
- **Docker Compose** — `up`, `down`, `ps`, `build`, `logs`, `restart`
- **Access control** — restrict to a whitelist of Telegram user IDs

## Requirements

- Python 3.10+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and on `PATH`
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- (Optional) `gh` CLI for GitHub PRs or `glab` CLI for GitLab MRs

## Setup

```bash
git clone https://github.com/<your-username>/teleclaude.git
cd teleclaude

pip install -r requirements.txt

cp .env.example .env
# Edit .env with your bot token and Telegram user ID
```

Run it:

```bash
python bot.py
```

For unattended "always on" operation, see **[Run as a systemd service](#run-as-a-systemd-service)** below.

## Commands

| Command | Description |
|---|---|
| `/workspace <path>` | Set base directory containing projects |
| `/workspace` | Show current workspace |
| `/project` | Pick a project from the workspace (paginated buttons) |
| `/project <path>` | Set/switch to a project directly |
| `/status` | Project, branch, session & command status |
| `/new` | Start a *fresh* Claude Code session |
| `/stop` | Stop the Claude session |
| *(plain text)* | Forwarded to active Claude session |
| `/t <cmd>` | Run a shell command (streaming output) |
| `/cancel` | Kill the running command |
| `/branch <name>` | `git checkout -b <name>` |
| `/git <args>` | Run `git <args>` in project dir |
| `/mr [title]` or `/pr [title]` | Push branch & create PR/MR |
| `/docker <sub>` | Docker Compose: up/down/ps/build/logs/restart |

## How it works

```
bot.py          Telegram handlers, per-chat state, message routing
claude_pty.py   Claude Code session manager (subprocess + stream-json parsing)
runner.py       Async shell runner with live message updates and cancel support
store.py        JSON-backed state: workspace, current project, per-project session ids
```

### Per-project daily memory

Switching projects (via `/project` picker or `/project <path>`) resumes that
project's Claude conversation **only if it was last used today** — otherwise
a fresh session is started.  `/new` always starts fresh, regardless of saved
state.  Session ids are persisted per `(chat, project)` in `state.json`.

### Message rendering

The Telegram forwarder distinguishes three kinds of Claude output:

| Kind | Rendering |
|---|---|
| Assistant prose | plain text |
| Tool calls (`🔧 …`) | inline `<code>` |
| Tool results | `<pre>` block |

So explanations read naturally while commands and their output stay in a
monospaced block.

### Large events

Claude's NDJSON stream can emit single events well over 64 KiB (long tool
results, long assistant messages). The session reader bumps the asyncio
StreamReader limit to 64 MiB and additionally tolerates
`LimitOverrunError` — one pathological line is skipped with a notice rather
than killing the rest of the stream.

## Run as a systemd service

A template unit is provided at `config/teleclaude.service`. It runs as a
**user** service — no `sudo` for daily ops, and it inherits your login's
credentials (which is what the `claude` CLI needs).

1. Copy the template and edit the four marked paths:

   ```bash
   mkdir -p ~/.config/systemd/user
   cp config/teleclaude.service ~/.config/systemd/user/
   ${EDITOR:-nano} ~/.config/systemd/user/teleclaude.service
   ```

   Replace every `/PATH/TO/...` with absolute paths on your machine:
   - `WorkingDirectory` / `EnvironmentFile` — your cloned repo
   - `Environment=PATH=` — directories containing the `claude` CLI and the
     Python interpreter (systemd does **not** inherit your shell PATH)
   - `ExecStart` — full path to the Python interpreter that has
     `python-telegram-bot` and `python-dotenv` installed

2. Enable and start:

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now teleclaude
   ```

3. Keep it alive across logout / reboot:

   ```bash
   sudo loginctl enable-linger "$USER"
   ```

   Without this, user services stop when you log out.

**Managing it:**

| Action | Command |
|---|---|
| Status | `systemctl --user status teleclaude` |
| Tail logs | `journalctl --user -u teleclaude -f` |
| Restart (after code edits) | `systemctl --user restart teleclaude` |
| Stop | `systemctl --user stop teleclaude` |
| Disable auto-start | `systemctl --user disable teleclaude` |

After editing the unit file itself, run
`systemctl --user daemon-reload && systemctl --user restart teleclaude`.

## License

MIT
