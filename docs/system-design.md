# System Design — BEDA Enquiry Processing System

## 1. System Overview

This system processes inbound enquiries from multiple channels (email, web forms, WhatsApp, chat widgets) and turns them into structured, actionable CRM records with minimal manual effort. The core design philosophy is **"AI proposes, application decides"** — language models handle classification and data extraction, but deterministic code controls every side effect. No CRM record is created, no email is sent, and no contact is modified without passing through validation, policy rules, and human approval.

The system is built for reliability over cleverness. Every processing step is logged, every AI output is stored alongside its evidence, and every failure routes to a human reviewer rather than failing silently. This makes the system auditable, debuggable, and safe to operate even when the AI model produces unexpected output.

> For the executive summary and project overview, see the [README](../README.md).
> For the architecture diagram and component overview, see the [Architecture Document](../architecture/architecture.md).

---

## 2. Detailed Data Model

### Entity-Relationship Overview

```
enquiries ──1:N──▶ ai_extractions ──1:N──▶ extracted_fields
    │
    ├──1:N──▶ crm_changesets ──1:N──▶ approvals
    │              │
    │              └──N:1──▶ contacts
    │
    ├──1:N──▶ draft_responses ──1:N──▶ approvals
    │
    └──1:N──▶ audit_events
```

An **enquiry** is the central entity. It spawns **AI extractions** (which break down into **extracted fields**), **CRM changesets** (staged writes that require **approval**), **draft responses** (also requiring approval), and **audit events** (the immutable log of everything that happened).

---

### `enquiries`

The core table. Every inbound message — regardless of channel — becomes one row here.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `UUID` | `PK, DEFAULT gen_random_uuid()` | Internal identifier |
| `source_channel` | `VARCHAR(50)` | `NOT NULL` | Origin channel: `email`, `web_form`, `whatsapp`, `chat` |
| `external_message_id` | `VARCHAR(512)` | `UNIQUE` | Source-native ID (email Message-ID header, form submission UUID, WhatsApp message ID). Used for idempotency |
| `sender_email` | `VARCHAR(320)` | | Sender's email address, if available |
| `sender_name` | `VARCHAR(255)` | | Sender's display name, if available |
| `raw_content` | `TEXT` | `NOT NULL` | Original message content exactly as received (HTML, plaintext, etc.) |
| `normalized_content` | `TEXT` | | Cleaned version: HTML stripped, signatures removed, attachments extracted. This is what the AI sees |
| `status` | `VARCHAR(30)` | `NOT NULL, DEFAULT 'new'` | Processing state (see state machine below) |
| `idempotency_key` | `VARCHAR(128)` | `UNIQUE, NOT NULL` | SHA-256 hash of `source_channel + external_message_id` |
| `received_at` | `TIMESTAMPTZ` | | When the original message was sent/submitted |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL, DEFAULT NOW()` | Row creation time |
| `updated_at` | `TIMESTAMPTZ` | | Last status change time |

**Status state machine:**

```
new → processing → classified → pending_info → pending_approval → approved → completed
                       │                              │
                       └──────────► failed ◄───────────┘
                                      │
                                      ▼
                                   archived
```

- `new`: Received and persisted, not yet queued
- `processing`: Worker has picked up the job
- `classified`: AI classification and extraction complete
- `pending_info`: Missing required fields; info-request draft generated
- `pending_approval`: CRM changeset or response draft awaiting human review
- `approved`: Human approved; execution pending
- `completed`: All actions executed successfully
- `failed`: Processing error; routed to manual review
- `archived`: Terminal state (junk mail, or completed and closed)

**Key design decisions:**

- **`idempotency_key`** prevents duplicate processing when webhooks retry delivery. The `UNIQUE` constraint ensures that inserting a duplicate raises a conflict error, which the application catches and returns `200 OK` without reprocessing. This is essential because email services like SendGrid will retry on non-2xx responses.
- **`raw_content` is preserved** even after normalisation. If normalisation introduces a bug (e.g., aggressively strips content), we can re-normalise from the original without data loss.
- **Status is a string, not a PostgreSQL ENUM.** Adding a new status to a PostgreSQL ENUM requires an `ALTER TYPE` migration, which takes a full table lock. Using `VARCHAR` with application-level validation is safer for a system that will evolve. The application enforces valid transitions.

---

### `ai_extractions`

Stores the result of each AI classification and extraction run. Multiple rows per enquiry are expected — re-processing after prompt changes, or A/B comparison between models.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `UUID` | `PK` | |
| `enquiry_id` | `UUID` | `FK → enquiries, NOT NULL` | The enquiry this extraction belongs to |
| `model_name` | `VARCHAR(100)` | `NOT NULL` | Model identifier, e.g. `gpt-4o-mini` |
| `model_version` | `VARCHAR(100)` | | Model snapshot/version string from the API response |
| `classification` | `VARCHAR(30)` | `NOT NULL` | Result: `sales_opportunity`, `support_request`, `junk`, `unclear` |
| `confidence` | `FLOAT` | `NOT NULL, CHECK (0.0 <= confidence <= 1.0)` | Model's self-reported confidence score |
| `extracted_data` | `JSONB` | | Full structured extraction result (company name, contact info, product interest, etc.) |
| `raw_model_response` | `JSONB` | | Complete API response body — headers, usage stats, full output. Stored for debugging |
| `processing_time_ms` | `INTEGER` | | Wall-clock time for the API call |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL, DEFAULT NOW()` | |

