# expressautomate.app — High-Level Platform Implementation Plan

## 1. Product Vision

expressautomate.app is an **AI Recruitment Intelligence & Operations Platform** designed for recruitment professionals and small recruitment agencies, starting with Microsoft 365 users.

**Positioning:** AI-powered automation, data intelligence, and operational transformation for recruitment workers and agencies, initially focused on Singapore's small-business Microsoft 365 market.

The platform should transform unstructured recruitment communications into structured, searchable, and analytically useful data.

Initial value proposition:

> Connect Microsoft 365 Outlook, automatically capture incoming recruitment emails, extract job information with AI, and turn daily recruiter activity into structured recruitment intelligence.

The long-term objective is not simply email automation.

The strategic asset is the **continuously growing recruitment dataset** created from daily operational activity.

---

## 2. Core Product Principles

### 2.1 Data-first architecture

Do not use Excel or Power Automate as the system of record.

The platform should own:

- raw source data
- structured extracted data
- activity history
- analytics data
- processing state
- extraction provenance

Excel should remain an optional export/integration target.

### 2.2 AI for interpretation, deterministic services for control

Use AI for:

- understanding unstructured email content
- extracting job information
- identifying multiple jobs in one email
- normalising job information
- classifying roles and skills
- detecting ambiguity
- semantic similarity
- duplicate opportunity detection

Use deterministic services for:

- authentication
- email retrieval
- webhook processing
- deduplication
- workflow state
- access control
- database updates
- auditing
- retry handling
- analytics calculations

### 2.3 Preserve source truth

Never discard the original email.

Store both source data and AI-derived data.

Source data should include message ID, thread ID, sender, recipients, subject, received time, original body, and attachment metadata.

AI-derived data should include company, job title, responsibilities, salary, working hours, skills, requirements, seniority, duration, employment type, location, industry, and hiring urgency.

Important extracted facts should retain evidence, confidence, model version, and extraction timestamp.

---

## 3. Target Users

Primary users:

- recruitment administrators
- recruitment consultants
- recruiters
- account managers
- recruitment agency owners

Initial target customer:

Small recruitment agencies using Microsoft 365, typically with 3–50 recruiters, heavy Outlook usage, Excel-based tracking, and limited internal IT capability.

---

## 4. High-Level Architecture

```text
                        expressautomate.app
                                |
              +-----------------+------------------+
              |                                    |
              v                                    v
        Web Application                      Background Services
              |                                    |
              v                                    v
       Recruiter Dashboard                 Microsoft Graph Integration
              |                                    |
              |                             OAuth + Mail.Read
              |                                    |
              |                         +----------+-----------+
              |                         |                      |
              |                         v                      v
              |                Change Notifications        Delta Sync
              |                    Webhook               Reconciliation
              |                         |                      |
              |                         +----------+-----------+
              |                                    |
              |                                    v
              |                             Email Ingestion
              |                                    |
              |                                    v
              |                             Raw Email Store
              |                                    |
              |                                    v
              |                             AI Extraction
              |                                    |
              |                                    v
              +----------------------------> Structured Data
                                                   |
                                                   v
                                              PostgreSQL
                                                   |
                          +------------------------+------------------------+
                          |                        |                        |
                          v                        v                        v
                     Operations               Analytics             Integrations
                          |                        |                        |
                          v                        v                        v
                     Job Intake              Trends / KPIs              Excel
                     Review Queue            Salary Data                 ATS
                     Search                  Demand Data                 Export/API
                     Workflow                Client Insights
```

---

## 5. Recommended Initial Technology Stack

Keep the first version deliberately simple.

### Frontend

- Next.js / React
- TypeScript
- responsive web interface

Core UI areas:

- Dashboard
- Email Intake
- Jobs
- Review Queue
- Search
- Analytics
- Settings / Integrations

### Backend

- FastAPI / Python
- REST API initially
- asynchronous background worker
- Microsoft Graph client
- AI extraction service

### Database

Start with:

- PostgreSQL

Optional:

- `pgvector` for semantic similarity and embeddings

Do not introduce a dedicated vector database, graph database, ClickHouse, Kafka, or Redis until there is a demonstrated requirement.

Redis may be added later for queues, distributed locks, caching, and job coordination.

### Deployment

Suitable initial options include Azure, AWS, Render, Railway, Fly.io, or Koyeb.

For Microsoft-centric customers, Azure provides a natural enterprise path, but the product should not be unnecessarily Azure-specific.

---

## 6. Microsoft 365 Integration

### 6.1 Authentication

Implement **Sign in with Microsoft** using Microsoft Entra ID OAuth 2.0.

