## Global Real-Time Collaborative Document Editor Architecture

This architecture is designed to meet the requirements of 100M MAU, 500K concurrent sessions, strict latency targets (P95 local < 100ms, P95 remote < 300ms), and strong data durability, while ensuring regional fault tolerance.

### 1. Core Synchronization Strategy (Conflict Resolution)

The fundamental challenge is maintaining an authoritative, conflict-free document state while handling concurrent, position-based edits [S1].

**Strategy Choice: Conflict-free Replicated Data Types (CRDTs)**
We select CRDTs over Operational Transformation (OT) because they eliminate the need for complex, centralized transformation logic and coordination locks, which simplifies scaling and reduces the risk of transformation logic errors [S2].

*   **Mechanism:** Edits are treated as commutative and idempotent operations. Each client applies its local edits immediately (for low latency) and then broadcasts the operation (the CRDT delta) to the server. The server applies the operation and propagates the resulting state change to all other connected clients. Because the merging logic is mathematical, concurrent updates from different users will always converge to the same final state, regardless of the order in which operations arrive [S12].
*   **Consistency Model:** CRDTs naturally support eventual consistency for the document content itself, which aligns with the requirement that cursor presence and typing indicators can be eventually consistent [S1].

**Handling Strong Consistency Requirements:**
For metadata that requires strong consistency (e.g., user permissions, document ownership, sharing links), we will use a strongly consistent database layer (e.g., leveraging Multi-Version Concurrency Control (MVCC) principles similar to Google Spanner [S3]) for these specific operations.

### 2. System Architecture Overview

The system is divided into three main planes: the Real-Time Plane, the Application Plane, and the Data Plane.

**High-Level Flow:**
Client $\xrightarrow{\text{WebSocket}}$ Edge Gateway $\xrightarrow{\text{Auth/Rate Limit}}$ Collaboration Service $\xrightarrow{\text{CRDT Operation}}$ Message Queue $\xrightarrow{\text{Persistence}}$ Document Store $\xrightarrow{\text{Broadcast}}$ Collaboration Service $\xrightarrow{\text{WebSocket}}$ Clients

### 3. Real-Time Sync Design (The Collaboration Service)

This service is the core engine responsible for managing persistent connections and applying CRDT logic.

*   **Communication Protocol:** **WebSockets** are mandatory for persistent, bi-directional, low-latency communication, replacing inefficient HTTP polling [S7]. This choice is superior for real-time applications due to its efficiency and low latency [S4].
*   **Connection Management:** Collaboration Servers must be designed to handle tens of thousands of concurrent WebSocket connections [S8]. These servers will be horizontally scaled behind an Elastic Load Balancer (ELB) [S9].
*   **Latency Management:**
    *   **Local Edits (P95 < 100ms):** Edits are applied locally on the client immediately upon user input (optimistic UI update). The operation is sent to the server asynchronously.
    *   **Remote Edits (P95 < 300ms):** The server processes the CRDT operation, persists it, and broadcasts the resulting delta to all other relevant clients. The low latency is achieved by minimizing network hops and using high-performance protocols like gRPC for inter-service communication [S6].

### 4. Data Flow and Subsystem Deep Dive

#### A. Request/Data Flow (Edit Operation)

1.  **Client $\rightarrow$ Edge Gateway:** Client sends a CRDT operation (delta) over a persistent WebSocket connection.
2.  **Edge Gateway:** Performs initial authentication, authorization checks, and rate limiting.
3.  **Collaboration Service:** Receives the operation.
    *   **Validation:** Validates the operation against the current document version state.
    *   **CRDT Application:** Applies the operation to the in-memory document state.
    *   **Persistence Queue:** The validated operation is immediately pushed onto a high-throughput Message Queue (e.g., Kafka/MSK) for durable storage and eventual broadcast.
4.  **Message Queue (Kafka/MSK):** Acts as the durable log of all committed edits, ensuring no committed edits are lost [S14].
5.  **Persistence Workers:** Consumers read from the queue, apply the CRDT operation to the authoritative Document Store, and update the version history.
6.  **Broadcast:** The Collaboration Service receives confirmation of persistence and broadcasts the finalized, canonical operation to all other connected clients for convergence.

#### B. Storage Choices

*   **Document Content (CRDT State):** A distributed, multi-master NoSQL database (e.g., Cassandra or DynamoDB principles [S3]) is ideal for storing the document state due to its multi-master replication capabilities, which support high availability and fault tolerance across regions [S3].
*   **Metadata (Permissions, Users, Version History):** A strongly consistent database (e.g., leveraging Spanner or Aurora principles [S3]) is used for user accounts, permissions, and version history to guarantee strong consistency for these critical data types.
*   **Search/Indexing:** For searching within the document content (e.g., finding specific terms), JSONB columns in a relational database (Postgres) can be indexed using **GIN** indexes to allow for fast querying of nested properties [S5].

