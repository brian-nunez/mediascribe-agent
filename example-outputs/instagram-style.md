Sifting through files...
Planning searches...
Knowledge lookup: architecture for large-scale social media platform with global distribution
Knowledge lookup: feed generation strategies for social media
Knowledge lookup: designing high-throughput media upload systems
Knowledge lookup: eventual consistency vs strong consistency in distributed systems
Knowledge lookup: scaling database for social media reads and writes
Knowledge lookup: observability patterns for microservices architecture
Evaluating research coverage...
Sifting through article: 25 Computer Papers You Should Read!
Sifting through article: 7 Must-know Strategies to Scale Your Database
Sifting through article: Are you Using CAP Theorem Wrong?
Sifting through article: Distributed Transactions Explained: 2 Phase Commit vs Saga Pattern
Sifting through article: 8 Most Important System Design Concepts You Should Know
Sifting through article: How Event Driven Architecture Works (System Design)
Sifting through article: KISS, SOLID, CAP, BASE: Important Terms You Might Not Know!
Sifting through article: System Design: Choosing A Database
Diagnostics: input_tokens=32518 output_tokens=4868 total_tokens=37386 tool_calls=1
Diagnostics written to diagnostics/agent-run-20260713T052752Z.json
This design outlines a highly distributed, microservices-based architecture optimized for extreme scale, global availability, and the specific read/write patterns of a social media platform. The design explicitly manages the trade-offs between strong consistency (for critical data) and eventual consistency (for high-volume interactions like likes and feeds).

---

## 1. Architectural Overview: Layered Microservices

The system will be decomposed into independent microservices, communicating primarily through an **Event-Driven Architecture (EDA)** backbone (Kafka) for decoupling and resilience.

### Core Architectural Layers:

1.  **Edge Layer (Global Distribution):** Handles ingress, security, and static content delivery.
2.  **API/Service Layer (Business Logic):** Stateless microservices handling user requests, authentication, and core business logic.
3.  **Data Layer (Persistence):** Polyglot persistence, utilizing specialized databases for different consistency and access patterns.
4.  **Asynchronous Backbone (Eventing):** The central nervous system for decoupling services and handling high-throughput data streams.

### Concrete Request/Data Flow Example: Posting a Video (Write Path)

This flow prioritizes fast acknowledgment for the user while ensuring media processing happens reliably in the background.

**Client $\rightarrow$ CDN $\rightarrow$ WAF $\rightarrow$ API Gateway $\rightarrow$ Load Balancer $\rightarrow$ Post Service $\rightarrow$ Queue $\rightarrow$ Media Processor $\rightarrow$ Object Storage $\rightarrow$ Event Broker $\rightarrow$ Feed Service (Fanout)**

1.  **Client Request:** Mobile client uploads video/photo metadata and media chunks.
2.  **Edge Processing (CDN/WAF):** The CDN handles initial static asset delivery (if applicable). The WAF inspects the request for malicious content, rate limits, and bot traffic.
3.  **API Gateway:** Performs initial authentication (JWT validation), rate limiting (Token Bucket), and request validation. Routes the request to the **Post Service**.
4.  **Post Service (Write Path):**
    *   Validates user permissions (via Auth Service).
    *   Writes the post metadata (user ID, media URL pointer) to the **Post Database (Strong Consistency)**.
    *   Publishes a `PostCreated` event to the **Event Broker (Kafka)**.
    *   Returns an immediate `202 Accepted` response to the client (acknowledgement within 1s).
5.  **Media Processor (Asynchronous):** A dedicated worker service consumes the `PostCreated` event. It handles the heavy lifting:
    *   Receives media chunks.
    *   Transcodes the video into various adaptive bitrates.
    *   Stores the final media files in **Object Storage (S3/GCS)**.
    *   Updates the Post Database with the final media URLs.
6.  **Feed Service (Fanout):** A dedicated service consumes the `PostCreated` event.
    *   Identifies all followers of the posting user (using a graph database or follower cache).
    *   Writes the new Post ID into the personalized feed caches (Redis) of all followers.

---

## 2. Component Deep Dive and Technology Choices

### A. Edge and Ingress
*   **CDN (Content Delivery Network):** Essential for global media availability. Caches all static assets (profile pictures, videos) geographically close to the user, drastically reducing latency for media delivery.
*   **WAF (Web Application Firewall):** Protects against OWASP Top 10, DDoS, and bot traffic.
*   **API Gateway:** Acts as the single entry point. Handles **Authentication (OAuth2/OIDC)**, **Rate Limiting** (per-user/per-IP using distributed counters in Redis), and request routing.
*   **Load Balancer:** L7 load balancing for intelligent routing to healthy service instances.