**Key design decisions:**

- **`raw_model_response`** is stored in full. When an extraction looks wrong, engineers can inspect exactly what the model returned without needing to reproduce the API call. This is critical for debugging prompt regressions.
- **Multiple extractions per enquiry** supports re-processing. If we update the prompt or switch models, we can re-extract and compare results. The application uses the most recent extraction by default.
- **`extracted_data` uses JSONB** because the extraction schema evolves as we tune prompts. Adding a new field (e.g., `budget_range`) doesn't require a schema migration — just a prompt update and application code change.

---

### `extracted_fields`

Individual fields from an AI extraction, stored separately with per-field confidence and evidence. This is the most important table for trust and auditability.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `UUID` | `PK` | |
| `extraction_id` | `UUID` | `FK → ai_extractions, NOT NULL` | Parent extraction |
| `field_name` | `VARCHAR(100)` | `NOT NULL` | Field identifier: `company_name`, `contact_email`, `budget_range`, `product_interest`, etc. |
| `field_value` | `TEXT` | | The extracted value. `NULL` if the field is missing |
| `confidence` | `FLOAT` | `CHECK (0.0 <= confidence <= 1.0)` | Per-field confidence score |
| `evidence_text` | `TEXT` | | Exact quote from the source message that supports this extracted value |
| `source_type` | `VARCHAR(20)` | `NOT NULL` | How this value was obtained: `explicit` (directly stated), `inferred` (derived from context), `missing` (not found) |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL, DEFAULT NOW()` | |

**Key design decisions:**

- **Per-field confidence and evidence.** A human reviewer doesn't just see "company: Acme Corp" — they see "company: Acme Corp (confidence: 0.92, evidence: 'We at Acme Corp are interested in...', source: explicit)". This lets reviewers make informed trust decisions.
- **`source_type` distinguishes explicit vs. inferred values.** If the AI infers a company name from an email domain, that's useful but less reliable than a directly stated name. The system never silently promotes an inferred value to a fact — the reviewer sees the distinction.
- **Evidence grounding is verifiable.** During deterministic validation (Stage 6), the application checks whether `evidence_text` actually appears in `normalized_content`. If it doesn't, the field is flagged — the AI hallucinated the evidence.

---

### `contacts`

Known contacts, built up from enquiries and synced with the external CRM.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `UUID` | `PK` | |
| `email` | `VARCHAR(320)` | `UNIQUE` | Email address, normalised to lowercase |
| `phone` | `VARCHAR(20)` | | Phone number in E.164 format (e.g., `+6281234567890`) |
| `name` | `VARCHAR(255)` | | Full name |
| `company_name` | `VARCHAR(255)` | | Company name as provided |
| `normalized_company_name` | `VARCHAR(255)` | | Lowercase, stripped of common suffixes (`Ltd`, `Inc`, `PT`, `CV`) for fuzzy matching |
| `crm_external_id` | `VARCHAR(255)` | | ID in the external CRM system (HubSpot contact ID, Salesforce ID, etc.) |
| `source_enquiry_id` | `UUID` | `FK → enquiries` | Which enquiry first created this contact |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL, DEFAULT NOW()` | |
| `updated_at` | `TIMESTAMPTZ` | | |

**Key design decisions:**

