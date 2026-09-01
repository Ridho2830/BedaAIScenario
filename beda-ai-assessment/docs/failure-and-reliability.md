# Failure Handling & Reliability Design

## Core Principle

**No enquiry should ever be silently lost.** Every failure is logged, retried where appropriate, and escalated to a human when automated recovery fails.

## 1. Failure Scenarios & Responses

### LLM Failures

| Failure Type | Detection | Response | Recovery |
|-------------|-----------|----------|----------|
| **Timeout** | Request exceeds configured timeout (e.g., 30s) | Retry with exponential backoff | Up to 3 retries, then DLQ |
| **Rate Limit (429)** | HTTP 429 or provider-specific rate limit error | Back off based on `Retry-After` header | Queue re-processing with delay |
| **Invalid Structured Output** | Schema validation fails (missing fields, wrong types) | Retry once with stronger model | If still invalid, escalate to human |
| **Provider Unavailable (5xx)** | HTTP 5xx or connection failure | Retry with exponential backoff | After 3 retries: DLQ + human alert |
| **Unexpected Response Format** | JSON parse error or non-JSON response | Log error, retry once | If persistent, DLQ + alert |

### CRM Failures

| Failure Type | Detection | Response | Recovery |
|-------------|-----------|----------|----------|
| **Client Error (4xx)** | HTTP 400-499 | Log error with payload details | 400: fix data and retry. 401/403: alert ops (credential issue). 409: handle conflict. 404: create new record |
| **Server Error (5xx)** | HTTP 500-599 | Retry with exponential backoff | Up to 3 retries, then DLQ |
| **Timeout** | Request exceeds CRM timeout (e.g., 15s) | Retry with backoff | Same as 5xx path |
| **Rate Limit** | CRM rate limit response | Queue with delay | Process within CRM rate limits |

### Email/Messaging Failures

| Failure Type | Detection | Response | Recovery |
|-------------|-----------|----------|----------|
| **Send Failure** | SMTP error or API error | Retry up to 3 times | DLQ + alert if persistent |
| **Bounce** | Bounce notification webhook | Log bounce, update contact record | Flag contact for review |
| **Template Error** | Missing variables or render failure | Use fallback plain text template | Alert if template system is broken |

### Queue Failures

| Failure Type | Detection | Response | Recovery |
|-------------|-----------|----------|----------|
| **Queue Unavailable** | Connection failure | In-memory buffer (limited) + alert | Auto-reconnect with backoff |
| **Message Lost** | Acknowledgment not received | Re-enqueue from database state | Idempotency key prevents duplicate processing |
| **Poison Message** | Repeated processing failure | Move to DLQ after max attempts | Manual inspection and reprocessing |

### Database Failures

| Failure Type | Detection | Response | Recovery |
|-------------|-----------|----------|----------|
| **Connection Failure** | Connection pool exhausted or timeout | Retry with backoff | Alert if sustained |
| **Write Failure** | Constraint violation, deadlock | Retry for transient errors | For constraint violations: log and investigate |
| **Read Failure** | Timeout or connection drop | Retry | Fallback: process without cached context |

## 2. Retry Strategy

### Exponential Backoff

```python
delay = BASE_DELAY * (2 ** (attempt - 1))  # 2s, 4s, 8s
delay = min(delay, MAX_DELAY)               # Cap at 60s
delay = delay + random_jitter(0, delay * 0.1)  # Add jitter to prevent thundering herd
```

| Parameter | Value |
|-----------|-------|
| Base delay | 2 seconds |
| Maximum delay | 60 seconds |
| Maximum retries | 3 (configurable per failure type) |
| Jitter | 0–10% of current delay |

### Retry Decision Matrix

| Error Type | Retryable? | Max Retries | Notes |
|------------|-----------|-------------|-------|
| Network timeout | ✅ | 3 | Transient, likely to succeed on retry |
| HTTP 429 (rate limit) | ✅ | 3 | Respect `Retry-After` header |
| HTTP 5xx | ✅ | 3 | Server-side transient error |
| HTTP 400 (bad request) | ❌ | 0 | Fix payload before retrying |
| HTTP 401/403 | ❌ | 0 | Credential/permission issue — alert ops |
| JSON parse error | ✅ | 1 | LLM may produce valid output on retry |
| Schema validation failure | ✅ | 1 | Retry with stronger model |
| Database constraint violation | ❌ | 0 | Data issue — investigate |
| Database deadlock | ✅ | 2 | Transient concurrency issue |

## 3. Idempotency

Idempotency ensures that processing the same enquiry multiple times produces the same result without side effects.

### Idempotency Key Generation

```python
key = sha256(f"{source}:{sender_email}:{body[:500]}")
```

### Where Idempotency Is Enforced

