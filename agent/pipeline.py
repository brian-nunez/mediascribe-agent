"""Orchestration: plan -> gather evidence -> synthesize -> answer.

Two model calls total (query planning + synthesis), each with small bounded
context. No enforcement loop, no re-research. Writes a compact diagnostics file
per run.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config, research, synthesize
from .config import Profile
from .util import progress


@dataclass
class RunResult:
    answer: str
    profile: Profile
    diagnostics_path: str
    usage: dict


def _ensure_sources_section(answer: str, evidence: research.EvidencePack) -> str:
    """If the model cited [S#] markers but forgot a Sources section, append one
    listing exactly the cited sources."""
    cited = set(re.findall(r"\[S(\d+)\]", answer))
    if not cited:
        return answer
    if re.search(r"(?im)^\s*#*\s*sources\b", answer):
        return answer
    lines = [
        f"{source.source_id}: {source.article.citation}"
        for source in evidence.sources
        if source.source_id[1:] in cited
    ]
    if not lines:
        return answer
    return answer.rstrip() + "\n\nSources:\n" + "\n".join(lines)


def run(prompt: str, requested_complexity: str | None = None) -> RunResult:
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc)

    profile = config.profile_for(prompt, requested_complexity)
    progress(f"complexity: {profile.name}")
    progress(f"evidence budget: ~{profile.evidence_token_budget} tokens")

    evidence = research.gather_evidence(prompt, profile)

    progress("writing answer")
    synth = synthesize.synthesize(prompt, evidence, profile)
    answer = _ensure_sources_section(synth.content.strip(), evidence)

    elapsed = round(time.perf_counter() - started, 3)
    usage = {
        "synthesis_input_tokens": synth.usage.get("input_tokens", 0),
        "synthesis_output_tokens": synth.usage.get("output_tokens", 0),
        "evidence_tokens_estimated": evidence.token_estimate,
    }

    diagnostics = {
        "run": {
            "started_at": started_at.isoformat(),
            "elapsed_seconds": elapsed,
            "prompt": prompt,
        },
        "configuration": {
            "mediascribe_base_url": config.MEDIASCRIBE_BASE_URL,
            "model_base_url": config.MODEL_BASE_URL,
            "model": config.MODEL_NAME,
            "complexity": profile.name,
            "evidence_token_budget": profile.evidence_token_budget,
        },
        "research": evidence.stats,
        "sources": [
            {
                "source_id": source.source_id,
                "blog_id": source.article.blog_id,
                "title": source.article.title,
                "section": source.article.section,
                "score": source.score,
                "snippet_count": len(source.snippets),
            }
            for source in evidence.sources
        ],
        "usage": usage,
        "answer_chars": len(answer),
    }
    path = _write_diagnostics(diagnostics)
    progress(
        f"done in {elapsed}s | evidence ~{
            usage['evidence_tokens_estimated']} tok "
        f"| synth in={usage['synthesis_input_tokens']} out={
            usage['synthesis_output_tokens']
        }"
    )
    progress(f"diagnostics: {path}")

    return RunResult(
        answer=answer,
        profile=profile,
        diagnostics_path=str(path),
        usage=usage,
    )


def _write_diagnostics(diagnostics: dict) -> Path:
    directory = Path(config.DIAGNOSTICS_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"agent-run-{stamp}.json"
    path.write_text(json.dumps(diagnostics, indent=2,
                    ensure_ascii=False), "utf-8")
    return path