- **`email` is unique and lowercase-normalised.** Email is the primary deduplication key. All comparisons are case-insensitive via stored normalisation.
- **`normalized_company_name`** enables simple fuzzy matching. "PT Acme Indonesia" and "Acme Indonesia, PT" both normalise to "acme indonesia". This catches obvious duplicates without requiring a full fuzzy-matching engine.
- **`crm_external_id`** links our internal contact to the CRM record. This is populated after a CRM write is executed, not when the contact is first created internally.

---

### `crm_changesets`

The staging area for CRM modifications. Changes are proposed here, reviewed, and only executed after approval.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `UUID` | `PK` | |
| `enquiry_id` | `UUID` | `FK → enquiries, NOT NULL` | Source enquiry |
| `contact_id` | `UUID` | `FK → contacts` | Target contact (if applicable) |
| `action_type` | `VARCHAR(30)` | `NOT NULL` | What to do: `create_contact`, `update_contact`, `create_lead`, `merge_contacts` |
| `proposed_changes` | `JSONB` | `NOT NULL` | Exact payload to send to the CRM API |
| `status` | `VARCHAR(20)` | `NOT NULL, DEFAULT 'pending'` | `pending` → `approved` → `executed` → (or `failed`), or `pending` → `rejected` |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL, DEFAULT NOW()` | |

**Key design decisions:**

- **This is the "air gap" between AI output and CRM mutation.** The AI never writes to the CRM directly. It proposes a changeset, and a human approves or rejects it. This prevents the AI from creating garbage contacts or corrupting existing records.
- **`proposed_changes` is a JSONB snapshot** of exactly what will be sent to the CRM API. The reviewer sees the exact payload, not an abstracted summary. What you approve is what gets executed.
- **Status transitions are enforced in application code:** `pending` can only move to `approved` or `rejected`. `approved` can only move to `executed` or `failed`. There is no path from `rejected` to `executed`.

---

### `approvals`

Tracks human review decisions for CRM changesets and draft responses.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `UUID` | `PK` | |
| `changeset_id` | `UUID` | `FK → crm_changesets, NULLABLE` | If approving a CRM changeset |
| `draft_response_id` | `UUID` | `FK → draft_responses, NULLABLE` | If approving a draft response |
| `action_category` | `VARCHAR(10)` | `NOT NULL` | Risk level: `low`, `medium`, `high` |
| `proposed_payload` | `JSONB` | `NOT NULL` | Frozen snapshot of what's being approved (so approval is immutable even if source data changes) |
| `status` | `VARCHAR(20)` | `NOT NULL, DEFAULT 'pending'` | `pending`, `approved`, `rejected` |
| `reviewer_id` | `VARCHAR(100)` | | Who made the decision |
| `reviewed_at` | `TIMESTAMPTZ` | | When the decision was made |
| `reviewer_notes` | `TEXT` | | Optional notes explaining the decision |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL, DEFAULT NOW()` | |

**Constraint:** Exactly one of `changeset_id` or `draft_response_id` must be non-null (`CHECK` constraint).

**Key design decisions:**

- **`proposed_payload` is a frozen snapshot.** Even if the underlying changeset or draft is modified after the approval record is created, the approval record shows exactly what was reviewed. This matters for audit.
- **`action_category` drives routing.** `high`-risk actions (e.g., merging contacts, deleting data) could require senior approval. `low`-risk actions (e.g., creating a new lead from a clear sales enquiry) could be routed to a broader pool. These rules live in the policy engine, not in this table.
- **`reviewer_notes`** captures the "why" behind rejections. This is valuable for improving the system — if reviewers consistently reject a certain type of extraction, the prompt needs adjustment.

---

### `draft_responses`

