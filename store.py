"""
store.py — JSON-backed state for TeleClaude.

Persists workspace, current project, and per-project Claude session ids.
Session entries carry a date so switching back on a new day starts fresh.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from threading import Lock
from typing import Any

_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
_lock = Lock()


def _load() -> dict[str, Any]:
    if not os.path.exists(_STATE_PATH):
        return {}
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(data: dict[str, Any]) -> None:
    tmp = _STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, _STATE_PATH)


def _chat(data: dict[str, Any], chat_id: int) -> dict[str, Any]:
    key = str(chat_id)
    if key not in data:
        data[key] = {"workspace": None, "current_project": None, "sessions": {}}
    return data[key]


def _today() -> str:
    return datetime.now().date().isoformat()


def get_workspace(chat_id: int) -> str | None:
    with _lock:
        return _chat(_load(), chat_id).get("workspace")


def set_workspace(chat_id: int, path: str) -> None:
    with _lock:
        data = _load()
        _chat(data, chat_id)["workspace"] = path
        _save(data)


def get_current_project(chat_id: int) -> str | None:
    with _lock:
        return _chat(_load(), chat_id).get("current_project")


def set_current_project(chat_id: int, path: str | None) -> None:
    with _lock:
        data = _load()
        _chat(data, chat_id)["current_project"] = path
        _save(data)


def get_today_session(chat_id: int, project: str) -> str | None:
    """Return saved session_id only if stored date == today, else None."""
    with _lock:
        entry = _chat(_load(), chat_id)["sessions"].get(project)
        if entry and entry.get("date") == _today():
            return entry.get("session_id")
        return None


def save_session(chat_id: int, project: str, session_id: str) -> None:
    with _lock:
        data = _load()
        _chat(data, chat_id)["sessions"][project] = {
            "date": _today(),
            "session_id": session_id,
        }
        _save(data)


def clear_session(chat_id: int, project: str) -> None:
    with _lock:
        data = _load()
        if _chat(data, chat_id)["sessions"].pop(project, None) is not None:
            _save(data)
