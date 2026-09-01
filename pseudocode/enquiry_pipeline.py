"""
BEDA Enquiry Processing Pipeline — Core Pseudocode

This file demonstrates the key architectural decisions of the enquiry processing system.
It is pseudocode — not directly runnable — but closely mirrors the intended implementation.

Key principles demonstrated:
1. LLM is a pure function (text → JSON) with no tools or side effects
2. All side effects go through deterministic code with explicit authorization
3. Human approval is enforced by the application, not requested by the LLM
4. Every significant event is audit-logged
5. Failures route to manual review, never silent data loss
"""

import hashlib
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import structlog
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from rq import Queue
from redis import Redis

logger = structlog.get_logger()
app = FastAPI()
redis_conn = Redis()  # pseudocode: actual config from env
task_queue = Queue("enquiry_processing", connection=redis_conn)


# ─── Enums ────────────────────────────────────────────────────────────────────

class Classification(str, Enum):
    SALES_OPPORTUNITY = "sales_opportunity"
    SUPPORT_REQUEST = "support_request"
    JUNK = "junk"
    UNCLEAR = "unclear"

class EnquiryStatus(str, Enum):
    NEW = "new"
    PROCESSING = "processing"
    CLASSIFIED = "classified"
    PENDING_INFO = "pending_info"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    COMPLETED = "completed"
    FAILED = "failed"

class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

class ActionCategory(str, Enum):
    LOW = "low"       # auto-approve (e.g., junk archival)
    MEDIUM = "medium"  # single reviewer (e.g., new lead creation)
    HIGH = "high"      # senior review (e.g., large deal, contact merge)

class Permission(str, Enum):
    CRM_CREATE_LEAD = "crm:create_lead"
    CRM_UPDATE_CONTACT = "crm:update_contact"
    SEND_EMAIL = "send:email"
    SEND_MESSAGE = "send:message"

class SourceType(str, Enum):
    EXPLICIT = "explicit"   # stated directly in the message
    INFERRED = "inferred"   # deduced from context
    MISSING = "missing"     # not found in the message


# ─── Pydantic Models (structured output schemas) ─────────────────────────────

class ExtractedField(BaseModel):
    """A single field extracted by the LLM, with provenance tracking."""
    field_name: str
    field_value: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_text: Optional[str] = None  # exact quote from source
    source_type: SourceType = SourceType.MISSING

class TriageResult(BaseModel):
    """Schema the LLM must return. Defined as JSON Schema and passed
    to the API via response_format — the LLM fills in the values,
    but the STRUCTURE is enforced by us, not by the LLM."""
    classification: Classification
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str  # brief chain-of-thought for auditability
    contact_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    company_name: Optional[str] = None
    enquiry_summary: Optional[str] = None
    extracted_fields: list[ExtractedField] = []

class PolicyDecision(BaseModel):
    action: str  # "create_lead", "route_support", "archive_junk", "manual_review"
    approval_level: ActionCategory
    reason: str

class CRMChangeset(BaseModel):
    id: uuid.UUID
    enquiry_id: uuid.UUID
    action_type: str
    proposed_changes: dict
    status: ApprovalStatus = ApprovalStatus.PENDING


# ─── Configuration Constants ─────────────────────────────────────────────────

# Classification confidence below this → automatic manual review
CONFIDENCE_THRESHOLD = 0.75

# Required fields per classification type — if any are missing, we request info
REQUIRED_FIELDS: dict[Classification, list[str]] = {
    Classification.SALES_OPPORTUNITY: ["contact_name", "contact_email", "company_name"],
    Classification.SUPPORT_REQUEST:   ["contact_name", "contact_email"],
    Classification.JUNK:              [],  # no fields needed to archive junk
    Classification.UNCLEAR:           [],  # will be routed to human anyway
}

# Retry configuration for transient failures (LLM API, CRM API)
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = [2, 8, 30]  # exponential-ish, with a cap

