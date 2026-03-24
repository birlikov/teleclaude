"""
bot.py — TeleClaude: Claude Code via Telegram.

Commands
--------
  /project [path]        set (or show) working project directory
  /status                current project + session overview

  /new                   start a Claude Code session inside the project dir
  /stop                  stop the Claude session
  (plain text)           forwarded to the active Claude session

  /t <cmd>               run any shell command in the project dir (streaming)
  /terminal <cmd>        alias for /t
  /cancel                kill the currently running command

  /branch <name>         git checkout -b <name>
  /git <args>            run `git <args>` in project dir
  /mr [title]            push current branch + create MR/PR (GitHub or GitLab)
  /pr [title]            alias for /mr

  /docker up             docker compose up -d
  /docker down           docker compose down
  /docker ps             docker compose ps
  /docker build          docker compose build
  /docker logs [svc]     docker compose logs --tail 100 [svc]
  /docker restart [svc]  docker compose restart [svc]
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import shlex
from dataclasses import dataclass

from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from claude_pty import ClaudeSession
from runner import stream_run

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

_raw_ids = os.getenv("ALLOWED_USER_IDS", "").strip()
ALLOWED: set[int] = (
    {int(x) for x in _raw_ids.split(",") if x.strip()} if _raw_ids else set()
)

MAX_TG   = 3800   # Telegram message char limit (with headroom)
IDLE_SEC = 2.5    # seconds of Claude silence → flush buffer

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-chat state
# ---------------------------------------------------------------------------

@dataclass
class ChatState:
    chat_id     : int
    project     : str | None = None                          # abs path to project
    claude_sess : ClaudeSession | None = None
    claude_task : asyncio.Task | None  = None
    runner_proc : asyncio.subprocess.Process | None = None   # cancellable cmd

_states: dict[int, ChatState] = {}


def get_state(chat_id: int) -> ChatState:
    if chat_id not in _states:
        _states[chat_id] = ChatState(chat_id=chat_id)
    return _states[chat_id]


def allowed(uid: int) -> bool:
    return not ALLOWED or uid in ALLOWED

# ---------------------------------------------------------------------------
# Claude session helpers
# ---------------------------------------------------------------------------

async def _kill_claude(state: ChatState) -> None:
    if state.claude_task:
        state.claude_task.cancel()
        try:
            await state.claude_task
        except asyncio.CancelledError:
            pass
        state.claude_task = None
    if state.claude_sess:
        state.claude_sess.stop()
        state.claude_sess = None


async def _forward_claude(state: ChatState, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Background task: drain session.queue and relay content to Telegram.
    While Claude is streaming output, keep editing a single "live" message.
    After IDLE_SEC seconds of silence, finalise and reset — Claude is waiting
    for input.
    """
    session = state.claude_sess
    chat_id = state.chat_id
    bot     = context.bot

    from telegram.error import BadRequest

    live = None
    buf  = ""

    async def flush():
        nonlocal live, buf
        text = buf.strip()
        buf  = ""
        if not text:
            return
        while text:
            piece   = text[:MAX_TG]
            text    = text[MAX_TG:]
            escaped = html.escape(piece)
            tg_text = f"<pre>{escaped}</pre>"
            if live is None:
                live = await bot.send_message(chat_id, tg_text, parse_mode="HTML")
            else:
                try:
                    await live.edit_text(tg_text, parse_mode="HTML")
                except BadRequest:
                    live = await bot.send_message(chat_id, tg_text, parse_mode="HTML")
            if text:
                live = None   # next piece needs a new message

    try:
        while True:
            try:
                chunk = await asyncio.wait_for(session.queue.get(), timeout=IDLE_SEC)
            except asyncio.TimeoutError:
                await flush()
                live = None   # next burst → fresh message
                continue

            if chunk is None:           # sentinel: session ended / process exited
                await flush()
                await bot.send_message(
                    chat_id,
                    "<i>Claude session ended.  /new to start again.</i>",
                    parse_mode="HTML",
                )
                state.claude_sess = None
                state.claude_task = None
                return

            buf += chunk
            if len(buf) >= MAX_TG:
                await flush()
                live = None

    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("_forward_claude crashed for chat %s", chat_id)


