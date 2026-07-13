Sifting through files...
Planning searches...
Knowledge lookup: architecture for large-scale social media feed generation and scaling
Knowledge lookup: eventual consistency vs strong consistency in distributed systems
Knowledge lookup: caching strategies for high-read social media platforms
Knowledge lookup: design patterns for handling high-throughput media uploads and processing
Knowledge lookup: distributed tracing and observability in microservices architecture
Knowledge lookup: sharding and partitioning strategies for user-generated content databases
Knowledge lookup: fanout-on-write vs fanout-on-read feed generation strategies
Knowledge lookup: rate limiting and backpressure in API gateways for high-traffic services
Evaluating research coverage...
Sifting through article: 7 Must-know Strategies to Scale Your Database
Sifting through article: Are you Using CAP Theorem Wrong?
Sifting through article: 8 Most Important System Design Concepts You Should Know
Sifting through article: System Design: Design YouTube
Sifting through article: How Event Driven Architecture Works (System Design)
Sifting through article: Trillions of Web Pages: Where Does Google Store Them?
Sifting through article: System Design Was HARD - Until You Knew the Trade-Offs, Part 2
Sifting through article: Stop Confusing API Gateway, Load Balancer And Reverse Proxy
Sifting through article: Top 12 Tips For API Security
Sifting through article: Application Load Balancer (ALB) Vs API Gateway // Pros Cons Comparison
Sifting through article: 20 System Design Concepts You Must Know - Final Part
Sifting through article: Distributed Transactions Explained: 2 Phase Commit vs Saga Pattern
Sifting through article: API Gateway vs Load Balancer
Sifting through article: APIs Explained in 6 Minutes!
Sifting through article: System Design Interview – BIGGEST Mistakes to Avoid
Sifting through article: KISS, SOLID, CAP, BASE: Important Terms You Might Not Know!
Sifting through article: Good APIs Vs Bad APIs: 7 Tips for API Design
Sifting through article: Polish Amazon Offers Deal So Good Their Servers Implode
Diagnostics: input_tokens=62930 output_tokens=5736 total_tokens=68666 tool_calls=1
Diagnostics written to diagnostics/agent-run-20260713T055609Z.json
This design outlines a globally distributed, highly available, and scalable architecture for a media platform targeting 500M MAU, focusing on decoupling high-throughput media ingestion from low-latency feed delivery.

The architecture is built upon a **Microservices** foundation, leveraging **Event-Driven Architecture (EDA)** for decoupling, and employing a **Hybrid Data Storage Model** to satisfy the conflicting requirements of strong consistency for user data and eventual consistency for social interactions.

---

## 1. Architectural Overview: Layered Design

The system is structured into four primary layers: the Edge Layer, the API/Ingress Layer, the Core Services Layer, and the Data/Processing Layer.

### End-to-End Request/Data Flow Diagram

The flow is split into two critical paths: **Media Ingestion (Write Path)** and **Feed Retrieval (Read Path)**.

**A. Media Ingestion (Write Path - High Throughput)**
1.  **Client $\rightarrow$ CDN/WAF:** Traffic hits the global edge, secured by WAF and TLS termination.
2.  **API Gateway:** Validates API keys, enforces rate limits, and routes the upload request.
3.  **Pre-signed URL Generation:** Gateway generates a temporary, secure URL for direct upload to Object Storage.
4.  **Client $\rightarrow$ Object Storage (S3/Blob):** Large media files are uploaded directly, bypassing application servers.
5.  **Storage Event:** Object Storage triggers an event (e.g., S3 Event Notification).
6.  **Message Broker (Kafka):** The event is published to a dedicated `media_upload_events` topic.
7.  **Processing Workers:** Dedicated worker services consume the event, initiate transcoding (DAG), and update metadata in the database.

**B. Feed Retrieval (Read Path - Low Latency)**
1.  **Client $\rightarrow$ CDN:** Requests for the home feed are served from the geographically closest edge cache.
2.  **API Gateway:** Validates user token (JWT) and applies user-specific rate limits.
3.  **Feed Service:**
    *   **Check Cache (Redis):** Attempts to retrieve the pre-computed feed for the user.
    *   **Cache Miss:** If the feed is missing, the service executes the **Fanout-on-Read** logic (fetch recent posts from followed users, aggregate, and rank).
    *   **Response:** Returns the ranked feed data.

