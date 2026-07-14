"""Configuration and complexity profiles.

Everything that used to be scattered across env vars and a 300-line profile
table lives here. The guiding principle for a small local model is: keep the
context the model sees SMALL and BOUNDED. Profiles therefore tune three things
only -- how many searches we run, how many articles we keep, and how many
tokens of evidence the model is allowed to see. There are no time budgets and
no "must find N solid articles" gates that can never be satisfied.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "")) if os.getenv(name) else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "")) if os.getenv(name) else default
    except ValueError:
        return default


# --- Mediascribe API -------------------------------------------------------
MEDIASCRIBE_BASE_URL = _env_str(
    "MEDIASCRIBE_BASE_URL", "http://10.0.0.132:9595"
).rstrip("/")
MEDIASCRIBE_TIMEOUT_SECONDS = _env_float("MEDIASCRIBE_TIMEOUT_SECONDS", 20.0)
MEDIASCRIBE_RETRIES = _env_int("MEDIASCRIBE_REQUEST_RETRIES", 3)
MEDIASCRIBE_RETRY_BASE_SECONDS = _env_float(
    "MEDIASCRIBE_RETRY_BASE_SECONDS", 1.5)

# --- Model (OpenAI-compatible server) --------------------------------------
MODEL_BASE_URL = _env_str(
    "OPENAI_BASE_URL", "http://10.0.0.119:8080/v1").rstrip("/")
MODEL_API_KEY = _env_str("OPENAI_API_KEY", "testing")
MODEL_NAME = _env_str("OPENAI_MODEL", "ggml-org/gemma-4-E2B-it-GGUF:Q8_0")
MODEL_TEMPERATURE = _env_float("OPENAI_TEMPERATURE", 0.2)
MODEL_TIMEOUT_SECONDS = _env_float("OPENAI_TIMEOUT_SECONDS", 180.0)

# The model's usable context window. We stay well under this on purpose: a 2B
# model reasons far better over a few thousand tokens than tens of thousands,
# even when the window technically allows more.
MODEL_CONTEXT_TOKENS = _env_int("OPENAI_CONTEXT_TOKENS", 128_000)

# --- Output -----------------------------------------------------------------
PROGRESS_STREAM = _env_str("AGENT_PROGRESS_STREAM", "stderr").lower()
DIAGNOSTICS_DIR = _env_str("AGENT_DIAGNOSTICS_DIR", "diagnostics")

# Rough token estimate. Good enough for budgeting; we never need exactness.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


@dataclass(frozen=True)
class Profile:
    """A complexity profile. Every field bounds the work we do."""

    name: str
    num_queries: int  # total search queries (deterministic + model)
    search_limit: int  # results requested per search
    fetch_articles: int  # max full articles to fetch and score
    max_sources: int  # articles kept in the evidence pack shown to model
    snippets_per_article: int  # evidence snippets kept per kept article
    evidence_token_budget: int  # hard cap on evidence-pack size (model input)
    answer_guidance: str
    answer_requirements: list[str] = field(default_factory=list)


PROFILES: dict[str, Profile] = {
    "simple": Profile(
        name="simple",
        num_queries=2,
        search_limit=8,
        fetch_articles=3,
        max_sources=2,
        snippets_per_article=2,
        evidence_token_budget=1_500,
        answer_guidance=(
            "Answer the concept directly and concisely: what it is, where it "
            "fits, when to use it, and the main tradeoffs. No heavy sections."
        ),
        answer_requirements=["direct answer",
                             "where it fits", "tradeoffs", "sources"],
    ),
    "standard": Profile(
        name="standard",
        num_queries=4,
        search_limit=10,
        fetch_articles=6,
        max_sources=4,
        snippets_per_article=2,
        evidence_token_budget=4_000,
        answer_guidance=(
            "Give a focused architecture answer: recommendation, the main "
            "request/data flow, key tradeoffs, and failure modes."
        ),
        answer_requirements=[
            "recommendation",
            "main flow",
            "key tradeoffs",
            "failure modes",
            "sources",
        ],
    ),
    "deep": Profile(
        name="deep",
        num_queries=8,
        search_limit=12,
        fetch_articles=12,
        max_sources=10,
        snippets_per_article=3,
        evidence_token_budget=16_000,
        answer_guidance=(
            "Produce a staff-level system-design brief: requirements/assumptions, "
            "end-to-end flow, source-of-truth choices, read/write scaling, "
            "caching and invalidation, async processing, failure handling, "
            "observability, and explicit tradeoffs. Keep it tight, not padded."
        ),
        answer_requirements=[
            "requirements/assumptions",
            "end-to-end flow",
            "source-of-truth choices",
            "scaling",
            "caching/invalidation",
            "async processing",
            "failure handling",
            "observability",
            "tradeoffs",
            "sources",
        ],
    ),
    "max": Profile(
        name="max",
        num_queries=12,
        search_limit=15,
        fetch_articles=18,
        max_sources=14,
        snippets_per_article=3,
        evidence_token_budget=32_000,
        answer_guidance=(
            "Produce a comprehensive, production-grade architecture for a "
            "large-scale, multi-subsystem system. Cover requirements/assumptions, "
            "capacity estimates, end-to-end request/data flows, per-subsystem "
            "deep dives, source-of-truth and consistency choices, read/write "
            "scaling, caching and invalidation, queues/events, rate limiting, "
            "retries/timeouts/circuit breakers, database pooling and sharding, "
            "regional failover, observability, failure modes, and a rollout "
            "plan. Be specific and decisive; avoid filler."
        ),
        answer_requirements=[
            "requirements/assumptions",
            "capacity estimates",
            "end-to-end flows",
            "per-subsystem deep dives",
            "consistency/durability",
            "scaling",
            "caching/invalidation",
            "queues/events",
            "failure handling",
            "regional failover",
            "observability",
            "rollout plan",
            "tradeoffs",
            "sources",
        ],
    ),
}

DEFAULT_COMPLEXITY = _env_str("AGENT_COMPLEXITY", "auto").strip().lower()


# Signals that a prompt is a big, multi-subsystem design task.
_DEEP_SIGNALS = (
    "design ",
    "architecture",
    "scale to",
    "million",
    "500m",
    "100m",
    "concurrent",
    "high throughput",
    "high-throughput",
    "distributed",
    "real-time",
    "real time",
    "globally",
    "global ",
    "rollout",
    "disaster recovery",
    "multi-region",
    "multi region",
)

# Signals that a prompt is a single-concept lookup.
_SIMPLE_SIGNALS = (
    "what is",
    "what's",
    "explain",
    "define",
    "difference between",
    "when should i use",
    "when to use",
    "pros and cons",
)


def classify_complexity(prompt: str) -> str:
    """Pick a profile name from the prompt using cheap local heuristics."""
    text = prompt.lower().strip()
    words = len(text.split())

    deep_hits = sum(1 for s in _DEEP_SIGNALS if s in text)
    simple_hits = sum(1 for s in _SIMPLE_SIGNALS if s in text)

    # A large, requirement-laden, multi-subsystem prompt is a "max" design task.
    if words >= 120 or deep_hits >= 6:
        return "max"
    # A long, requirement-laden prompt is almost always a deep design task.
    if words >= 60 or deep_hits >= 3:
        return "deep"
    if simple_hits and words <= 25 and deep_hits == 0:
        return "simple"
    if deep_hits >= 1:
        return "deep"
    return "standard"


def resolve_profile(requested: str | None) -> Profile:
    """Resolve a requested complexity (or 'auto') against a prompt later."""
    name = (requested or DEFAULT_COMPLEXITY or "auto").strip().lower()
    if name in PROFILES:
        return PROFILES[name]
    return PROFILES["standard"]


def profile_for(prompt: str, requested: str | None) -> Profile:
    name = (requested or DEFAULT_COMPLEXITY or "auto").strip().lower()
    if name == "auto" or name not in PROFILES:
        name = classify_complexity(prompt)
    return PROFILES[name]