Minimum delegated scopes:

```text
openid
profile
email
User.Read
Mail.Read
offline_access
```

Do not request:

```text
Mail.ReadWrite
Mail.Send
Application Mail.Read
```

unless a future feature explicitly requires them.

Product security message:

> expressautomate.app uses read-only Microsoft 365 access. It cannot send, modify, or delete email.

### 6.2 Initial mailbox onboarding

When a user connects Outlook:

1. Authenticate with Microsoft.
2. Request delegated read-only permissions.
3. Register mailbox connection.
4. Ask where ingestion should begin.
5. Optionally select mailbox folder.
6. Perform initial synchronisation.
7. Create Microsoft Graph change-notification subscription.
8. Store subscription expiry.
9. Begin automatic processing.

Suggested initial sync choices:

- Today
- Last 3 days
- Last 7 days
- Custom date

Avoid downloading the user's entire mailbox.

---

## 7. Real-Time Email Ingestion

Use Microsoft Graph **change notifications** rather than polling as the primary ingestion mechanism.

```text
New Outlook Email
        |
        v
Microsoft Graph
        |
        v
Webhook Notification
        |
        v
expressautomate.app Webhook API
        |
        v
Queue Message ID
        |
        v
Return HTTP 202
        |
        v
Background Worker
        |
        v
Fetch Email via Graph
```

The webhook must not perform full AI extraction synchronously.

---

## 8. Graph Subscription Management

Microsoft Graph subscriptions expire.

Implement a subscription manager that:

- creates subscriptions
- tracks expiry
- renews before expiry
- recreates failed subscriptions
- logs subscription state
- detects disconnected accounts

Example state:

```text
tenant
user
mailbox
subscription_id
resource
created_at
expires_at
status
last_renewed_at
```

---

## 9. Delta Synchronisation / Reconciliation

Webhooks provide speed, but the platform also needs recovery protection.

Implement Microsoft Graph delta synchronisation to:

- recover missed notifications
- handle webhook downtime
- recover from temporary API failures
- verify mailbox consistency

Recommended pattern:

```text
Webhook
   |
   +----> near-real-time ingestion

Periodic Delta Sync
   |
   +----> reconciliation / recovery
```

The delta token/checkpoint should be stored per mailbox or folder.

---

## 10. Raw Email Storage

Store incoming email metadata before AI extraction.

Suggested entity:

`email_messages`

Example fields:

```text
id
tenant_id
user_id
mailbox_id
graph_message_id
conversation_id
internet_message_id
sender_name
sender_email
subject
received_datetime
body_text
body_html
has_attachments
processing_status
created_at
updated_at
```

Use Microsoft message identifiers for duplicate protection.

Raw email data should be logically separated from extracted recruitment data.

---

## 11. Email Pre-processing

Recruitment messages may contain:

- normal paragraphs
- bullet lists
- copied job advertisements
- HTML tables
- signatures
- disclaimers
- email chains
- forwarded messages
- multiple vacancies
- inconsistent labels
- incomplete information

Pre-processing should:

- convert HTML to usable text
- preserve subject and sender context
- remove obvious technical noise
- avoid aggressive removal of forwarded content
- identify attachment references
- retain original source

Do not use regex as the primary information extraction mechanism.

---

## 12. AI Extraction Layer

The AI service converts unstructured email content into a stable structured schema.

The model must identify information by **meaning**, not by fixed position or labels.

Example input:

```text
SUBJECT:
Treasury Opportunity

SENDER:
recruiter@agency.sg

BODY:
Our client is looking for someone to support their Treasury desk.
Budget is around 5.5k.
The successful candidate should have around 3 years' banking
experience. Normal working hours are roughly 9 to 6.
```

Expected output:

```json
{
  "jobs": [
    {
      "company": "Not mentioned",
      "job_position": "Treasury Support",
      "salary": "Around SGD 5,500",
      "working_hours": "Approximately 9am-6pm",
      "requirements": "Around 3 years of banking experience"
    }
  ]
}
```

---

## 13. AI Output Contract

Use a structured output schema.

Top-level model:

```json
{
  "jobs": []
}
```

Suggested fields per job:

```text
job_index
company
job_position
job_description
responsibilities
salary_raw
salary_min
salary_max
currency
salary_period
working_hours
requirements
skills
seniority
employment_type
duration
work_location
industry
urgency
status
missing_fields
extraction_status
```

This schema supports multiple vacancies in one email.

---

## 14. Extraction Provenance

Important extracted values should retain evidence.

Example:

