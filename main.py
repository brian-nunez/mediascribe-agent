import argparse
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI


MEDIASCRIBE_BASE_URL = os.getenv(
    "MEDIASCRIBE_BASE_URL",
    "http://10.0.0.132:9595",  # "https://mediascribe.b8z.me"
).rstrip("/")
REQUEST_TIMEOUT_SECONDS = float(os.getenv("MEDIASCRIBE_TIMEOUT_SECONDS", "20"))
REQUEST_RETRIES = int(os.getenv("MEDIASCRIBE_REQUEST_RETRIES", "4"))
REQUEST_RETRY_BASE_SECONDS = float(os.getenv("MEDIASCRIBE_RETRY_BASE_SECONDS", "1.5"))
DEFAULT_MODEL_BASE_URL = "http://10.0.0.119:8080/v1"
DEFAULT_MODEL_NAME = "ggml-org/gemma-4-E2B-it-GGUF:Q8_0"
DIAGNOSTICS_DIR = Path(os.getenv("AGENT_DIAGNOSTICS_DIR", "diagnostics"))
PROGRESS_STREAM = os.getenv("AGENT_PROGRESS_STREAM", "stdout").lower()
RESEARCH_QUERY_LIMIT = int(os.getenv("AGENT_RESEARCH_QUERY_LIMIT", "20"))
RESEARCH_SEARCH_LIMIT = int(os.getenv("AGENT_RESEARCH_SEARCH_LIMIT", "20"))
RESEARCH_DEFAULT_ARTICLES = int(os.getenv("AGENT_RESEARCH_DEFAULT_ARTICLES", "24"))
RESEARCH_MAX_ARTICLES = int(os.getenv("AGENT_RESEARCH_MAX_ARTICLES", "32"))
RESEARCH_MIN_GOOD_ARTICLES = int(os.getenv("AGENT_RESEARCH_MIN_GOOD_ARTICLES", "12"))
RESEARCH_MIN_QUALITY_SCORE = float(
    os.getenv("AGENT_RESEARCH_MIN_QUALITY_SCORE", "0.35")
)
RESEARCH_MAX_FETCH_ATTEMPTS = int(os.getenv("AGENT_RESEARCH_MAX_FETCH_ATTEMPTS", "80"))
RESEARCH_ARTICLE_DELAY_SECONDS = float(
    os.getenv("AGENT_RESEARCH_ARTICLE_DELAY_SECONDS", "0.25")
)
RESEARCH_ENFORCEMENT_ATTEMPTS = int(
    os.getenv("AGENT_RESEARCH_ENFORCEMENT_ATTEMPTS", "4")
)
AGENT_COMPLEXITY = os.getenv("AGENT_COMPLEXITY", "auto").strip().lower()
AGENT_OUTPUT_MODE = os.getenv("AGENT_OUTPUT_MODE", "normal").strip().lower()

RESEARCH_PROFILES = {
    "simple": {
        "query_limit": 4,
        "search_limit": 10,
        "default_articles": 4,
        "max_articles": 6,
        "min_good_articles": 1,
        "max_fetch_attempts": 10,
        "enforcement_attempts": 1,
        "research_budget_seconds": 30,
        "query_guidance": "Use 2 to 4 keyword searches and fetch 1 to 4 strong articles.",
    },
    "standard": {
        "query_limit": 8,
        "search_limit": 15,
        "default_articles": 8,
        "max_articles": 12,
        "min_good_articles": 4,
        "max_fetch_attempts": 24,
        "enforcement_attempts": 2,
        "research_budget_seconds": 90,
        "query_guidance": "Use 4 to 8 keyword searches and fetch 4 to 8 strong articles.",
    },
    "deep": {
        "query_limit": 14,
        "search_limit": 20,
        "default_articles": 18,
        "max_articles": 24,
        "min_good_articles": 8,
        "max_fetch_attempts": 50,
        "enforcement_attempts": 3,
        "research_budget_seconds": 180,
        "query_guidance": "Use 10 to 14 keyword searches and fetch 8 to 12 strong articles.",
    },
    "max": {
        "query_limit": 20,
        "search_limit": 20,
        "default_articles": 24,
        "max_articles": 32,
        "min_good_articles": 16,
        "max_fetch_attempts": 80,
        "enforcement_attempts": 4,
        "research_budget_seconds": 300,
        "query_guidance": "Use 14 to 20 keyword searches and fetch 12 to 16 strong articles.",
    },
}

OUTPUT_MODES = {
    "brief": {
        "guidance": "Answer in a compact form. Prioritize the direct answer and key tradeoffs.",
        "requirements": [
            "direct answer",
            "where it fits",
            "main tradeoffs",
            "sources",
        ],
    },
    "normal": {
        "guidance": "Use balanced depth. Include enough architecture detail without exhaustive planning.",
        "requirements": [
            "recommendation",
            "requirements or assumptions",
            "main flow",
            "key tradeoffs",
            "failure modes",
            "sources",
        ],
    },
    "detailed": {
        "guidance": "Go deeper on architecture, operations, tradeoffs, and implementation detail.",
        "requirements": [
            "requirements and assumptions",
            "end-to-end flow",
            "source-of-truth choices",
            "read and write paths",
            "scaling",
            "caching and invalidation",
            "async processing",
            "failure handling",
            "observability",
            "tradeoffs",
            "sources",
        ],
    },
    "interview": {
        "guidance": "Use a system-design interview format with scoped assumptions and crisp decisions.",
        "requirements": [
            "requirements",
            "capacity assumptions when useful",
            "API or interface sketch when useful",
            "data model",
            "high-level design",
            "deep dives",
            "bottlenecks",
            "tradeoffs",
            "sources",
        ],
    },
    "production-plan": {
        "guidance": "Frame the answer as a production implementation and operations plan.",
        "requirements": [
            "target architecture",
            "implementation phases",
            "data migration or rollout",
            "operational controls",
            "observability",
            "failure handling",
            "security controls",
            "risks",
            "sources",
        ],
    },
}
CURRENT_RESEARCH_PROFILE: dict[str, Any] | None = None

SESSION = requests.Session()
SESSION.headers.update(
    {
        "Accept": "application/json, text/html;q=0.9",
        "User-Agent": "mediascribe-agent-architect/0.1.0",
    }
)