# ---------------------------------------------------------------------------
# Shared runner helper (thin wrapper so every handler looks the same)
# ---------------------------------------------------------------------------

async def _run(cmd: str, state: ChatState, update) -> None:
    """Run *cmd* in the project dir, streaming output back to *update*."""
    if state.runner_proc:
        await update.message.reply_text(
            "A command is already running.  Use /cancel first."
        )
        return
    await stream_run(
        cmd      = cmd,
        cwd      = state.project,
        send_msg = lambda t, **kw: update.message.reply_text(t, **kw),
        edit_msg = lambda m, t, **kw: m.edit_text(t, **kw),
        chat_id  = state.chat_id,
        store_proc = lambda p: setattr(state, "runner_proc", p),
    )


# ---------------------------------------------------------------------------
# /start  /help
# ---------------------------------------------------------------------------

HELP = (
    "<b>TeleClaude</b>\n\n"
    "<b>Project</b>\n"
    "  /project &lt;path&gt;       — set working directory\n"
    "  /project              — show current project\n"
    "  /status               — project + session overview\n\n"
    "<b>Claude Code</b>\n"
    "  /new                  — start Claude session in project dir\n"
    "  /stop                 — stop Claude session\n"
    "  (just type)           — send message to active Claude session\n\n"
    "<b>Terminal</b>\n"
    "  /t &lt;cmd&gt;              — run any shell command (streaming output)\n"
    "  /cancel               — cancel running command\n\n"
    "<b>Git</b>\n"
    "  /branch &lt;name&gt;        — git checkout -b &lt;name&gt;\n"
    "  /git &lt;args&gt;           — run git &lt;args&gt; in project\n"
    "  /mr [title]           — push + create MR/PR\n\n"
    "<b>Docker</b>\n"
    "  /docker up            — docker compose up -d\n"
    "  /docker down          — docker compose down\n"
    "  /docker ps            — docker compose ps\n"
    "  /docker build         — docker compose build\n"
    "  /docker logs [svc]    — last 100 lines of logs\n"
    "  /docker restart [svc] — restart service(s)\n"
)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return
    await update.message.reply_text(HELP, parse_mode="HTML")


# ---------------------------------------------------------------------------
# /project
# ---------------------------------------------------------------------------