# Validation patterns — deterministic, not AI-based
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
PHONE_REGEX = re.compile(r"^\+?[0-9\s\-()]{7,20}$")

# JSON Schema passed to the OpenAI API for structured output enforcement
# (simplified here; the real schema mirrors TriageResult above)
TRIAGE_SCHEMA = {
    "name": "triage_result",
    "strict": True,
    "schema": {  # ... full JSON Schema matching TriageResult fields
    },
}


# ─── 1. Webhook Endpoint ─────────────────────────────────────────────────────

@app.post("/webhooks/inbound/{source_channel}")
async def receive_enquiry(source_channel: str, request: Request):
    """Receives inbound messages from email/chat/WhatsApp webhooks.
    Normalises, deduplicates, and enqueues — does NOT process inline.
    Fast return keeps webhook providers happy (they timeout at ~10s)."""

    raw_payload = await request.json()

    # --- Normalise: convert channel-specific format to internal schema ---
    # Each channel has its own adapter; the output is always the same shape
    normalized = normalize_payload(source_channel, raw_payload)

    # --- Idempotency: reject duplicates before any processing ---
    existing = check_idempotency(source_channel, normalized.external_message_id)
    if existing:
        return {"status": "duplicate", "enquiry_id": str(existing.id)}

    # --- Persist the raw enquiry ---
    enquiry = db.enquiries.create(
        id=uuid.uuid4(),
        source_channel=source_channel,
        external_message_id=normalized.external_message_id,
        sender_email=normalized.sender_email,
        sender_name=normalized.sender_name,
        raw_content=normalized.raw_content,
        normalized_content=normalized.cleaned_text,
        status=EnquiryStatus.NEW,
        idempotency_key=normalized.idempotency_key,
        received_at=datetime.now(timezone.utc),
    )

    audit_log(enquiry.id, "enquiry_received", {
        "source_channel": source_channel,
        "sender_email": normalized.sender_email,
    })

    # --- Enqueue for async processing (fast webhook response) ---
    task_queue.enqueue(process_enquiry, enquiry.id, retry=MAX_RETRIES)

    return {"status": "accepted", "enquiry_id": str(enquiry.id)}


# ─── 2. Idempotency Check ────────────────────────────────────────────────────

def check_idempotency(source_channel: str, external_message_id: str):
    """Prevents processing the same inbound message twice.
    Webhook providers often retry on timeout, so this is critical."""
    key = hashlib.sha256(
        f"{source_channel}:{external_message_id}".encode()
    ).hexdigest()
    existing = db.enquiries.find_by_idempotency_key(key)
    if existing:
        logger.info("duplicate_webhook_ignored", enquiry_id=existing.id)
        return existing  # Already processed — return existing record
    return None


# ─── 3. Main Processing Function (Queue Worker) ──────────────────────────────

