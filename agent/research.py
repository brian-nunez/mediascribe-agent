"""Deterministic research: plan queries, search, fetch, rank, and build a
small token-budgeted evidence pack for the model.

This is the heart of the redesign. Instead of handing a 2B model a mega-tool
and a 50KB JSON blob, the pipeline itself does the searching and the model only
ever sees a compact, ranked, size-capped set of snippets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config, llm
from .config import Profile
from .mediascribe import Article, SearchHit, fetch_article, search
from .util import progress, truncate

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "for",
    "from", "how", "i", "if", "in", "into", "is", "it", "its", "me", "my", "need",
    "of", "on", "or", "so", "that", "the", "then", "there", "this", "to", "up",
    "use", "using", "want", "was", "we", "what", "when", "where", "which", "who",
    "why", "will", "with", "you", "your", "give", "design", "build", "make",
    "assume", "system", "app", "application", "like", "must", "should", "have",
    "has", "get", "info", "would", "could", "also", "very", "high", "low",
}

_WORD = re.compile(r"[a-z0-9]+(?:[-+][a-z0-9]+)*")


def keywords(text: str, limit: int = 12) -> list[str]:
    """Salient keywords in first-seen order, stopwords removed."""
    out: list[str] = []
    seen: set[str] = set()
    for token in _WORD.findall(text.lower()):
        if len(token) <= 2 or token in _STOPWORDS or token.isdigit():
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= limit:
            break
    return out


# --- Query planning --------------------------------------------------------

_QUERY_SYSTEM = (
    "You turn an engineering question into short keyword search queries for a "
    "technical documentation search engine. Reply with ONLY a JSON array of "
    "strings. Each query is 2-6 keywords (nouns, technologies, patterns), not a "
    "sentence. No prose, no explanation."
)


def _deterministic_queries(prompt: str) -> list[str]:
    terms = keywords(prompt, limit=8)
    queries: list[str] = []
    if terms:
        queries.append(" ".join(terms[:5]))
    if len(terms) > 3:
        queries.append(" ".join(terms[:3]))
    return queries


def plan_queries(prompt: str, profile: Profile) -> list[str]:
    """Combine deterministic keyword queries with a small model proposal.

    The deterministic queries guarantee we always have something searchable
    even if the model returns garbage; the model queries add topic-shaped
    angles. Model queries are preferred, capped to the profile.
    """
    deterministic = _deterministic_queries(prompt)

    model_queries: list[str] = []
    try:
        result = llm.chat(
            messages=[
                {"role": "system", "content": _QUERY_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Give up to {profile.num_queries} keyword search "
                        f"queries for this question:\n\n{truncate(prompt, 1500)}"
                    ),
                },
            ],
            temperature=0.3,
            max_tokens=256,
        )
        model_queries = llm.extract_string_list(
            result.content, max_items=profile.num_queries
        )
    except Exception as exc:  # network / server issues must not kill research
        progress(f"query planning fell back to keywords ({exc})")

    ordered: list[str] = []
    seen: set[str] = set()
    for query in model_queries + deterministic:
        norm = " ".join(keywords(query, limit=8)) or query.strip().lower()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        ordered.append(query.strip())
        if len(ordered) >= profile.num_queries:
            break

    if not ordered:  # absolute last resort
        ordered = [prompt.strip()[:120]]
    return ordered


# --- Markdown sectioning + scoring -----------------------------------------

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")


def split_sections(markdown: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, body) sections."""
    sections: list[tuple[str, str]] = []
    heading = ""
    body: list[str] = []

    def flush() -> None:
        text = "\n".join(body).strip()
        if heading or text:
            sections.append((heading, text))

    for line in markdown.splitlines():
        match = _HEADING.match(line)
        if match:
            flush()
            heading = match.group(1).strip()
            body = []
        else:
            body.append(line)
    flush()
    return sections or [("", markdown.strip())]


def _overlap(text: str, terms: set[str]) -> int:
    if not terms:
        return 0
    lowered = text.lower()
    return sum(1 for term in terms if term in lowered)


def score_article(article: Article, terms: set[str]) -> float:
    """Simple, transparent relevance score in [0, 1]."""
    if not terms:
        return 0.0
    markdown = article.markdown or ""
    title_hits = _overlap(article.title, terms)
    body_hits = _overlap(markdown, terms)
    term_score = min(body_hits / len(terms), 1.0)
    title_score = min(title_hits / max(min(len(terms), 5), 1), 1.0)
    length_score = min(len(markdown) / 2500, 1.0)
    return round(0.55 * term_score + 0.20 * title_score + 0.25 * length_score, 4)


