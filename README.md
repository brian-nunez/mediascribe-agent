# mediascribe-agent

A technical design architect agent backed by the Mediascribe API, rebuilt to
run well on a **small local model with a limited context window** (e.g. Gemma
3n E2B on llama.cpp with 128k context).

## Why this was rewritten

The previous version wrapped a 2B model in a heavy agent framework and asked it
to drive research through a single mega-tool. That failed three ways:

- **Endless loops.** An enforcement loop demanded up to 16 "solid" articles and
  retried up to 4× when the target could not be met — which, for many topics,
  it never could. A broad prompt took ~9 minutes.
- **Context overflow.** One research call returned ~52 KB (~15k tokens) of
  nested scoring JSON; multiple calls per run pushed input past 80k tokens. A 2B
  model cannot reason over that regardless of the window size.
- **Weak answers.** Buried in noise, with an impossible quality gate, the model
  returned "best available" thin answers.

## The new design

Orchestration lives in **code**, not in the model. The model is asked to do two
small, bounded things only.

```
prompt
  │
  ├─ 1. plan_queries    (1 tiny model call + deterministic keyword fallback)
  │        → a few compact keyword queries
  │
  ├─ 2. gather_evidence (pure Python — no model)
  │        search → dedup → fetch top articles → score → rank
  │        → keep top K → extract best snippets
  │        → TOKEN-BUDGETED evidence pack (hard cap)
  │
  └─ 3. synthesize      (1 model call)
           short system prompt + compact evidence → final answer
```

Key properties:

- **Two model calls total.** No tool-calling loop, no re-research.
- **Bounded context.** The evidence pack has a hard token budget per profile
  (`simple` ~1.5k, `standard` ~4k, `deep` ~16k, `max` ~32k). Snippets are
  trimmed and weak sources dropped to stay under budget. The budget is a hard
  ceiling, not a target — small questions still produce small packs.
- **No impossible gates.** Research always returns the *best available*
  evidence within the profile bounds; it never loops trying to hit a fixed count.
- **Compact tool output.** The model sees ranked `[S1] title` + short snippets,
  never raw scoring internals (those stay in diagnostics).

## Run

```bash
uv run python main.py "Design a Redis-backed rate limiter"
uv run python main.py --complexity simple "what is redis?"
uv run python main.py --complexity deep "Design a global Instagram-style feed"
echo "Design a cache strategy for a social feed" | uv run python main.py
```

Progress lines go to **stderr**, the final answer to **stdout**, so you can
redirect the answer cleanly:

```bash
uv run python main.py "design instagram" > answer.md
```

## Configuration (environment variables)

| Variable | Default | Purpose |
| --- | --- | --- |
| `MEDIASCRIBE_BASE_URL` | `http://10.0.0.132:9595` | Mediascribe API base |
| `MEDIASCRIBE_TIMEOUT_SECONDS` | `20` | Per-request timeout |
| `MEDIASCRIBE_REQUEST_RETRIES` | `3` | Retries on 429/5xx |
| `OPENAI_BASE_URL` | `http://10.0.0.119:8080/v1` | Model server (OpenAI-compatible) |
| `OPENAI_API_KEY` | `testing` | Model server key |
| `OPENAI_MODEL` | `ggml-org/gemma-4-E2B-it-GGUF:Q8_0` | Model name |
| `OPENAI_TEMPERATURE` | `0.2` | Sampling temperature |
| `OPENAI_TIMEOUT_SECONDS` | `180` | Model call timeout |
| `AGENT_COMPLEXITY` | `auto` | `auto`, `simple`, `standard`, `deep` |
| `AGENT_PROGRESS_STREAM` | `stderr` | `stderr` or `stdout` |
| `AGENT_DIAGNOSTICS_DIR` | `diagnostics` | Where run diagnostics are written |

## Complexity profiles

| Profile | Queries | Fetch | Sources kept | Evidence budget |
| --- | --- | --- | --- | --- |
| `simple` | 2 | 3 | 2 | ~1.5k tokens |
| `standard` | 4 | 6 | 4 | ~4k tokens |
| `deep` | 8 | 12 | 10 | ~16k tokens |
| `max` | 12 | 18 | 14 | ~32k tokens |

`auto` classifies the prompt locally (length + design signals) before running:
short concept questions → `simple`, design decisions → `standard`, system-design
prompts → `deep`, and large multi-subsystem specs (≥120 words or many design
signals, like the Instagram / Google Docs prompts) → `max`.

Budgets are set here in `agent/config.py` — raise `evidence_token_budget`,
`max_sources`, or `fetch_articles` per profile if you want to push more into the
128k window.

## Diagnostics

Every run writes `diagnostics/agent-run-<timestamp>.json` with the chosen
profile, the queries used, research stats (candidates found, articles fetched,
sources kept, evidence token estimate), the kept sources, and model token usage.
The files are small (a few KB), unlike the previous multi-run dumps.

## Project layout

```
main.py                CLI entry point
agent/
  config.py            env + complexity profiles + token budgets
  mediascribe.py       Mediascribe HTTP client (search, fetch_article)
  llm.py               OpenAI-compatible chat client + robust list parsing
  research.py          query planning, search/fetch/rank, evidence pack
  synthesize.py        single-call answer generation
  pipeline.py          orchestration + diagnostics
  util.py              progress logging + text helpers
tests/
  test_offline.py      unit tests for the pure (non-network) logic
```

## Tests

Offline unit tests cover the logic that does not touch the network (keyword
extraction, scoring, snippet selection, token budgeting, complexity
classification, and the model-output list parser):

```bash
uv run python -m pytest -q
```

End-to-end behavior requires the Mediascribe API and the model server to be
reachable on your network.