async def cmd_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    state   = get_state(chat_id)

    if not ctx.args:
        if state.project:
            await update.message.reply_text(
                f"Current project:\n<code>{html.escape(state.project)}</code>",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                "No project set.  Example:\n<code>/project ~/code/my-app</code>",
                parse_mode="HTML",
            )
        return

    path = os.path.expanduser(" ".join(ctx.args))
    path = os.path.abspath(path)

    if not os.path.isdir(path):
        await update.message.reply_text(
            f"Directory not found:\n<code>{html.escape(path)}</code>",
            parse_mode="HTML",
        )
        return

    state.project = path

    # Pull a bit of context to confirm it's a repo
    proc = await asyncio.create_subprocess_shell(
        "git remote get-url origin 2>/dev/null; echo '---'; git branch --show-current 2>/dev/null",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=path,
    )
    out, _ = await proc.communicate()
    info   = out.decode().strip()

    msg = f"✅ Project set to:\n<code>{html.escape(path)}</code>"
    if info and info != "---":
        msg += f"\n\n<pre>{html.escape(info)}</pre>"

    await update.message.reply_text(msg, parse_mode="HTML")


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return
    state = get_state(update.effective_chat.id)
    proj  = f"<code>{html.escape(state.project)}</code>" if state.project else "<i>not set</i>"
    sess  = "✅ active" if (state.claude_sess and state.claude_sess.alive) else "❌ none"
    cmd   = "⚙ running" if state.runner_proc else "—"

    lines = [
        f"<b>Project:</b>  {proj}",
        f"<b>Claude:</b>   {sess}",
        f"<b>Command:</b>  {cmd}",
    ]

    # Also show git branch if project is set
    if state.project:
        p = await asyncio.create_subprocess_shell(
            "git branch --show-current 2>/dev/null",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=state.project,
        )
        o, _ = await p.communicate()
        branch = o.decode().strip()
        if branch:
            lines.append(f"<b>Branch:</b>   <code>{html.escape(branch)}</code>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# /new  /stop
# ---------------------------------------------------------------------------

async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    state   = get_state(chat_id)

    await _kill_claude(state)

    sess = ClaudeSession(cwd=state.project)
    sess.start()
    state.claude_sess = sess
    state.claude_task = asyncio.create_task(_forward_claude(state, ctx))

    note = f" in <code>{html.escape(state.project)}</code>" if state.project else ""
    await update.message.reply_text(
        f"Claude session ready{note}.  Send your task.",
        parse_mode="HTML",
    )


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return
    state = get_state(update.effective_chat.id)
    if not state.claude_sess:
        await update.message.reply_text("No active Claude session.")
        return
    await _kill_claude(state)
    await update.message.reply_text("Claude session stopped.")


# ---------------------------------------------------------------------------
# /t  /terminal  — arbitrary shell command
# ---------------------------------------------------------------------------

async def cmd_terminal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text(
            "Usage: <code>/t &lt;command&gt;</code>", parse_mode="HTML"
        )
        return
    state = get_state(update.effective_chat.id)
    await _run(" ".join(ctx.args), state, update)


# ---------------------------------------------------------------------------
# /cancel
# ---------------------------------------------------------------------------

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return
    state = get_state(update.effective_chat.id)
    if state.runner_proc:
        state.runner_proc.terminate()
        await update.message.reply_text("Cancelled.")
    else:
        await update.message.reply_text("Nothing is running.")


async def on_cancel_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline ✖ Cancel button callback."""
    query   = update.callback_query
    chat_id = int(query.data.split(":")[1])
    state   = get_state(chat_id)
    if state.runner_proc:
        state.runner_proc.terminate()
        await query.answer("Cancelled.")
    else:
        await query.answer("Nothing to cancel.")
    try:
        await query.message.edit_reply_markup(None)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# /branch
# ---------------------------------------------------------------------------

async def cmd_branch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text(
            "Usage: <code>/branch &lt;name&gt;</code>", parse_mode="HTML"
        )
        return
    state = get_state(update.effective_chat.id)
    name  = ctx.args[0]
    await _run(f"git checkout -b {shlex.quote(name)}", state, update)


# ---------------------------------------------------------------------------
# /git
# ---------------------------------------------------------------------------

async def cmd_git(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text(
            "Usage: <code>/git &lt;subcommand&gt;</code>", parse_mode="HTML"
        )
        return
    state = get_state(update.effective_chat.id)
    sub   = ctx.args[0]

    # Convenience expansions
    aliases = {"s": "status", "st": "status", "co": "checkout"}
    sub = aliases.get(sub, sub)

    if sub == "log" and len(ctx.args) == 1:
        cmd = "git log --oneline -20"
    else:
        cmd = "git " + shlex.join([sub] + list(ctx.args[1:]))

    await _run(cmd, state, update)


# ---------------------------------------------------------------------------
# /docker
# ---------------------------------------------------------------------------

_DOCKER_SIMPLE = {
    "up"   : "docker compose up -d",
    "down" : "docker compose down",
    "ps"   : "docker compose ps",
    "build": "docker compose build",
}


async def cmd_docker(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text(
            "Usage: /docker &lt;up|down|ps|build|logs [svc]|restart [svc]&gt;",
            parse_mode="HTML",
        )
        return

    state = get_state(update.effective_chat.id)
    sub   = ctx.args[0].lower()
    rest  = ctx.args[1:]

    if sub in _DOCKER_SIMPLE:
        cmd = _DOCKER_SIMPLE[sub]
    elif sub == "logs":
        svc = (" " + shlex.quote(rest[0])) if rest else ""
        cmd = f"docker compose logs --tail=100{svc}"
    elif sub == "restart":
        svc = (" " + shlex.quote(rest[0])) if rest else ""
        cmd = f"docker compose restart{svc}"
    else:
        # pass through unknown subcommands as-is
        cmd = "docker compose " + shlex.join(ctx.args)

    await _run(cmd, state, update)


# ---------------------------------------------------------------------------
# /mr  /pr  — create Merge Request / Pull Request
# ---------------------------------------------------------------------------

async def cmd_mr(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    state   = get_state(chat_id)

    if not state.project:
        await update.message.reply_text(
            "Set a project first: <code>/project /path/to/repo</code>",
            parse_mode="HTML",
        )
        return

    title = " ".join(ctx.args) if ctx.args else ""

    # Detect platform from remote URL
    p = await asyncio.create_subprocess_shell(
        "git remote get-url origin",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=state.project,
    )
    out, _ = await p.communicate()
    remote = out.decode().strip()

    if "github.com" in remote:
        if title:
            mr_cmd = f'gh pr create --title {shlex.quote(title)} --body ""'
        else:
            mr_cmd = "gh pr create --fill"
        platform = "GitHub PR"
    elif "gitlab" in remote:
        if title:
            mr_cmd = (
                f'glab mr create --title {shlex.quote(title)} '
                f'--description "" --push --yes'
            )
        else:
            mr_cmd = "glab mr create --push --yes"
        platform = "GitLab MR"
    else:
        await update.message.reply_text(
            f"Cannot detect platform from remote:\n<code>{html.escape(remote)}</code>\n\n"
            "Use <code>/t gh pr create ...</code> or <code>/t glab mr create ...</code> manually.",
            parse_mode="HTML",
        )
        return

    await update.message.reply_text(
        f"Pushing branch and creating {platform}…", parse_mode="HTML"
    )

    # Push first
    push = await asyncio.create_subprocess_shell(
        "git push -u origin HEAD",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=state.project,
    )
    push_out, _ = await asyncio.wait_for(push.communicate(), timeout=60)
    push_text   = push_out.decode().strip()

    if push.returncode != 0:
        await update.message.reply_text(
            f"<b>git push failed:</b>\n<pre>{html.escape(push_text)}</pre>",
            parse_mode="HTML",
        )
        return

    # Create MR/PR
    await _run(mr_cmd, state, update)


# ---------------------------------------------------------------------------
# Plain text → Claude session
# ---------------------------------------------------------------------------

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return
    state = get_state(update.effective_chat.id)
    if not state.claude_sess or not state.claude_sess.alive:
        await update.message.reply_text(
            "No active Claude session.  Use /new to start one."
        )
        return
    await state.claude_sess.send(update.message.text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _set_commands(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("project", "Set or show working project directory"),
        BotCommand("status",  "Project, branch & session overview"),
        BotCommand("new",     "Start Claude Code session in project dir"),
        BotCommand("stop",    "Stop Claude session"),
        BotCommand("t",       "Run any shell command (streaming output)"),
        BotCommand("cancel",  "Cancel the running command"),
        BotCommand("branch",  "git checkout -b <name>"),
        BotCommand("git",     "Run git <args> in project dir"),
        BotCommand("mr",      "Push branch + create MR/PR"),
        BotCommand("docker",  "Docker compose: up/down/ps/logs/build/restart"),
        BotCommand("help",    "Show all commands"),
    ])


def main() -> None:
    app = Application.builder().token(TOKEN).post_init(_set_commands).build()

    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("project",         cmd_project))
    app.add_handler(CommandHandler("status",          cmd_status))
    app.add_handler(CommandHandler("new",             cmd_new))
    app.add_handler(CommandHandler("stop",            cmd_stop))
    app.add_handler(CommandHandler(["t", "terminal"], cmd_terminal))
    app.add_handler(CommandHandler("cancel",          cmd_cancel))
    app.add_handler(CommandHandler("branch",          cmd_branch))
    app.add_handler(CommandHandler("git",             cmd_git))
    app.add_handler(CommandHandler("docker",          cmd_docker))
    app.add_handler(CommandHandler(["mr", "pr"],      cmd_mr))

    app.add_handler(CallbackQueryHandler(on_cancel_button, pattern=r"^cancel:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("TeleClaude bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