AI-generated response drafts that require human approval before sending.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `UUID` | `PK` | |
| `enquiry_id` | `UUID` | `FK → enquiries, NOT NULL` | Source enquiry |
| `draft_content` | `TEXT` | `NOT NULL` | AI-generated draft text |
| `draft_type` | `VARCHAR(30)` | `NOT NULL` | `acknowledgement`, `info_request`, `routing_note` |
| `model_name` | `VARCHAR(100)` | | Which model generated this draft |
| `status` | `VARCHAR(20)` | `NOT NULL, DEFAULT 'pending'` | `pending`, `approved`, `edited`, `sent`, `rejected` |
| `approved_content` | `TEXT` | | Final version after human review/editing |
| `approved_by` | `VARCHAR(100)` | | Who approved or edited |
| `sent_at` | `TIMESTAMPTZ` | | When the response was actually sent |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL, DEFAULT NOW()` | |

**Key design decisions:**

- **`approved_content` is separate from `draft_content`.** This preserves the original AI draft so we can measure edit distance — how much did the human change? High edit rates on a particular draft type signal that the prompt needs improvement.
- **`edited` status** means the reviewer modified the draft before approving. The system tracks the distinction between "approved as-is" and "approved with edits" for quality measurement.
- **No auto-send.** Even acknowledgement emails go through the approval queue. The cost of a bad auto-sent email (brand damage, wrong info) far exceeds the cost of a 5-minute delay for human review.

---

### `audit_events`

The immutable event log. Every significant action in the system produces an audit event.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | `UUID` | `PK, DEFAULT gen_random_uuid()` | |
| `enquiry_id` | `UUID` | `NULLABLE` | Associated enquiry (null for system-level events like startup, config changes) |
| `event_type` | `VARCHAR(100)` | `NOT NULL` | Event identifier (see list below) |
| `actor_id` | `VARCHAR(100)` | | Who or what triggered the event |
| `actor_type` | `VARCHAR(20)` | `NOT NULL` | `system`, `ai`, `human` |
| `event_data` | `JSONB` | | Full context — varies by event type |
| `ip_address` | `VARCHAR(45)` | | Source IP for webhook and user-initiated events |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL, DEFAULT NOW()` | |

**Event types include:**
- `enquiry_received` — new enquiry ingested
- `classification_complete` — AI classification finished
- `validation_passed` / `validation_failed` — deterministic checks
- `evidence_grounding_failed` — AI-provided evidence not found in source text
- `dedup_match_found` — duplicate contact detected
- `changeset_staged` — CRM changeset created
- `approval_requested` — item sent to review queue
- `approved` / `rejected` — human decision recorded
- `crm_write_executed` / `crm_write_failed` — CRM API call result
- `response_sent` — outbound email/message sent
- `error_occurred` — unhandled error captured
- `manual_review_assigned` — enquiry routed to manual processing

**Key design decisions:**

- **The database role used by the application has `INSERT` only on this table — no `UPDATE`, no `DELETE`.** This is enforced at the PostgreSQL permission level, not just application code. Even if the application has a bug, it cannot modify or delete audit records.
- **`event_data` is JSONB** to accommodate different event types without schema changes. A `classification_complete` event includes the classification result and confidence; an `approved` event includes the reviewer ID and approval payload.
- **`actor_type` distinguishes system, AI, and human actions.** This makes it trivial to filter for "show me everything the AI did" or "show me all human decisions" when investigating an issue.

---

## 3. Technology Choices Deep Dive

### Why Python + FastAPI

**FastAPI** is the backend framework for this system. The choice is driven by practical considerations:

- **Async support.** FastAPI is built on Starlette and supports `async/await` natively. Webhook endpoints need to accept requests quickly, enqueue work, and return — async handlers are a natural fit.
- **LLM ecosystem.** The `openai` Python SDK, `tiktoken` for token counting, and most AI tooling is Python-first. Using Python avoids language-boundary friction.
- **Automatic OpenAPI documentation.** FastAPI generates interactive API docs from type annotations. This accelerates development and makes the API self-documenting for team members.
- **Familiarity.** I've built REST APIs in Python before. Reducing framework-learning risk is pragmatic for a project where the complexity is in the pipeline logic, not the framework.
- **Pydantic for validation.** FastAPI uses Pydantic models for request/response validation. The same Pydantic models define the structured output schemas sent to the LLM, keeping the data contracts consistent.

**Alternative considered:** Node.js + Express. Viable and fast, but Python's AI/ML tooling is significantly stronger. TypeScript would add type safety, but Pydantic + Python type hints cover the same ground for this use case.

### Why PostgreSQL

**PostgreSQL 15+** is the primary data store.

- **JSONB columns.** AI extraction results have a semi-structured shape that evolves as prompts are tuned. JSONB lets us store flexible data without constant schema migrations, while still supporting indexing and querying (`@>`, `->>` operators).
- **ACID compliance.** The audit trail requires strong consistency guarantees. A partially-written audit event or a phantom changeset approval would undermine the system's trustworthiness. PostgreSQL's transaction semantics prevent this.
- **Permission-level access control.** PostgreSQL roles and grants let us enforce append-only on the `audit_events` table at the database level, not just application code. The application's database user literally cannot execute `UPDATE` or `DELETE` on that table.
- **Mature and battle-tested.** PostgreSQL is well-documented, has a large community, and handles the scale we need (hundreds of enquiries/day) with minimal tuning.
- **Full-text search.** If BEDA later wants to search enquiries by keyword, PostgreSQL's `tsvector`/`tsquery` is built-in. No need for a separate Elasticsearch instance at this scale.

