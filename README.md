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

Run it (use `tmux` or `screen` to keep it alive):

```bash
python bot.py
```

## Commands

| Command | Description |
|---|---|
| `/project <path>` | Set working project directory |
| `/project` | Show current project |
| `/status` | Project, branch, session & command status |
| `/new` | Start a Claude Code session |
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
```

## License

MIT
