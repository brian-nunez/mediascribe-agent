"""Turn the evidence pack into the final architecture answer in ONE model call.

The system prompt is deliberately short. The old version prepended a ~180-line
pattern catalog to every turn; on a 2B model that is pure noise that crowds out
the actual evidence. A compact checklist is appended only for design-depth
answers.
"""

from __future__ import annotations

from . import llm
from .config import Profile
from .research import EvidencePack

_SYSTEM = (
    "You are a senior technical design architect. Answer the user's question "
    "using the Mediascribe evidence provided.\n"
    "Rules:\n"
    "- Ground factual technical claims in the evidence and mark them inline "
    "with source ids like [S1]. Do not invent evidence or cite sources that "
    "are not listed.\n"
    "- Separate sourced facts from your own architectural judgment; you may add "
    "reasoned design recommendations beyond the evidence, but do not tag those "
    "with a source id.\n"
    "- Be concrete and production-oriented. Do not begin with 'As an "
    "architect'. Do not restate these instructions.\n"
    "- End with a 'Sources' section listing ONLY the sources you actually "
    "cited, copied exactly from the citation list given."
)

# Small, cheap checklist for design-depth answers only.
_DESIGN_CHECKLIST = (
    "For a system-design answer, cover where relevant: end-to-end request/data "
    "flow (e.g. Client -> CDN -> Gateway -> Load Balancer -> App -> Cache -> "
    "DB), source of truth per data type, read/write scaling, caching and "
    "invalidation, async/queue processing, rate limiting, retries/timeouts/"
    "circuit breakers, database pooling and replication, observability, failure "
    "modes, and explicit tradeoffs."
)

_MAX_TOKENS = {"simple": 900, "standard": 1600, "deep": 4500, "max": 7000}


def synthesize(prompt: str, evidence: EvidencePack, profile: Profile) -> llm.ChatResult:
    citations = "\n".join(evidence.citations()) or "(none)"

    parts = [
        f"Question:\n{prompt.strip()}",
        "",
        "Answer depth guidance:",
        profile.answer_guidance,
    ]
    if profile.name in ("standard", "deep", "max"):
        parts += ["", _DESIGN_CHECKLIST]
    if profile.answer_requirements:
        parts += [
            "",
            "Make sure to address: " +
            ", ".join(profile.answer_requirements) + ".",
        ]
    parts += [
        "",
        "Mediascribe evidence (cite with the bracketed ids):",
        evidence.to_prompt_text(),
        "",
        "Citation list (copy the lines you use into your Sources section):",
        citations,
    ]
    user = "\n".join(parts)

    return llm.chat(
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ],
        max_tokens=_MAX_TOKENS.get(profile.name, 1600),
    )