def request_get(path: str, params: dict[str, Any] | None = None) -> requests.Response:
    last_response = None
    for attempt in range(REQUEST_RETRIES + 1):
        response = SESSION.get(
            f"{MEDIASCRIBE_BASE_URL}{path}",
            params=params,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        last_response = response
        if response.status_code not in {429, 500, 502, 503, 504}:
            response.raise_for_status()
            return response

        if attempt >= REQUEST_RETRIES:
            response.raise_for_status()

        retry_after = response.headers.get("Retry-After")
        try:
            delay = (
                float(retry_after)
                if retry_after
                else REQUEST_RETRY_BASE_SECONDS * (2**attempt)
            )
        except ValueError:
            delay = REQUEST_RETRY_BASE_SECONDS * (2**attempt)

        status(f"Rate limited; waiting {delay:.1f}s...")
        time.sleep(delay)

    assert last_response is not None
    last_response.raise_for_status()
    return last_response


def request_json(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = request_get(path, params=params)
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object from Mediascribe.")
    return payload


def request_text(path: str) -> str:
    response = request_get(path)
    return response.text


def compact_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


def env_int(name: str, fallback: int) -> int:
    if name not in os.environ:
        return fallback
    return int(os.environ[name])


def normalize_complexity(value: str) -> str:
    normalized = str(value or "auto").strip().lower()
    if normalized in {"simple", "standard", "deep", "max", "auto"}:
        return normalized
    return "auto"


def normalize_output_mode(value: str) -> str:
    normalized = str(value or "normal").strip().lower()
    if normalized in OUTPUT_MODES:
        return normalized
    return "normal"


def classify_complexity(prompt: str) -> str:
    text = str(prompt or "").lower()
    words = re.findall(r"[a-zA-Z0-9]+", text)
    word_count = len(words)

    max_markers = {
        "500m",
        "million",
        "global",
        "multi-region",
        "instagram",
        "youtube",
        "twitter",
        "tiktok",
        "facebook",
        "netflix",
        "google docs",
        "large scale",
        "planet scale",
    }
    deep_markers = {
        "architecture",
        "design",
        "system design",
        "scalable",
        "scale",
        "sharding",
        "distributed",
        "high availability",
        "real-time",
        "websocket",
        "feed",
        "multi tenant",
        "failure modes",
    }
    simple_prefixes = (
        "what is ",
        "what are ",
        "explain ",
        "define ",
        "difference between ",
        "compare ",
        "why is ",
    )
    standard_prefixes = (
        "should i use ",
        "when should i use ",
        "would you use ",
        "which should i use ",
    )

    if any(marker in text for marker in max_markers):
        return "max"
    if any(marker in text for marker in deep_markers):
        return "deep"
    if text.startswith(standard_prefixes):
        return "standard"
    if word_count <= 10 or text.startswith(simple_prefixes):
        return "simple"
    return "standard"


def select_research_profile(
    prompt: str,
    requested_complexity: str | None = None,
    output_mode: str | None = None,
    research_budget_seconds: int | None = None,
) -> dict[str, Any]:
    requested = normalize_complexity(requested_complexity or AGENT_COMPLEXITY)
    selected_output_mode = normalize_output_mode(output_mode or AGENT_OUTPUT_MODE)
    selected = classify_complexity(prompt) if requested == "auto" else requested
    base = dict(RESEARCH_PROFILES[selected])
    mode_config = OUTPUT_MODES[selected_output_mode]
    base.update(
        {
            "name": selected,
            "requested": requested,
            "auto_classified": requested == "auto",
            "output_mode": selected_output_mode,
            "output_guidance": mode_config["guidance"],
            "answer_requirements": mode_config["requirements"],
            "query_limit": env_int("AGENT_RESEARCH_QUERY_LIMIT", base["query_limit"]),
            "search_limit": env_int("AGENT_RESEARCH_SEARCH_LIMIT", base["search_limit"]),
            "default_articles": env_int(
                "AGENT_RESEARCH_DEFAULT_ARTICLES", base["default_articles"]
            ),
            "max_articles": env_int("AGENT_RESEARCH_MAX_ARTICLES", base["max_articles"]),
            "min_good_articles": env_int(
                "AGENT_RESEARCH_MIN_GOOD_ARTICLES", base["min_good_articles"]
            ),
            "max_fetch_attempts": env_int(
                "AGENT_RESEARCH_MAX_FETCH_ATTEMPTS", base["max_fetch_attempts"]
            ),
            "enforcement_attempts": env_int(
                "AGENT_RESEARCH_ENFORCEMENT_ATTEMPTS",
                base["enforcement_attempts"],
            ),
            "research_budget_seconds": (
                research_budget_seconds
                if research_budget_seconds is not None
                else env_int(
                    "AGENT_RESEARCH_BUDGET_SECONDS",
                    base["research_budget_seconds"],
                )
            ),
        }
    )
    return base


def active_research_profile() -> dict[str, Any]:
    if CURRENT_RESEARCH_PROFILE is not None:
        return CURRENT_RESEARCH_PROFILE
    return select_research_profile("")


def normalize_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = re.split(r"\n|;|,(?=\s*[A-Za-z0-9])", value)
    else:
        items = [value]

    normalized = []
    seen = set()
    for item in items:
        text = str(item).strip(" -\t\r\n")
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


QUERY_STOPWORDS = {
    "about",
    "against",
    "also",
    "and",
    "architecture",
    "architecting",
    "around",
    "backed",
    "based",
    "best",
    "build",
    "building",
    "can",
    "compare",
    "considerations",
    "design",
    "designing",
    "detail",
    "details",
    "does",
    "for",
    "from",
    "give",
    "highly",
    "high-volume",
    "how",
    "implementation",
    "implementing",
    "into",
    "large-scale",
    "like",
    "make",
    "need",
    "on",
    "patterns",
    "platform",
    "please",
    "recommend",
    "should",
    "strategies",
    "strategy",
    "system",
    "systems",
    "that",
    "the",
    "their",
    "these",
    "this",
    "through",
    "tradeoffs",
    "using",
    "vs",
    "versus",
    "with",
    "would",
}


QUERY_SYNONYMS = {
    "availability": "high availability",
    "available": "high availability",
    "highly": "high availability",
    "mau": "MAU",
    "qps": "QPS",
    "rps": "RPS",
    "cdn": "CDN",
    "dns": "DNS",
    "jwt": "JWT",
    "api": "API",
    "apis": "API",
    "redis": "Redis",
    "postgres": "Postgres",
    "postgresql": "PostgreSQL",
    "kafka": "Kafka",
    "rabbitmq": "RabbitMQ",
    "sqs": "SQS",
    "websocket": "WebSocket",
    "websockets": "WebSocket",
    "crdt": "CRDT",
    "crdts": "CRDT",
    "ot": "OT",
    "lua": "Lua",
}


def keywordize_query(query: Any, max_terms: int = 8) -> str:
    text = str(query or "").strip()
    if not text:
        return ""

    raw_tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9+#./-]*", text)
    keywords = []
    seen = set()
    for raw_token in raw_tokens:
        token = raw_token.strip("-_/.,:;()[]{}").lower()
        if len(token) < 2:
            continue
        if token in QUERY_STOPWORDS:
            continue
        keyword = QUERY_SYNONYMS.get(token, raw_token)
        key = keyword.lower()
        if key in seen:
            continue
        seen.add(key)
        keywords.append(keyword)
        if len(keywords) >= max_terms:
            break

    return " ".join(keywords) if keywords else text


def status(message: str) -> None:
    stream = sys.stderr if PROGRESS_STREAM == "stderr" else sys.stdout
    print(message, file=stream, flush=True)


def truncate(value: Any, max_chars: int = 1200) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}... [truncated {len(text) - max_chars} chars]"


def clean_final_answer(content: Any) -> str:
    text = str(content or "").lstrip()
    return re.sub(
        r"^(as\s+(an?\s+)?(technical\s+design\s+)?architect,?\s*)",
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    ).lstrip()


def citation(title: Any, blog_url: Any, source_url: Any, section: Any) -> str:
    return (
        f"Title: {title or 'Untitled'} | "
        f"Blog URL: {blog_url or 'unavailable'} | "
        f"Source URL: {source_url or 'unavailable'} | "
        f"Mediascribe section_name: {section or 'unavailable'}"
    )


def find_language_markdown(blog: dict[str, Any], language: str) -> dict[str, str]:
    languages = blog.get("languages")
    if not isinstance(languages, list):
        return {"language": language, "markdown": ""}

    fallback = None
    for item in languages:
        if not isinstance(item, dict):
            continue
        item_language = str(item.get("language") or "")
        if item_language == language:
            return {
                "language": item_language,
                "markdown": str(item.get("markdown") or ""),
            }
        if fallback is None:
            fallback = item

    if isinstance(fallback, dict):
        return {
            "language": str(fallback.get("language") or ""),
            "markdown": str(fallback.get("markdown") or ""),
        }

    return {"language": language, "markdown": ""}


def parse_feed_page(page_html: str) -> dict[str, Any]:
    match = re.search(
        r'<script id="initial-feed-data" type="application/json">(.*?)</script>',
        page_html,
        re.DOTALL,
    )
    if not match:
        raise ValueError(
            "Could not find initial-feed-data in the Mediascribe landing page."
        )

    embedded_json = html.unescape(match.group(1))
    payload = json.loads(embedded_json)
    if not isinstance(payload, dict) or not payload.get("page"):
        raise ValueError("Mediascribe landing page did not include a feed payload.")

    return payload


def transcript_preview(raw_transcript: str, max_chars: int = 2500) -> str:
    if not raw_transcript:
        return ""

    try:
        chunks = json.loads(raw_transcript)
    except json.JSONDecodeError:
        return raw_transcript[:max_chars]

    if not isinstance(chunks, list):
        return raw_transcript[:max_chars]

    lines = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        index = chunk.get("chunk_index")
        content = str(chunk.get("content") or "").strip()
        if content:
            lines.append(f"chunk {index}: {content}")

    return "\n\n".join(lines)[:max_chars]


def fetch_blog_api(blog_id: str) -> dict[str, Any]:
    payload = request_json(f"/api/public/blogs/{blog_id}")
    blog = payload.get("blog")
    if not isinstance(blog, dict):
        raise ValueError("Mediascribe blog API did not include a blog payload.")
    return blog


def article_payload(blog: dict[str, Any], language: str = "en") -> dict[str, Any]:
    selected = find_language_markdown(blog, language)
    blog_url = f"{MEDIASCRIBE_BASE_URL}/blog/{blog.get('id')}"
    return {
        "blog_id": blog.get("id"),
        "title": blog.get("title"),
        "mediascribe_section": blog.get("section_name"),
        "blog_url": blog_url,
        "source_url": blog.get("source_url"),
        "language": selected["language"],
        "markdown": selected["markdown"],
        "transcript_preview": transcript_preview(str(blog.get("transcript") or "")),
        "updated_at": blog.get("updated_at"),
        "citation": citation(
            blog.get("title"),
            blog_url,
            blog.get("source_url"),
            blog.get("section_name"),
        ),
    }


def research_terms(*values: Any) -> list[str]:
    terms = []
    seen = set()
    for value in values:
        if isinstance(value, list):
            iterable = value
        else:
            iterable = [value]
        for item in iterable:
            for token in re.findall(r"[a-zA-Z0-9][a-zA-Z0-9-]{2,}", str(item).lower()):
                if token in {
                    "and",
                    "are",
                    "for",
                    "from",
                    "how",
                    "into",
                    "like",
                    "should",
                    "system",
                    "that",
                    "the",
                    "this",
                    "use",
                    "what",
                    "when",
                    "with",
                }:
                    continue
                if token in seen:
                    continue
                seen.add(token)
                terms.append(token)
    return terms


GENERIC_TITLE_MARKERS = {
    "ai proof",
    "any system",
    "blogs",
    "concepts",
    "explained",
    "hard",
    "interview",
    "mistakes",
    "popular",
    "should know",
    "tips",
    "wrong",
}


def is_generic_article_title(title: str) -> bool:
    title_lower = title.lower()
    return any(marker in title_lower for marker in GENERIC_TITLE_MARKERS)


def recommended_sources(articles: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    candidates = []
    seen = set()
    for article in articles:
        citation_value = article.get("citation")
        if not citation_value or citation_value in seen:
            continue
        seen.add(citation_value)
        quality = article.get("quality") if isinstance(article.get("quality"), dict) else {}
        candidates.append(
            {
                "title": article.get("title"),
                "citation": citation_value,
                "quality_score": quality.get("quality_score"),
                "topic_specificity": quality.get("topic_specificity"),
                "matched_search_query": article.get("matched_search_query"),
                "coverage_hits": quality.get("coverage_hits", [])[:5],
            }
        )

    return sorted(
        candidates,
        key=lambda item: (
            float(item.get("topic_specificity") or 0),
            float(item.get("quality_score") or 0),
        ),
        reverse=True,
    )[:limit]


def score_article_quality(
    article: dict[str, Any],
    matched_query: str,
    all_queries: list[str],
    must_cover: list[str],
) -> dict[str, Any]:
    title = str(article.get("title") or "")
    markdown = str(article.get("markdown") or "")
    combined = f"{title}\n{article.get('mediascribe_section') or ''}\n{markdown}".lower()
    title_lower = title.lower()
    front_matter = f"{title}\n{markdown[:1200]}".lower()
    terms = research_terms(matched_query, all_queries, must_cover)
    matched_terms = [term for term in terms if term in combined]
    title_matches = [term for term in terms if term in title_lower]
    specific_title_matches = [
        term
        for term in title_matches
        if term
        not in {
            "architecture",
            "concepts",
            "design",
            "interview",
            "patterns",
            "system",
            "systems",
        }
    ]
    front_matter_matches = [term for term in terms if term in front_matter]
    coverage_hits = []
    exact_phrase_hits = []
    for item in must_cover:
        item_terms = research_terms(item)
        if not item_terms:
            continue
        normalized_phrase = " ".join(item_terms)
        if normalized_phrase and normalized_phrase in combined:
            exact_phrase_hits.append(item)
        item_matches = [term for term in item_terms if term in combined]
        if len(item_terms) <= 2:
            required_matches = len(item_terms)
        else:
            required_matches = max(2, (len(item_terms) * 3 + 3) // 4)
        if len(item_matches) >= required_matches:
            coverage_hits.append(item)

    query_phrase_hits = []
    for query in all_queries:
        query_terms = research_terms(query)
        if len(query_terms) < 2:
            continue
        normalized_query = " ".join(query_terms)
        if normalized_query in combined:
            query_phrase_hits.append(query)

    term_score = len(matched_terms) / max(len(terms), 1)
    title_score = len(title_matches) / max(min(len(terms), 8), 1)
    coverage_score = len(coverage_hits) / max(len(must_cover), 1) if must_cover else 0.0
    length_score = min(len(markdown) / 5000, 1.0)
    search_score = min(float(article.get("search_score") or 0) / 5, 1.0)
    score = (
        (term_score * 0.30)
        + (title_score * 0.20)
        + (coverage_score * 0.20)
        + (length_score * 0.20)
        + (search_score * 0.10)
    )

    if must_cover:
        required_coverage_hits = min(len(must_cover), 3)
        topic_specificity = max(
            min(len(exact_phrase_hits) / 2, 1.0),
            min(len(query_phrase_hits) / 2, 1.0),
            min(len(specific_title_matches) / 3, 1.0),
            1.0
            if (
                coverage_score >= 0.5
                and len(specific_title_matches) >= 1
                and len(front_matter_matches) >= 6
            )
            else 0.0,
            1.0 if coverage_score >= 0.6 and len(front_matter_matches) >= 6 else 0.0,
        )
        coverage_gate = len(coverage_hits) >= required_coverage_hits or (
            coverage_score >= 0.5
            and len(specific_title_matches) >= 1
            and len(front_matter_matches) >= 6
        )
        topic_gate = (
            bool(exact_phrase_hits)
            or bool(query_phrase_hits)
            or (len(front_matter_matches) >= 5 and coverage_score >= 0.5)
            or (len(title_matches) >= 2 and coverage_score >= 0.5)
        )
        relevance_gate = (
            coverage_gate
            and topic_gate
            and (not is_generic_article_title(title) or topic_specificity >= 0.75)
        )
    else:
        topic_specificity = min(len(specific_title_matches) / 3, 1.0)
        relevance_gate = bool(title_matches) or term_score >= 0.25

    return {
        "quality_score": round(score, 4),
        "topic_specificity": round(topic_specificity, 4),
        "generic_title": is_generic_article_title(title),
        "matched_terms": matched_terms[:20],
        "title_matches": title_matches[:10],
        "specific_title_matches": specific_title_matches[:10],
        "front_matter_matches": front_matter_matches[:10],
        "coverage_hits": coverage_hits,
        "exact_phrase_hits": exact_phrase_hits,
        "query_phrase_hits": query_phrase_hits[:5],
        "markdown_chars": len(markdown),
        "relevance_gate": relevance_gate,
        "is_solid": (
            score >= RESEARCH_MIN_QUALITY_SCORE
            and len(markdown) >= 2500
            and relevance_gate
        ),
    }


@tool
def search_mediascribe(query: str, limit: int = 10) -> str:
    """Search Mediascribe source docs. Use this before answering architecture questions."""
    search_query = keywordize_query(query)
    status(f"Knowledge lookup: {search_query}")
    safe_limit = clamp_int(limit, 1, 20)
    payload = request_json("/api/search", {"q": search_query, "limit": safe_limit})
    results = []

    for item in payload.get("results", []):
        if not isinstance(item, dict):
            continue
        blog_id = str(item.get("blog_id") or "")
        blog_url = f"{MEDIASCRIBE_BASE_URL}/blog/{blog_id}" if blog_id else None
        results.append(
            {
                "blog_id": blog_id,
                "title": item.get("title"),
                "mediascribe_section": item.get("section_name"),
                "blog_url": blog_url,
                "source_url": item.get("source_url"),
                "preview": item.get("preview"),
                "score": item.get("score"),
                "citation": citation(
                    item.get("title"),
                    blog_url,
                    item.get("source_url"),
                    item.get("section_name"),
                ),
            }
        )

    return compact_json(
        {
            "query": query,
            "search_query": payload.get("query", search_query),
            "count": len(results),
            "results": results,
        }
    )


def perform_mediascribe_research(
    primary_query: str,
    related_queries: list[str] | str | None = None,
    max_articles: int | None = None,
    min_good_articles: int | None = None,
    must_cover: list[str] | str | None = None,
    language: str = "en",
) -> dict[str, Any]:
    status("Planning searches...")
    profile = active_research_profile()
    research_started = time.perf_counter()
    research_budget_seconds = max(1, int(profile["research_budget_seconds"]))
    budget_exhausted = False

    def budget_state() -> dict[str, Any]:
        elapsed = time.perf_counter() - research_started
        remaining = max(0.0, research_budget_seconds - elapsed)
        return {
            "budget_seconds": research_budget_seconds,
            "elapsed_seconds": round(elapsed, 3),
            "remaining_budget_seconds": round(remaining, 3),
            "budget_exhausted": elapsed >= research_budget_seconds,
        }

    related_query_items = normalize_text_list(related_queries)
    must_cover_items = normalize_text_list(must_cover)
    queries = [primary_query]
    queries.extend(related_query_items)
    queries.extend(must_cover_items)

    query_pairs = []
    seen_search_queries = set()
    for query in queries:
        clean_query = str(query).strip()
        if not clean_query:
            continue
        search_query = keywordize_query(clean_query)
        if not search_query:
            continue
        key = search_query.lower()
        if key in seen_search_queries:
            continue
        seen_search_queries.add(key)
        query_pairs.append({"query": clean_query, "search_query": search_query})

    if not query_pairs:
        return {"error": "primary_query is required"}

    search_runs = []
    unique_results = {}
    results_by_query = []
    query_limit = clamp_int(int(profile["query_limit"]), 1, 24)
    search_limit = clamp_int(int(profile["search_limit"]), 1, 20)
    active_query_pairs = query_pairs[:query_limit]
    original_queries = [item["query"] for item in active_query_pairs]
    search_queries = [item["search_query"] for item in active_query_pairs]
    scoring_queries = original_queries + search_queries
    for query_pair in active_query_pairs:
        if budget_state()["budget_exhausted"]:
            budget_exhausted = True
            status("Research budget exhausted during search planning.")
            break
        query = query_pair["query"]
        search_query = query_pair["search_query"]
        status(f"Knowledge lookup: {search_query}")
        payload = request_json(
            "/api/search", {"q": search_query, "limit": search_limit}
        )
        results = (
            payload.get("results") if isinstance(payload.get("results"), list) else []
        )
        query_results = []
        search_runs.append(
            {
                "query": query,
                "search_query": search_query,
                "count": len(results),
                "top_results": [
                    {
                        "blog_id": item.get("blog_id"),
                        "title": item.get("title"),
                        "mediascribe_section": item.get("section_name"),
                        "score": item.get("score"),
                    }
                    for item in results[:8]
                    if isinstance(item, dict)
                ],
            }
        )

        for item in results:
            if not isinstance(item, dict):
                continue
            blog_id = str(item.get("blog_id") or "")
            if not blog_id:
                continue
            score = float(item.get("score") or 0)
            query_results.append(
                {
                    "score": score,
                    "matched_query": query,
                    "matched_search_query": search_query,
                    "search_result": item,
                }
            )
            current = unique_results.get(blog_id)
            if current is None or score > current["score"]:
                unique_results[blog_id] = {
                    "score": score,
                    "matched_query": query,
                    "matched_search_query": search_query,
                    "search_result": item,
                }
        results_by_query.append(query_results)

    ranked_results = sorted(
        unique_results.values(),
        key=lambda item: item["score"],
        reverse=True,
    )

    profile_max_articles = int(profile["max_articles"])
    requested_max_articles = (
        int(max_articles) if max_articles is not None else int(profile["default_articles"])
    )
    safe_max_articles = clamp_int(requested_max_articles, 1, profile_max_articles)
    safe_min_good_articles = min(int(profile["min_good_articles"]), safe_max_articles)
    safe_max_fetch_attempts = max(safe_max_articles, int(profile["max_fetch_attempts"]))
    selected_results = []
    selected_blog_ids = set()

    status("Evaluating research coverage...")
    for query_results in results_by_query:
        for result in query_results:
            blog_id = str(result["search_result"].get("blog_id") or "")
            if not blog_id or blog_id in selected_blog_ids:
                continue
            selected_results.append(result)
            selected_blog_ids.add(blog_id)
            break
        if len(selected_results) >= safe_max_articles:
            break

    for result in ranked_results:
        if len(selected_results) >= safe_max_articles:
            break
        blog_id = str(result["search_result"].get("blog_id") or "")
        if not blog_id or blog_id in selected_blog_ids:
            continue
        selected_results.append(result)
        selected_blog_ids.add(blog_id)

    candidate_results = selected_results + [
        result
        for result in ranked_results
        if str(result["search_result"].get("blog_id") or "") not in selected_blog_ids
    ]

    articles = []
    rejected_articles = []
    article_errors = []
    attempted_blog_ids = set()
    for result in candidate_results:
        if budget_state()["budget_exhausted"]:
            budget_exhausted = True
            status("Research budget exhausted during article review.")
            break
        if len(articles) >= safe_max_articles:
            break
        if len(attempted_blog_ids) >= safe_max_fetch_attempts:
            break
        blog_id = result["search_result"].get("blog_id")
        if not blog_id:
            continue
        blog_id = str(blog_id)
        if blog_id in attempted_blog_ids:
            continue
        attempted_blog_ids.add(blog_id)
        title = str(result["search_result"].get("title") or "untitled article").strip()
        status(f"Sifting through article: {title}")
        try:
            blog = fetch_blog_api(blog_id)
        except Exception as exc:
            status(f"Skipping article after retries: {title}")
            article_errors.append(
                {
                    "blog_id": blog_id,
                    "title": title,
                    "matched_query": result["matched_query"],
                    "matched_search_query": result["matched_search_query"],
                    "error": str(exc),
                }
            )
            continue
        payload = article_payload(blog, language)
        payload["matched_query"] = result["matched_query"]
        payload["matched_search_query"] = result["matched_search_query"]
        payload["search_score"] = result["score"]
        quality = score_article_quality(
            payload,
            matched_query=result["matched_query"],
            all_queries=scoring_queries,
            must_cover=must_cover_items,
        )
        payload["quality"] = quality
        if quality["is_solid"]:
            status(f"Solid article found: {title}")
            articles.append(payload)
        else:
            status(f"Article looks weak; continuing search: {title}")
            rejected_articles.append(
                {
                    "blog_id": blog_id,
                    "title": title,
                    "matched_query": result["matched_query"],
                    "matched_search_query": result["matched_search_query"],
                    "quality": quality,
                }
            )
        time.sleep(RESEARCH_ARTICLE_DELAY_SECONDS)

        solid_count = sum(
            1 for article in articles if article.get("quality", {}).get("is_solid")
        )
        if (
            solid_count >= safe_min_good_articles
            and len(articles) >= safe_min_good_articles
        ):
            break

    if not articles and rejected_articles:
        status("No solid articles found; keeping best weak articles as fallback.")
        fallback_by_score = sorted(
            rejected_articles,
            key=lambda item: item["quality"]["quality_score"],
            reverse=True,
        )[: min(safe_min_good_articles, safe_max_articles)]
        fallback_ids = {item["blog_id"] for item in fallback_by_score}
        attempted_blog_ids.clear()
        for result in candidate_results:
            if budget_state()["budget_exhausted"]:
                budget_exhausted = True
                status("Research budget exhausted during fallback article review.")
                break
            blog_id = str(result["search_result"].get("blog_id") or "")
            if blog_id not in fallback_ids or blog_id in attempted_blog_ids:
                continue
            attempted_blog_ids.add(blog_id)
            try:
                blog = fetch_blog_api(blog_id)
            except Exception:
                continue
            payload = article_payload(blog, language)
            payload["matched_query"] = result["matched_query"]
            payload["matched_search_query"] = result["matched_search_query"]
            payload["search_score"] = result["score"]
            payload["quality"] = score_article_quality(
                payload,
                matched_query=result["matched_query"],
                all_queries=scoring_queries,
                must_cover=must_cover_items,
            )
            payload["fallback_weak_article"] = True
            articles.append(payload)

    final_budget_state = budget_state()
    budget_exhausted = budget_exhausted or bool(final_budget_state["budget_exhausted"])

    solid_articles = [
        article for article in articles if article.get("quality", {}).get("is_solid")
    ]
    source_shortlist = recommended_sources(
        solid_articles if solid_articles else articles,
        limit=8,
    )

    return {
        "queries": original_queries,
        "search_queries": search_queries,
        "search_runs": search_runs,
        "complexity_profile": profile,
        "output_mode": profile["output_mode"],
        "answer_requirements": profile["answer_requirements"],
        "research_budget": {**final_budget_state, "budget_exhausted": budget_exhausted},
        "articles": articles,
        "recommended_sources": source_shortlist,
        "source_policy": (
            "Use only recommended_sources for the final Sources section unless a "
            "non-recommended article directly supports a specific claim. Cite 3 to "
            "8 sources maximum, prefer topic-specific articles, and do not cite "
            "generic background articles unless they are explicitly used."
        ),
        "rejected_articles": rejected_articles,
        "article_errors": article_errors,
        "quality_requirements": {
            "min_good_articles": safe_min_good_articles,
            "min_quality_score": RESEARCH_MIN_QUALITY_SCORE,
            "max_fetch_attempts": safe_max_fetch_attempts,
            "fetch_attempts": len(attempted_blog_ids),
            "must_cover": must_cover_items,
            "solid_articles": sum(
                1 for article in articles if article.get("quality", {}).get("is_solid")
            ),
            "recommended_source_count": len(source_shortlist),
            "budget_exhausted": budget_exhausted,
        },
        "selection_strategy": (
            "Fetched candidate articles iteratively. Each article was scored against "
            "the matched query, all research queries, must-cover criteria, markdown "
            "length, and search score. Weak articles are skipped once minimum "
            "coverage is satisfied."
        ),
        "research_note": (
            "These articles were selected after multiple searches. Use only their "
            "full markdown content for sourced claims."
        ),
    }


@tool
def research_mediascribe(
    primary_query: str,
    related_queries: list[str] | str | None = None,
    max_articles: int | None = None,
    min_good_articles: int | None = None,
    must_cover: list[str] | str | None = None,
    language: str = "en",
) -> str:
    """Research Mediascribe with multiple searches, then fetch full articles for the best unique results."""
    return compact_json(
        perform_mediascribe_research(
            primary_query=primary_query,
            related_queries=related_queries,
            max_articles=max_articles,
            min_good_articles=min_good_articles,
            must_cover=must_cover,
            language=language,
        )
    )


@tool
def list_mediascribe_landing_page(limit: int = 10) -> str:
    """Read the Mediascribe landing page feed and return recent article metadata."""
    status("Sifting through files...")
    safe_limit = clamp_int(limit, 1, 20)
    payload = parse_feed_page(request_text("/"))
    page = payload["page"]
    items = page.get("items") if isinstance(page.get("items"), list) else []
    sections = page.get("sections") if isinstance(page.get("sections"), list) else []

    articles = []
    for item in items[:safe_limit]:
        if not isinstance(item, dict):
            continue
        blog_id = str(item.get("id") or "")
        blog_url = f"{MEDIASCRIBE_BASE_URL}/blog/{blog_id}" if blog_id else None
        articles.append(
            {
                "blog_id": blog_id,
                "title": item.get("title"),
                "mediascribe_section": item.get("section_name"),
                "blog_url": blog_url,
                "source_url": item.get("source_url"),
                "preview": item.get("preview"),
                "updated_at": item.get("updated_at"),
                "languages": item.get("languages"),
                "citation": citation(
                    item.get("title"),
                    blog_url,
                    item.get("source_url"),
                    item.get("section_name"),
                ),
            }
        )

    return compact_json(
        {
            "total": page.get("total"),
            "articles": articles,
            "sections": [
                {
                    "id": section.get("id"),
                    "name": section.get("name"),
                    "count": section.get("count"),
                }
                for section in sections
                if isinstance(section, dict)
            ],
        }
    )


@tool
def get_mediascribe_article(blog_id: str, language: str = "en") -> str:
    """Fetch a specific Mediascribe article by blog_id from /api/public/blogs/{blog_id}."""
    status("Sifting through files...")
    safe_blog_id = str(blog_id).strip()
    if not safe_blog_id:
        return compact_json({"error": "blog_id is required"})

    blog = fetch_blog_api(safe_blog_id)
    return compact_json(article_payload(blog, language))


@tool
def inspect_mediascribe_blog_page(blog_id: str) -> str:
    """Confirm that a Mediascribe article exists and return lightweight API metadata."""
    status("Sifting through files...")
    safe_blog_id = str(blog_id).strip()
    if not safe_blog_id:
        return compact_json({"error": "blog_id is required"})

    blog = fetch_blog_api(safe_blog_id)
    blog_url = f"{MEDIASCRIBE_BASE_URL}/blog/{blog.get('id')}"
    languages = blog.get("languages") if isinstance(blog.get("languages"), list) else []

    return compact_json(
        {
            "blog_id": blog.get("id"),
            "title": blog.get("title"),
            "mediascribe_section": blog.get("section_name"),
            "blog_url": blog_url,
            "source_url": blog.get("source_url"),
            "languages": [
                item.get("language")
                for item in languages
                if isinstance(item, dict) and item.get("language")
            ],
            "updated_at": blog.get("updated_at"),
            "citation": citation(
                blog.get("title"),
                blog_url,
                blog.get("source_url"),
                blog.get("section_name"),
            ),
        }
    )


def build_llm():
    return ChatOpenAI(
        base_url=os.getenv("OPENAI_BASE_URL", DEFAULT_MODEL_BASE_URL),
        api_key=os.getenv("OPENAI_API_KEY", "testing"),
        model=os.getenv("OPENAI_MODEL", DEFAULT_MODEL_NAME),
        temperature=float(os.getenv("OPENAI_TEMPERATURE", "0.2")),
    )


ARCHITECTURE_PATTERN_CATALOG = """
Use this as an architecture pattern checklist when designing systems. Treat it as architectural vocabulary and decision scaffolding; Mediascribe tool results remain the source for sourced factual claims.

Request path and edge patterns:
- Browser/Mobile Client -> DNS -> CDN -> WAF -> API Gateway -> Load Balancer -> App Service -> Cache -> Database.
- Browser/Mobile Client -> CDN -> Object Storage for static media delivery.
- Mobile Client -> API Gateway -> Auth Middleware -> Rate Limiter -> Backend-for-Frontend -> Domain Services.
- Edge Cache -> Regional Cache -> Service Cache -> Database for layered caching.
- DNS routing: latency-based routing, weighted routing, geo routing, failover routing.
- Anycast edge routing for globally distributed ingress.
- WAF rules for OWASP threats, bot protection, IP reputation, country blocking, request-size limits.
- TLS termination at CDN, gateway, or load balancer; re-encryption to origin when needed.
- Static asset versioning, immutable cache keys, cache-busting manifests.
- Image/video optimization: thumbnails, adaptive bitrate, transcoding, signed URLs, range requests.

Load balancer and gateway patterns:
- L4 load balancer for TCP-level routing and high-throughput pass-through.
- L7 load balancer for HTTP routing, host/path routing, header routing, cookie routing.
- Reverse proxy for termination, compression, buffering, retries, and upstream routing.
- API gateway for auth, rate limits, request validation, API keys, quotas, routing, observability.
- Backend-for-Frontend for web/mobile-specific aggregation.
- Service mesh for mTLS, retries, traffic splitting, circuit breaking, and service-to-service telemetry.
- Canary releases, blue/green deployments, weighted traffic shifting, shadow traffic.
- Health checks: startup, readiness, liveness, deep dependency checks.
- Connection draining and graceful shutdown to avoid dropped requests.
- Sticky sessions only when unavoidable; prefer stateless services.

Rate limiting, fairness, and abuse controls:
- Token bucket for burst-tolerant request limits.
- Leaky bucket for steady egress shaping.
- Fixed window, sliding window log, and sliding window counter rate limiters.
- Per-user, per-IP, per-token, per-tenant, and per-route quotas.
- Global rate limits plus local per-instance limiters.
- Distributed counters with Redis INCR/TTL or Lua scripts.
- Adaptive throttling under load.
- Admission control and overload protection.
- Backpressure instead of unbounded queues.
- Idempotency keys for retry-safe writes.
- Request deduplication and replay protection.

Reliability and failure-control patterns:
- Timeouts on every network call.
- Retries with exponential backoff and jitter.
- Retry budgets to avoid retry storms.
- Circuit breakers with closed/open/half-open states.
- Bulkheads to isolate resource pools and failure domains.
- Fallbacks and graceful degradation.
- Hedged requests for tail-latency reduction on idempotent reads.
- Deadlines propagated across service calls.
- Load shedding when latency or queue depth crosses thresholds.
- Poison message handling and dead-letter queues.
- Saga pattern for distributed workflows.
- Outbox pattern for reliable event publishing after database writes.
- Inbox pattern for idempotent event consumption.
- Compensating actions when distributed operations fail.
- Disaster recovery: RPO/RTO targets, backups, restores, failover drills.

Caching patterns:
- Browser cache, CDN cache, reverse proxy cache, application cache, distributed cache, database cache.
- Cache-aside: app reads cache, loads from DB on miss, then writes cache.
- Read-through cache: cache layer loads missing data.
- Write-through cache: write cache and backing store together.
- Write-behind cache: write cache first, asynchronously persist.
- Refresh-ahead cache for hot keys.
- TTL-based expiry, explicit invalidation, versioned keys.
- Negative caching for misses and not-found results.
- Request coalescing/singleflight to prevent duplicate cache fills.
- Cache stampede prevention: locks, probabilistic early expiration, stale-while-revalidate.
- Hot-key mitigation: key splitting, local cache, request collapsing.
- Eviction policies: LRU, LFU, FIFO, TTL.
- Redis patterns: counters, sorted sets, streams, pub/sub, locks, sessions, rate limits.

Database and persistence patterns:
- PostgreSQL as primary relational store for transactional data.
- PgBouncer for connection pooling and database connection protection.
- Read replicas for read scaling.
- Primary/replica topology with async or sync replication tradeoffs.
- Partitioning for large tables: range, hash, list partitioning.
- Sharding for horizontal write scaling.
- Consistent hashing for shard distribution and reduced key movement.
- CQRS: separate write model from read model.
- Materialized views and denormalized read models.
- Indexing strategy: B-tree, GIN, GiST, composite indexes, covering indexes, partial indexes.
- Query plan analysis, slow query logs, and cardinality checks.
- OLTP vs OLAP split.
- Search index for text search and relevance ranking.
- Time-series store for metrics/events.
- Object storage for blobs/media/backups.
- Graph store for deep relationship traversals when relational joins become painful.
- Document store for flexible object-shaped data when schema volatility justifies it.
- Consistency choices: strong consistency, read-your-writes, monotonic reads, eventual consistency.
- Transactions, optimistic locking, pessimistic locking, compare-and-swap.
- Migration patterns: expand/contract, backfill, dual-write carefully, cutover with verification.

Message queues, eventing, and async patterns:
- Message queue for decoupling slow work from request path.
- Event streaming for append-only logs and replayable events.
- Kafka/Pulsar-style topics for durable ordered streams.
- RabbitMQ/SQS-style queues for work distribution.
- Pub/sub for fanout notifications.
- Work queues for background jobs.
- Priority queues for urgent work.
- Delay queues and scheduled jobs.
- Dead-letter queues for failed messages.
- At-least-once delivery with idempotent consumers.
- Exactly-once as an end-to-end design goal only when every component supports it.
- Fanout/fanin, map/reduce-style background processing.
- Event sourcing for systems where event history is the source of truth.
- Change data capture for database-to-stream integration.
- Stream processing for windowed aggregations and real-time analytics.

Scaling patterns:
- Vertical scaling as a short-term lever.
- Horizontal scaling for stateless app services.
- Autoscaling by CPU, memory, queue depth, request rate, latency, or custom business metrics.
- Stateless services behind load balancers.
- Stateful services with partitioning, replication, and failover.
- Read scaling with replicas and caches.
- Write scaling with sharding, batching, queues, and partitioned ownership.
- Regional scaling: active/passive, active/active, cell-based architecture.
- Cell-based architecture to limit blast radius.
- Multi-tenant isolation: tenant-aware routing, quotas, per-tenant partitions.
- Hotspot detection and mitigation.
- Capacity planning using QPS, payload size, fanout factor, storage growth, and peak multiplier.

Application and service design patterns:
- Monolith first when scope is small; modular monolith when domain boundaries are emerging.
- Microservices when independent scaling, ownership, or deployment justifies operational cost.
- Domain-driven service boundaries.
- API composition for aggregating multiple services.
- Command/query separation for clean write/read flows.
- Workflow orchestration for long-running business processes.
- Choreography for loosely coupled event-driven services.
- Idempotent command handlers.
- Stateless app servers, externalized sessions.
- Configuration via environment and runtime config service.
- Feature flags for release control.

Security and identity patterns:
- OAuth2/OIDC with centralized identity provider.
- JWT for stateless auth when revocation constraints are acceptable.
- Server-side sessions for stronger revocation/control.
- mTLS between services.
- Secrets management and rotation.
- Least privilege IAM.
- Request signing for service-to-service or public API integrity.
- Audit logs for sensitive operations.
- PII encryption at rest and in transit.
- Row-level authorization and tenant isolation.
- CSRF protection for browser forms; CORS configured narrowly.

Observability and operations patterns:
- Structured logs with correlation/request IDs.
- Distributed tracing across gateway, services, queues, and databases.
- RED metrics: rate, errors, duration.
- USE metrics: utilization, saturation, errors.
- SLIs/SLOs/error budgets.
- Golden signals: latency, traffic, errors, saturation.
- Dashboards per service and per user journey.
- Alert on symptoms, not only causes.
- Synthetic checks and canaries.
- Audit trails for user/admin/security actions.
- Runbooks, rollback plans, incident timelines.
- Load testing, soak testing, chaos testing.

Data delivery and product patterns:
- Feed systems: fanout-on-write, fanout-on-read, hybrid fanout, celebrity problem handling.
- Timeline ranking: candidate generation, scoring, ranking, re-ranking, diversity constraints.
- Notification systems: preference checks, batching, dedupe, quiet hours, delivery receipts.
- Chat systems: WebSockets, long polling fallback, inbox/outbox, delivery/read receipts.
- File upload systems: direct-to-object-storage uploads, multipart upload, checksum validation.
- Search systems: indexing pipeline, inverted index, ranking, freshness, autocomplete.
- Recommendation systems: offline training, online serving, feature stores, feedback events.
- Analytics systems: event collection, stream ingestion, warehouse/lakehouse, dashboards.

Answer construction patterns:
- Always include an end-to-end request/data flow for system design questions.
- Name the critical path and remove non-critical work from it with queues.
- Identify source of truth per data type.
- State where caching sits and how invalidation works.
- State where rate limits and backpressure sit.
- State where retries/circuit breakers/timeouts sit.
- State how reads scale, how writes scale, and how failures degrade.
- Separate must-have baseline from later scale-out upgrades.
""".strip()


SYSTEM_PROMPT = f"""
You are a technical design architect.

Use Mediascribe as the source documentation for factual technical claims.
Mediascribe base URL: {MEDIASCRIBE_BASE_URL}

Tool policy:
- Before any final answer to a technical question, call research_mediascribe.
- You create the research queries yourself from the user's question.
- research_mediascribe accepts primary_query, related_queries, must_cover, min_good_articles, and max_articles.
- Write search queries as compact keyword phrases, not full sentences.
- Good query shape: 2 to 7 keywords focused on nouns, technologies, patterns, algorithms, constraints, and failure modes.
- Bad query shape: "architecture for a global social media platform with 500M MAU and high-volume media uploads and feed reads".
- Good query alternatives: "social media feed scaling", "media upload pipeline", "social graph sharding", "fanout write read", "CDN object storage".
- For normal technical questions, provide 6 to 10 varied searches and use max_articles=12 to 18.
- For broad or complex system design questions, provide 10 to 14 varied searches and use max_articles=18 to 24.
- Provide must_cover criteria that define what a useful article set must contain, such as "conflict resolution", "durable operation log", "database scaling", "failure modes", "observability", or problem-specific subsystems.
- Use min_good_articles=8 for normal questions and 12 to 16 for broad system design questions.
- Your searches should cover the exact topic, adjacent architecture concepts, implementation details, data models, critical-path flows, scaling bottlenecks, consistency/durability, reliability patterns, failure modes, observability, and rollout concerns when relevant.
- research_mediascribe searches Mediascribe, fetches full articles from /api/public/blogs/{{blog_id}}, scores article quality, keeps searching through candidates, and returns the strongest set it can find.
- If the first research result is too generic, too sparse, misses a major subsystem, or would lead to an answer below 90% confidence, call research_mediascribe a second time with sharper queries before answering.
- Do not answer from search previews alone.
- Do not rely on the first search result alone.
- Use list_mediascribe_landing_page when you need recent articles, sections, or landing-page context.
- Use get_mediascribe_article when the user names a specific blog_id or when research_mediascribe did not fetch enough detail.
- Use /api/search for search and /api/public/blogs/{{blog_id}} for full article detail through the provided tools.
- Use /blog/{{blog_id}} only as the public citation URL, not as the article content source.
- Do not claim Mediascribe says something unless it came from tool results.

Answer policy:
- Ask for missing requirements when they affect architecture choices.
- Give an architect's recommendation, not a generic article summary.
- Aim for staff-level design quality. A final answer should be good enough that a senior engineer would score it 90%+ for correctness, specificity, tradeoff awareness, and operational realism.
- Before finalizing, check whether the answer includes: requirements/assumptions, end-to-end flows, source-of-truth choices, read/write scaling, caching/invalidation, async processing, failure handling, consistency/durability, observability, rollout, and explicit tradeoffs. If important parts are weak, research more before answering.
- Use the architecture pattern catalog below as a checklist for practical design recommendations.
- For system design questions, include a concrete request/data flow such as Browser -> CDN -> WAF -> Gateway -> Load Balancer -> App -> Cache -> Database, adapted to the problem.
- Include relevant cross-cutting patterns: load balancing, gateways, rate limiting, retries, timeouts, circuit breakers, caching, queues, database pooling, scaling, observability, and failure modes.
- Do not start with "As an architect," or explain that you are acting as an architect.
- Pick a response shape that fits the question instead of repeating a fixed template.
- For broad concept questions, provide a concise architect brief: what it is, where it fits, when to use it, when to avoid it, and the operational concerns.
- For design-decision questions, lead with the decision, then give rationale, tradeoffs, risks, and implementation notes.
- For comparison questions, use a decision matrix with a clear recommendation.
- For review questions, lead with risks and gaps before recommendations.
- For short-answer requests, keep the answer short and skip formal sections unless they add clarity.
- Separate Mediascribe-sourced facts from your architectural judgment.
- End with a "Sources" section.
- In "Sources", cite only articles that directly support claims in the final answer.
- Prefer research_mediascribe.recommended_sources for citations.
- Use 3 to 8 sources maximum unless the user explicitly asks for exhaustive sourcing.
- Do not cite rejected articles, generic background articles, or articles you did not materially use.
- In "Sources", copy the citation field exactly for every Mediascribe article used.
- Be direct and production-oriented.
- State uncertainty when Mediascribe does not cover a requested detail.

Architecture pattern catalog:
{ARCHITECTURE_PATTERN_CATALOG}
""".strip()


def system_prompt_for_profile(profile: dict[str, Any]) -> str:
    return (
        f"{SYSTEM_PROMPT}\n\n"
        "Active complexity profile:\n"
        f"- complexity: {profile['name']}\n"
        f"- requested_complexity: {profile['requested']}\n"
        f"- query_limit: {profile['query_limit']}\n"
        f"- search_limit: {profile['search_limit']}\n"
        f"- max_articles: {profile['max_articles']}\n"
        f"- min_good_articles: {profile['min_good_articles']}\n"
        f"- max_fetch_attempts: {profile['max_fetch_attempts']}\n"
        f"- research_budget_seconds: {profile['research_budget_seconds']}\n"
        f"- output_mode: {profile['output_mode']}\n"
        f"- guidance: {profile['query_guidance']}\n"
        f"- output_guidance: {profile['output_guidance']}\n"
        f"- answer_requirements: {', '.join(profile['answer_requirements'])}\n"
        "- Match answer depth to this profile. For simple questions, answer directly "
        "after a small lookup. For max questions, produce a full architecture brief."
    )


def build_agent():
    return create_agent(
        model=build_llm(),
        tools=[
            research_mediascribe,
            list_mediascribe_landing_page,
            get_mediascribe_article,
            inspect_mediascribe_blog_page,
        ],
    )


def message_usage(message: Any) -> dict[str, int]:
    usage = getattr(message, "usage_metadata", None)
    if isinstance(usage, dict) and usage:
        return {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        }

    response_metadata = getattr(message, "response_metadata", None)
    token_usage = (
        response_metadata.get("token_usage")
        if isinstance(response_metadata, dict)
        else None
    )
    if isinstance(token_usage, dict) and token_usage:
        input_tokens = int(token_usage.get("prompt_tokens") or 0)
        output_tokens = int(token_usage.get("completion_tokens") or 0)
        total_tokens = int(
            token_usage.get("total_tokens") or input_tokens + output_tokens
        )
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }

    return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def message_tool_calls(message: Any) -> list[dict[str, Any]]:
    tool_calls = getattr(message, "tool_calls", None)
    if isinstance(tool_calls, list):
        return [
            {
                "id": call.get("id"),
                "name": call.get("name"),
                "args": call.get("args"),
            }
            for call in tool_calls
            if isinstance(call, dict)
        ]

    additional_kwargs = getattr(message, "additional_kwargs", None)
    raw_tool_calls = (
        additional_kwargs.get("tool_calls")
        if isinstance(additional_kwargs, dict)
        else None
    )
    if not isinstance(raw_tool_calls, list):
        return []

    calls = []
    for call in raw_tool_calls:
        if not isinstance(call, dict):
            continue
        function = (
            call.get("function") if isinstance(call.get("function"), dict) else {}
        )
        calls.append(
            {
                "id": call.get("id"),
                "name": function.get("name"),
                "args": function.get("arguments"),
            }
        )
    return calls


def tool_call_names(result: dict[str, Any]) -> list[str]:
    messages = result.get("messages", []) if isinstance(result, dict) else []
    names = []
    for message in messages:
        for call in message_tool_calls(message):
            name = call.get("name")
            if name:
                names.append(str(name))
    return names


def research_payloads(result: dict[str, Any]) -> list[dict[str, Any]]:
    messages = result.get("messages", []) if isinstance(result, dict) else []
    tool_call_names_by_id = {}
    payloads = []

    for message in messages:
        for call in message_tool_calls(message):
            call_id = call.get("id")
            call_name = call.get("name")
            if call_id and call_name:
                tool_call_names_by_id[str(call_id)] = str(call_name)

    for message in messages:
        message_type = getattr(message, "type", message.__class__.__name__)
        tool_call_id = getattr(message, "tool_call_id", None)
        if message_type != "tool" and not tool_call_id:
            continue

        name = getattr(message, "name", None)
        if not name and tool_call_id:
            name = tool_call_names_by_id.get(str(tool_call_id))
        if name != "research_mediascribe":
            continue

        try:
            payload = json.loads(str(getattr(message, "content", "") or "{}"))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payloads.append(payload)

    return payloads


def research_quality(result: dict[str, Any]) -> dict[str, Any]:
    payloads = research_payloads(result)
    solid_blog_ids = set()
    article_blog_ids = set()
    rejected_count = 0
    max_min_good = 0
    max_fetch_attempts = 0
    budget_exhausted = False

    for payload in payloads:
        research_budget = payload.get("research_budget")
        if isinstance(research_budget, dict):
            budget_exhausted = budget_exhausted or bool(
                research_budget.get("budget_exhausted")
            )

        requirements = payload.get("quality_requirements")
        if isinstance(requirements, dict):
            budget_exhausted = budget_exhausted or bool(
                requirements.get("budget_exhausted")
            )
            max_min_good = max(
                max_min_good, int(requirements.get("min_good_articles") or 0)
            )
            max_fetch_attempts = max(
                max_fetch_attempts, int(requirements.get("fetch_attempts") or 0)
            )

        for article in payload.get("articles", []):
            if not isinstance(article, dict):
                continue
            blog_id = article.get("blog_id")
            if blog_id:
                article_blog_ids.add(str(blog_id))
                if article.get("quality", {}).get("is_solid"):
                    solid_blog_ids.add(str(blog_id))

        rejected = payload.get("rejected_articles")
        if isinstance(rejected, list):
            rejected_count += len(rejected)

    target = max_min_good or int(active_research_profile()["min_good_articles"])
    return {
        "research_calls": len(payloads),
        "solid_articles": len(solid_blog_ids),
        "articles": len(article_blog_ids),
        "rejected_articles": rejected_count,
        "target_solid_articles": target,
        "max_fetch_attempts_seen": max_fetch_attempts,
        "budget_exhausted": budget_exhausted,
        "accepted": len(solid_blog_ids) >= target or budget_exhausted,
    }


def invoke_agent_with_required_research(
    agent: Any, prompt: str, profile: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    attempts = []
    user_message = prompt

    max_attempts = max(1, int(profile["enforcement_attempts"]))
    system_prompt = system_prompt_for_profile(profile)
    last_result = None
    for attempt in range(1, max_attempts + 1):
        result = agent.invoke(
            {
                "messages": [
                    SystemMessage(system_prompt),
                    HumanMessage(user_message),
                ],
            }
        )
        last_result = result
        names = tool_call_names(result)
        quality = research_quality(result)
        attempts.append(
            {
                "attempt": attempt,
                "tool_call_names": names,
                "research_quality": quality,
                "accepted": "research_mediascribe" in names and quality["accepted"],
            }
        )

        if "research_mediascribe" in names and quality["accepted"]:
            return result, attempts

        if "research_mediascribe" in names and attempt >= max_attempts:
            status("Research coverage is still thin; returning best available answer.")
            return result, attempts

        status("Research required: retrying with Mediascribe lookup...")
        if "research_mediascribe" in names:
            status(
                "Research coverage too thin: "
                f"solid_articles={quality['solid_articles']} "
                f"target={quality['target_solid_articles']}; retrying..."
            )
        user_message = (
            "Do not answer yet. First call research_mediascribe with your own "
            "primary_query, related_queries, and must_cover criteria for this "
            "user question. If prior research was thin, use different, sharper, "
            "more subsystem-specific searches. Pull enough full articles to meet "
            "the target solid article count before compiling the final answer.\n\n"
            f"Prior research quality: {compact_json(quality)}\n\n"
            f"User question: {prompt}"
        )

    if last_result is not None:
        return last_result, attempts

    raise RuntimeError(
        f"Agent did not call research_mediascribe after {max_attempts} attempts."
    )


def build_diagnostics(
    prompt: str,
    result: dict[str, Any],
    started_at: datetime,
    elapsed_seconds: float,
    complexity_profile: dict[str, Any],
    research_enforcement: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    messages = result.get("messages", []) if isinstance(result, dict) else []
    usage_records = []
    tool_calls = []
    tool_results = []
    message_trace = []
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    for index, message in enumerate(messages):
        usage = message_usage(message)
        if any(usage.values()):
            usage_records.append({"message_index": index, **usage})
            totals["input_tokens"] += usage["input_tokens"]
            totals["output_tokens"] += usage["output_tokens"]
            totals["total_tokens"] += usage["total_tokens"]

        calls = message_tool_calls(message)
        for call in calls:
            tool_calls.append({"message_index": index, **call})

        message_type = getattr(message, "type", message.__class__.__name__)
        content = getattr(message, "content", "")
        name = getattr(message, "name", None)
        tool_call_id = getattr(message, "tool_call_id", None)

        if message_type == "tool" or tool_call_id:
            tool_results.append(
                {
                    "message_index": index,
                    "name": name,
                    "tool_call_id": tool_call_id,
                    "content_chars": len(str(content)),
                    "content_preview": truncate(content, 1200),
                }
            )

        message_trace.append(
            {
                "index": index,
                "type": message_type,
                "name": name,
                "tool_call_id": tool_call_id,
                "content_chars": len(str(content)),
                "content_preview": truncate(content, 1200),
                "tool_calls": calls,
                "usage": usage if any(usage.values()) else None,
            }
        )

    return {
        "run": {
            "started_at": started_at.isoformat(),
            "elapsed_seconds": round(elapsed_seconds, 3),
            "prompt": prompt,
        },
        "configuration": {
            "mediascribe_base_url": MEDIASCRIBE_BASE_URL,
            "openai_base_url": os.getenv("OPENAI_BASE_URL", DEFAULT_MODEL_BASE_URL),
            "openai_model": os.getenv("OPENAI_MODEL", DEFAULT_MODEL_NAME),
            "temperature": float(os.getenv("OPENAI_TEMPERATURE", "0.2")),
            "complexity_profile": complexity_profile,
        },
        "usage_totals": totals,
        "usage_records": usage_records,
        "research_enforcement": research_enforcement or [],
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "message_trace": message_trace,
    }


def write_diagnostics(diagnostics: dict[str, Any]) -> Path:
    DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = DIAGNOSTICS_DIR / f"agent-run-{timestamp}.json"
    path.write_text(
        json.dumps(diagnostics, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


def parse_cli_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Mediascribe-backed architecture agent."
    )
    parser.add_argument(
        "--complexity",
        choices=["auto", "simple", "standard", "deep", "max"],
        default=None,
        help="Override automatic research complexity.",
    )
    parser.add_argument(
        "--output-mode",
        choices=list(OUTPUT_MODES.keys()),
        default=None,
        help="Control final answer shape.",
    )
    parser.add_argument(
        "--research-budget-seconds",
        type=int,
        default=None,
        help="Override the selected profile's research time budget.",
    )
    parser.add_argument("prompt", nargs="*", help="Architecture question.")
    return parser.parse_args(argv)


def read_prompt(args: argparse.Namespace) -> str:
    if args.prompt:
        return " ".join(args.prompt).strip()

    if not sys.stdin.isatty():
        return sys.stdin.read().strip()

    return input("Architecture question: ").strip()


def main() -> int:
    global CURRENT_RESEARCH_PROFILE

    args = parse_cli_args(sys.argv[1:])
    prompt = read_prompt(args)
    if not prompt:
        print("Provide an architecture question.")
        return 2

    complexity_profile = select_research_profile(
        prompt,
        requested_complexity=args.complexity,
        output_mode=args.output_mode,
        research_budget_seconds=args.research_budget_seconds,
    )
    CURRENT_RESEARCH_PROFILE = complexity_profile
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    status("Sifting through files...")
    status(f"Complexity: {complexity_profile['name']}")
    status(f"Output mode: {complexity_profile['output_mode']}")
    status(f"Research budget: {complexity_profile['research_budget_seconds']}s")
    agent = build_agent()
    result, research_enforcement = invoke_agent_with_required_research(
        agent, prompt, complexity_profile
    )
    elapsed_seconds = time.perf_counter() - started
    diagnostics = build_diagnostics(
        prompt,
        result,
        started_at,
        elapsed_seconds,
        complexity_profile=complexity_profile,
        research_enforcement=research_enforcement,
    )
    diagnostics_path = write_diagnostics(diagnostics)
    usage = diagnostics["usage_totals"]
    status(
        "Diagnostics: "
        f"input_tokens={usage['input_tokens']} "
        f"output_tokens={usage['output_tokens']} "
        f"total_tokens={usage['total_tokens']} "
        f"tool_calls={len(diagnostics['tool_calls'])}"
    )
    status(f"Diagnostics written to {diagnostics_path}")
    print(clean_final_answer(result["messages"][-1].content))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
