"""LLM client for an OpenAI-compatible server (llama.cpp, vLLM, etc.).

We talk to /v1/chat/completions directly with `requests`. No agent framework,
no tool-calling loop -- the pipeline decides everything, and the model is only
ever asked to produce plain text (a JSON array of queries, or the final
answer). That keeps behavior predictable on a small model.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import requests

from . import config


@dataclass
class ChatResult:
    content: str
    usage: dict[str, int] = field(default_factory=dict)


def chat(
    messages: list[dict[str, str]],
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> ChatResult:
    body: dict[str, Any] = {
        "model": config.MODEL_NAME,
        "messages": messages,
        "temperature": config.MODEL_TEMPERATURE if temperature is None else temperature,
        "stream": False,
    }
    if max_tokens is not None:
        body["max_tokens"] = max_tokens

    resp = requests.post(
        f"{config.MODEL_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {config.MODEL_API_KEY}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=config.MODEL_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    payload = resp.json()

    choices = payload.get("choices") or []
    content = ""
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        content = str(message.get("content") or "")

    usage_raw = payload.get("usage") or {}
    usage = {
        "input_tokens": int(usage_raw.get("prompt_tokens") or 0),
        "output_tokens": int(usage_raw.get("completion_tokens") or 0),
        "total_tokens": int(usage_raw.get("total_tokens") or 0),
    }
    return ChatResult(content=content.strip(), usage=usage)


# --- Robust parsing helpers (small models format sloppily) -----------------

_CODE_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_string_list(text: str, max_items: int = 12) -> list[str]:
    """Best-effort extraction of a list of short strings from model output.

    Handles: a JSON array, a fenced JSON array, or a plain bullet/numbered
    list. Always returns a clean list (possibly empty); never raises.
    """
    if not text:
        return []

    candidates: list[str] = []

    fenced = _CODE_FENCE.search(text)
    search_space = fenced.group(1) if fenced else text

    # Try to locate and parse a JSON array anywhere in the text.
    start = search_space.find("[")
    end = search_space.rfind("]")
    if start != -1 and end != -1 and end > start:
        blob = search_space[start : end + 1]
        try:
            parsed = json.loads(blob)
            if isinstance(parsed, list):
                candidates = [str(x) for x in parsed]
        except (json.JSONDecodeError, ValueError):
            candidates = []

    # Fallback: treat each non-empty line as an item, stripping list markers.
    if not candidates:
        for line in text.splitlines():
            line = line.strip()
            line = re.sub(r"^[-*\d.)\]\s\"']+", "", line).strip().strip("\",'")
            if line and not line.lower().startswith(("here", "sure", "queries")):
                candidates.append(line)

    cleaned: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        item = item.strip().strip("\"'").strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(item)
        if len(cleaned) >= max_items:
            break
    return cleaned
