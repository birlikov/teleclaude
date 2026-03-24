"""
claude_pty.py — Claude Code session via `claude -p` (print / non-interactive mode).

Each user message spawns:
    claude -p <message> --output-format stream-json --dangerously-skip-permissions

as a subprocess in the project directory.  The NDJSON stream is parsed to
extract assistant text, tool-use notifications, and tool results, which are
pushed into self.queue for the Telegram forwarder to read.

Conversation continuity is maintained automatically via --resume <session_id>.
"""
from __future__ import annotations

import asyncio
import json


class ClaudeSession:
    """
    Manages Claude Code interaction via subprocess (print mode).

    User messages are queued and processed one at a time.  Text chunks,
    tool notifications, and errors are pushed to self.queue for the
    Telegram bot to consume.

    Usage
    -----
    sess = ClaudeSession(cwd="/path/to/project")
    sess.start()                          # begin processing loop
    await sess.send("refactor auth.py")  # queue a message
    chunk = await sess.queue.get()        # None = session ended
    sess.stop()
    """

    def __init__(self, cwd: str | None = None):
        self.cwd        = cwd
        self.session_id : str | None = None           # for --resume
        self.queue      : asyncio.Queue[str | None] = asyncio.Queue()
        self.alive      = True
        self._in        : asyncio.Queue[str] = asyncio.Queue()
        self._loop_task : asyncio.Task | None = None

    # ── public API ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background message-processing loop."""
        self._loop_task = asyncio.create_task(self._process_loop())

    async def send(self, text: str) -> None:
        """Queue a user message for Claude to process."""
        if self.alive:
            await self._in.put(text)

    def stop(self) -> None:
        """Stop the session; sends None sentinel to the output queue."""
        self.alive = False
        if self._loop_task:
            self._loop_task.cancel()

    # ── internal ───────────────────────────────────────────────────────────

    async def _process_loop(self) -> None:
        """Drain the input queue, processing messages one at a time."""
        try:
            while self.alive:
                try:
                    msg = await asyncio.wait_for(self._in.get(), timeout=0.5)
                    await self._run(msg)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass
        finally:
            await self.queue.put(None)   # signal the Telegram forwarder to stop

    async def _run(self, message: str) -> None:
        """Invoke `claude -p` and stream parsed results into self.queue."""
        args = [
            'claude', '-p', message,
            '--output-format', 'stream-json',
            '--verbose',
            '--dangerously-skip-permissions',
        ]
        if self.session_id:
            args += ['--resume', self.session_id]

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
            )

            async for raw in proc.stdout:
                line = raw.decode('utf-8', errors='replace').strip()
                if not line:
                    continue
                try:
                    await self._handle_event(json.loads(line))
                except json.JSONDecodeError:
                    pass   # shouldn't happen in stream-json mode

            await proc.wait()

            if proc.returncode not in (0, None):
                err = (await proc.stderr.read()).decode('utf-8', errors='replace').strip()
                if err:
                    await self.queue.put(f'\n⚠️  {err}\n')

        except FileNotFoundError:
            await self.queue.put(
                '⚠️  `claude` command not found.  Is Claude Code installed and on PATH?\n'
            )
        except Exception as e:
            await self.queue.put(f'⚠️  Error: {e}\n')

    async def _handle_event(self, data: dict) -> None:
        """Parse one JSON event and push human-readable text to the queue."""
        t = data.get('type', '')

        # Always capture the session ID so we can resume the conversation
        if sid := data.get('session_id'):
            self.session_id = sid

        if t == 'assistant':
            for block in data.get('message', {}).get('content', []):
                btype = block.get('type')

                if btype == 'text':
                    text = block.get('text', '').strip()
                    if text:
                        await self.queue.put(text + '\n')

                elif btype == 'tool_use':
                    # Narrate what Claude is about to do
                    name = block.get('name', 'tool')
                    inp  = block.get('input', {})
                    if 'command' in inp:
                        summary = f'$ {inp["command"]}'
                    elif 'file_path' in inp:
                        summary = f'{name}: {inp["file_path"]}'
                    else:
                        summary = name
                    await self.queue.put(f'🔧 `{summary}`\n')

        elif t == 'tool_result':
            # Show tool output (truncated if long)
            content = data.get('content', '')
            if isinstance(content, list):
                for c in content:
                    if c.get('type') == 'text':
                        text = c.get('text', '').strip()
                        if text:
                            snippet = text[:800] + ('…' if len(text) > 800 else '')
                            await self.queue.put(f'```\n{snippet}\n```\n')
            elif isinstance(content, str) and content.strip():
                snippet = content.strip()[:800]
                await self.queue.put(f'```\n{snippet}\n```\n')

        elif t == 'result':
            # Update session_id from the final result message (most reliable)
            if sid := data.get('session_id'):
                self.session_id = sid