### B. Data Persistence Strategy (Polyglot Persistence)

| Data Type | Consistency Need | Recommended Store | Scaling Strategy | Rationale |
| :--- | :--- | :--- | :--- | :--- |
| **User Accounts, Security Data** | Strong Consistency (ACID) | **PostgreSQL** (with PgBouncer) | Sharding (by User ID) + Read Replicas | Ensures transactional integrity for critical user data. PgBouncer manages connection pooling efficiently. |
| **Media Files (Photos/Videos)** | Durability | **Object Storage** (S3/GCS) | Massive horizontal scaling | Cost-effective, highly durable, and designed for petabyte-scale storage. |
| **Likes, Comments, Shares** | Eventual Consistency (BASE) | **Cassandra/DynamoDB** | Multi-master replication | Optimized for high-volume, high-write throughput and eventual consistency. |
| **Follower Graph** | High Availability | **Graph Database (Neo4j)** | Partitioning/Replication | Efficiently handles complex relationship queries (who follows whom). |
| **Feed Caches** | Low Latency | **Redis Cluster** | In-memory, distributed caching | Stores pre-computed, ranked feeds for sub-300ms P95 latency. |

### C. Feed Generation Strategy (The Core Challenge)

The feed must balance freshness (new posts) with relevance (ranking).

1.  **Fanout-on-Write (For Celebrities/High-Follower Accounts):** When a celebrity posts, the `Feed Service` immediately pushes the post ID into the Redis feed caches of all their followers.
    *   *Pros:* Extremely fast read latency (O(1) lookup).
    *   *Cons:* High write amplification for celebrity posts (20K uploads/sec peak $\times$ 100M followers is infeasible).
2.  **Fanout-on-Read (For General Users):** When a regular user requests their feed, the service queries the **Feed Cache (Redis)** for pre-computed lists, and then merges/ranks the results from a smaller set of sources (e.g., posts from followed users, trending posts).
    *   *Pros:* Low write amplification.
    *   *Cons:* Higher read latency, requires complex ranking algorithms.
3.  **Ranking Pipeline:** A dedicated, asynchronous service consumes all new posts and runs a **Ranking Model** (ML service) to score posts based on user history, engagement signals, and content type. This ranked list is then used to populate the Redis caches.

### D. Reliability, Failure Control, and Resilience

| Pattern | Location | Purpose | Implementation Detail |
| :--- | :--- | :--- | :--- |
| **Timeouts & Retries** | All Service Calls | Handle transient network errors. | Implement **Exponential Backoff with Jitter** on all external service calls. Set strict, short timeouts (e.g., 500ms for internal calls). |
| **Circuit Breakers** | Service Mesh (or Service Logic) | Prevent cascading failures. | Implement circuit breakers (e.g., using Hystrix/Resilience4j) on calls to slow or failing downstream services (e.g., the ML Ranking Service). |
| **Bulkheads** | Service Deployment | Isolate resource pools. | Deploy services into separate resource pools (e.g., separate thread pools or Kubernetes node pools) so that failure in one service (e.g., Notification) cannot exhaust resources needed by another (e.g., Posting). |
| **Dead-Letter Queues (DLQs)** | Event Broker (Kafka) | Handle poison messages. | Any message that fails processing after a defined number of retries is automatically routed to a DLQ for manual inspection, preventing message loss. |
| **Saga Pattern (Orchestration)** | Complex Workflows | Manage distributed transactions. | Used for multi-step processes (e.g., complex moderation workflows or multi-service checkout). The Orchestrator service tracks state and executes compensating actions upon failure. |
| **Idempotency Keys** | Write Services | Prevent duplicate processing. | Use unique request IDs for all write operations to ensure that retries do not result in duplicate data (critical for media processing). |

### E. Database Scaling and Connection Management

*   **PgBouncer:** Deployed in front of the PostgreSQL cluster to manage and pool database connections efficiently. This prevents the database from being overwhelmed by connection setup/teardown overhead, crucial for handling high concurrent traffic.
*   **Sharding:** The PostgreSQL cluster will be sharded, likely by **User ID Hash**. This distributes the write load horizontally and allows for independent scaling of data partitions.
*   **Read Scaling:** Extensive use of **Read Replicas** for the PostgreSQL shards to handle the 2M feed reads/sec peak.
*   **CQRS (Command Query Responsibility Segregation):** The write path (Commands) uses the strongly consistent PostgreSQL cluster. The read path (Queries, like feed generation) uses the highly scalable, eventually consistent Cassandra/Redis layer.

