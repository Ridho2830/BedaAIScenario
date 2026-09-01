# System Architecture

## High-Level Architecture

```mermaid
flowchart TD
    subgraph Sources["Inbound Sources"]
        Email["📧 Email\n(Webhook)"]
        Web["🌐 Website Form\n(POST)"]
        Msg["💬 Messaging\n(Webhook)"]
    end

    subgraph Ingestion["Ingestion Layer"]
        API["API Gateway\n- Webhook signature verification\n- Rate limiting\n- Auth check"]
        Norm["Normalizer\n- Source-specific → uniform format\n- Sanitization\n- Idempotency key generation"]
        IdemCheck{"Idempotency\nCheck"}
    end

    subgraph Queue["Queue Layer"]
        EQ["📋 Enquiry Queue\n(Redis / RabbitMQ)"]
        DLQ["⚠️ Dead Letter Queue"]
    end

    subgraph AI["AI Processing (Untrusted Proposals)"]
        SpamCheck["🔍 Deterministic\nSpam Filter"]
        Classify["🤖 LLM Classification\n& Extraction"]
        Validate["✅ Schema Validation\n(Deterministic)"]
        Draft["📝 Response Drafting\n(LLM)"]
    end

    subgraph Policy["Deterministic Policy Engine"]
        Rules["Business Rules\n- Routing logic\n- Risk assessment\n- Confidence thresholds"]
        DupDetect["Duplicate Detection\n- Email match\n- Phone match\n- Company name match"]
    end

    subgraph CRM_Sys["CRM & Data"]
        CRM["CRM Service\n(Controlled API)"]
        DB[("PostgreSQL\nEnquiries, Leads,\nAudit Events")]
    end

    subgraph Human["Human-in-the-Loop"]
        ApprovalQ["⏳ Approval Queue"]
        Dashboard["📊 Review Dashboard"]
        HumanAct["👤 Human Decision\nApprove / Reject / Edit"]
    end

    subgraph Execution["Controlled Execution"]
        ActionSvc["Action Service\n- CRM writes\n- Email sends\n- Notifications"]
        Notify["🔔 Notification Service"]
    end

    AuditLog["📜 Audit Log\n(Immutable)"]

    %% Flow
    Email --> API
    Web --> API
    Msg --> API
    API --> Norm
    Norm --> IdemCheck
    IdemCheck -->|"New"| EQ
    IdemCheck -->|"Duplicate\nSubmission"| AuditLog

    EQ --> SpamCheck
    SpamCheck -->|"Spam"| AuditLog
    SpamCheck -->|"Not Spam"| Classify
    Classify --> Validate
    Validate -->|"Valid"| Rules
    Validate -->|"Invalid"| DLQ

    Rules --> DupDetect
    DupDetect --> ApprovalQ
    Rules -->|"Low Risk\nHigh Confidence"| ActionSvc

    ApprovalQ --> Dashboard
    Dashboard --> HumanAct
    HumanAct -->|"Approved"| ActionSvc
    HumanAct -->|"Rejected"| AuditLog

    ActionSvc --> CRM
    ActionSvc --> Notify
    CRM --> DB

    Draft -.->|"Drafts for\nHuman Review"| ApprovalQ

    %% Audit everything
    API --> AuditLog
    Classify --> AuditLog
    Rules --> AuditLog
    ActionSvc --> AuditLog
    HumanAct --> AuditLog
    DLQ --> AuditLog

    %% Failure paths
    EQ -->|"Retry Failed"| DLQ
    DLQ --> Notify

    %% Styling
    classDef source fill:#4a90d9,stroke:#2c5f8a,color:#fff
    classDef ingestion fill:#5ba85b,stroke:#3d7a3d,color:#fff
    classDef queue fill:#e8a838,stroke:#b8831d,color:#fff
    classDef ai fill:#9b59b6,stroke:#7d3c98,color:#fff
    classDef policy fill:#3498db,stroke:#2471a3,color:#fff
    classDef human fill:#e74c3c,stroke:#c0392b,color:#fff
    classDef execute fill:#1abc9c,stroke:#16a085,color:#fff
    classDef audit fill:#95a5a6,stroke:#7f8c8d,color:#fff

    class Email,Web,Msg source
    class API,Norm,IdemCheck ingestion
    class EQ,DLQ queue
    class SpamCheck,Classify,Validate,Draft ai
    class Rules,DupDetect policy
    class ApprovalQ,Dashboard,HumanAct human
    class ActionSvc,Notify execute
    class AuditLog audit
```

## Trust Boundary Diagram