---

## 2. Component Deep Dive and Technology Choices

### A. Edge and Ingress Layer
*   **CDN (Content Delivery Network):** Essential for global availability and low latency delivery of static assets (profile pictures, videos) and cached feed content.
*   **WAF (Web Application Firewall):** Deployed at the edge to mitigate common web threats (SQLi, XSS) and block known malicious IPs.
*   **Global DNS/Anycast:** Used for intelligent routing to direct users to the nearest regional data center, minimizing network latency.

### B. API/Ingress Layer (The Orchestrator)
*   **API Gateway (e.g., AWS API Gateway or Kong):** This is the single entry point for all client traffic.
    *   **Function:** Centralizes **Authentication (JWT validation)**, **Rate Limiting** (per-user/per-IP quotas), **Request Validation**, and **API Versioning**.
    *   **Role:** Decouples the client interface from the internal microservice topology.

### C. Core Services Layer (Microservices)
Services are designed around domain boundaries (User, Post, Follow, Notification, Search).

*   **User/Profile Service (Strong Consistency):**
    *   **Database:** **Sharded PostgreSQL** (or a similar strongly consistent RDBMS). Sharding by `User_ID` ensures that all user-specific data (profile, credentials) resides on a specific shard, optimizing for strong consistency and horizontal write scaling for user data.
    *   **Pooling:** **PgBouncer** is deployed in front of the PostgreSQL cluster to manage and pool database connections efficiently, preventing connection exhaustion under high load.
*   **Post/Interaction Service (Eventual Consistency):**
    *   **Database:** A distributed NoSQL store (e.g., Cassandra or DynamoDB) is suitable for high-volume, high-write operations like likes and comments, where **Eventual Consistency (BASE)** is acceptable.
*   **Search Service:** Utilizes a dedicated **Search Index** (e.g., Elasticsearch) fed by events from the Post Service to enable fast, full-text search capabilities.

### D. Data and Processing Layer
*   **Object Storage (S3/Blob):** The source of truth for all raw media files. Provides massive scalability and durability.
*   **Message Broker (Apache Kafka):** The central nervous system. Used for durable, ordered event streaming:
    *   `media_upload_events` (for transcoding triggers).
    *   `post_interaction_events` (for likes/comments).
*   **Transcoding Workers:** A fleet of containerized workers consuming the Kafka stream. They operate as a **Directed Acyclic Graph (DAG)** to parallelize video processing (e.g., generating 1080p, 720p, thumbnail versions simultaneously).

---

## 3. Critical Pattern Implementation Details

### A. Feed Generation Strategy (The Core Challenge)
Given the requirement for a ranked home feed and the existence of "Celebrity Accounts" (100M+ followers), a **Hybrid Fanout Strategy** is required:

1.  **Fanout-on-Write (For Celebrities/High-Follower Users):** When a celebrity posts, the system immediately pushes that post ID into the pre-computed feed caches of all their followers. This is computationally expensive but ensures the feed is instantly ready for the celebrity's audience (low read latency for high-value users).
2.  **Fanout-on-Read (For General Users):** For users with moderate follower counts, the feed is assembled on demand by querying the Post Service for recent posts from all followed accounts and then ranking them. This is simpler to manage but introduces higher latency for these users.
3.  **Caching:** The final ranked feed for every user is stored in a **Distributed Cache (Redis)** with a short TTL. This is the primary mechanism to meet the **P95 feed latency under 300ms** requirement.

### B. Reliability and Failure Control
*   **Retries & Circuit Breakers:** Implemented within the **Service Mesh** (if adopted) or within the API Gateway/Service clients.
    *   **Strategy:** Use **Exponential Backoff with Jitter** for transient network errors. Implement **Circuit Breakers** on all inter-service calls to prevent cascading failures when a downstream service becomes slow or unavailable.