| Step | Mechanism |
|------|-----------|
| **Enquiry ingestion** | Idempotency key checked against database before creating new record |
| **Queue processing** | Enquiry status checked before processing — skip if already completed |
| **CRM lead creation** | Check for existing lead by enquiry ID before creating |
| **Approval requests** | Prevent duplicate approval requests for the same enquiry + action |
| **Audit log writes** | Event ID + timestamp prevent duplicate audit entries |

### Implementation

```python
# At ingestion
existing = db.find_by_idempotency_key(key)
if existing:
    return existing  # Already processed, return existing result

# At queue processing
enquiry = db.get(enquiry_id)
if enquiry.status in [ACTIONED, FAILED]:
    return  # Already processed or permanently failed
```

## 4. Dead Letter Queue (DLQ)

The DLQ is the safety net for enquiries that cannot be processed after all retries.

### When Enquiries Move to DLQ

1. Maximum retries exceeded for any failure type
2. Poison messages that consistently fail processing
3. Invalid messages that cannot be parsed

### DLQ Handling

```
Enquiry fails after max retries
    ↓
Move to DLQ with full context:
  - Original message
  - All error details from each attempt
  - Processing run history
  - Last known state
    ↓
Update enquiry status to FAILED
    ↓
Create audit event
    ↓
Alert operations team (Slack/email)
    ↓
Human operator inspects and decides:
  - Fix and reprocess
  - Manually process
  - Discard (with reason)
```

### DLQ Monitoring

- **Alert**: When any message enters the DLQ
- **Dashboard**: DLQ depth visible on operations dashboard
- **SLA**: DLQ items should be reviewed within 4 hours during business hours
- **Metric**: DLQ entry rate tracked for anomaly detection

## 5. Prevention of Silent Data Loss

Multiple layers prevent enquiries from disappearing:

| Layer | Prevention Mechanism |
|-------|---------------------|
| **Ingestion** | Enquiry persisted to database *before* enqueuing for processing |
| **Queue** | Message acknowledged only after successful processing |
| **Processing** | Every step updates enquiry status in database |
| **Failure** | Failed enquiries move to DLQ — never deleted |
| **Audit** | Every state transition logged with timestamp |
| **Monitoring** | Alert on: queue depth growth, DLQ entries, stale enquiries (stuck in PROCESSING for > N minutes) |

### Stale Enquiry Detection

A background job runs periodically to find "stuck" enquiries:

```python
# Find enquiries that have been PROCESSING for more than 15 minutes
stale = db.find(status=PROCESSING, updated_before=now() - 15_minutes)
for enquiry in stale:
    # Re-enqueue for processing
    queue.enqueue("enquiry_processing", {
        "enquiry_id": enquiry.id,
        "attempt": 1,
        "reason": "stale_recovery"
    })
    audit.log(enquiry.id, "system", "stale_recovery", {})
```

## 6. Monitoring & Alerting

### Key Metrics

| Metric | Alert Threshold | Purpose |
|--------|----------------|---------|
| Enquiry processing latency (p95) | > 60 seconds | Detect processing slowdowns |
| Queue depth | > 100 messages | Detect processing backlog |
| DLQ depth | > 0 | Any DLQ entry needs attention |
| LLM error rate | > 5% in 5 minutes | Detect provider issues |
| CRM error rate | > 5% in 5 minutes | Detect CRM issues |
| Classification confidence (avg) | < 0.6 over 1 hour | Detect model degradation |
| Human approval queue age | > 4 hours | Prevent SLA breaches |
| Stale enquiry count | > 0 | Detect stuck processing |

### Structured Logging

Every log entry includes:
- `timestamp`: ISO 8601
- `level`: INFO, WARN, ERROR, CRITICAL
- `enquiry_id`: For tracing through the pipeline
- `step`: Current processing step
- `actor`: system, ai, or user ID
- `detail`: Structured context (never includes PII or secrets)

Example:
```json
{
  "timestamp": "2026-09-01T14:30:00Z",
  "level": "ERROR",
  "enquiry_id": "enq_abc123",
  "step": "llm_classification",
  "actor": "system",
  "detail": {
    "error": "timeout",
    "model": "gpt-4o-mini",
    "attempt": 2,
    "latency_ms": 30000
  }
}
```

## 7. Graceful Degradation

If a critical dependency is unavailable, the system degrades gracefully rather than crashing:

| Dependency Down | Degradation |
|----------------|-------------|
| LLM Provider | Enquiries queue up. Alert team. Human processes manually if sustained outage. |
| CRM | Enquiries processed and classified, but CRM writes deferred. Retry when CRM recovers. |
| Queue | Ingestion returns 503. Client retries. Alert team immediately. |
| Database | System cannot accept new enquiries. Returns 503. Alert team immediately. |
| Email/Messaging | Draft responses saved but not sent. Retry when service recovers. |