**Alternative considered:** MongoDB. Its flexible schema is appealing for AI outputs, but MongoDB's weaker transaction guarantees and the lack of row-level permission controls make it a poor fit for an audit-critical system.

### Why Redis + RQ

**Redis** serves as both the job queue backend and a cache. **RQ (Redis Queue)** is the Python job queue library.

- **Simplicity.** RQ has a minimal API: enqueue a function, workers pick it up, done. At the expected throughput (hundreds of enquiries/day), we don't need the complexity of a distributed streaming platform.
- **Python-native.** RQ workers are Python processes that import your functions directly. No serialisation protocol to debug, no separate configuration language.
- **Redis as cache.** The same Redis instance caches CRM API responses and enrichment data (e.g., company lookups) with TTL-based expiry. One less infrastructure component to manage.
- **Retry and failure handling.** RQ supports automatic retry with configurable backoff. Failed jobs are moved to a failed queue for inspection. This maps well to our "failures route to manual review" principle.

**Alternative considered:** Celery. More features (scheduled tasks, result backends, canvas workflows) but significantly more complexity. RQ's simplicity is a feature at this scale, not a limitation.

**Alternative considered:** Temporal. A powerful workflow orchestration engine that would model the pipeline stages elegantly. But Temporal introduces significant operational overhead (its own server cluster, visibility store, etc.) that isn't justified for this throughput.

**Alternative considered:** Kafka. Built for millions of events/second across distributed consumers. Massive overkill for hundreds of enquiries/day. The operational cost of running Kafka (ZooKeeper, partitions, consumer groups, offset management) would dwarf the application itself.

### Why OpenAI GPT-4o-mini / GPT-4o

The system uses a **tiered model strategy** — cheaper models for bulk work, expensive models only where quality matters.

| Task | Model | Cost | Reasoning |
|---|---|---|---|
| Classification + extraction | GPT-4o-mini | ~$0.15/1M input tokens | High accuracy for structured tasks. Structured output (JSON Schema mode) eliminates parsing failures |
| Response drafting | GPT-4o | ~$2.50/1M input tokens | Customer-facing text requires higher quality. Still cheaper than a human writing from scratch |

- **Structured output / JSON Schema mode.** The API is instructed to return output conforming to a specific JSON Schema. This eliminates the "parse the markdown/freeform response" failure mode that plagues many LLM integrations. If the model can't fill a field, it returns `null` — it doesn't hallucinate a markdown table.
- **Cost at scale.** At 500 enquiries/day with an average of 500 tokens per enquiry: classification costs ~$0.04/day. Response drafting (assuming 200 drafts/day at 1000 tokens each) costs ~$0.50/day. Total LLM cost is under $20/month. This is negligible compared to the human time saved.

**Alternative considered:** Claude (Anthropic). Comparable quality, strong at following instructions. Could be swapped in without architectural changes.

**Alternative considered:** Gemini (Google). Gemini Flash offers very low cost for classification tasks. A viable alternative, especially if the project later needs multimodal capabilities (processing image attachments).

**Alternative considered:** Self-hosted models via Ollama (e.g., Qwen). I experimented with this approach in a campus room reservation project — it works for development and offers data privacy benefits. However, for production, a cloud API provides better reliability, lower operational burden, and access to more capable models. The architecture abstracts the LLM behind a service interface, so switching providers requires only a new adapter implementation.

### Why Generic CRM Abstraction

BEDA's specific CRM system is not specified in the brief, so the system uses an **adapter pattern**:

```python
class CRMAdapter(Protocol):
    def find_contact(self, email: str) -> Optional[CRMContact]: ...
    def create_contact(self, data: ContactCreate) -> CRMContact: ...
    def update_contact(self, id: str, data: ContactUpdate) -> CRMContact: ...
    def create_lead(self, data: LeadCreate) -> CRMLead: ...
```

- **Concrete implementations** would be written for HubSpot, Salesforce, Zoho, or whatever BEDA uses. Each implementation translates the generic interface to the specific CRM's API and field mapping.
- **Testing benefit.** A `MockCRMAdapter` returns predictable responses, making integration tests fast and deterministic. No need to hit a real CRM API during development.
- **Migration safety.** If BEDA switches CRM providers, only the adapter implementation changes. The pipeline, validation, and approval logic remain untouched.