*   **Saga Pattern (For Complex Workflows):** Used for multi-step processes like complex post moderation or payment flows. The **Orchestration Saga** pattern is recommended here to manage the state and execute precise compensating actions upon failure.
*   **Dead-Letter Queues (DLQs):** Every message queue (Kafka topic, SQS queue) must have a DLQ. Messages that fail processing after a defined number of retries are moved to the DLQ for manual inspection, ensuring no data is silently lost.

### C. Rate Limiting and Backpressure
*   **Rate Limiting:** Implemented at the **API Gateway** layer, using a **Token Bucket** algorithm, applied per authenticated user ID and per IP address. This protects the backend services from abuse and ensures fair usage.
*   **Backpressure:** Managed by the **Message Broker (Kafka)**. If the Transcoding Workers cannot keep up with the incoming `media_upload_events`, the broker buffers the events. This prevents the ingestion path from crashing, allowing the system to absorb spikes by queuing work rather than dropping requests.

---

## 4. Scaling, Consistency, and Observability

### A. Database Scaling Summary
*   **User Data (Strong Consistency):** **Horizontal Sharding** based on `User_ID` is mandatory to distribute the load and meet the 500M MAU requirement.
*   **Media/Interaction Data (Eventual Consistency):** Utilizing NoSQL databases (Cassandra/DynamoDB) for likes, comments, and post metadata allows for massive write scaling and high availability, accepting the BASE trade-off.
*   **Connection Pooling:** **PgBouncer** ensures the PostgreSQL cluster remains performant by efficiently managing the high volume of connections from the application services.

### B. Caching Strategy Summary
| Data Type | Cache Layer | Pattern | Invalidation Strategy | Goal |
| :--- | :--- | :--- | :--- | :--- |
| **User Profiles/Auth** | Distributed Cache (Redis) | Cache-Aside | TTL based on profile update events. | Low-latency profile retrieval. |
| **Home Feed** | Distributed Cache (Redis) | Cache-Aside | Event-driven invalidation upon new post creation. | Meet P95 < 300ms latency. |
| **Static Media** | CDN Cache | TTL-based | Long TTLs for immutable assets. | Global availability, reduced origin load. |

### C. Observability Stack
A comprehensive observability stack is non-negotiable for a system of this complexity.

*   **Distributed Tracing (e.g., OpenTelemetry):** Mandatory. Every request (from API Gateway through all services, queues, and databases) must carry a **Correlation ID** to trace the entire path, crucial for debugging latency spikes and failure modes in the EDA.
*   **Metrics (RED/USE):** Collect metrics on **Rate** (requests/sec), **Errors** (HTTP 5xx rates), and **Duration** (latency) for every service and component.
*   **Logging:** Structured logging with the Correlation ID to allow rapid debugging across service boundaries.
*   **Alerting:** Set alerts on **SLOs/SLIs** (e.g., P95 feed latency > 300ms, Kafka consumer lag > X seconds, error rate > Y%).

### D. Failure Modes and Regional Resilience
*   **Regional Failure:** The architecture must be **Active/Active** across multiple geographic regions. Data replication (e.g., PostgreSQL cross-region replication, Kafka geo-replication) ensures that if an entire region fails, traffic can be instantly rerouted to a healthy region.
*   **Data Inconsistency:** Handled by the **BASE** model for non-critical data (likes/comments). For critical user data, strong consistency is maintained via sharded PostgreSQL and synchronous replication within a region.
*   **Worker Failure:** Handled by the **Message Broker's** retry mechanism and **DLQs**. Failed transcoding jobs are retried, and permanently failed jobs are routed to a DLQ for manual reprocessing.

---

## 5. Rollout and Evolution Plan

The rollout should follow an incremental, risk-mitigated approach.

1.  **Phase 1: Core Foundation (MVP):**
    *   Implement the **User Service** (Sharded PostgreSQL) with strong consistency.
    *   Implement the **API Gateway** with basic JWT authentication and simple rate limiting.
    *   Implement the **Media Ingestion Write Path** using Pre-signed URLs and basic S3 storage.
    *   Implement a basic **Fanout-on-Write** feed for a small set of "celebrity" users.