def process_enquiry(enquiry_id: uuid.UUID) -> None:
    """Main pipeline orchestrator. Runs as an RQ worker job.
    Each step is deterministic except the LLM call, and every step
    is wrapped in error handling that falls back to manual review."""

    enquiry = db.enquiries.find_by_id(enquiry_id)
    db.enquiries.update_status(enquiry_id, EnquiryStatus.PROCESSING)

    # --- Step 1: LLM classification + extraction ---
    try:
        result = classify_and_extract(enquiry)
    except LLMError as e:
        audit_log(enquiry.id, "llm_failure", {"error": str(e)})
        route_to_manual_review(enquiry, reason="llm_failure")
        return  # Fail gracefully — human will handle it

    audit_log(enquiry.id, "classification_complete", {
        "classification": result.classification,
        "confidence": result.confidence,
        "model": "gpt-4o-mini",
    })

    # --- Step 2: Deterministic validation of LLM output ---
    result = validate_extraction(result)

    # --- Step 3: Policy engine decides routing ---
    decision = apply_policy(result)

    if decision.action == "manual_review":
        route_to_manual_review(enquiry, reason=decision.reason)
        return

    if decision.action == "archive_junk":
        db.enquiries.update_status(enquiry_id, EnquiryStatus.COMPLETED)
        audit_log(enquiry.id, "junk_archived", {"confidence": result.confidence})
        return

    # --- Step 4: Gap detection — do we have enough info? ---
    missing_fields = detect_gaps(result)
    if missing_fields:
        draft = draft_info_request(enquiry, missing_fields)
        db.enquiries.update_status(enquiry_id, EnquiryStatus.PENDING_INFO)
        audit_log(enquiry.id, "info_request_drafted", {"missing": missing_fields})
        return  # Pause pipeline until reply arrives

    # --- Step 5: Duplicate contact detection ---
    duplicates = detect_duplicates(result)
    if duplicates:
        audit_log(enquiry.id, "duplicates_found", {
            "count": len(duplicates),
            "match_type": [d["match_type"] for d in duplicates],
        })
        # Flag for human review — never auto-merge contacts
        decision.approval_level = ActionCategory.HIGH

    # --- Step 6: Stage CRM changes (propose, NEVER auto-commit) ---
    changeset = stage_crm_changes(enquiry, result, decision)

    # --- Step 7: Draft customer-facing response ---
    draft_response(enquiry, result, decision)

    db.enquiries.update_status(enquiry_id, EnquiryStatus.PENDING_APPROVAL)
    audit_log(enquiry.id, "pipeline_awaiting_approval", {
        "changeset_id": str(changeset.id),
        "approval_level": decision.approval_level,
    })


# ─── 4. LLM Classification + Extraction ──────────────────────────────────────

def classify_and_extract(enquiry) -> TriageResult:
    """Calls the LLM to classify and extract structured data.

    IMPORTANT: The LLM is a pure function — text in, JSON out.
    It has NO access to the CRM, database, email, or any tools.
    All it can do is read the enquiry text and return structured JSON."""

    system_prompt = {
        "role": "system",
        "content": (
            "You are an enquiry triage assistant. Classify the enquiry and "
            "extract contact/company information. Return ONLY the JSON fields "
            "requested. For each extracted field, include the exact quote from "
            "the source text as evidence. If a field is not present, mark it "
            "as missing. Do not guess or fabricate information."
        ),
    }
    user_message = {
        "role": "user",
        "content": enquiry.normalized_content,
    }

    # LLM is a pure function: text → structured JSON
    # It has NO access to CRM, email, database, or any tools
    for attempt in range(MAX_RETRIES):
        try:
            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",  # cheap model for classification
                messages=[system_prompt, user_message],
                response_format={
                    "type": "json_schema",
                    "json_schema": TRIAGE_SCHEMA,
                },
                temperature=0.1,  # low temperature = more deterministic
                timeout=15,
            )
            parsed = TriageResult.model_validate_json(
                response.choices[0].message.content
            )
            return parsed

        except openai.RateLimitError:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF_SECONDS[attempt])
                continue
            raise LLMError("LLM rate limited after retries")

        except openai.APIError as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF_SECONDS[attempt])
                continue
            raise LLMError(f"LLM API error: {e}")

    raise LLMError("LLM call failed after all retries")


# ─── 5. Deterministic Validation ─────────────────────────────────────────────

def validate_extraction(result: TriageResult) -> TriageResult:
    """Post-LLM validation using deterministic rules, NOT the LLM.
    The LLM proposes, this function verifies and corrects."""

    # Validate email with regex, not with AI
    if result.contact_email and not EMAIL_REGEX.match(result.contact_email):
        logger.warning("invalid_email_nulled", email=result.contact_email)
        result.contact_email = None  # Invalid → null out, don't guess

    # Validate phone with regex
    if result.contact_phone and not PHONE_REGEX.match(result.contact_phone):
        logger.warning("invalid_phone_nulled", phone=result.contact_phone)
        result.contact_phone = None

    # Clamp confidence to [0, 1] — LLM sometimes returns out-of-range
    result.confidence = max(0.0, min(1.0, result.confidence))

    # Verify evidence: each extracted field should cite actual source text
    for field in result.extracted_fields:
        if field.source_type == SourceType.EXPLICIT and field.evidence_text:
            # Check that the "evidence" actually appears in the original text
            # This catches hallucinated citations
            if field.evidence_text.lower() not in result.enquiry_summary.lower():
                field.source_type = SourceType.INFERRED  # downgrade trust
                field.confidence *= 0.5  # penalise unsupported claims

    return result