```json
{
  "salary": {
    "raw": "$5k-$7k depending on experience",
    "normalised_min": 5000,
    "normalised_max": 7000,
    "currency": "SGD",
    "evidence": "$5k-$7k depending on experience",
    "confidence": 0.97
  }
}
```

This provides traceability, review support, model evaluation, and reprocessing capability.

---

## 15. Missing Information Policy

AI must never fabricate absent information.

For example, `"office hours"` must not automatically become `"9am-6pm"`.

Likewise, `"central Singapore"` must not become `"Raffles Place"`.

Return `Not mentioned` or preserve the original wording.

False data is more damaging than missing data.

---

## 16. Multiple Jobs Per Email

Support this from the first schema version.

```text
Email
 |
 +-- Job 1: Maybank Treasury Support
 |
 +-- Job 2: TotalEnergies Accountant
 |
 +-- Job 3: ABC Pharma QA Executive
```

Recommended model:

> One job opportunity = one structured job record.

The source email remains linked to all extracted opportunities.

---

## 17. Recruitment Data Model

The platform should move beyond an email-centric model.

Recommended core entities:

```text
Tenant
User
Mailbox
Email
Email Thread

Company
Contact
Client

Opportunity
Role
Role Skill
Skill
Requirement
Compensation
Location

Extraction
Extraction Evidence

Activity
Status History
```

Relationships should preserve:

```text
Email
  -> Opportunity
  -> Company
  -> Role
  -> Skills
  -> Compensation
  -> Location
```

---

## 18. Multi-Tenancy

Multi-tenancy must be designed from day one.

Every business record should contain:

```text
tenant_id
```

This includes emails, opportunities, companies, skills, analytics, embeddings, extractions, users, and mailboxes.

Agency A data must never be accessible to Agency B.

Recommended controls:

- tenant-aware database queries
- row-level security where appropriate
- strict API authorisation
- tenant-scoped storage
- tenant-scoped analytics
- tenant-scoped embeddings

---

## 19. Deduplication

The system needs two forms of deduplication.

### Email deduplication

Use identifiers such as:

```text
graph_message_id
internet_message_id
```

### Opportunity deduplication

Different emails may describe the same actual vacancy.

Potential signals:

- company
- job title similarity
- skill overlap
- location
- salary range
- received timing
- semantic similarity
- client/recruiter source

The system should flag:

```text
Possible duplicate / updated vacancy
```

rather than silently merging records.

---

## 20. Semantic Search

Use embeddings later for:

- similar job detection
- duplicate opportunity detection
- natural-language job search
- role clustering
- skills similarity
- candidate matching

Initial implementation can use PostgreSQL + `pgvector`.

Example queries:

```text
Show cloud engineering jobs paying above $8k.

Find roles similar to this Maybank Treasury role.

Which clients asked for GenAI skills this quarter?
```

---

## 21. Operational Dashboard — MVP

The first dashboard should remain simple.

### Overview

Show:

```text
New Emails Today
Jobs Extracted
Ready
Need Review
Failed
Duplicate Candidates
```

### Jobs view

Columns:

```text
Received Date
Company
Position
Salary
Working Hours
Requirements
Duration
Location
Source
Extraction Status
```

### Review Queue

Show only:

- ambiguous extraction
- missing critical information
- low-confidence fields
- possible duplicates
- multiple-job parsing issues

The objective is:

> Humans review exceptions instead of manually processing every email.

---

## 22. Search and Filtering

MVP filters:

- date
- company
- recruiter
- job title
- salary
- skill
- location
- employment type
- duration
- status
- extraction status

Later add natural-language search.

---

## 23. Excel Export

Excel remains valuable because recruiters already use it.

Support:

- export current filtered results
- export today's jobs
- export selected jobs
- export approved jobs

Potential future feature:

- automatic synchronisation into a specific OneDrive/SharePoint workbook

Excel should not be the primary database.

---

## 24. Analytics Foundation

Store enough structured data from day one to support analytics.

Important dimensions:

```text
Time
Company
Role
Skill
Salary
Location
Employment Type
Industry
Recruiter
Client
```

Important measures:

```text
Job volume
Role demand
Salary distribution
Skill demand
Client activity
Opportunity ageing
Repeat vacancy rate
Recruiter response time
Extraction quality
```

---

## 25. Initial Analytics

### Job Intake

- jobs received today
- jobs received this week
- jobs by company
- jobs by recruiter
- jobs by role category

### Salary

- average salary
- median salary
- salary range by job title
- salary range by company

### Skills

- most requested skills
- skills by role
- emerging skill frequency

### Client Activity

