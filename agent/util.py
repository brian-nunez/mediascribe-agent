"""Tiny shared helpers."""

from __future__ import annotations

import sys

from . import config


def progress(message: str) -> None:
    """Human-readable progress line. Goes to stderr by default so it never
    contaminates the final answer printed on stdout."""
    stream = sys.stderr if config.PROGRESS_STREAM != "stdout" else sys.stdout
    print(f"... {message}", file=stream, flush=True)


def truncate(text: str, max_chars: int) -> str:
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"
