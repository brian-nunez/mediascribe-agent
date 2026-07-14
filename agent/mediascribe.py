"""Mediascribe HTTP client.

Two endpoints matter:
  - GET /api/search?q=&limit=            -> {"query", "results": [...]}
  - GET /api/public/blogs/{blog_id}      -> {"blog": {...}}

Everything the rest of the app needs comes back as plain dicts with a stable
shape, so no other module has to know the raw API layout.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests

from . import config
from .util import progress

_SESSION = requests.Session()
_SESSION.headers.update(
    {
        "Accept": "application/json",
        "User-Agent": "mediascribe-agent/0.2.0",
    }
)

_RETRYABLE = {429, 500, 502, 503, 504}


class MediascribeError(RuntimeError):
    pass


@dataclass
class SearchHit:
    blog_id: str
    title: str
    section: str
    source_url: str
    score: float
    preview: str


@dataclass
class Article:
    blog_id: str
    title: str
    section: str
    source_url: str
    blog_url: str
    language: str
    markdown: str
    updated_at: str

    @property
    def citation(self) -> str:
        return (
            f"Title: {self.title or 'Untitled'} | "
            f"URL: {self.blog_url or 'unavailable'} | "
            f"Source: {self.source_url or 'unavailable'} | "
            f"Section: {self.section or 'unavailable'}"
        )


def _get(path: str, params: dict[str, Any] | None = None) -> requests.Response:
    url = f"{config.MEDIASCRIBE_BASE_URL}{path}"
    last_exc: Exception | None = None
    for attempt in range(config.MEDIASCRIBE_RETRIES + 1):
        try:
            resp = _SESSION.get(
                url, params=params, timeout=config.MEDIASCRIBE_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            last_exc = exc
            if attempt >= config.MEDIASCRIBE_RETRIES:
                raise MediascribeError(f"GET {path} failed: {exc}") from exc
        else:
            if resp.status_code not in _RETRYABLE:
                resp.raise_for_status()
                return resp
            if attempt >= config.MEDIASCRIBE_RETRIES:
                resp.raise_for_status()
        delay = config.MEDIASCRIBE_RETRY_BASE_SECONDS * (2**attempt)
        progress(f"retrying {path} in {delay:.1f}s")
        time.sleep(delay)
    raise MediascribeError(f"GET {path} failed after retries: {last_exc}")


def _json(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = _get(path, params).json()
    if not isinstance(payload, dict):
        raise MediascribeError(f"{path} did not return a JSON object")
    return payload


def search(query: str, limit: int) -> list[SearchHit]:
    """Run one keyword search. Returns normalized hits (may be empty)."""
    payload = _json("/api/search", {"q": query, "limit": max(1, min(limit, 20))})
    hits: list[SearchHit] = []
    for item in payload.get("results", []):
        if not isinstance(item, dict):
            continue
        blog_id = str(item.get("blog_id") or "").strip()
        if not blog_id:
            continue
        try:
            score = float(item.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        hits.append(
            SearchHit(
                blog_id=blog_id,
                title=str(item.get("title") or "").strip(),
                section=str(item.get("section_name") or "").strip(),
                source_url=str(item.get("source_url") or "").strip(),
                score=score,
                preview=str(item.get("preview") or "").strip(),
            )
        )
    return hits


def _select_markdown(blog: dict[str, Any], language: str) -> tuple[str, str]:
    languages = blog.get("languages")
    if not isinstance(languages, list):
        return language, ""
    fallback: dict[str, Any] | None = None
    for item in languages:
        if not isinstance(item, dict):
            continue
        if str(item.get("language") or "") == language:
            return language, str(item.get("markdown") or "")
        if fallback is None:
            fallback = item
    if fallback is not None:
        return str(fallback.get("language") or language), str(
            fallback.get("markdown") or ""
        )
    return language, ""


def fetch_article(blog_id: str, language: str = "en") -> Article:
    """Fetch one full article by id."""
    payload = _json(f"/api/public/blogs/{blog_id}")
    blog = payload.get("blog")
    if not isinstance(blog, dict):
        raise MediascribeError(f"blog {blog_id} missing 'blog' payload")
    lang, markdown = _select_markdown(blog, language)
    blog_url = f"{config.MEDIASCRIBE_BASE_URL}/blog/{blog.get('id')}"
    return Article(
        blog_id=str(blog.get("id") or blog_id),
        title=str(blog.get("title") or "").strip(),
        section=str(blog.get("section_name") or "").strip(),
        source_url=str(blog.get("source_url") or "").strip(),
        blog_url=blog_url,
        language=lang,
        markdown=markdown,
        updated_at=str(blog.get("updated_at") or ""),
    )