---

## 4. Processing Pipeline Detail

Each stage is a discrete, testable function. Stages communicate through the database — each stage reads the current state, does its work, writes the result, and updates the status. This makes the pipeline resumable: if a worker crashes between stages, the next worker picks up from the last committed state.

> For the complete pseudocode implementation, see the [Pipeline Pseudocode](../pseudocode/enquiry_pipeline.py).

### Stage 1: Ingestion

Inbound messages arrive through channel-specific webhooks and endpoints:

| Channel | Endpoint | Mechanism |
|---|---|---|
| Email | `POST /api/v1/webhooks/email` | SendGrid/Mailgun inbound parse webhook |
| Web form | `POST /api/v1/enquiries` | Direct form POST from BEDA's website |
| WhatsApp | `POST /api/v1/webhooks/whatsapp` | WhatsApp Business API webhook |
| Chat widget | `POST /api/v1/webhooks/chat` | Chat platform webhook (Intercom, Crisp, etc.) |

Each webhook endpoint:
1. **Verifies the request signature** (HMAC-SHA256) using the provider's signing secret. Unverified requests are rejected with `401`.
2. **Extracts** channel-specific fields (email headers, form fields, message body) into a common `EnquiryPayload` Pydantic model.
3. **Returns `200 OK` quickly** — all processing happens asynchronously after ingestion.

### Stage 2: Normalisation

Pure deterministic string processing — no AI involved.

- **Strip HTML tags** from email bodies, preserving text content. Uses a whitelist-based sanitiser (not regex) to handle malformed HTML safely.
- **Remove email signatures.** Heuristic-based: split on `--`, `___`, or common signature patterns (`Sent from my iPhone`, `Best regards,`). Conservative — if uncertain, keeps the content.
- **Extract attachment metadata** (filename, MIME type, size). Attachment content is stored separately (filesystem or object storage), not in the database. The normalised content includes a reference like `[Attachment: proposal.pdf, 2.1MB]`.
- **Build canonical JSON:**
  ```json
  {
    "source": "email",
    "sender_email": "john@acme.com",
    "sender_name": "John Smith",
    "body": "We are interested in your villa management services for our property in Canggu...",
    "attachments": [{"filename": "proposal.pdf", "type": "application/pdf", "size_bytes": 2200000}],
    "received_at": "2026-09-01T10:30:00Z"
  }
  ```

### Stage 3: Idempotency Check

Prevents duplicate processing when webhooks retry delivery.

1. Compute `idempotency_key = SHA-256(source_channel + ":" + external_message_id)`.
2. Attempt `INSERT INTO enquiries ... ON CONFLICT (idempotency_key) DO NOTHING`.
3. If the insert succeeds → new enquiry, proceed.
4. If the insert is a no-op (conflict) → duplicate. Return `200 OK` to the webhook provider but do not enqueue any work. Log the duplicate event to `audit_events`.

This uses PostgreSQL's `ON CONFLICT` clause, which is atomic — no race condition between "check if exists" and "insert".

### Stage 4: Queue

```python
rq_queue.enqueue(
    process_enquiry,
    enquiry_id,
    retry=Retry(max=3, interval=[30, 120, 300]),  # retry after 30s, 2min, 5min
    job_timeout="5m",
)
```

- The enquiry status is updated to `processing`.
- RQ workers pick up jobs from the queue and execute them.
- If a job fails after all retries, it moves to the failed queue and the enquiry status is set to `failed`, routing it to manual review.

### Stage 5: AI Classification + Extraction

The worker loads the enquiry's `normalized_content` and calls GPT-4o-mini with a structured output schema.

**System prompt includes:**
- Classification definitions with examples (what counts as `sales_opportunity` vs. `support_request` vs. `junk` vs. `unclear`)
- Extraction field list with descriptions
- Grounding rules: "Only extract information explicitly stated or directly inferable from the text. If a field is not present, return null. For each extracted field, provide an exact quote from the text as evidence."
- Output JSON Schema enforced via OpenAI's `response_format` parameter

**The structured output schema ensures:**
- Classification is one of the valid enum values
- Confidence is a float between 0 and 1
- Each field has a value, confidence, evidence, and source_type
- The model cannot return free-form text — it must conform to the schema

