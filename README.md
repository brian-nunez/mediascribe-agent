# mediascribe-agent

- Technical design architect agent backed by Mediascribe.
- Landing page context comes from `https://mediascribe.b8z.me/`.
- Search uses `https://mediascribe.b8z.me/api/search`.
- Full article detail uses `https://mediascribe.b8z.me/api/public/blogs/{blog_id}`.
- Public citations still use `https://mediascribe.b8z.me/blog/{blog_id}`.

## Run

```bash
uv run python main.py "Design a Redis-backed rate limiter"
```

```bash
echo "Design a cache strategy for a social feed" | uv run python main.py
```

## Configuration

- `MEDIASCRIBE_BASE_URL`: defaults to `https://mediascribe.b8z.me`
- `MEDIASCRIBE_TIMEOUT_SECONDS`: defaults to `20`
- `MEDIASCRIBE_REQUEST_RETRIES`: defaults to `4`
- `MEDIASCRIBE_RETRY_BASE_SECONDS`: defaults to `1.5`
- `OPENAI_BASE_URL`: defaults to `http://10.0.0.119:8080/v1`
- `OPENAI_API_KEY`: defaults to `testing`
- `OPENAI_MODEL`: defaults to `ggml-org/gemma-4-E2B-it-GGUF:Q8_0`
- `OPENAI_TEMPERATURE`: defaults to `0.2`
- `AGENT_DIAGNOSTICS_DIR`: defaults to `diagnostics`
- `AGENT_PROGRESS_STREAM`: defaults to `stdout`; set to `stderr` if you want progress separate from the final answer.
- `AGENT_RESEARCH_QUERY_LIMIT`: defaults to `20`
- `AGENT_RESEARCH_SEARCH_LIMIT`: defaults to `20`
- `AGENT_RESEARCH_DEFAULT_ARTICLES`: defaults to `24`
- `AGENT_RESEARCH_MAX_ARTICLES`: defaults to `32`
- `AGENT_RESEARCH_MIN_GOOD_ARTICLES`: defaults to `12`
- `AGENT_RESEARCH_MIN_QUALITY_SCORE`: defaults to `0.35`
- `AGENT_RESEARCH_MAX_FETCH_ATTEMPTS`: defaults to `80`
- `AGENT_RESEARCH_ARTICLE_DELAY_SECONDS`: defaults to `0.25`
- `AGENT_RESEARCH_ENFORCEMENT_ATTEMPTS`: defaults to `4`

## Tools

- `research_mediascribe(primary_query, related_queries, max_articles, min_good_articles, must_cover, language)`: runs multiple Mediascribe searches, deduplicates results, fetches full articles from `/api/public/blogs/{blog_id}`, scores article quality, and keeps searching through candidates until it finds a strong set or exhausts the fetch budget.
- `list_mediascribe_landing_page(limit)`: reads the landing page feed and section metadata.
- `get_mediascribe_article(blog_id, language)`: fetches `/api/public/blogs/{blog_id}` and returns full article markdown plus citations.
- `inspect_mediascribe_blog_page(blog_id)`: checks article metadata and available languages.

## Research Behavior

- The model sees the user question first.
- The model must call `research_mediascribe` before answering technical questions.
- The model creates the primary and related search queries for `research_mediascribe`.
- The model also supplies `must_cover` criteria so research can evaluate whether articles are actually useful.
- Broad architecture questions should use 10 to 14 query angles and fetch 18 to 24 full articles.
- The CLI rejects attempts that answer without `research_mediascribe` and retries with a stricter instruction.
- The CLI also rejects thin research when the tool returns fewer solid articles than requested, then forces another pass with sharper model-created queries.
- Research runs the model-created searches instead of trusting the first query.
- Research fetches article content, scores quality, accepts solid articles, and continues through candidates when an article is weak.
- Progress output includes each lookup query and each selected article title.
- The research pass fetches full article markdown for the best unique results.
- The diagnostics file stores retry attempts under `research_enforcement`.
- This is tuned for large-context runs. Expect much higher token usage than earlier versions.

## Response Style

- The agent should respond like a technical design architect without literally saying `As an architect,`.
- Output shape adapts to the question instead of reusing one fixed template.
- Design answers prioritize decisions, fit, tradeoffs, risks, and implementation details.
- Mediascribe facts are separated from architectural judgment.
- The agent is instructed to research again if the answer would not meet roughly 90% confidence for correctness, specificity, tradeoffs, and operational realism.
- System design answers use a broad architecture pattern catalog.
- Expected coverage includes request/data flow, CDNs, WAFs, gateways, load balancers, rate limiters, retries, timeouts, circuit breakers, caches, queues, PgBouncer, databases, scaling, observability, and failure modes.
- Example flow: `Browser -> CDN -> WAF -> Gateway -> Load Balancer -> App -> Cache -> Database`.

## Diagnostics

- Every run writes `diagnostics/agent-run-<timestamp>.json`.
- The file includes token usage reported by the model provider, tool calls, tool results, message previews, timing, and runtime configuration.
- Token counts are `0` when the configured OpenAI-compatible server does not return usage metadata.