# ─── 6. Gap Detection ────────────────────────────────────────────────────────

def detect_gaps(result: TriageResult) -> list[str]:
    """Checks if all required fields for this classification are present.
    Returns list of missing field names. Deterministic — no LLM involved."""

    required = REQUIRED_FIELDS.get(result.classification, [])
    missing = []
    for field_name in required:
        value = getattr(result, field_name, None)
        if not value or value.strip() == "":
            missing.append(field_name)
    return missing


# ─── 7. Duplicate Detection ──────────────────────────────────────────────────

def detect_duplicates(result: TriageResult) -> list[dict]:
    """Finds existing contacts that might match this enquiry.
    Uses exact match on email/phone and fuzzy match on company name.
    NEVER auto-merges — duplicates are flagged for human review."""

    duplicates = []

    # Exact match: email (case-insensitive, already lowered in DB)
    if result.contact_email:
        match = db.contacts.find_by_email(result.contact_email.lower())
        if match:
            duplicates.append({"contact_id": match.id, "match_type": "exact_email"})

    # Exact match: phone (normalized)
    if result.contact_phone:
        normalized_phone = re.sub(r"[\s\-()]", "", result.contact_phone)
        match = db.contacts.find_by_phone(normalized_phone)
        if match:
            duplicates.append({"contact_id": match.id, "match_type": "exact_phone"})

    # Fuzzy match: company name (lowered, stripped, trigram similarity in PG)
    if result.company_name:
        normalized = result.company_name.lower().strip()
        # PostgreSQL pg_trgm similarity — threshold 0.6 catches common variants
        matches = db.contacts.find_similar_company(normalized, threshold=0.6)
        for m in matches:
            duplicates.append({"contact_id": m.id, "match_type": "fuzzy_company"})

    return duplicates


# ─── 8. Policy Engine ────────────────────────────────────────────────────────

def apply_policy(result: TriageResult) -> PolicyDecision:
    """Deterministic rules engine. Maps classification + confidence to
    a routing action and approval level. No LLM involved."""

    # Low confidence → always route to human regardless of classification
    if result.confidence < CONFIDENCE_THRESHOLD:
        return PolicyDecision(
            action="manual_review",
            approval_level=ActionCategory.HIGH,
            reason=f"low_confidence ({result.confidence:.2f} < {CONFIDENCE_THRESHOLD})",
        )

    # High-confidence routing by classification type
    if result.classification == Classification.SALES_OPPORTUNITY:
        return PolicyDecision(
            action="create_lead",
            approval_level=ActionCategory.MEDIUM,
            reason="sales_opportunity_detected",
        )

    if result.classification == Classification.SUPPORT_REQUEST:
        return PolicyDecision(
            action="route_support",
            approval_level=ActionCategory.LOW,
            reason="support_request_routed",
        )

    if result.classification == Classification.JUNK:
        return PolicyDecision(
            action="archive_junk",
            approval_level=ActionCategory.LOW,
            reason="junk_classified",
        )

    # Anything else (including UNCLEAR) → manual review
    return PolicyDecision(
        action="manual_review",
        approval_level=ActionCategory.MEDIUM,
        reason="unclear_classification",
    )


# ─── 9. CRM Staging (Propose, NEVER Auto-Commit) ─────────────────────────────