The full API response (including token usage and model version) is stored in `ai_extractions.raw_model_response`. Individual fields are stored in `extracted_fields`.

### Stage 6: Deterministic Validation

After AI extraction, deterministic code validates every field:

| Check | Action on failure |
|---|---|
| Email format (regex: RFC 5322 simplified) | Null out the field, flag for review |
| Phone format (E.164: `+` followed by 7-15 digits) | Null out the field |
| Confidence value is 0.0–1.0 | Reject the extraction, re-queue |
| Classification is a known enum value | Reject the extraction, flag for review |
| Evidence text appears in `normalized_content` | Flag the field as `evidence_grounding_failed` |

**Evidence grounding check** is critical. If the AI claims evidence "We need 50 villas managed" but that text doesn't appear in the normalised content, the AI hallucinated the evidence. The field is flagged, and the reviewer sees a warning. This is a simple substring check — cheap and effective.

### Stage 7: Gap Detection

Each classification has a set of required fields:

| Classification | Required fields |
|---|---|
| `sales_opportunity` | `company_name`, `contact_name`, `contact_email`, `product_interest` |
| `support_request` | `contact_name`, `contact_email` |
| `junk` | (none) |
| `unclear` | (none) |

Any required field that is `null` or has `source_type = 'missing'` is added to a `missing_fields` list. If the list is non-empty, the enquiry status moves to `pending_info` and the system drafts an info-request response (Stage 11).

### Stage 8: Duplicate Detection

Deterministic deduplication against existing contacts:

1. **Exact email match:** `SELECT * FROM contacts WHERE email = lower(:extracted_email)`. If found → `exact_match`.
2. **Phone match:** If email doesn't match but phone does → `probable_match`.
3. **Company name similarity:** Compare `normalized_company_name` values. If identical → `probable_match`. (Future enhancement: Levenshtein distance or trigram similarity via `pg_trgm`.)
4. **No match found:** `no_match` — will create a new contact.

Dedup results are stored in the `event_data` of a `dedup_match_found` audit event, including which fields matched and the matched contact ID.

### Stage 9: Policy Engine

Deterministic routing rules — no AI involved. Implemented as a series of `if/elif` conditions:

```python
if classification == "junk" and confidence >= 0.95:
    action = "auto_archive"         # log and archive, no human review needed
elif classification == "unclear" or confidence < 0.7:
    action = "manual_review"        # route to human, AI wasn't confident enough
elif missing_fields:
    action = "request_info"         # draft an info-request email
elif classification == "sales_opportunity" and confidence >= 0.8:
    action = "route_to_sales"       # create CRM lead, notify sales team
elif classification == "support_request" and confidence >= 0.8:
    action = "route_to_support"     # create CRM ticket, notify support team
else:
    action = "manual_review"        # default: human decides
```

These thresholds are configurable. Starting conservative (high confidence required) and loosening over time as the system proves reliable is the right approach.

### Stage 10: CRM Staging

Based on the policy engine's decision and the dedup result, the system builds a CRM changeset:

- **`no_match` + `sales_opportunity`:** Changeset with `action_type = 'create_contact'` + `action_type = 'create_lead'`.
- **`exact_match` + `sales_opportunity`:** Changeset with `action_type = 'create_lead'` linked to the existing contact.
- **`probable_match`:** Changeset with `action_type = 'merge_contacts'` — higher risk, higher `action_category`.

The changeset's `proposed_changes` JSONB contains the exact API payload. For example:

```json
{
  "action_type": "create_contact",
  "payload": {
    "email": "john@acme.com",
    "first_name": "John",
    "last_name": "Smith",
    "company": "Acme Corp",
    "source": "inbound_enquiry"
  }
}
```

The changeset status is `pending`. It is **not** sent to the CRM.

### Stage 11: Response Drafting

If the policy engine determines a response is needed (acknowledgement, info request, or routing note), the system calls GPT-4o:

- **Few-shot examples** from previously approved responses are included in the prompt. This steers tone and format toward what BEDA has already approved.
- **Template variables** (sender name, company, missing fields) are injected into the prompt.
- The draft is stored in `draft_responses` with `status = 'pending'`.
- The draft is **not sent** — it goes to the approval queue.

### Stage 12: Approval Queue

An approval record is created for each pending changeset and/or draft response:

1. `action_category` is assigned based on the action type:
   - `low`: Create a new lead from a clear sales enquiry
   - `medium`: Create or update a contact
   - `high`: Merge contacts, modify existing CRM records
