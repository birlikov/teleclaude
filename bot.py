"""
bot.py — TeleClaude: Claude Code via Telegram.

Commands
--------
  /workspace [path]      set (or show) base directory containing projects
  /project               pick a project from the workspace (paginated buttons)
  /project <path>        set (or switch to) a project directory explicitly
  /status                current project + session overview

  /new                   start a *fresh* Claude Code session in the project dir
  /stop                  stop the Claude session
  (plain text)           forwarded to the active Claude session
                         (auto-resumes today's per-project memory if idle)

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
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import store
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

MAX_TG      = 3800   # Telegram message char limit (with headroom)
IDLE_SEC    = 2.5    # seconds of Claude silence → flush buffer
PAGE_SIZE   = 8      # project picker: buttons per page

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
    workspace   : str | None = None                          # abs path to base dir
    project     : str | None = None                          # abs path to project
    claude_sess : ClaudeSession | None = None
    claude_task : asyncio.Task | None  = None
    runner_proc : asyncio.subprocess.Process | None = None   # cancellable cmd

_states: dict[int, ChatState] = {}


def get_state(chat_id: int) -> ChatState:
    if chat_id not in _states:
        s = ChatState(chat_id=chat_id)
        # Rehydrate persisted workspace + project so bot restarts are transparent
        s.workspace = store.get_workspace(chat_id)
        s.project   = store.get_current_project(chat_id)
        _states[chat_id] = s
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


def _fmt_chunk(kind: str, content: str) -> str:
    """Render one tagged Claude chunk to a self-contained HTML fragment."""
    if kind == "text":
        return html.escape(content) + "\n\n"
    if kind == "tool":
        return f"🔧 <code>{html.escape(content)}</code>\n"
    if kind == "result":
        return f"<pre>{html.escape(content)}</pre>\n"
    if kind == "notice":
        return f"<i>{html.escape(content)}</i>\n"
    return html.escape(content) + "\n"


async def _forward_claude(state: ChatState, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Background task: drain session.queue and relay content to Telegram.

    Each queue item is a (kind, content) tuple — 'text' renders as plain
    Telegram text; 'tool', 'result', and 'notice' render as code/italic
    blocks. Assistant prose and tool output live side-by-side in the same
    message, with live editing while Claude is streaming and a flush after
    IDLE_SEC of silence.
    """
    session = state.claude_sess
    chat_id = state.chat_id
    bot     = context.bot

    from telegram.error import BadRequest

    live = None          # current Telegram Message being edited (or None)
    live_html = ""       # HTML currently displayed in `live`
    pending: list[str] = []   # HTML fragments not yet flushed/edited in

    async def _send(text: str):
        return await bot.send_message(chat_id, text, parse_mode="HTML")

    async def _edit_or_send(msg, text: str):
        try:
            await msg.edit_text(text, parse_mode="HTML")
            return msg
        except BadRequest:
            return await _send(text)

    async def refresh():
        """Show everything in `pending` — edit `live` if it still fits, else split at fragment boundaries."""
        nonlocal live, live_html, pending
        if not pending:
            return

        combined = live_html + "".join(pending)
        # Simple case: everything fits into the current live message.
        if len(combined) <= MAX_TG:
            if live is None:
                live = await _send(combined)
            else:
                live = await _edit_or_send(live, combined)
            live_html = combined
            pending = []
            return

        # Overflow: pack fragments into MAX_TG-sized groups, starting with
        # whatever is already shown in `live` (we can't un-send those bytes,
        # so they anchor the first group even if it means the first group
        # stays larger than strictly needed).
        groups: list[str] = []
        cur = live_html
        for frag in pending:
            if cur and len(cur) + len(frag) > MAX_TG:
                groups.append(cur)
                cur = frag
            else:
                cur += frag
        if cur:
            groups.append(cur)

        # First group: edit/send into `live`. Subsequent groups: new messages.
        for i, g in enumerate(groups):
            if i == 0:
                if live is None:
                    live = await _send(g)
                else:
                    live = await _edit_or_send(live, g)
            else:
                live = await _send(g)
        live_html = groups[-1]
        pending = []

    try:
        while True:
            try:
                item = await asyncio.wait_for(session.queue.get(), timeout=IDLE_SEC)
            except asyncio.TimeoutError:
                await refresh()
                # Idle: next burst starts a fresh message.
                live = None
                live_html = ""
                continue

            if item is None:           # sentinel: session ended / process exited
                await refresh()
                await _send("<i>Claude session ended.  /new to start again.</i>")
                state.claude_sess = None
                state.claude_task = None
                return

            kind, content = item
            pending.append(_fmt_chunk(kind, content))
            if len(live_html) + sum(len(p) for p in pending) >= MAX_TG:
                await refresh()

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
    "<b>Workspace / Project</b>\n"
    "  /workspace &lt;path&gt;     — set base dir containing projects\n"
    "  /workspace            — show current workspace\n"
    "  /project              — pick a project (paginated buttons)\n"
    "  /project &lt;path&gt;       — set a project directly\n"
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
# /workspace
# ---------------------------------------------------------------------------