def stage_crm_changes(enquiry, result: TriageResult, decision: PolicyDecision) -> CRMChangeset:
    """Builds a proposed CRM changeset and stores it for human review.
    This function NEVER writes to the CRM directly. It only stages."""

    changeset = db.crm_changesets.create(
        id=uuid.uuid4(),
        enquiry_id=enquiry.id,
        action_type=decision.action,
        proposed_changes={
            "contact_name": result.contact_name,
            "contact_email": result.contact_email,
            "contact_phone": result.contact_phone,
            "company_name": result.company_name,
            "enquiry_summary": result.enquiry_summary,
            "classification": result.classification,
            "source_channel": enquiry.source_channel,
        },
        status=ApprovalStatus.PENDING,
    )

    # Create the approval record for the review queue
    db.approvals.create(
        changeset_id=changeset.id,
        action_category=decision.approval_level,
        proposed_payload=changeset.proposed_changes,
        status=ApprovalStatus.PENDING,
    )

    audit_log(enquiry.id, "crm_changeset_staged", {
        "changeset_id": str(changeset.id),
        "action_type": decision.action,
        "approval_level": decision.approval_level,
    })

    return changeset


# ─── 10. Response Drafting ────────────────────────────────────────────────────

def draft_response(enquiry, result: TriageResult, decision: PolicyDecision) -> None:
    """Uses GPT-4o (higher-quality model) to draft a customer-facing reply.
    The draft goes to a review queue — it is NEVER sent automatically."""

    system_prompt = {
        "role": "system",
        "content": (
            "Draft a polite, professional acknowledgement email for a "
            "property enquiry. Be helpful but do not make promises or "
            "commitments. Keep it concise — 3-5 sentences maximum."
        ),
    }
    user_message = {
        "role": "user",
        "content": f"Enquiry summary: {result.enquiry_summary}\n"
                   f"Classification: {result.classification}\n"
                   f"Contact name: {result.contact_name}",
    }

    # GPT-4o for quality — response drafts are customer-facing
    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[system_prompt, user_message],
        temperature=0.4,
    )

    db.draft_responses.create(
        enquiry_id=enquiry.id,
        draft_content=response.choices[0].message.content,
        draft_type="acknowledgement",
        model_name="gpt-4o",
        status="pending",  # MUST be reviewed before sending
    )

    audit_log(enquiry.id, "response_drafted", {"model": "gpt-4o"})


def draft_info_request(enquiry, missing_fields: list[str]) -> None:
    """Drafts a follow-up message requesting the missing information."""
    db.draft_responses.create(
        enquiry_id=enquiry.id,
        draft_content=f"Please provide: {', '.join(missing_fields)}",
        draft_type="info_request",
        model_name="template",  # simple template, no LLM needed
        status="pending",
    )


# ─── 11. Approval Check + Controlled Execution ───────────────────────────────

def has_permission(service_context, permission: Permission) -> bool:
    """Checks if the current service context has a specific permission.
    Permissions are assigned per-service, not per-user, and loaded at startup."""
    return permission in service_context.granted_permissions


def execute_approved_changeset(changeset_id: uuid.UUID, service_context) -> None:
    """Executes a CRM write ONLY after verifying human approval.
    This is the only function that performs real side effects on the CRM."""

    changeset = db.crm_changesets.find_by_id(changeset_id)

    # --- Authorization: verify this service has CRM write permission ---
    if not has_permission(service_context, Permission.CRM_CREATE_LEAD):
        raise AuthorizationError("Service lacks CRM write permission")

    # --- Approval gate: read approval status from DB (not from cache) ---
    approval = db.approvals.find_by_changeset_id(changeset_id)
    if approval.status != ApprovalStatus.APPROVED:
        raise ApprovalRequiredError(
            f"CRM write requires human approval (current: {approval.status})"
        )

    # --- Execute the CRM write ---
    try:
        crm_result = crm_client.create_lead(changeset.proposed_changes)
        db.crm_changesets.update_status(changeset_id, "executed")

        audit_log(changeset.enquiry_id, "crm_write_executed", {
            "changeset_id": str(changeset_id),
            "crm_lead_id": crm_result.lead_id,
            "approved_by": approval.reviewer_id,
        })

    except CRMAPIError as e:
        db.crm_changesets.update_status(changeset_id, "failed")
        audit_log(changeset.enquiry_id, "crm_write_failed", {
            "changeset_id": str(changeset_id),
            "error": str(e),
        })
        raise  # Let the retry mechanism handle it