2.  **Phase 2: Scale and Decoupling:**
    *   Introduce **Kafka** for all asynchronous communication.
    *   Implement the **Transcoding DAG** workers.
    *   Introduce **Redis** for feed caching.
    *   Implement **Fanout-on-Read** for general users.
    *   Implement comprehensive **Observability** stack.
3.  **Phase 3: Global Scale and Resilience:**
    *   Deploy **Multi-Region Active/Active** infrastructure.
    *   Implement **Circuit Breakers** and **Saga Orchestration** for complex workflows.
    *   Optimize **PgBouncer** configuration for peak load.
    *   Conduct **Chaos Engineering** drills to validate regional failover and circuit breaker effectiveness.

---

## Sources

*   **Title: 7 Must-know Strategies to Scale Your Database | Blog URL: https://mediascribe.b8z.me/blog/59b5061f-a340-44aa-9df7-09d9b39f4229 | Source URL: https://www.youtube.com/watch?v=_1IKwnbscQU | Mediascribe section_name: ByteByteGo** (Used for Sharding, Indexing, Replication, Caching)
*   **Title: How Event Driven Architecture Works (System Design) | Blog URL: https://mediascribe.b8z.me/blog/9ac3b625-d255-43cd-8010-fcfab1e12350 | Source URL: https://www.youtube.com/watch?v=9E4EXBZnN7U | Mediascribe section_name: Arjay McCandless** (Used for EDA, Decoupling, Saga Pattern)
*   **Title: API Gateway vs Load Balancer | Blog URL: https://mediascribe.b8z.me/blog/5af293fb-73ae-49d4-a7bf-5ee8140fa390 | Source URL: https://www.youtube.com/shorts/WiGWygckdv0 | Mediascribe section_name: Arjay McCandless** (Used for LB vs. AG distinction)
*   **Title: Top 12 Tips For API Security | Blog URL: https://mediascribe.b8z.me/blog/31982c2e-69fb-4f19-bd54-2355a8a53769 | Source URL: https://www.youtube.com/watch?v=6WZ6S-qmtqY | Mediascribe section_name: ByteByteGo** (Used for OAuth2, JWT, Rate Limiting, Input Validation)
*   **Title: Distributed Transactions Explained: 2 Phase Commit vs Saga Pattern | Blog URL: https://mediascribe.b8z.me/blog/7419e531-4f94-4215-bf24-fd809674d63f | Source URL: https://www.youtube.com/watch?v=DOFflggE_0Q | Mediascribe section_name: Hello Interview** (Used for 2PC vs. Saga trade-off)
*   **Title: Trillions of Web Pages: Where Does Google Store Them? | Blog URL: https://mediascribe.b8z.me/blog/40b3aed6-86da-4daf-9e58-b887071c7f3b | Source URL: https://www.youtube.com/watch?v=nBvDtj-p6VM | Mediascribe section_name: ByteByteGo** (Used for Partitioning and Sharding)
*   **Title: Application Load Balancer (ALB) Vs API Gateway // Pros Cons Comparison | Blog URL: https://mediascribe.b8z.me/blog/eb4a7c0c-c68d-45f0-9cbe-7b4f07bc22ec | Source URL: https://www.youtube.com/v=YKT9bWvy_PI | Mediascribe section_name: Cloud With Raj** (Used for ALB vs. API Gateway comparison)
*   **Title: KISS, SOLID, CAP, BASE: Important Terms You Might Not Know! | Blog URL: https://mediascribe.b8z.me/blog/90376b16-e66f-45b4-be8b-555f4d83cda4 | Source URL: https://www.youtube.com/watch?v=cTyZ_hbmbDw | Mediascribe section_name: ByteByteGo** (Used for CAP/BASE/SOLID/KISS principles)
*   **Title: System Design Interview – BIGGEST Mistakes to Avoid | Blog URL: https://mediascribe.b8z.me/blog/ca2566a2-b1ae-4f3b-95bf-c5e565c09392 | Source URL: https://www.youtube.com/v=OvufRkoD-D0 | Mediascribe section_name: ByteByteGo** (Used for requirements gathering and trade-off discussion)
