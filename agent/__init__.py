"""Mediascribe architecture agent.

A small, deterministic pipeline tuned for a local ~2B model with a limited
context window. Code drives search / fetch / rank; the model is only asked to
do two small, bounded things: propose search queries and write the final
answer. See README.md for the full design rationale.
"""

__all__ = ["config", "mediascribe", "llm", "research", "synthesize", "pipeline"]