async def cmd_workspace(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    state   = get_state(chat_id)

    if not ctx.args:
        if state.workspace:
            await update.message.reply_text(
                f"Workspace:\n<code>{html.escape(state.workspace)}</code>\n\n"
                "Use /project to pick a subdirectory.",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                "No workspace set.  Example:\n<code>/workspace ~/Desktop</code>",
                parse_mode="HTML",
            )
        return

    path = os.path.abspath(os.path.expanduser(" ".join(ctx.args)))
    if not os.path.isdir(path):
        await update.message.reply_text(
            f"Directory not found:\n<code>{html.escape(path)}</code>",
            parse_mode="HTML",
        )
        return

    state.workspace = path
    store.set_workspace(chat_id, path)
    await update.message.reply_text(
        f"✅ Workspace set to:\n<code>{html.escape(path)}</code>\n\n"
        "Use /project to pick a project.",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /project  — paginated picker, or explicit path
# ---------------------------------------------------------------------------

def _list_projects(workspace: str) -> list[str]:
    try:
        entries = sorted(os.listdir(workspace))
    except OSError:
        return []
    return [
        e for e in entries
        if not e.startswith(".") and os.path.isdir(os.path.join(workspace, e))
    ]


def _project_keyboard(projects: list[str], page: int) -> InlineKeyboardMarkup:
    pages = max(1, (len(projects) + PAGE_SIZE - 1) // PAGE_SIZE)
    page  = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    chunk = projects[start:start + PAGE_SIZE]

    rows: list[list[InlineKeyboardButton]] = []
    for i, name in enumerate(chunk):
        idx = start + i
        label = name if len(name) <= 40 else name[:37] + "…"
        rows.append([InlineKeyboardButton(label, callback_data=f"proj:s:{idx}")])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("← Prev", callback_data=f"proj:p:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="proj:noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Next →", callback_data=f"proj:p:{page+1}"))
    rows.append(nav)
    return InlineKeyboardMarkup(rows)


async def _switch_project(state: ChatState, ctx: ContextTypes.DEFAULT_TYPE,
                          path: str, reply_to) -> None:
    """Kill current session, set new project, auto-spawn Claude (resume today's id or fresh)."""
    await _kill_claude(state)
    state.project = path
    store.set_current_project(state.chat_id, path)

    sid = store.get_today_session(state.chat_id, path)
    _start_claude(state, ctx, path, sid)

    label = os.path.basename(path) or path
    mode  = "resumed today's session" if sid else "fresh session"
    await reply_to.reply_text(
        f"✅ Project: <code>{html.escape(label)}</code>\n"
        f"<code>{html.escape(path)}</code>\n\n"
        f"Claude {mode}.  Send a message.",
        parse_mode="HTML",
    )


def _start_claude(state: ChatState, ctx: ContextTypes.DEFAULT_TYPE,
                  path: str, session_id: str | None) -> None:
    """Spawn a ClaudeSession wired to persist session_id updates for (chat_id, path)."""
    chat_id = state.chat_id
    sess = ClaudeSession(
        cwd=path,
        session_id=session_id,
        on_session_id=lambda sid, p=path, cid=chat_id: store.save_session(cid, p, sid),
        on_stale_resume=lambda p=path, cid=chat_id: store.clear_session(cid, p),
    )
    sess.start()
    state.claude_sess = sess
    state.claude_task = asyncio.create_task(_forward_claude(state, ctx))


async def cmd_project(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    state   = get_state(chat_id)

    # Explicit path: /project <path>
    if ctx.args:
        path = os.path.abspath(os.path.expanduser(" ".join(ctx.args)))
        if not os.path.isdir(path):
            await update.message.reply_text(
                f"Directory not found:\n<code>{html.escape(path)}</code>",
                parse_mode="HTML",
            )
            return
        await _switch_project(state, ctx, path, update.message)
        return

    # No args: paginated picker from workspace
    if not state.workspace:
        cur = (
            f"\n\nCurrent project:\n<code>{html.escape(state.project)}</code>"
            if state.project else ""
        )
        await update.message.reply_text(
            "No workspace set.  Use <code>/workspace &lt;path&gt;</code> first, "
            "or <code>/project &lt;path&gt;</code> to set a project directly." + cur,
            parse_mode="HTML",
        )
        return

    projects = _list_projects(state.workspace)
    if not projects:
        await update.message.reply_text(
            f"No subdirectories under workspace:\n<code>{html.escape(state.workspace)}</code>",
            parse_mode="HTML",
        )
        return

    header = f"Pick a project from <code>{html.escape(state.workspace)}</code>:"
    if state.project:
        header += f"\n\nCurrent: <code>{html.escape(os.path.basename(state.project))}</code>"
    await update.message.reply_text(
        header,
        parse_mode="HTML",
        reply_markup=_project_keyboard(projects, 0),
    )


async def on_project_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline-keyboard callback for the /project picker."""
    query   = update.callback_query
    chat_id = update.effective_chat.id
    state   = get_state(chat_id)
    parts   = (query.data or "").split(":")

    if len(parts) < 2:
        await query.answer()
        return

    action = parts[1]
    if action == "noop":
        await query.answer()
        return

    if not state.workspace:
        await query.answer("No workspace set.")
        return

    projects = _list_projects(state.workspace)

    if action == "p" and len(parts) >= 3:
        try:
            page = int(parts[2])
        except ValueError:
            await query.answer()
            return
        try:
            await query.edit_message_reply_markup(_project_keyboard(projects, page))
        except Exception:
            pass
        await query.answer()
        return

    if action == "s" and len(parts) >= 3:
        try:
            idx = int(parts[2])
        except ValueError:
            await query.answer()
            return
        if idx >= len(projects):
            await query.answer("Project list changed.  /project again.")
            return
        name = projects[idx]
        path = os.path.join(state.workspace, name)
        await query.answer(f"Switching to {name}…")
        try:
            await query.edit_message_reply_markup(None)
        except Exception:
            pass
        await _switch_project(state, ctx, path, query.message)
        return

    await query.answer()


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

    # /new is explicit reset — always fresh, ignoring any saved session_id.
    # The new session_id (once assigned) will overwrite today's saved id.
    _start_claude(state, ctx, state.project, None)

    note = f" in <code>{html.escape(state.project)}</code>" if state.project else ""
    await update.message.reply_text(
        f"Claude session ready{note} (fresh).  Send your task.",
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

    # Auto-spin a session using today's memory (if any) so switching / restarting
    # is transparent.  Only /new forces a fresh reset.
    if not state.claude_sess or not state.claude_sess.alive:
        if not state.project:
            await update.message.reply_text(
                "No project set.  Use /workspace and /project first."
            )
            return
        if not os.path.isdir(state.project):
            await update.message.reply_text(
                f"Project directory no longer exists:\n<code>{html.escape(state.project)}</code>",
                parse_mode="HTML",
            )
            return
        sid = store.get_today_session(state.chat_id, state.project)
        _start_claude(state, ctx, state.project, sid)

    await state.claude_sess.send(update.message.text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _set_commands(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("workspace", "Set or show workspace base directory"),
        BotCommand("project", "Pick a project (or set one directly)"),
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
    app.add_handler(CommandHandler("workspace",       cmd_workspace))
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

    app.add_handler(CallbackQueryHandler(on_cancel_button,  pattern=r"^cancel:"))
    app.add_handler(CallbackQueryHandler(on_project_button, pattern=r"^proj:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("TeleClaude bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
