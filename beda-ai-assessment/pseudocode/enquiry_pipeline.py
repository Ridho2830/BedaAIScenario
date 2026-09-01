"""
BEDA AI Enquiry Processing Pipeline
====================================
Core processing pipeline for ingesting, classifying, extracting,
and routing business enquiries with human-in-the-loop controls.

This is pseudocode demonstrating architecture and control flow.
Not all imports/classes are fully implemented.
"""

import hashlib
import logging
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

logger = logging.getLogger("enquiry_pipeline")


# ─── Enums ───────────────────────────────────────────────────────────────────

class EnquiryStatus(Enum):
    RECEIVED = "received"
    NORMALIZED = "normalized"
    DUPLICATE = "duplicate"
    PROCESSING = "processing"
    CLASSIFIED = "classified"
    NEEDS_APPROVAL = "needs_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    ACTIONED = "actioned"
    FAILED = "failed"


class Intent(Enum):
    SALES = "sales"
    SUPPORT = "support"
    SPAM = "spam"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


class RiskLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ApprovalStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# ─── Configuration ───────────────────────────────────────────────────────────

CONFIDENCE_THRESHOLD_AUTO = 0.85      # Auto-proceed if confidence >= this
CONFIDENCE_THRESHOLD_ESCALATE = 0.50  # Escalate to stronger model if below this
MAX_RETRIES = 3
RETRY_BASE_DELAY_SECONDS = 2


# ─── Service Interfaces (abstractions) ──────────────────────────────────────

class LLMService:
    """Abstraction over LLM provider. Enforces structured output schemas."""

    def classify_and_extract(self, normalized_content: str, schema: dict) -> dict:
        """
        Send normalized enquiry to LLM with a strict output schema.
        Returns parsed structured output or raises ValidationError.

        - Uses system prompt that clearly separates instructions from user content
        - Enforces JSON schema on response
        - Does NOT include any secrets, CRM credentials, or tool access in prompt
        """
        raise NotImplementedError

    def draft_response(self, context: dict, template_guidelines: str) -> dict:
        """
        Draft a response based on classification and extraction results.
        Returns draft text + metadata. Draft is NEVER sent automatically.
        """
        raise NotImplementedError


class CRMService:
    """
    Controlled CRM interface with scoped permissions.
    The AI layer NEVER gets direct CRM credentials.
    All writes go through this service with validation.
    """

    def find_contact(self, email: str = None, phone: str = None,
                     company: str = None) -> list:
        """Search for existing contacts. Read-only operation."""
        raise NotImplementedError

    def create_lead(self, validated_data: dict) -> str:
        """Create a new lead. Returns lead ID. Requires validated data."""
        raise NotImplementedError

    def update_lead(self, lead_id: str, fields: dict,
                    sensitive: bool = False) -> bool:
        """
        Update lead fields. Sensitive updates require prior approval.
        Only whitelisted fields can be updated.
        """
        raise NotImplementedError


class NotificationService:
    """Send internal notifications (Slack, email, dashboard)."""

    def notify_team(self, channel: str, message: str, priority: str):
        raise NotImplementedError

    def request_approval(self, approval_record: dict) -> str:
        """Create approval request. Returns approval ID."""
        raise NotImplementedError


class AuditService:
    """Immutable audit trail for all significant actions."""

    def log(self, enquiry_id: str, actor: str, action: str,
            detail: dict, timestamp: datetime = None):
        """
        Log an audit event. Actor is 'system', 'ai', or a user ID.
        Detail includes relevant context without sensitive data.
        """
        raise NotImplementedError


class QueueService:
    """Message queue for async processing."""

    def enqueue(self, queue_name: str, payload: dict):
        raise NotImplementedError

    def move_to_dlq(self, queue_name: str, payload: dict, error: str):
        raise NotImplementedError


# ─── Database Repository ────────────────────────────────────────────────────