#### C. Caching Strategy

*   **Edge Caching (CDN):** Use a Content Delivery Network (CDN) to cache static assets and initial document loads geographically closer to the user, minimizing latency for non-real-time content [S10].
*   **In-Memory Caching (Redis/Elasticache):** Use an in-memory store for caching frequently accessed document states or user session tokens to reduce load on the primary database layer.

### 5. Message Queue, Rate Limiting, and Resilience

*   **Message Queue/Event Design:** **Apache Kafka/Amazon MSK** is used as the central nervous system. It provides high-throughput, durable, ordered logging of all edit operations. This decouples the real-time ingestion path from the persistence path, ensuring durability and allowing for replayability in case of service failure [S9].
*   **Rate Limiting:** Implemented at the Edge Gateway to protect the Collaboration Service from abuse and denial-of-service attacks. Limits should be applied per user/session and per document to prevent a single user from overwhelming the system [S4].
*   **Retries and Timeouts:**
    *   **Client-to-Server:** Use exponential backoff for transient network errors.
    *   **Inter-Service:** Implement circuit breakers (e.g., using patterns like Hystrix) between the Collaboration Service and the database/queue consumers to prevent cascading failures if a downstream service becomes slow or unavailable [S4].
*   **Failure Modes and Recovery:**
    *   **Single Node Failure:** Handled by the distributed nature of the database (multi-master replication) and the Message Queue (Kafka's replication factor).
    *   **Regional Failure (Disaster Recovery):** Deploy the entire stack across multiple geographic regions. Use **Multi-Region Deployment** to minimize latency and ensure failover. Data replication must be asynchronous between regions, but strong consistency metadata must be managed via consensus protocols like **RAFT** [S3].
    *   **Data Loss Prevention:** The Message Queue acts as the source of truth for committed edits. If a persistence worker fails, it can resume processing from the last committed offset in the queue, ensuring documents never lose committed edits [S14].

### 6. Database Scaling and Pooling

*   **Database Sharding:** User data (profiles, permissions) will be sharded based on the **User ID** to distribute load horizontally [S8]. Document content storage will utilize the inherent sharding capabilities of the chosen NoSQL solution.
*   **Connection Pooling (PgBouncer/Equivalent):** For relational components (metadata), connection pooling mechanisms like PgBouncer should be used to efficiently manage and reuse database connections, reducing overhead and improving throughput under high concurrency [S5].

### 7. Observability

Comprehensive observability is critical for managing a system with complex real-time interactions.

*   **Metrics:** Track latency at every hop (Client $\rightarrow$ Gateway $\rightarrow$ Service $\rightarrow$ Queue $\rightarrow$ DB). Monitor WebSocket connection counts, queue lag, and error rates.
*   **Tracing:** Implement distributed tracing (e.g., OpenTelemetry) to track a single edit operation end-to-end across microservices, crucial for diagnosing the 300ms propagation latency target [S6].
*   **Alerting:** Set alerts based on SLA breaches (e.g., P95 remote propagation exceeding 300ms) and data integrity checks (e.g., queue backlog growth).
*   **Forensic Readiness:** Implement monitoring to track the integrity of the backup chain and data consistency checks, preparing for forensic recovery if data corruption is detected [S14].

### 8. Rollout Plan

1.  **Phase 1: Core Infrastructure & Consistency (MVP):** Deploy the core WebSocket servers, the Message Queue, and the strongly consistent metadata database. Implement basic CRUD operations for document creation and permission management.
2.  **Phase 2: Real-Time Synchronization:** Implement the CRDT logic within the Collaboration Service. Focus on achieving the P95 remote propagation target (300ms) in a controlled environment.
3.  **Phase 3: Global Scale & Resilience:** Implement multi-region deployment, global CDN integration, and full disaster recovery testing (DR drills) to validate failover mechanisms and data durability [S14].
4.  **Phase 4: Feature Expansion:** Introduce advanced features like complex version history, real-time presence indicators (eventually consistent), and advanced search indexing (GIN indexes) [S5].

***

Sources
S1: System Design: Google Docs #coding #programming #systemdesign
S2: Real-Time Collaboration Explained - System Design
S3: 25 Computer Papers You Should Read!
S4: System Design Interview – BIGGEST Mistakes to Avoid
S5: I replaced my entire stack with Postgres…
S6: APIs Explained in 6 Minutes!
S7: A WebSocket Is an HTTP Request That Stops Being HTTP
S8: FAANG System Design Interview: Design A Chat System (WhatsApp, Facebook Messenger, Discord, Slack)
S9: Top 50+ AWS Services Explained in 10 Minutes
S10: 8 Most Important System Design Concepts You Should Know
S12: Are you Using CAP Theorem Wrong?
S14: One Server Broke. They Lost Everything.
