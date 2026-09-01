# Failure Handling and Reliability

> Cross-references: [Architecture Diagram](../architecture/architecture.md) · [System Design](system-design.md) · [Security and Risk](security-and-risk.md) · [Pipeline Pseudocode](../pseudocode/enquiry_pipeline.py)

---

## 1. Design Philosophy

Four principles govern how this system handles failure:

1. **No enquiry is ever silently lost.** Every failure path ends at either automatic retry, a manual review queue, or an operational alert. There is no code path where an enquiry disappears without a trace.

2. **Idempotency everywhere.** Every operation is safe to retry. Duplicate webhooks, duplicate queue jobs, duplicate CRM writes — all are handled gracefully through idempotency keys and state checks.

3. **Graceful degradation over hard failure.** If the LLM is down, the system still works — enquiries route to manual review. If the CRM is down, changesets queue up and execute when it recovers. The system prefers slower-but-working over fast-but-broken.

4. **Data integrity above throughput.** If the database is unavailable, processing halts entirely rather than operating without persistence. Losing data is worse than being slow.

---

## 2. Failure Scenarios

### 2.1 LLM API Failures

| Scenario | Detection | Response | Recovery |
|----------|-----------|----------|----------|
| Timeout (>10s) | Request timeout | Retry up to 2x with exponential backoff (2s, 4s delays) | After 3 total attempts: mark enquiry status `failed`, route to manual review queue, alert ops |
| Rate limit (HTTP 429) | Status code check | Respect `Retry-After` header, pause this worker | Queue pauses for the provider; other jobs in the queue continue processing |
| Server error (5xx) | Status code check | Retry up to 3x with exponential backoff (2s, 4s, 8s) | After all retries fail: manual review queue |
| Invalid JSON response | JSON parse failure | Retry once with the same prompt | Second failure: mark as `failed`, route to manual review |
| Malformed structured output | Pydantic schema validation | Retry once (model non-determinism may produce valid output) | Second failure: manual review |
| Nonsensical classification | Confidence < 0.3 | Treat as classification failure | Route to manual review with flag `low_confidence` |
| Provider completely unavailable | Connection error or DNS failure | Activate feature flag: route ALL new enquiries directly to manual review queue | No AI processing occurs; no data loss. Alert immediately. |

**Retry implementation:**

```python
import asyncio
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((TimeoutError, LLMServerError)),
)
async def call_llm(prompt: str, schema: dict) -> dict:
    """Call LLM with automatic retry on transient failures."""
    # ... implementation
    pass
```

> [!IMPORTANT]
> The feature flag for LLM provider outage must be toggleable **without a deployment**. Implementation: a Redis key (`feature:llm_enabled`) checked before each AI processing job. Set to `false` via admin endpoint or direct Redis CLI. When `false`, the ingestion pipeline skips the AI queue entirely and routes enquiries to the manual review queue.

### 2.2 CRM API Failures

| Scenario | Detection | Response | Recovery |
|----------|-----------|----------|----------|
| Validation error (400) | Status code | Log error details, mark changeset status `failed` | **Do NOT retry** — this is a data issue. Alert for human investigation. Reviewer corrects the proposed data and re-submits. |
| Authentication error (401/403) | Status code | Alert immediately — credential issue | Requires manual credential rotation. All CRM writes pause until resolved. |
| Not found (404) | Status code | Log, mark changeset `failed` | Human investigates — the CRM record may have been deleted or modified externally. |
| Server error (5xx) | Status code | Retry up to 3x with exponential backoff (5s, 10s, 20s) | After all retries: move to dead-letter queue. Changeset stays in `pending` status. |
| Timeout | Request timeout | Retry with backoff. Include idempotency key in retry. | After retries fail: dead-letter queue. Idempotency key prevents duplicate writes if the original request actually succeeded. |
| Rate limit (429) | Status code / `Retry-After` header | Back off, respect `Retry-After` | Queue throttles CRM write rate. Processing continues at reduced speed. |

> [!NOTE]
> **Idempotency detail:** Every CRM write request includes the changeset UUID as an idempotency key (either in a request header or as part of the payload, depending on CRM API support). If a write times out but actually succeeded on the CRM side, the retry will be safely recognized as a duplicate and ignored. This is critical for preventing duplicate contacts or leads.

