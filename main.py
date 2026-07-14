"""Mediascribe architecture agent — CLI entry point.

Usage:
    uv run python main.py "Design a Redis-backed rate limiter"
    uv run python main.py --complexity simple "what is redis?"
    echo "Design a cache strategy for a social feed" | uv run python main.py

The agent researches Mediascribe (deterministically, in code) and then makes a
single model call to write the answer. See README.md for the design.
"""

from __future__ import annotations

import argparse
import sys

from agent import config
from agent.pipeline import run


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mediascribe-backed technical design architect agent."
    )
    parser.add_argument(
        "--complexity",
        choices=["auto", *config.PROFILES.keys()],
        default=None,
        help="Override automatic complexity detection (default: auto).",
    )
    parser.add_argument("prompt", nargs="*", help="The architecture question.")
    return parser.parse_args(argv)


def read_prompt(args: argparse.Namespace) -> str:
    if args.prompt:
        return " ".join(args.prompt).strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return input("Architecture question: ").strip()


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    prompt = read_prompt(args)
    if not prompt:
        print("Provide an architecture question.", file=sys.stderr)
        return 2

    try:
        result = run(prompt, requested_complexity=args.complexity)
    except Exception as exc:  # surface a clean error, not a traceback dump
        print(f"Agent failed: {exc}", file=sys.stderr)
        return 1

    print(result.answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