2. The assigned reviewer is notified (dashboard notification, email, or Slack message).
3. The enquiry status is updated to `pending_approval`.

The reviewer sees:
- Original enquiry content
- AI classification with confidence
- Extracted fields with evidence and confidence
- Proposed CRM changes (exact payload)
- Draft response (if applicable)
- Any validation warnings or dedup matches

### Stage 13: Execution (After Approval)

When a reviewer approves:

1. The application re-checks the approval status in the database (defense against race conditions or UI bugs).
2. **CRM write:** Call the CRM adapter with the approved changeset payload. If the API call fails, the changeset status moves to `failed` and an `error_occurred` audit event is logged. The system does not retry automatically — a human must investigate and re-approve.
3. **Response send:** Send the approved (or edited) response via the appropriate channel (email API for email enquiries, WhatsApp API for WhatsApp enquiries).
4. **Status update:** Enquiry status moves to `completed`.
5. **Audit logging:** Every action is recorded — who approved, what was executed, what the CRM API returned, when the email was sent.

When a reviewer rejects:

1. The changeset/draft status moves to `rejected`.
2. `reviewer_notes` captures the reason.
3. The enquiry status may move to `archived` (if it was junk the AI missed) or remain open for manual handling.

---

## 5. API Endpoints

All endpoints require authentication via Bearer token (JWT). Webhook endpoints verify provider-specific HMAC signatures instead of Bearer tokens.

### Webhook Endpoints (Inbound)

| Method | Path | Description | Auth |
|---|---|---|---|
| `POST` | `/api/v1/webhooks/email` | Inbound email (SendGrid/Mailgun parse) | HMAC signature |
| `POST` | `/api/v1/webhooks/whatsapp` | Inbound WhatsApp message | HMAC signature |
| `POST` | `/api/v1/webhooks/chat` | Inbound chat widget message | HMAC signature |

### Enquiry Endpoints

| Method | Path | Description | Auth |
|---|---|---|---|
| `POST` | `/api/v1/enquiries` | Submit a web form enquiry | API key |
| `GET` | `/api/v1/enquiries` | List enquiries (filterable by status, channel, date range) | Bearer JWT |
| `GET` | `/api/v1/enquiries/{id}` | Get full enquiry detail (includes extractions, changesets, drafts) | Bearer JWT |

### Approval Endpoints

| Method | Path | Description | Auth |
|---|---|---|---|
| `GET` | `/api/v1/approvals/pending` | List all pending approval items | Bearer JWT |
| `GET` | `/api/v1/approvals/{id}` | Get approval detail (includes proposed payload, source enquiry) | Bearer JWT |
| `POST` | `/api/v1/approvals/{id}/approve` | Approve an action (body: optional `reviewer_notes`) | Bearer JWT |
| `POST` | `/api/v1/approvals/{id}/reject` | Reject an action (body: required `reviewer_notes`) | Bearer JWT |

### Audit Endpoints

| Method | Path | Description | Auth |
|---|---|---|---|
| `GET` | `/api/v1/audit/{enquiry_id}` | Get full audit trail for an enquiry | Bearer JWT |
| `GET` | `/api/v1/audit` | Search audit events (filterable by event_type, actor, date range) | Bearer JWT |

### Design Notes on API Security

- **Webhook endpoints do not use Bearer tokens.** They verify the request signature using the provider's shared secret (HMAC-SHA256). The signing secret is stored in environment variables (dev) or a secret manager (prod). Unverified webhooks are rejected with `401 Unauthorized`.
- **Rejection requires notes.** The `POST .../reject` endpoint requires `reviewer_notes` in the request body. This ensures rejections are documented for future prompt improvement.
- **Rate limiting** is applied to all endpoints via middleware. Webhook endpoints have higher limits (providers may send bursts). API endpoints have standard limits per API key / user.
- **All responses include `X-Request-ID`** for request tracing across logs.

---

## Cross-References

- **[Architecture Diagram](../architecture/architecture.md)** — visual component overview and data flow
- **[Security and Risk Analysis](security-and-risk.md)** — threat model, mitigation strategies, data privacy
- **[Failure and Reliability](failure-and-reliability.md)** — failure modes, recovery procedures, monitoring
- **[Pipeline Pseudocode](../pseudocode/enquiry_pipeline.py)** — implementation-level pseudocode for the processing pipeline