### F. Observability Stack

A robust observability stack is non-negotiable for a system of this complexity.

*   **Distributed Tracing (Mandatory):** Implement OpenTelemetry across all services. Every request, from the API Gateway through the Kafka broker to the database, must carry a **Correlation ID**. This allows engineers to trace the entire path of a request and pinpoint latency bottlenecks.
*   **Metrics (RED/USE):** Collect metrics for **Rate** (QPS), **Errors** (HTTP 5xx, queue failures), and **Duration** (latency) for every service and database endpoint.
*   **Logging:** Structured logging (JSON format) with mandatory inclusion of the **Correlation ID** for every log line, enabling rapid debugging of cross-service issues.
*   **Alerting:** Set alerts on **Golden Signals** (Latency, Traffic, Errors, Saturation) and queue depth. Alerting must focus on symptoms (e.g., "Queue depth for Media Processor > 10,000") rather than just causes.
*   **Chaos Engineering:** Regularly run controlled failure simulations (e.g., killing database replicas, injecting network latency) to validate the Circuit Breakers, Retries, and Fallbacks are functioning as designed.

---

## 3. Failure Modes and Mitigation Summary

| Failure Mode | Impact | Mitigation Strategy |
| :--- | :--- | :--- |
| **Regional Outage** | Loss of service in a geographic area. | **Regional Scaling (Active/Active):** Deploy services across multiple global regions. DNS routing (Geo Routing) directs users to the nearest healthy region. |
| **Media Upload Failure** | User frustration, lost content. | **Idempotency Keys & Queues:** Use idempotency keys to prevent duplicate processing. Failed jobs go to DLQ for manual reprocessing. |
| **Database Partition** | Loss of strong consistency for critical data. | **PACAL/BASE Trade-off:** Critical data uses strong consistency (sharded/replicated). Non-critical data (likes) uses eventual consistency (Cassandra). |
| **Cascading Failure** | System-wide slowdown/crash. | **Circuit Breakers & Bulkheads:** Isolate failing services and prevent resource exhaustion across the system. |
| **Stale Feed Data** | Poor user experience. | **Caching Strategy:** Aggressive TTLs on Redis feeds, combined with **Stale-While-Revalidate** to serve slightly stale data while asynchronously refreshing the cache. |
| **Event Broker Failure** | Loss of data flow/inconsistency. | **Broker Replication:** Use Kafka/Pulsar with high replication factors across multiple brokers/availability zones to ensure data durability. |

---

## 4. Rollout Plan (Phased Approach)

Given the scale and complexity, a phased rollout is essential to manage risk.

### Phase 1: Core Foundation (MVP)
*   **Focus:** Authentication, User Profiles (Strong Consistency DB), Basic Posting (Metadata only), and Static Asset Delivery (CDN).
*   **Architecture:** Monolithic/Modular Monolith for core services. Single primary database instance.
*   **Key Deliverables:** Implement basic Observability (Logging/Metrics). Validate the core request path.

### Phase 2: High-Throughput Ingestion
*   **Focus:** Implementing the Media Upload pipeline and the Event Broker.
*   **Architecture:** Introduce **Object Storage** and the **Asynchronous Media Processor**. Implement the **Transactional Outbox Pattern** for reliable event publishing.
*   **Key Deliverables:** Achieve P95 media upload acknowledgement < 1s. Validate the decoupling between write and processing.

### Phase 3: Scaling and Feed Generation
*   **Focus:** Horizontal scaling and read path optimization.
*   **Architecture:** Implement **Sharding** for PostgreSQL. Introduce **Redis Cluster** for feed caching. Implement the **Fanout-on-Write** strategy for a small subset of high-follower accounts.
*   **Key Deliverables:** Achieve P95 feed latency < 300ms. Validate the read path performance under load.

### Phase 4: Global Resilience and Advanced Features
*   **Focus:** Global distribution, advanced consistency, and complex features.
*   **Architecture:** Implement **Regional Scaling (Active/Active)**. Introduce **Graph Database** for the follower network. Implement the **Saga Orchestration** pattern for complex features (e.g., complex moderation).
*   **Key Deliverables:** System survives regional failure tests. Full implementation of advanced security and rate limiting policies.