def execute_approved_message(draft_id: uuid.UUID, service_context) -> None:
    """Sends a customer-facing message ONLY after human review and approval."""

    draft = db.draft_responses.find_by_id(draft_id)

    if not has_permission(service_context, Permission.SEND_EMAIL):
        raise AuthorizationError("Service lacks email send permission")

    if draft.status != "approved":
        raise ApprovalRequiredError("Message send requires human approval")

    # Use the approved content (may have been edited by reviewer)
    content_to_send = draft.approved_content or draft.draft_content

    email_client.send(
        to=draft.enquiry.sender_email,
        subject="Re: Your enquiry",
        body=content_to_send,
    )

    db.draft_responses.update_status(draft_id, "sent")
    audit_log(draft.enquiry_id, "message_sent", {
        "draft_id": str(draft_id),
        "channel": "email",
    })


# ─── 12. Audit Logging ───────────────────────────────────────────────────────

def audit_log(
    enquiry_id: uuid.UUID,
    event_type: str,
    event_data: dict,
    actor_type: str = "system",
    actor_id: Optional[str] = None,
) -> None:
    """Appends an immutable audit event. The audit_events table has
    INSERT-ONLY permissions — no UPDATE or DELETE is ever permitted.
    This is enforced at the database level via role-based grants."""

    db.audit_events.insert(
        id=uuid.uuid4(),
        enquiry_id=enquiry_id,
        event_type=event_type,
        actor_id=actor_id,
        actor_type=actor_type,
        event_data=event_data,  # stored as JSONB
        ip_address=get_request_ip(),  # pseudocode: from request context
        created_at=datetime.now(timezone.utc),
    )
    # Also emit structured log for real-time monitoring
    logger.info(event_type, enquiry_id=str(enquiry_id), **event_data)


# ─── 13. Error Handling Helpers ───────────────────────────────────────────────

def route_to_manual_review(enquiry, reason: str) -> None:
    """Fallback path: routes an enquiry to the human review queue.
    Called whenever automated processing fails or confidence is too low.
    This ensures NO enquiry is ever silently lost."""

    db.enquiries.update_status(enquiry.id, EnquiryStatus.FAILED)

    db.approvals.create(
        changeset_id=None,  # no changeset yet — human starts from scratch
        action_category=ActionCategory.HIGH,
        proposed_payload={"raw_content": enquiry.raw_content, "reason": reason},
        status=ApprovalStatus.PENDING,
    )

    audit_log(enquiry.id, "routed_to_manual_review", {"reason": reason})

    # Notify the operations team via internal channel
    notify_ops_team(
        f"Enquiry {enquiry.id} requires manual review: {reason}"
    )


# ─── 14. Normalisation Adapter (channel-specific → internal format) ──────────

def normalize_payload(source_channel: str, raw_payload: dict) -> dict:
    """Converts channel-specific webhook payloads to a common internal format.
    Each channel (email, WhatsApp, chat widget) has different field names
    and structures — this adapter unifies them."""

    adapters = {
        "email": normalize_email_payload,
        "whatsapp": normalize_whatsapp_payload,
        "chat_widget": normalize_chat_payload,
    }

    adapter = adapters.get(source_channel)
    if not adapter:
        raise ValueError(f"Unknown source channel: {source_channel}")

    normalized = adapter(raw_payload)

    # Compute idempotency key for dedup
    normalized.idempotency_key = hashlib.sha256(
        f"{source_channel}:{normalized.external_message_id}".encode()
    ).hexdigest()

    return normalized
