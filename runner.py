"""
runner.py — Async shell command execution with streaming Telegram output.

The main entry point is `stream_run()`, which:
  - spawns the command via asyncio subprocess
  - edits a single "live" Telegram message as output arrives
  - shows a ✖ Cancel inline button while running
  - removes the button and appends ✅/❌ on completion
"""
from __future__ import annotations

import asyncio
import html
import time
from collections.abc import Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.error import BadRequest, RetryAfter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_DISPLAY  = 3500   # chars shown in the live message (leave room for markup)
EDIT_RATE    = 1.5    # min seconds between Telegram message edits
CMD_TIMEOUT  = 120    # default command timeout (seconds)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cancel_kb(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✖ Cancel", callback_data=f"cancel:{chat_id}")
    ]])


def _fmt(buf: str, final: bool, returncode: int | None) -> str:
    display = buf[-MAX_DISPLAY:] if buf else ""
    escaped = html.escape(display)
    body    = f"<pre>{escaped}</pre>" if display else ""

    if not final:
        return body or "<i>running…</i>"

    icon = "✅" if returncode == 0 else f"❌  exit {returncode}"
    return f"{body}\n{icon}" if body else icon


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

async def stream_run(
    cmd       : str,
    cwd       : str | None,
    send_msg  : Callable[..., Awaitable[Message]],
    edit_msg  : Callable[..., Awaitable[None]],
    chat_id   : int,
    store_proc: Callable,           # called with proc (or None when done)
    timeout   : float = CMD_TIMEOUT,
) -> int:
    """
    Execute *cmd* in *cwd*, streaming stdout+stderr to a live Telegram message.

    Parameters
    ----------
    send_msg   : async (**kwargs) → Message  (usually update.message.reply_text)
    edit_msg   : async (msg, text, **kwargs) → None  (usually msg.edit_text)
    store_proc : callback(proc | None) — stores the subprocess for cancellation
    """
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
    )
    store_proc(proc)

    buf    = ""
    live   : Message | None = None
    t_last = 0.0

    async def _refresh(final: bool = False) -> None:
        nonlocal live, t_last
        text = _fmt(buf, final, proc.returncode)
        kb   = None if final else _cancel_kb(chat_id)
        try:
            if live is None:
                live = await send_msg(text, parse_mode="HTML", reply_markup=kb)
            else:
                await edit_msg(live, text, parse_mode="HTML", reply_markup=kb)
        except BadRequest:
            # content unchanged or message gone — send fresh
            try:
                live = await send_msg(text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                pass
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
        t_last = time.monotonic()

    deadline = time.monotonic() + timeout
    await _refresh()  # show "running…" immediately with Cancel button

    while True:
        if time.monotonic() > deadline:
            proc.kill()
            buf += "\n\n⏱  timed out"
            break

        try:
            chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=0.3)
        except asyncio.TimeoutError:
            if proc.returncode is not None:
                break
            if time.monotonic() - t_last >= EDIT_RATE:
                await _refresh()
            continue

        if not chunk:
            break

        buf += chunk.decode("utf-8", errors="replace")

        if time.monotonic() - t_last >= EDIT_RATE:
            await _refresh()

    await proc.wait()
    await _refresh(final=True)
    store_proc(None)
    return proc.returncode or 0