class EnquiryRepository:
    """Database operations for enquiries."""

    def find_by_idempotency_key(self, key: str) -> Optional[dict]:
        raise NotImplementedError

    def get_by_id(self, enquiry_id: str) -> Optional[dict]:
        raise NotImplementedError

    def create(self, enquiry: dict) -> str:
        raise NotImplementedError

    def update_status(self, enquiry_id: str, status: EnquiryStatus):
        raise NotImplementedError

    def save_extraction(self, enquiry_id: str, extraction: dict):
        raise NotImplementedError

    def save_processing_run(self, run: dict) -> str:
        raise NotImplementedError


# ─── Core Pipeline ──────────────────────────────────────────────────────────

class EnquiryPipeline:
    """
    Main processing pipeline.

    Design principles:
    1. AI proposes, application decides
    2. Deterministic code controls all side effects
    3. Humans approve high-impact actions
    4. Every decision is auditable
    5. Failures are recoverable
    """

    def __init__(self, llm: LLMService, crm: CRMService,
                 notifications: NotificationService, audit: AuditService,
                 queue: QueueService, repo: EnquiryRepository):
        self.llm = llm
        self.crm = crm
        self.notifications = notifications
        self.audit = audit
        self.queue = queue
        self.repo = repo

        # Whitelist of CRM fields the system may auto-update
        self.auto_update_fields = {
            "first_name", "last_name", "email", "phone",
            "company_name", "enquiry_summary", "source", "priority"
        }
        # Fields that always require human approval to change
        self.sensitive_fields = {
            "deal_value", "contract_status", "billing_info",
            "account_owner", "do_not_contact"
        }

    # ── Step 1: Ingest ──────────────────────────────────────────────────

    def ingest(self, raw_payload: dict, source: str) -> dict:
        """
        Entry point. Receives raw enquiry from webhook/form/email.
        Validates webhook signature before this point (in the API layer).
        """
        self.audit.log(
            enquiry_id=None, actor="system", action="enquiry_received",
            detail={"source": source, "payload_size": len(str(raw_payload))}
        )

        # Step 1a: Normalize the raw input into a consistent format
        normalized = self._normalize(raw_payload, source)

        # Step 1b: Generate idempotency key to prevent duplicate processing
        idempotency_key = self._compute_idempotency_key(normalized)
        normalized["idempotency_key"] = idempotency_key

        # Step 1c: Check if we've already processed this exact enquiry
        existing = self.repo.find_by_idempotency_key(idempotency_key)
        if existing:
            self.audit.log(
                enquiry_id=existing["id"], actor="system",
                action="duplicate_submission_skipped",
                detail={"idempotency_key": idempotency_key}
            )
            logger.info(f"Duplicate submission detected: {idempotency_key}")
            return existing

        # Step 1d: Persist the enquiry
        enquiry_id = self.repo.create(normalized)
        normalized["id"] = enquiry_id

        self.audit.log(
            enquiry_id=enquiry_id, actor="system", action="enquiry_created",
            detail={"source": source, "idempotency_key": idempotency_key}
        )

        # Step 1e: Enqueue for async processing
        self.queue.enqueue("enquiry_processing", {
            "enquiry_id": enquiry_id,
            "attempt": 1
        })

        return normalized

    # ── Step 2: Normalize ───────────────────────────────────────────────

    def _normalize(self, raw_payload: dict, source: str) -> dict:
        """
        Convert source-specific payload into a uniform internal format.
        This is purely deterministic — no LLM involved.
        """
        if source == "email":
            return {
                "source": "email",
                "sender_email": raw_payload.get("from", "").strip().lower(),
                "sender_name": raw_payload.get("from_name", "").strip(),
                "subject": raw_payload.get("subject", "").strip(),
                "body": raw_payload.get("body_plain", "").strip(),
                "received_at": datetime.utcnow().isoformat(),
                "raw_payload": raw_payload,
                "status": EnquiryStatus.NORMALIZED.value,
            }
        elif source == "web_form":
            return {
                "source": "web_form",
                "sender_email": raw_payload.get("email", "").strip().lower(),
                "sender_name": raw_payload.get("name", "").strip(),
                "subject": raw_payload.get("subject", ""),
                "body": raw_payload.get("message", "").strip(),
                "company": raw_payload.get("company", "").strip(),
                "received_at": datetime.utcnow().isoformat(),
                "raw_payload": raw_payload,
                "status": EnquiryStatus.NORMALIZED.value,
            }
        elif source == "messaging":
            return {
                "source": "messaging",
                "sender_email": None,
                "sender_name": raw_payload.get("sender_name", "").strip(),
                "sender_id": raw_payload.get("sender_id", ""),
                "body": raw_payload.get("text", "").strip(),
                "channel": raw_payload.get("channel", ""),
                "received_at": datetime.utcnow().isoformat(),
                "raw_payload": raw_payload,
                "status": EnquiryStatus.NORMALIZED.value,
            }
        else:
            raise ValueError(f"Unknown source: {source}")

    # ── Step 3: Idempotency ─────────────────────────────────────────────

    def _compute_idempotency_key(self, normalized: dict) -> str:
        """
        Generate a deterministic key from source + sender + content.
        Prevents processing the same enquiry twice.
        """
        raw = f"{normalized['source']}:{normalized.get('sender_email', '')}:" \
              f"{normalized.get('body', '')[:500]}"
        return hashlib.sha256(raw.encode()).hexdigest()

    # ── Step 4: Process (async worker picks this up) ────────────────────

    def process(self, enquiry_id: str, attempt: int = 1):
        """
        Main processing logic. Called by queue worker.
        Wraps the full pipeline in error handling with retry.
        """
        try:
            enquiry = self.repo.get_by_id(enquiry_id)
            if not enquiry:
                logger.error(f"Enquiry {enquiry_id} not found")
                return
            if enquiry["status"] in [EnquiryStatus.ACTIONED.value,
                                     EnquiryStatus.FAILED.value]:
                logger.info(f"Enquiry {enquiry_id} already processed, skipping")
                return  # Idempotent: don't reprocess completed enquiries
            self.repo.update_status(enquiry_id, EnquiryStatus.PROCESSING)

            # Step 4a: Quick deterministic spam check BEFORE calling LLM
            if self._deterministic_spam_check(enquiry):
                self.repo.update_status(enquiry_id, EnquiryStatus.CLASSIFIED)
                self.audit.log(
                    enquiry_id=enquiry_id, actor="system",
                    action="classified_spam_deterministic",
                    detail={"method": "rule_based"}
                )
                return

            # Step 4b: LLM classification and extraction
            extraction = self._classify_and_extract(enquiry_id, enquiry)

            # Step 4c: Validate structured output (deterministic)
            validated = self._validate_extraction(extraction)
            if not validated["is_valid"]:
                # Invalid LLM output — retry with stronger model or escalate
                self._handle_invalid_extraction(enquiry_id, extraction,
                                                validated, attempt)
                return

            # Step 4d: Save validated extraction
            self.repo.save_extraction(enquiry_id, validated["data"])
            self.repo.update_status(enquiry_id, EnquiryStatus.CLASSIFIED)

            # Step 4e: Apply deterministic business rules
            decision = self._apply_business_rules(enquiry_id, validated["data"])

            # Step 4f: Handle duplicate/existing contact detection
            duplicate_result = self._detect_duplicates(validated["data"])

            # Step 4g: Route based on decision
            self._route(enquiry_id, validated["data"], decision,
                        duplicate_result)

        except LLMServiceError as e:
            self._handle_llm_failure(enquiry_id, attempt, e)
        except CRMServiceError as e:
            self._handle_crm_failure(enquiry_id, attempt, e)
        except Exception as e:
            self._handle_unexpected_failure(enquiry_id, attempt, e)

    # ── Step 4b: LLM Classification ────────────────────────────────────

    def _classify_and_extract(self, enquiry_id: str, enquiry: dict) -> dict:
        """
        Send enquiry content to LLM for classification and extraction.

        Key controls:
        - Only send the enquiry body and subject — no CRM creds, no secrets
        - Enforce a strict JSON output schema
        - System prompt separates instructions from untrusted user content
        - Treat the entire LLM response as an UNTRUSTED PROPOSAL
        """
        # Prepare sanitized content (no internal metadata, no secrets)
        sanitized_content = {
            "subject": enquiry.get("subject", ""),
            "body": enquiry.get("body", ""),
            "sender_name": enquiry.get("sender_name", ""),
            # Intentionally exclude: raw_payload, internal IDs, CRM data
        }

        # Define expected output schema
        output_schema = {
            "intent": {"type": "string", "enum": ["sales", "support",
                                                   "spam", "incomplete",
                                                   "unknown"]},
            "confidence": {"type": "number", "min": 0.0, "max": 1.0},
            "contact": {
                "name": {"type": "string", "nullable": True},
                "email": {"type": "string", "nullable": True},
                "phone": {"type": "string", "nullable": True},
            },
            "company": {
                "value": {"type": "string", "nullable": True},
                "confidence": {"type": "number"},
                "evidence": {"type": "string"},
            },
            "requirements": {"type": "array", "items": {"type": "string"}},
            "missing_information": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"},
            "priority": {"type": "string", "enum": ["low", "medium", "high"]},
            "recommended_action": {"type": "string",
                                   "enum": ["auto_create_lead",
                                            "human_review",
                                            "request_more_info",
                                            "route_to_support",
                                            "discard_spam"]},
        }

        run_start = datetime.utcnow()

        # Call LLM — the response is an UNTRUSTED PROPOSAL
        result = self.llm.classify_and_extract(
            normalized_content=sanitized_content,
            schema=output_schema
        )

        # Record the processing run for auditability
        self.repo.save_processing_run({
            "enquiry_id": enquiry_id,
            "model": result.get("model_used", "unknown"),
            "step": "classify_and_extract",
            "input_hash": hashlib.sha256(
                str(sanitized_content).encode()
            ).hexdigest(),
            "output": result,
            "started_at": run_start.isoformat(),
            "completed_at": datetime.utcnow().isoformat(),
            "status": "completed",
        })

        return result

    # ── Step 4c: Validate Extraction ────────────────────────────────────

    def _validate_extraction(self, extraction: dict) -> dict:
        """
        Deterministic validation of LLM output.
        The LLM output is treated as an UNTRUSTED PROPOSAL.
        """
        errors = []

        # Check required fields exist
        if "intent" not in extraction:
            errors.append("missing_intent")
        elif extraction["intent"] not in [i.value for i in Intent]:
            errors.append("invalid_intent_value")

        # Check confidence is a valid number
        confidence = extraction.get("confidence")
        if confidence is None or not isinstance(confidence, (int, float)):
            errors.append("missing_or_invalid_confidence")
        elif not (0.0 <= confidence <= 1.0):
            errors.append("confidence_out_of_range")

        # Validate email format if provided
        contact_email = extraction.get("contact", {}).get("email")
        if contact_email and not self._is_valid_email(contact_email):
            errors.append("invalid_email_format")

        # Check for hallucination signals: extracted data not in source
        # (simplified — real implementation would check against source text)
        company_info = extraction.get("company", {})
        if company_info.get("value") and not company_info.get("evidence"):
            errors.append("company_extracted_without_evidence")

        if errors:
            return {"is_valid": False, "errors": errors, "data": extraction}

        return {"is_valid": True, "errors": [], "data": extraction}

    # ── Step 4e: Business Rules ─────────────────────────────────────────

    def _apply_business_rules(self, enquiry_id: str,
                              extraction: dict) -> dict:
        """
        Deterministic business rules. No LLM involved.
        Maps extraction results to concrete actions.
        """
        intent = extraction.get("intent", "unknown")
        confidence = extraction.get("confidence", 0.0)
        recommended = extraction.get("recommended_action", "human_review")

        # Rule 1: Spam with high confidence → auto-discard
        if intent == "spam" and confidence >= CONFIDENCE_THRESHOLD_AUTO:
            return {
                "action": "discard",
                "risk_level": RiskLevel.LOW,
                "requires_approval": False,
                "reason": "High-confidence spam classification"
            }

        # Rule 2: Low confidence on any classification → human review
        if confidence < CONFIDENCE_THRESHOLD_ESCALATE:
            return {
                "action": "human_review",
                "risk_level": RiskLevel.HIGH,
                "requires_approval": True,
                "reason": f"Low confidence ({confidence:.2f}) requires human review"
            }

        # Rule 3: Sales enquiry → create lead (medium risk)
        if intent == "sales":
            requires_approval = confidence < CONFIDENCE_THRESHOLD_AUTO
            return {
                "action": "create_lead",
                "risk_level": RiskLevel.MEDIUM if not requires_approval
                              else RiskLevel.HIGH,
                "requires_approval": requires_approval,
                "reason": "Sales enquiry — create CRM lead"
            }

        # Rule 4: Support enquiry → route to support
        if intent == "support":
            return {
                "action": "route_to_support",
                "risk_level": RiskLevel.LOW,
                "requires_approval": False,
                "reason": "Support enquiry — route to support team"
            }

        # Rule 5: Incomplete enquiry → draft clarification
        if intent == "incomplete":
            return {
                "action": "draft_clarification",
                "risk_level": RiskLevel.MEDIUM,
                "requires_approval": True,  # Outbound comms need approval
                "reason": "Incomplete enquiry — draft clarification for review"
            }

        # Default: unknown → human review
        return {
            "action": "human_review",
            "risk_level": RiskLevel.HIGH,
            "requires_approval": True,
            "reason": "Unclassifiable enquiry — requires human review"
        }

    # ── Step 4f: Duplicate Detection ────────────────────────────────────

    def _detect_duplicates(self, extraction: dict) -> dict:
        """
        Deterministic duplicate detection.
        Uses exact and fuzzy matching on known identifiers.
        The LLM is NOT involved in merge/dedup decisions.
        """
        contact = extraction.get("contact", {})
        email = contact.get("email")
        phone = contact.get("phone")
        company = extraction.get("company", {}).get("value")

        matches = []

        # Exact match on email (strongest signal)
        if email:
            email_matches = self.crm.find_contact(email=email)
            for m in email_matches:
                matches.append({"contact": m, "match_type": "exact_email",
                                "confidence": 1.0})

        # Exact match on phone
        if phone:
            phone_matches = self.crm.find_contact(phone=phone)
            for m in phone_matches:
                if not any(existing["contact"]["id"] == m["id"]
                           for existing in matches):
                    matches.append({"contact": m, "match_type": "exact_phone",
                                    "confidence": 0.95})

        # Normalized company name match (lower confidence)
        if company:
            company_matches = self.crm.find_contact(
                company=company.strip().lower()
            )
            for m in company_matches:
                if not any(existing["contact"]["id"] == m["id"]
                           for existing in matches):
                    matches.append({"contact": m,
                                    "match_type": "company_name",
                                    "confidence": 0.70})

        if not matches:
            return {"status": "new_record", "matches": []}

        best_match = max(matches, key=lambda x: x["confidence"])

        if best_match["confidence"] >= 0.95:
            return {"status": "exact_match", "matches": matches,
                    "best_match": best_match}
        elif best_match["confidence"] >= 0.70:
            return {"status": "probable_match", "matches": matches,
                    "best_match": best_match,
                    "requires_human_confirmation": True}
        else:
            return {"status": "possible_duplicate", "matches": matches,
                    "requires_human_confirmation": True}

    # ── Step 4g: Routing ────────────────────────────────────────────────

    def _route(self, enquiry_id: str, extraction: dict, decision: dict,
               duplicate_result: dict):
        """
        Route the enquiry based on business rules and duplicate detection.
        Enforces approval requirements — this is NOT optional.
        """
        self.audit.log(
            enquiry_id=enquiry_id, actor="system", action="routing_decision",
            detail={"decision": decision, "duplicate": duplicate_result}
        )

        # If approval is required, create approval request and STOP
        if decision["requires_approval"]:
            self._request_human_approval(
                enquiry_id=enquiry_id,
                action=decision["action"],
                risk_level=decision["risk_level"],
                context={
                    "extraction": extraction,
                    "decision": decision,
                    "duplicate_result": duplicate_result,
                }
            )
            return  # Processing pauses here until human acts

        # If duplicate requires confirmation, also pause
        if duplicate_result.get("requires_human_confirmation"):
            self._request_human_approval(
                enquiry_id=enquiry_id,
                action="confirm_duplicate_handling",
                risk_level=RiskLevel.MEDIUM,
                context={
                    "extraction": extraction,
                    "duplicate_result": duplicate_result,
                }
            )
            return

        # Auto-execute low-risk actions
        self._execute_action(enquiry_id, decision, extraction,
                             duplicate_result)

    # ── Human Approval ──────────────────────────────────────────────────

    def _request_human_approval(self, enquiry_id: str, action: str,
                                risk_level: RiskLevel, context: dict):
        """
        Create an approval request. The system STOPS processing until
        a human approves or rejects.

        Approval is enforced by the APPLICATION, not requested by the LLM.
        """
        self.repo.update_status(enquiry_id, EnquiryStatus.NEEDS_APPROVAL)

        approval_record = {
            "enquiry_id": enquiry_id,
            "action": action,
            "risk_level": risk_level.value,
            "context_summary": context,
            "status": ApprovalStatus.PENDING.value,
            "requested_at": datetime.utcnow().isoformat(),
        }

        approval_id = self.notifications.request_approval(approval_record)

        self.audit.log(
            enquiry_id=enquiry_id, actor="system",
            action="approval_requested",
            detail={"approval_id": approval_id, "action": action,
                    "risk_level": risk_level.value}
        )

        # Notify relevant team member
        self.notifications.notify_team(
            channel="approvals",
            message=f"Approval needed for enquiry {enquiry_id}: {action}",
            priority=risk_level.value
        )

    def handle_approval_decision(self, approval_id: str, decision: str,
                                 reviewer_id: str, reason: str = ""):
        """
        Called when a human approves or rejects an action.
        This is a separate endpoint — triggered by human interaction.
        """
        # ... load approval record ...
        approval = {}  # loaded from DB

        self.audit.log(
            enquiry_id=approval["enquiry_id"], actor=reviewer_id,
            action=f"approval_{decision}",
            detail={"approval_id": approval_id, "reason": reason}
        )

        if decision == "approved":
            self.repo.update_status(approval["enquiry_id"],
                                    EnquiryStatus.APPROVED)
            # Now execute the action that was waiting
            self._execute_action(
                approval["enquiry_id"],
                approval["context_summary"]["decision"],
                approval["context_summary"]["extraction"],
                approval["context_summary"].get("duplicate_result", {})
            )
        else:
            self.repo.update_status(approval["enquiry_id"],
                                    EnquiryStatus.REJECTED)

    # ── Controlled Action Execution ─────────────────────────────────────

    def _execute_action(self, enquiry_id: str, decision: dict,
                        extraction: dict, duplicate_result: dict):
        """
        Execute the decided action through controlled service calls.
        All CRM operations go through the CRMService abstraction.
        The AI never directly calls CRM APIs.
        """
        action = decision["action"]

        try:
            if action == "create_lead":
                self._create_crm_lead(enquiry_id, extraction,
                                      duplicate_result)
            elif action == "route_to_support":
                self._route_to_support(enquiry_id, extraction)
            elif action == "draft_clarification":
                self._draft_and_queue_response(enquiry_id, extraction)
            elif action == "discard":
                self._discard_spam(enquiry_id)
            elif action == "human_review":
                pass  # Already handled by approval flow

            self.repo.update_status(enquiry_id, EnquiryStatus.ACTIONED)
            self.audit.log(
                enquiry_id=enquiry_id, actor="system",
                action="action_executed",
                detail={"action": action}
            )

        except Exception as e:
            self.audit.log(
                enquiry_id=enquiry_id, actor="system",
                action="action_failed",
                detail={"action": action, "error": str(e)}
            )
            raise

    def _create_crm_lead(self, enquiry_id: str, extraction: dict,
                         duplicate_result: dict):
        """
        Create or update CRM lead through the controlled CRM service.
        Only whitelisted fields are written.
        """
        contact = extraction.get("contact", {})

        # Build lead data with ONLY validated, whitelisted fields
        lead_data = {}
        for field in ["name", "email", "phone"]:
            value = contact.get(field)
            if value and field in self.auto_update_fields:
                lead_data[field] = value

        company = extraction.get("company", {})
        if company.get("value") and company.get("confidence", 0) >= 0.7:
            lead_data["company_name"] = company["value"]

        lead_data["enquiry_summary"] = extraction.get("summary", "")
        lead_data["source"] = "ai_processed"
        lead_data["priority"] = extraction.get("priority", "medium")

        # Handle duplicate: update existing vs create new
        if duplicate_result.get("status") == "exact_match":
            existing_id = duplicate_result["best_match"]["contact"]["id"]
            self.crm.update_lead(existing_id, lead_data, sensitive=False)
            self.audit.log(
                enquiry_id=enquiry_id, actor="system",
                action="crm_lead_updated",
                detail={"lead_id": existing_id,
                        "fields_updated": list(lead_data.keys())}
            )
        else:
            lead_id = self.crm.create_lead(lead_data)
            self.audit.log(
                enquiry_id=enquiry_id, actor="system",
                action="crm_lead_created",
                detail={"lead_id": lead_id}
            )

    def _draft_and_queue_response(self, enquiry_id: str, extraction: dict):
        """
        Use AI to draft a clarification response, but NEVER send it.
        The draft goes into an approval queue for human review.
        """
        draft = self.llm.draft_response(
            context={
                "intent": extraction.get("intent"),
                "missing_information": extraction.get("missing_information", []),
                "sender_name": extraction.get("contact", {}).get("name", ""),
            },
            template_guidelines="Professional, concise clarification request. "
                                "Do not make promises or commitments. "
                                "Do not invent information."
        )

        # Draft is saved but NOT sent — requires human approval
        self._request_human_approval(
            enquiry_id=enquiry_id,
            action="send_clarification_response",
            risk_level=RiskLevel.HIGH,
            context={
                "draft_response": draft["text"],
                "extraction": extraction,
            }
        )

        self.audit.log(
            enquiry_id=enquiry_id, actor="ai",
            action="response_drafted",
            detail={"draft_length": len(draft["text"]),
                    "model": draft.get("model_used", "unknown")}
        )

    # ── Deterministic Checks ────────────────────────────────────────────

    def _deterministic_spam_check(self, enquiry: dict) -> bool:
        """
        Quick rule-based spam detection BEFORE spending LLM tokens.
        Checks known spam patterns, blocklists, etc.
        """
        body = enquiry.get("body", "").lower()
        subject = enquiry.get("subject", "").lower()

        # Simple keyword blocklist (in production, use a maintained list)
        spam_keywords = ["buy now", "free offer", "click here",
                         "unsubscribe", "nigerian prince"]
        for keyword in spam_keywords:
            if keyword in body or keyword in subject:
                return True

        # Check sender against blocklist (deterministic DB lookup)
        # blocked = self.repo.is_sender_blocked(enquiry.get("sender_email"))
        # if blocked:
        #     return True

        return False

    def _is_valid_email(self, email: str) -> bool:
        """Basic email format validation."""
        import re
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        return bool(re.match(pattern, email))

    def _discard_spam(self, enquiry_id: str):
        """Mark enquiry as spam. No CRM record created."""
        self.repo.update_status(enquiry_id, EnquiryStatus.CLASSIFIED)
        self.audit.log(
            enquiry_id=enquiry_id, actor="system",
            action="spam_discarded", detail={}
        )

    def _route_to_support(self, enquiry_id: str, extraction: dict):
        """Route to support team via internal notification."""
        self.notifications.notify_team(
            channel="support",
            message=f"New support enquiry {enquiry_id}: "
                    f"{extraction.get('summary', 'No summary')}",
            priority=extraction.get("priority", "medium")
        )

    # ── Failure Handling ────────────────────────────────────────────────

    def _handle_llm_failure(self, enquiry_id: str, attempt: int,
                            error: Exception):
        """
        Handle LLM service failures with retry and escalation.
        Enquiry is NEVER silently lost.
        """
        logger.error(f"LLM failure for {enquiry_id} (attempt {attempt}): "
                     f"{error}")

        self.audit.log(
            enquiry_id=enquiry_id, actor="system",
            action="llm_failure",
            detail={"attempt": attempt, "error": str(error)}
        )

        if attempt < MAX_RETRIES:
            delay = RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
            self.queue.enqueue("enquiry_processing", {
                "enquiry_id": enquiry_id,
                "attempt": attempt + 1,
                "delay_seconds": delay,
            })
        else:
            # Max retries exceeded — move to DLQ and alert human
            self.queue.move_to_dlq("enquiry_processing", {
                "enquiry_id": enquiry_id
            }, error=str(error))
            self.repo.update_status(enquiry_id, EnquiryStatus.FAILED)
            self.notifications.notify_team(
                channel="alerts",
                message=f"ALERT: Enquiry {enquiry_id} failed after "
                        f"{MAX_RETRIES} attempts. Manual processing required.",
                priority="high"
            )

    def _handle_crm_failure(self, enquiry_id: str, attempt: int,
                            error: Exception):
        """Handle CRM API failures. Same retry pattern."""
        self._handle_llm_failure(enquiry_id, attempt, error)  # reuse pattern

    def _handle_unexpected_failure(self, enquiry_id: str, attempt: int,
                                   error: Exception):
        """Catch-all for unexpected errors. Always alert, never lose data."""
        logger.critical(f"Unexpected error for {enquiry_id}: {error}")
        self.audit.log(
            enquiry_id=enquiry_id, actor="system",
            action="unexpected_failure",
            detail={"attempt": attempt, "error": str(error),
                    "error_type": type(error).__name__}
        )
        self.queue.move_to_dlq("enquiry_processing", {
            "enquiry_id": enquiry_id
        }, error=str(error))
        self.repo.update_status(enquiry_id, EnquiryStatus.FAILED)
        self.notifications.notify_team(
            channel="alerts",
            message=f"CRITICAL: Unexpected failure for enquiry {enquiry_id}",
            priority="high"
        )

    def _handle_invalid_extraction(self, enquiry_id: str, extraction: dict,
                                   validation: dict, attempt: int):
        """Handle invalid LLM output: retry with stronger model or escalate."""
        self.audit.log(
            enquiry_id=enquiry_id, actor="system",
            action="invalid_extraction",
            detail={"errors": validation["errors"], "attempt": attempt}
        )

        if attempt < 2:
            # Retry with a stronger model
            self.queue.enqueue("enquiry_processing", {
                "enquiry_id": enquiry_id,
                "attempt": attempt + 1,
                "use_stronger_model": True,
            })
        else:
            # Escalate to human
            self._request_human_approval(
                enquiry_id=enquiry_id,
                action="manual_classification",
                risk_level=RiskLevel.HIGH,
                context={"reason": "AI extraction failed validation",
                         "errors": validation["errors"]}
            )


# ─── Custom Exceptions ──────────────────────────────────────────────────────

class LLMServiceError(Exception):
    pass

class CRMServiceError(Exception):
    pass