**Why 400 errors are not retried:** A 400 response means the data we sent is invalid according to the CRM's schema. Retrying the same data will produce the same error. The correct response is to flag the changeset for human review, where the reviewer can see the validation error and correct the proposed data.

### 2.3 Email and Messaging Failures

| Scenario | Response | Notes |
|----------|----------|-------|
| Send API error (non-timeout) | Retry up to 3x with backoff. Draft remains in `approved` status. | After all retries: alert reviewer. Draft is available for manual send. |
| Send timeout | Retry with backoff. Check delivery status via provider API if supported. | Use message ID from first attempt to check if delivery actually occurred. |
| All retries exhausted | Alert reviewer. Draft stays in `approved` status. | Reviewer can manually copy the approved text and send through alternative channel. |
| Bounce / rejection notification | Log bounce event. Update contact record with `delivery_issue` flag. | Do not automatically retry bounced messages. Flag for human review of contact data. |
| Recipient unsubscribed | Respect unsubscribe. Log event. Do not re-send. | Compliance with email regulations (CAN-SPAM, GDPR). |

### 2.4 Queue Failures (Redis)

| Scenario | Response | Data Safety |
|----------|----------|-------------|
| Redis temporarily unavailable (seconds) | Ingestion layer buffers incoming messages in a bounded in-memory queue (max 100 items). Alert immediately. | Enquiries already saved to PostgreSQL before queuing. In-memory buffer is a bridge, not primary storage. |
| Redis persistent failure (minutes+) | Processing halts. Ingestion continues saving to PostgreSQL with status `new`. Alert escalated. | No data loss. When Redis recovers, a recovery job scans for `new` enquiries not yet queued. |
| Worker crash mid-job | Python RQ marks the job as failed and re-queues automatically (configurable retry count). | Job is retried. Idempotency checks prevent duplicate processing. |
| Poison pill job (always fails) | After N retries (configurable, default: 3), job is moved to the RQ failed job registry. Alert triggered. | Job data preserved in failed registry for investigation. Original enquiry safe in PostgreSQL. |

**Recovery scan after Redis outage:**

```python
async def recovery_scan():
    """Find enquiries saved to DB but never queued for processing."""
    unprocessed = await db.fetch_all(
        """
        SELECT id FROM enquiries
        WHERE status = 'new'
        AND created_at < NOW() - INTERVAL '5 minutes'
        AND id NOT IN (SELECT enquiry_id FROM ai_extractions)
        ORDER BY created_at ASC
        LIMIT 100
        """
    )
    for enquiry in unprocessed:
        queue.enqueue(process_enquiry, enquiry["id"])
```

### 2.5 Database Failures (PostgreSQL)

| Scenario | Response | Rationale |
|----------|----------|-----------|
| Connection pool exhausted | Queue workers wait and retry connection. Alert if sustained beyond 30 seconds. | Transient issue — usually resolves as connections are returned to pool. |
| Database completely down | **All processing halts.** Incoming webhooks return HTTP 503. Alert immediately (page on-call). | Data integrity is non-negotiable. Operating without persistence risks data loss. |
| Disk full | Alert immediately. No writes possible until disk space freed or volume expanded. | Predictable failure — monitoring should catch this before it happens (alert at 80% capacity). |
| Transaction deadlock | Retry the transaction once with a short backoff (100ms). | PostgreSQL reports deadlocks clearly. Single retry usually succeeds. If persistent, investigate query patterns. |
| Replication lag (if read replicas used) | Read queries may return stale data. Critical reads (approval checks) always use primary. | Acceptable for dashboard queries; not acceptable for authorization decisions. |

---

## 3. Idempotency Design

### Why Idempotency Matters

In a system with queues, retries, and multiple services, the same operation may execute more than once. Network timeouts, worker crashes, and duplicate webhooks all cause retries. Without idempotency, retries create duplicate records, duplicate emails, and duplicate CRM entries.

**Idempotency guarantee:** Re-executing any operation produces the same result as executing it once.

### Implementation by Component