- most active clients
- client hiring volume
- roles received by client
- role frequency

---

## 26. Advanced Analytics Roadmap

Later analytics can include:

### Demand trends

```text
Java Developer     +24%
Data Engineer      +41%
Business Analyst   -12%
```

### Salary intelligence

```text
AI Engineer
Singapore

P25
Median
P75
Trend
```

### Client intelligence

```text
Client hiring activity
Repeated vacancies
Salary competitiveness
Role ageing
Role changes
Hiring acceleration
```

### Skill intelligence

```text
Fastest-growing skills
Skill combinations
Skills associated with salary premiums
Industry-specific demand
```

---

## 27. Activity / Event History

Do not overwrite important changes.

Store events such as:

```text
Opportunity created
Salary changed
Requirements changed
Location changed
Duration changed
Status changed
Duplicate identified
Recruiter reviewed
Exported to Excel
```

This creates longitudinal data.

Example future insight:

```text
Client increased salary twice after the position remained unfilled.
```

---

## 28. Recruitment Intelligence Layer

As the dataset grows, expressautomate.app can evolve from operations software into an intelligence platform.

Potential insights:

- which clients are hiring most
- which industries are accelerating
- which roles are becoming difficult to fill
- which skills are increasingly requested
- salary movements
- role ageing
- duplicate/repeated demand
- client hiring behaviour
- recruiter workflow efficiency

This should become a major product differentiator.

---

## 29. Knowledge Graph — Future

A graph database is not required initially.

However, preserve relationships that can later support a knowledge graph.

```text
Company
   |
   +--> hires --> Role
                   |
                   +--> requires --> Skill
                   |
                   +--> pays --> Salary
                   |
                   +--> located at --> Location
```

PostgreSQL is sufficient initially.

Introduce a graph database only if relationship-heavy query patterns justify it.

---

## 30. Security and Privacy

Minimum controls:

- delegated read-only Microsoft Graph access
- encrypted OAuth/refresh tokens
- encryption in transit
- encryption at rest
- tenant isolation
- least-privilege permissions
- audit logging
- secure secrets management
- configurable retention
- account disconnection/revocation
- user deletion workflow
- data export workflow

Avoid storing unnecessary email data.

---

## 31. AI Data Governance

Track:

```text
model
provider
prompt_version
schema_version
extraction_timestamp
confidence
evidence
```

This allows:

- reprocessing old records
- model migration
- extraction audits
- customer dispute handling
- reproducible analytics

Do not overwrite historical extraction metadata.

---

## 32. Model Strategy

Do not use the most expensive reasoning model by default.

Most email extraction should use a fast, cost-efficient structured-output model.

Use stronger models only for:

- very long emails
- complex forwarded threads
- multiple vacancies
- conflicting information
- low-confidence retries

Suggested routing:

```text
Normal Email
    |
    v
Fast/Cheap Extraction Model
    |
    +---- High confidence ---> Accept
    |
    +---- Low confidence ----> Stronger Model
                                |
                                +---- Still unclear ---> Human Review
```

---

## 33. Observability

Track platform health from the beginning.

Important metrics:

```text
emails received
emails processed
processing latency
Graph webhook failures
subscription renewal failures
AI extraction failures
AI latency
tokens/cost per extraction
duplicate rate
review rate
database errors
export errors
```

Use structured application logging.

---

## 34. Cost Control

Initial cost drivers:

- application hosting
- PostgreSQL
- AI API usage
- email volume
- attachment processing
- storage

Control methods:

- retrieve only new emails
- use webhook events rather than constant polling
- use delta queries for reconciliation
- use smaller models for normal extraction
- avoid embedding everything unnecessarily
- process attachments only when required

---

## 35. MVP Scope

The MVP should prove the complete data loop.

### Account

- user registration
- tenant creation
- Microsoft login

### Outlook

- Connect Microsoft 365
- delegated `Mail.Read`
- initial mailbox sync
- Graph webhook subscription
- subscription renewal
- delta reconciliation

### Email ingestion

- ingest new emails
- deduplicate emails
- store source data
- pre-process body

### AI

- identify recruitment email
- structured job extraction
- multiple jobs per email
- missing-field handling
- extraction status
- evidence/confidence for critical fields

### Jobs

- list extracted jobs
- detail page
- search/filter
- edit extracted data
- approve/reject
- review queue

### Data

- PostgreSQL source of truth
- tenant isolation
- activity log

### Export

- Excel/CSV export

### Basic analytics

- job volume
- companies
- roles
- salary
- skills
- locations

---

## 36. Phase 2

