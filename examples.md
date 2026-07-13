```bash
uv run python main.py "If i have a process I need to throttle, how can I do that? I need to send info then recieve info in a high throughput app."

uv run python main.py "Design a global Instagram-style media platform for 500M monthly active users and 50M daily active users. Users can upload photos, short videos, like/comment/share posts, follow accounts,
  receive notifications, search users/posts, and view a ranked home feed.

  Assume:
  - 80% mobile traffic
  - 20K media uploads/sec peak
  - 2M feed reads/sec peak
  - P95 feed latency under 300ms
  - P95 media upload acknowledgement under 1s
  - Media must be globally available
  - Likes/comments can be eventually consistent
  - Account/profile/security data must be strongly consistent
  - Celebrity accounts can have 100M+ followers
  - System must survive regional failure

  Give me the architecture, request/data flows, storage choices, caching strategy, feed generation strategy, queue/event design, rate limiting, retries, circuit breakers, database scaling,
  PgBouncer/connection pooling, observability, failure modes, and rollout plan." > example-outputs/instagram-style.md

uv run python main.py "Design a global real-time collaborative document editor like Google Docs.

  Assume:
  - 100M monthly active users
  - 5M daily active users
  - 500K concurrent editing sessions at peak
  - 100 users can edit the same document concurrently
  - P95 local edit acknowledgement under 100ms
  - P95 remote edit propagation under 300ms
  - Documents must support text, comments, suggestions, presence, version history, permissions, sharing links, and offline edits
  - User/account/permission changes require strong consistency
  - Cursor presence and typing indicators can be eventually consistent
  - The system must survive regional failure
  - Documents must never lose committed edits

  Give me the architecture, request/data flows, conflict resolution strategy, storage choices, caching strategy, real-time sync design, message queue/event design, rate limiting, retries,
  circuit breakers, database scaling, PgBouncer/connection pooling, observability, failure modes, disaster recovery, and rollout plan." > example-outputs/google-docs-style.md
```