| Operation | Idempotency Key | Mechanism | What Happens on Duplicate |
|-----------|-----------------|-----------|---------------------------|
| Enquiry ingestion | `external_message_id` (from email/messaging provider) | UNIQUE constraint on `enquiries.idempotency_key` | INSERT is rejected by database constraint. Webhook returns 200 (already processed). |
| AI processing | `enquiry.status` check | Worker checks if enquiry is already `classified` before processing | If already classified, worker skips processing and logs `already_processed`. |
| CRM write | Changeset UUID as idempotency key | Sent to CRM API in request header | CRM recognizes duplicate request and returns existing record. |
| Message send | `draft_responses.id` | Application checks `draft.status` before sending | If status is already `sent`, send is blocked. Returns existing send confirmation. |
| Approval recording | `approvals.changeset_id` + `status` check | Application checks for existing approval before recording | If already approved/rejected, returns existing decision. |

### Database-Level Enforcement

```sql
-- Enquiry deduplication
ALTER TABLE enquiries ADD CONSTRAINT uq_enquiries_idempotency
    UNIQUE (idempotency_key);

-- Status transition validation (application-level, backed by this constraint)
-- Prevents invalid transitions like 'executed' → 'pending'
-- Enforced in application code with optimistic locking:
UPDATE crm_changesets
SET status = 'executed', updated_at = NOW()
WHERE id = $1 AND status = 'approved'  -- Only transition from 'approved'
RETURNING id;
-- If no rows returned, the transition was invalid.

-- Audit events: append-only
REVOKE UPDATE, DELETE ON audit_events FROM PUBLIC;
```

### Valid Status Transitions

Enquiry status transitions are strictly ordered:

```
new → processing → classified → pending_approval → approved → completed
                 ↘ failed (from any state)
                 ↘ pending_info (from classified, loops back to processing)
```

Any transition not in this graph is rejected by the application. This prevents state corruption from retries or race conditions.

---

## 4. Dead-Letter Queue

Jobs that exhaust all retry attempts are moved to a dead-letter queue (DLQ) rather than being silently discarded.

### Structure

| Field | Description |
|-------|-------------|
| `id` | Unique identifier for the DLQ entry |
| `original_job_id` | Reference to the original queue job |
| `enquiry_id` | FK to the enquiry being processed |
| `job_type` | What the job was doing: `ai_processing`, `crm_write`, `message_send` |
| `error_message` | The last error message before the job was moved to DLQ |
| `error_traceback` | Full stack trace for debugging |
| `retry_count` | How many times the job was retried |
| `original_payload` | The complete job payload (for manual retry) |
| `created_at` | When the job entered the DLQ |
| `resolved_at` | When the job was resolved (NULL if still pending) |
| `resolution` | How it was resolved: `retried_successfully`, `manually_processed`, `abandoned` |

### DLQ Operations

- **Review:** Ops team reviews DLQ entries daily (or when alert fires)
- **Retry:** After fixing the root cause, a DLQ entry can be manually retried with the original payload
- **Manual processing:** If automated retry is not possible, the enquiry is processed manually and the DLQ entry is marked `manually_processed`
- **Abandon:** In rare cases (e.g., duplicate that was already handled), the entry is marked `abandoned` with a justification note

### Alerting

| DLQ Size | Alert Level | Action |
|----------|-------------|--------|
| > 0 | Info | Logged for daily review |
| > 10 | Warning | Active notification to ops team |
| > 50 | Critical | Investigate for systemic issue (provider outage, bug, data corruption) |

---

## 5. Monitoring and Alerting

### Key Metrics (Prometheus)

**Throughput metrics:**
- `enquiries_received_total` — counter, labeled by `source_channel` (email, whatsapp, chat)
- `enquiries_processed_total` — counter, labeled by `classification` and `outcome` (success, failed, manual_review)
- `enquiries_processing_duration_seconds` — histogram, end-to-end processing time

**AI metrics:**
- `llm_request_duration_seconds` — histogram, labeled by `model` (gpt-4o-mini, gpt-4o)
- `llm_request_errors_total` — counter, labeled by `error_type` (timeout, rate_limit, server_error, invalid_response)
- `llm_confidence_score` — histogram, distribution of classification confidence scores
- `llm_cost_dollars_total` — counter, estimated cost based on token usage