def best_snippets(article: Article, terms: set[str], count: int, max_chars: int) -> list[str]:
    """Pick the most on-topic sections and return short excerpts."""
    scored: list[tuple[int, str, str]] = []
    for heading, body in split_sections(article.markdown or ""):
        if not body:
            continue
        hits = _overlap(f"{heading}\n{body}", terms)
        scored.append((hits, heading, body))
    scored.sort(key=lambda item: item[0], reverse=True)

    snippets: list[str] = []
    for hits, heading, body in scored[: max(count, 1)]:
        if hits == 0 and snippets:
            break
        excerpt = truncate(" ".join(body.split()), max_chars)
        label = f"{heading}: " if heading else ""
        snippets.append(f"{label}{excerpt}".strip())
    if not snippets:
        snippets = [truncate(" ".join((article.markdown or "").split()), max_chars)]
    return snippets


# --- Evidence pack ---------------------------------------------------------


@dataclass
class Source:
    source_id: str
    article: Article
    score: float
    snippets: list[str]

    def to_text(self) -> str:
        lines = [f"[{self.source_id}] {self.article.title or 'Untitled'}"]
        if self.article.section:
            lines[0] += f" ({self.article.section})"
        for snippet in self.snippets:
            lines.append(f"  - {snippet}")
        return "\n".join(lines)


@dataclass
class EvidencePack:
    sources: list[Source]
    queries: list[str]
    stats: dict = field(default_factory=dict)

    def to_prompt_text(self) -> str:
        if not self.sources:
            return "No Mediascribe evidence was found for this question."
        return "\n\n".join(source.to_text() for source in self.sources)

    def citations(self) -> list[str]:
        return [
            f"{source.source_id}: {source.article.citation}" for source in self.sources
        ]

    @property
    def token_estimate(self) -> int:
        return config.estimate_tokens(self.to_prompt_text())


def _fit_budget(sources: list[Source], budget: int) -> list[Source]:
    """Trim snippets, then drop weakest sources, until under the token budget.
    Always keeps at least one source."""

    def total() -> int:
        return config.estimate_tokens(
            "\n\n".join(source.to_text() for source in sources)
        )

    # First pass: shorten snippets uniformly if we're over.
    shrink = 0
    while sources and total() > budget and shrink < 4:
        for source in sources:
            source.snippets = [
                truncate(snippet, max(120, len(snippet) - 150))
                for snippet in source.snippets
            ]
        shrink += 1

    # Second pass: drop the lowest-scored source until we fit.
    while len(sources) > 1 and total() > budget:
        sources.pop()  # already sorted best-first
    return sources


def gather_evidence(prompt: str, profile: Profile) -> EvidencePack:
    """Full research pass. Never loops; returns the best available evidence
    within the profile's bounds."""
    terms = set(keywords(prompt, limit=14))

    progress("planning searches")
    queries = plan_queries(prompt, profile)

    # Search + dedup by blog_id, keeping the highest search score seen.
    candidates: dict[str, SearchHit] = {}
    for query in queries:
        progress(f"search: {query}")
        try:
            hits = search(query, profile.search_limit)
        except Exception as exc:
            progress(f"search failed ({exc})")
            continue
        for hit in hits:
            existing = candidates.get(hit.blog_id)
            if existing is None or hit.score > existing.score:
                candidates[hit.blog_id] = hit

    ranked_hits = sorted(candidates.values(), key=lambda h: h.score, reverse=True)

    # Fetch the top candidates and score them.
    scored: list[Source] = []
    fetched = 0
    for hit in ranked_hits:
        if fetched >= profile.fetch_articles:
            break
        progress(f"reading: {hit.title or hit.blog_id}")
        try:
            article = fetch_article(hit.blog_id)
        except Exception as exc:
            progress(f"skip article ({exc})")
            continue
        fetched += 1
        if len((article.markdown or "").strip()) < 400:
            continue  # empty / stub article, nothing to cite
        score = score_article(article, terms)
        scored.append(Source(source_id="", article=article, score=score, snippets=[]))

    scored.sort(key=lambda s: s.score, reverse=True)
    kept = scored[: profile.max_sources]

    # Build snippets for the kept sources.
    snippet_chars = max(160, profile.evidence_token_budget * config.CHARS_PER_TOKEN
                        // max(profile.max_sources * profile.snippets_per_article, 1))
    # Large-budget profiles get richer per-snippet excerpts, not just more sources.
    snippet_cap = 1400 if profile.evidence_token_budget >= 10_000 else 600
    for source in kept:
        source.snippets = best_snippets(
            source.article,
            terms,
            profile.snippets_per_article,
            max_chars=min(snippet_chars, snippet_cap),
        )

    kept = _fit_budget(kept, profile.evidence_token_budget)

    for index, source in enumerate(kept, start=1):
        source.source_id = f"S{index}"

    stats = {
        "queries": queries,
        "candidates_found": len(candidates),
        "articles_fetched": fetched,
        "sources_kept": len(kept),
        "top_score": kept[0].score if kept else 0.0,
        "evidence_tokens": config.estimate_tokens(
            "\n\n".join(source.to_text() for source in kept)
        ),
    }
    progress(
        f"evidence ready: {len(kept)} sources, "
        f"~{stats['evidence_tokens']} tokens"
    )
    return EvidencePack(sources=kept, queries=queries, stats=stats)