After validating the MVP:

- PDF/Word attachment extraction
- job-title normalisation
- company normalisation
- skills taxonomy
- better salary normalisation
- semantic search
- opportunity deduplication
- role clustering
- automated Excel sync
- Teams notification
- ATS integrations
- agency-level dashboards
- recruiter activity analytics

---

## 37. Phase 3

Intelligence capabilities:

- salary benchmarking
- market-demand trends
- role-demand forecasting
- client hiring behaviour
- repeated vacancy analysis
- role ageing analysis
- emerging skill detection
- recruiter productivity insights
- natural-language analytics
- AI-generated client reports

---

## 38. Phase 4

Platform expansion:

- CV ingestion
- candidate profiles
- candidate/job matching
- submission tracking
- interview workflow
- recruiter-client communication intelligence
- placement analytics
- agency performance analytics
- external API
- marketplace/integrations

---

## 39. Suggested Delivery Sequence

### Stage 1 — Foundation

Build:

- repository
- environments
- PostgreSQL
- tenant/user model
- authentication
- basic web shell
- logging

### Stage 2 — Microsoft Integration

Build:

- Microsoft OAuth
- Graph email read
- initial sync
- webhook endpoint
- subscription manager
- delta reconciliation

Validate reliability before building advanced UI.

### Stage 3 — Data Ingestion

Build:

- raw email schema
- email deduplication
- HTML/text conversion
- processing queue
- retry handling
- processing logs

### Stage 4 — AI Extraction

Build:

- extraction prompt
- JSON schema
- multiple-job support
- evidence/confidence
- missing-field policy
- model routing
- evaluation dataset

### Stage 5 — Operational Product

Build:

- job list
- job details
- review queue
- edit/approve
- filters/search
- Excel/CSV export

This is the first customer-usable release.

### Stage 6 — Analytics

Build:

- analytics data model
- role trends
- salary analytics
- skills analytics
- client activity
- dashboard

### Stage 7 — Intelligence

Add:

- embeddings
- semantic search
- duplicate vacancy detection
- clustering
- longitudinal insights
- natural-language analytics

---

## 40. MVP Success Criteria

### Ingestion

- new Outlook email captured automatically
- no Power Automate dependency
- webhook ingestion works reliably
- missed events recovered through delta sync

### Extraction

- unstructured recruiter email converted into structured jobs
- multiple jobs correctly separated
- missing information not fabricated
- ambiguous information routed to review

### Operations

- recruiter can review and correct extracted records
- recruiter can search/filter jobs
- recruiter can export approved data to Excel

### Data

- original email remains traceable
- structured data stored independently
- all records tenant-isolated
- processing history auditable

### Analytics

The system can already answer:

```text
How many jobs did we receive this month?
Which clients sent the most roles?
What positions are most common?
What salary ranges are we seeing?
Which skills are requested most often?
```

---

## 41. What Not to Build Initially

Avoid premature complexity.

Do not build initially:

- full ATS
- candidate CRM
- mobile application
- custom LLM
- knowledge graph database
- dedicated vector database
- Kafka infrastructure
- ClickHouse
- complex agent framework
- workflow designer
- public marketplace
- cross-agency data sharing

First prove:

```text
Email
  -> Data
  -> Intelligence
  -> Recruiter Value
```

---

## 42. Strategic Product Flywheel

```text
More Recruitment Emails
          |
          v
More Structured Opportunities
          |
          v
Richer Recruitment Dataset
          |
          v
Better Analytics
          |
          v
Better Recruitment Intelligence
          |
          v
Better Decisions
          |
          v
More Daily Usage
          |
          +--------------------------+
                                     |
                                     v
                               More Data
```

The LLM is not the core moat.

The long-term moat is:

> **A continuously growing, structured, longitudinal recruitment dataset and the intelligence derived from daily recruitment operations.**

---

## 43. Recommended First Release Architecture

```text
                Browser
                   |
                   v
             Next.js / React
                   |
                   v
                FastAPI
                   |
        +----------+-----------+
        |                      |
        v                      v
Microsoft Graph           AI Extraction
        |                      |
        v                      v
 Outlook Email        Structured jobs[]
        |                      |
        +----------+-----------+
                   |
                   v
               PostgreSQL
                   |
          +--------+---------+
          |                  |
          v                  v
     Operations          Analytics
          |
          v
     Excel / CSV Export
```

Supporting backend components:

```text
Webhook Endpoint
Background Worker
Subscription Manager
Delta Sync
Retry/Error Handling
Audit Log
```

This is sufficient to launch a serious MVP without over-engineering the platform.