**CRM metrics:**
- `crm_write_duration_seconds` — histogram
- `crm_write_errors_total` — counter, labeled by `error_type`
- `crm_changesets_pending` — gauge, number of changesets awaiting execution

**Operational metrics:**
- `approval_queue_size` — gauge, number of items awaiting human review
- `approval_response_time_seconds` — histogram, time from changeset creation to approval/rejection
- `dead_letter_queue_size` — gauge
- `redis_queue_depth` — gauge, number of jobs waiting in each queue

### Alert Rules

| Condition | Severity | Notification | Recommended Action |
|-----------|----------|--------------|-------------------|
| LLM error rate > 10% over 5 minutes | Warning | Slack / email | Investigate LLM provider status. Prepare to enable manual-review feature flag. |
| LLM error rate > 50% over 5 minutes | Critical | Slack + PagerDuty | Enable manual-review feature flag immediately. All enquiries route to human review. |
| CRM write error rate > 20% over 5 minutes | Warning | Slack / email | Check CRM API health. Review recent changeset payloads for data issues. |
| CRM authentication failure (any) | Critical | Slack + PagerDuty | Credential issue. Rotate CRM credentials immediately. |
| DLQ size > 10 | Warning | Slack / email | Review dead-letter queue. Identify common failure pattern. |
| DLQ size > 50 | Critical | Slack + PagerDuty | Systemic issue. Investigate root cause before queue grows further. |
| Approval queue oldest item > 4 hours | Warning | Slack | Notify reviewers. Check reviewer availability. |
| Approval queue oldest item > 24 hours | Critical | Slack + escalation | Escalate to team lead. Enquiries are blocked waiting for review. |
| Database connection errors (any) | Critical | PagerDuty | Immediate investigation. Check database health, connection pool, network. |
| Disk usage > 80% | Warning | Slack / email | Plan disk expansion. Check for unexpected data growth. |
| Disk usage > 95% | Critical | PagerDuty | Immediate action. Risk of write failures. |

### Structured Logging

All services use Python `structlog` for consistent, machine-parseable JSON logs:

```python
import structlog

logger = structlog.get_logger()

logger.info(
    "enquiry_classified",
    enquiry_id="550e8400-e29b-41d4-a716-446655440000",
    classification="sales_opportunity",
    confidence=0.87,
    model="gpt-4o-mini",
    processing_time_ms=1230,
    service="ai_worker",
)
```

**Output:**
```json
{
  "timestamp": "2026-09-01T16:38:00Z",
  "level": "info",
  "event": "enquiry_classified",
  "enquiry_id": "550e8400-e29b-41d4-a716-446655440000",
  "classification": "sales_opportunity",
  "confidence": 0.87,
  "model": "gpt-4o-mini",
  "processing_time_ms": 1230,
  "service": "ai_worker"
}
```

**Every log entry includes:**
- `timestamp` — ISO 8601 format, UTC
- `level` — debug, info, warning, error, critical
- `event` — descriptive event name (snake_case)
- `enquiry_id` — if applicable, for correlation
- `service` — which service generated the log
- `correlation_id` — for tracing a request across services

**PII redaction in logs:**
- Email addresses: `john@example.com` → `j***@example.com`
- Phone numbers: `+628123456789` → `+62812***6789`
- Names: logged as-is (needed for debugging, not considered high-sensitivity PII in this context)
- Redaction is applied by a structlog processor, not manually per log call

---

## 6. Graceful Degradation Modes

| Component Down | System Behaviour | Human Impact | Recovery |
|----------------|------------------|--------------|----------|
| **LLM API** | Feature flag activated → all enquiries skip AI processing and go directly to manual review queue | Reviewers classify and extract manually. Slower but fully functional. | Disable feature flag. Queue workers automatically process backlog. |
| **CRM API** | AI processing and classification continue normally. Approved changesets queue up in `pending` status. | No immediate impact on enquiry processing. CRM updates are delayed. | CRM writes execute automatically when API recovers. DLQ reviewed for failures. |
| **Email sending API** | Approved response drafts remain in `approved` status. Not sent. | Reviewer is alerted. Can manually copy approved text and send via alternative channel. | Queued sends execute when API recovers. |
| **Redis** | Short outage: in-memory buffer bridges the gap. Long outage: ingestion saves to PostgreSQL but processing halts. | New enquiries are safe but not processed until Redis recovers. | Recovery scan finds unprocessed enquiries and queues them. |
| **PostgreSQL** | **Full system halt.** Webhooks return 503. No processing occurs. | Complete outage. All operations suspended. | Database recovery → all systems resume. Audit log integrity verified. |

