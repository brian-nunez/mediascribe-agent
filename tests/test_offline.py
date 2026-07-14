"""Offline unit tests for the pure (non-network) logic.

These never touch the Mediascribe API or the model server, so they run
anywhere. End-to-end behavior must be validated on a machine that can reach
both services.

    uv run python -m pytest -q
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import config, llm, research
from agent.mediascribe import Article


# --- config.classify_complexity -------------------------------------------

def test_classify_simple():
    assert config.classify_complexity("what is redis?") == "simple"
    assert config.classify_complexity("explain CAP theorem") == "simple"


def test_classify_deep_from_signals():
    prompt = "Design a global distributed real-time system for 500M users"
    assert config.classify_complexity(prompt) == "deep"


def test_classify_deep_from_length():
    prompt = " ".join(["scale"] * 70)
    assert config.classify_complexity(prompt) == "deep"


def test_classify_max_from_length():
    prompt = " ".join(["subsystem"] * 130)
    assert config.classify_complexity(prompt) == "max"


def test_classify_max_from_signals():
    prompt = (
        "Design a global distributed real-time multi-region system that must "
        "survive regional failure with disaster recovery and rollout"
    )
    assert config.classify_complexity(prompt) == "max"


def test_all_profiles_have_answer_tokens():
    from agent import synthesize
    for name in config.PROFILES:
        assert name in synthesize._MAX_TOKENS


def test_classify_standard_default():
    assert config.classify_complexity("how do i throttle a queue consumer") == "standard"


def test_profile_for_respects_override():
    assert config.profile_for("what is redis?", "deep").name == "deep"
    assert config.profile_for("design a huge system", "auto").name in config.PROFILES


# --- llm.extract_string_list ----------------------------------------------

def test_extract_json_array():
    text = 'Here you go: ["redis cluster", "sharding", "replication"]'
    assert llm.extract_string_list(text) == ["redis cluster", "sharding", "replication"]


def test_extract_fenced_json():
    text = "```json\n[\"a\", \"b\"]\n```"
    assert llm.extract_string_list(text) == ["a", "b"]


def test_extract_bullet_fallback():
    text = "- rate limiting\n- token bucket\n* redis counters"
    assert llm.extract_string_list(text) == [
        "rate limiting",
        "token bucket",
        "redis counters",
    ]


def test_extract_dedup_and_cap():
    text = '["x", "x", "y", "z", "w"]'
    assert llm.extract_string_list(text, max_items=2) == ["x", "y"]


def test_extract_empty():
    assert llm.extract_string_list("") == []


# --- research.keywords -----------------------------------------------------

def test_keywords_strips_stopwords():
    kw = research.keywords("How do I scale a Redis cluster for high throughput?")
    assert "redis" in kw and "cluster" in kw
    assert "how" not in kw and "the" not in kw


def test_keywords_dedup_and_limit():
    kw = research.keywords("redis redis redis cluster", limit=2)
    assert kw == ["redis", "cluster"]


# --- research.split_sections ----------------------------------------------

def test_split_sections():
    md = "# Intro\nhello\n\n## Details\nworld\nmore"
    sections = research.split_sections(md)
    headings = [h for h, _ in sections]
    assert "Intro" in headings and "Details" in headings


def test_split_sections_no_headings():
    sections = research.split_sections("just a paragraph")
    assert len(sections) == 1


# --- research.score_article -----------------------------------------------

def _article(title: str, markdown: str) -> Article:
    return Article(
        blog_id="b1",
        title=title,
        section="Test",
        source_url="http://x",
        blog_url="http://x/blog/b1",
        language="en",
        markdown=markdown,
        updated_at="",
    )


def test_score_article_relevant_beats_irrelevant():
    terms = {"redis", "cache", "scaling", "cluster"}
    good = _article(
        "Redis Cluster Scaling",
        ("Redis is a cache. " * 40) + " cluster scaling sharding replication",
    )
    bad = _article("Cooking Pasta", "boil water and add salt " * 40)
    assert research.score_article(good, terms) > research.score_article(bad, terms)


def test_score_article_empty_terms():
    assert research.score_article(_article("x", "y"), set()) == 0.0


# --- research.best_snippets -----------------------------------------------

def test_best_snippets_prefers_on_topic_section():
    md = "# Off topic\nbananas and apples\n\n# Redis\nredis cache cluster scaling details"
    terms = {"redis", "cache", "cluster", "scaling"}
    snippets = research.best_snippets(_article("t", md), terms, count=1, max_chars=200)
    assert "redis" in snippets[0].lower()


def test_best_snippets_respects_max_chars():
    md = "# H\n" + ("word " * 500)
    snippets = research.best_snippets(_article("t", md), {"word"}, count=1, max_chars=100)
    # Excerpt is bounded by max_chars; a short "H: " heading label may prefix it.
    assert len(snippets[0]) <= 100 + len("H: ") + 1


# --- research._fit_budget --------------------------------------------------

def test_fit_budget_drops_sources_when_over():
    sources = [
        research.Source(
            source_id="",
            article=_article(f"Article {i}", "x"),
            score=1.0 - i * 0.1,
            snippets=["y " * 400],
        )
        for i in range(5)
    ]
    fitted = research._fit_budget(list(sources), budget=200)
    assert 1 <= len(fitted) < 5


def test_fit_budget_keeps_at_least_one():
    sources = [
        research.Source(
            source_id="",
            article=_article("Big", "z"),
            score=1.0,
            snippets=["huge " * 5000],
        )
    ]
    assert len(research._fit_budget(sources, budget=10)) == 1


# --- config.estimate_tokens ------------------------------------------------

def test_estimate_tokens():
    assert config.estimate_tokens("") == 0
    assert config.estimate_tokens("abcd") == 1
    assert config.estimate_tokens("a" * 400) == 100