```mermaid
flowchart LR
    subgraph Untrusted["🔴 UNTRUSTED ZONE"]
        ExtInput["External Input\n(Email, Form, Message)"]
        LLMOutput["LLM Output\n(Classification, Extraction, Drafts)"]
    end

    subgraph Boundary["🟡 VALIDATION BOUNDARY"]
        WebhookAuth["Webhook Signature\nVerification"]
        SchemaVal["Schema Validation\n& Sanitization"]
        ConfCheck["Confidence\nThreshold Check"]
        PolicyCheck["Business Rule\nEnforcement"]
    end

    subgraph Trusted["🟢 TRUSTED ZONE"]
        AppLogic["Application Logic\n& Routing"]
        CRMWrite["Controlled CRM\nOperations"]
        AuditWrite["Audit Log\nWrites"]
        HumanApproval["Human Approval\nEnforcement"]
    end

    ExtInput --> WebhookAuth
    WebhookAuth --> SchemaVal
    LLMOutput --> SchemaVal
    SchemaVal --> ConfCheck
    ConfCheck --> PolicyCheck
    PolicyCheck --> AppLogic
    AppLogic --> CRMWrite
    AppLogic --> AuditWrite
    AppLogic --> HumanApproval
```

## Model Routing Strategy

```mermaid
flowchart TD
    Input["Incoming Enquiry"]
    SpamFilter["Deterministic Spam Filter\n(Keywords, Blocklist)\n💰 Cost: $0"]
    SmallModel["Small/Cheap Model\n(e.g., GPT-4o-mini, Claude Haiku)\n💰 Cost: ~$0.001/request"]
    ConfHigh{"Confidence\n≥ 0.85?"}
    StrongerModel["Stronger Model\n(e.g., GPT-4o, Claude Sonnet)\n💰 Cost: ~$0.01/request"]
    ConfMed{"Confidence\n≥ 0.50?"}
    HumanReview["👤 Human Review\n💰 Cost: Human time"]
    Continue["✅ Continue Processing"]

    Input --> SpamFilter
    SpamFilter -->|"Spam"| Discard["🗑️ Discard"]
    SpamFilter -->|"Not Spam"| SmallModel
    SmallModel --> ConfHigh
    ConfHigh -->|"YES"| Continue
    ConfHigh -->|"NO"| StrongerModel
    StrongerModel --> ConfMed
    ConfMed -->|"YES"| Continue
    ConfMed -->|"NO"| HumanReview

    style Discard fill:#e74c3c,color:#fff
    style Continue fill:#27ae60,color:#fff
    style HumanReview fill:#f39c12,color:#fff
```

## Approval Flow

```mermaid
sequenceDiagram
    participant W as Queue Worker
    participant P as Policy Engine
    participant A as Approval Service
    participant D as Dashboard
    participant H as Human Reviewer
    participant C as CRM Service
    participant L as Audit Log

    W->>P: Submit extraction result
    P->>P: Apply business rules
    P->>P: Assess risk level

    alt Low Risk + High Confidence
        P->>C: Execute action directly
        C->>L: Log action
    else Medium/High Risk
        P->>A: Create approval request
        A->>D: Display in dashboard
        A->>H: Send notification
        H->>D: Review context + AI proposal
        alt Approved
            H->>A: Approve (with optional edits)
            A->>C: Execute approved action
            C->>L: Log approved action
        else Rejected
            H->>A: Reject (with reason)
            A->>L: Log rejection
        end
    end
```

## Component Descriptions

### Ingestion Layer
- **API Gateway**: Receives webhooks and form submissions. Verifies webhook signatures, enforces rate limits, and performs basic auth checks. No business logic here.
- **Normalizer**: Converts source-specific payloads (email headers, form fields, messaging API formats) into a uniform internal structure. Purely deterministic.
- **Idempotency Check**: Generates a SHA-256 hash from source + sender + content to prevent duplicate processing of the same submission.

### Queue Layer
- **Enquiry Queue**: Async processing buffer. Decouples ingestion from processing, enabling retries and backpressure handling.
- **Dead Letter Queue (DLQ)**: Catches permanently failed enquiries after max retries. Triggers alerts. Human operators can inspect and reprocess.

### AI Processing
- **Deterministic Spam Filter**: Rule-based keyword and blocklist check. Runs before the LLM to avoid unnecessary API costs.
- **LLM Classification & Extraction**: Sends sanitized enquiry content to an LLM with a strict output schema. Response is treated as an **untrusted proposal**.
- **Schema Validation**: Deterministic check that LLM output conforms to expected types, value ranges, and evidence requirements.
- **Response Drafting**: LLM generates a draft response. Drafts are **never sent automatically** — they go to the approval queue.

### Policy Engine
- **Business Rules**: Deterministic routing based on intent, confidence, and risk. Maps classifications to concrete actions (create lead, route to support, request clarification, discard).
- **Duplicate Detection**: Exact and fuzzy matching on email, phone, and company name against existing CRM records. No LLM involvement in merge decisions.

### Human-in-the-Loop
- **Approval Queue**: Stores pending actions with full context for human review.
- **Review Dashboard**: Presents the AI's proposal alongside the original enquiry, extracted data, confidence scores, and evidence. Humans can approve, reject, or edit.
- **Human Decision**: Approval is enforced by the application. The LLM cannot bypass this gate.

### Controlled Execution
- **Action Service**: Executes approved actions (CRM writes, email sends) through scoped service accounts. Never exposes raw credentials to the AI layer.
- **Notification Service**: Sends internal alerts (Slack, email) for approvals, failures, and escalations.

### Audit Log
- Immutable append-only log of every significant event: ingestion, classification, routing decisions, approvals, CRM writes, failures.