> [!CAUTION]
> PostgreSQL is the single non-negotiable dependency. Every other component can fail and the system degrades gracefully. If the database is down, the system stops entirely. This is intentional — operating without persistence risks silent data loss, which violates the core design principle.

---

## 7. Recovery Procedures

### After LLM Provider Outage

1. Confirm LLM API is responding normally (health check or test request)
2. Disable the manual-review feature flag (set `feature:llm_enabled` to `true` in Redis or config)
3. Queue workers automatically pick up enquiries with status `new` that were not yet processed
4. Enquiries that were manually reviewed during the outage are already in the system with human-provided classifications — no re-processing needed
5. Monitor error rates for 15 minutes to confirm stability
6. **No special data migration or recovery action required**

### After CRM API Outage

1. Confirm CRM API is responding (health check endpoint or test read)
2. Pending changesets in the retry queue are automatically processed with idempotency keys preventing duplicates
3. Review the dead-letter queue for any changesets that permanently failed during the outage
4. For DLQ items: investigate root cause, fix data if needed, retry manually
5. Verify CRM data consistency by spot-checking recent changesets against CRM records

### After Redis Outage

1. Confirm Redis is responding and accepting connections
2. Run the recovery scan to find enquiries saved to PostgreSQL but never queued:
   ```sql
   SELECT COUNT(*) FROM enquiries
   WHERE status = 'new'
   AND created_at < NOW() - INTERVAL '5 minutes'
   AND id NOT IN (SELECT enquiry_id FROM ai_extractions);
   ```
3. Execute recovery job to queue these enquiries for processing
4. Monitor queue depth and processing rate to confirm backlog is clearing
5. Check if any in-memory buffered items were lost (compare webhook provider delivery logs with `enquiries` table)

### After Database Outage

1. Database recovered and accepting connections
2. Verify data integrity:
   - Check audit log for the last recorded event before outage
   - Verify no partial transactions (orphaned records)
   - Run consistency checks on foreign key relationships
3. Check Redis for any buffered items that need to be persisted
4. Resume all services (they should reconnect automatically via connection pool)
5. Monitor error rates and processing throughput for 30 minutes
6. Review webhook provider dashboards for any deliveries that failed during outage (most providers will retry automatically)

---

## 8. Operational Runbook Summary

### Daily

- [ ] Review dead-letter queue — investigate and resolve any entries
- [ ] Check approval queue age — ensure no enquiry is stuck awaiting review for more than 4 hours
- [ ] Review error rate trends — LLM errors, CRM errors, ingestion errors
- [ ] Verify queue depth is stable (not growing unboundedly)

### Weekly

- [ ] Classification accuracy spot-check — sample 10–20 recent classifications and verify correctness
- [ ] Review API cost trends — check LLM token usage and cost against budget
- [ ] Review security alerts — failed webhook verifications, unusual access patterns
- [ ] Check disk usage and database growth trends

### Monthly

- [ ] Rotate API keys (LLM provider, CRM, webhook secrets) per rotation policy
- [ ] Review and update spam/junk filter rules based on recent junk enquiries
- [ ] Test failover procedures — simulate LLM outage (feature flag), verify manual review works
- [ ] Review and update alert thresholds based on traffic patterns
- [ ] Back up and verify backup restoration process

### On Incident

1. **Identify** — what failed and what is the blast radius?
2. **Mitigate** — activate feature flags, pause processing, or failover as appropriate
3. **Communicate** — notify affected team members
4. **Resolve** — fix root cause and verify recovery
5. **Document** — post-incident review, update runbook if needed, log in audit trail

---

*This document should be reviewed after any production incident, when new failure modes are discovered, or when infrastructure changes are made.*
