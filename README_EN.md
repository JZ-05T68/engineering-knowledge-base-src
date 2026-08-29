# Engineering Knowledge Base v0.6.0

[Chinese](README.md) | [English](README_EN.md)

**Engineering Knowledge Base (EKB)**

A local-first personal knowledge system for accumulating, organizing, verifying and reusing long-term knowledge assets.

EKB is built for individual users who accumulate engineering experience over time. It turns actively imported materials, manually recorded experience and verifiable sources into local knowledge assets, instead of reducing the project to a PDF summarizer, a RAG tool or a general-purpose chatbot.

## Positioning

General-purpose AI can generate answers, but it cannot naturally preserve one person's long-term experience, understand why a project made a particular decision, how a failure was located, or why a failed path should not be repeated.

EKB focuses on accumulating the following over time:

- project decisions and their context;
- debugging processes and troubleshooting paths;
- errors, failures and rework experience;
- learning notes and conceptual understanding;
- reusable engineering practices.

These items become traceable, verifiable and reusable personal knowledge assets through sources, revisions, fingerprints, evidence and retrieval:

```text
materials → understanding → knowledge objects → source verification → knowledge memory → retrieval → reuse → engineering capability
```

## Core Architecture

```text
Source ── Fingerprint ──> Knowledge Object ── Revision ──> auditable evolution
  │                              │
  ├── Document / Page            ├── Relation
  ├── Note / Evidence            └── Knowledge Memory
  │                                      │
  └──────── Provenance ──────────────────┤
                                         v
                       Retrieval (Page / Object / Memory)
```

| Component | Purpose |
| --- | --- |
| **Knowledge Object** | Organizes concepts, facts, principles, experience, problems and decisions into manageable knowledge units. |
| **Source** | Links a knowledge object to a local document, page, note or evidence item. |
| **Revision** | Preserves knowledge-object changes as an append-only history instead of silently overwriting evolution. |
| **Fingerprint** | Captures a canonical SHA-256 fingerprint of a source to detect valid, changed, missing or unknown states. |
| **Evidence** | Keeps source anchors and human confirmation states for whole pages, text selections or image regions. |
| **Knowledge Memory** | Manually records problem solving, experience and decisions, with links to knowledge objects or other local sources. |
| **Retrieval** | Provides local retrieval and source links across pages, knowledge objects and knowledge memories. |
| **AI capability** | An optional, auditable, citation-constrained, on-demand enhancement layer that never replaces local facts or human confirmation. |

## v0.6.0 — Agent Foundation

v0.6.0 builds on the v0.5.3 audited AI integration by establishing the Agent Foundation and the
hosted deployment foundation: a local single-step read-only Agent, a frozen public HTTP contract
and container packaging. **Actual public-cloud deployment was deferred out of this release gate by
the maintainer on 2026-08-28; this release neither includes nor claims any public deployment.**

Current capabilities:

- **Single-step read-only Agent**: one request runs Decision → 0/1 Tool → Final Answer; no loops,
  no multi-step planning, zero Agent-autonomous retries, at most two logical model calls.
- **7 READ_ONLY tools**: `page_search`, `knowledge_search`, `get_knowledge_object`,
  `get_knowledge_memory`, `inspect_provenance`, `inspect_source_integrity`, `get_evidence`;
  the registry admits read-only tools only, and unknown or disallowed tools fail closed.
- **rag_answer is the Final Answer Stage, not a Tool**: the final answer is generated only from
  Tool evidence and reuses the existing citation validation; unknown, forged or out-of-range
  citations never surface as a half-finished answer.
- **Source integrity semantics**: `inspect_source_integrity` reads already-captured fingerprint
  states (valid / changed / missing / unknown) without refreshing, recomputing or writing; stale
  sources are never silently treated as reliable facts.
- **Hosted HTTP Agent API (WP2)**: `/health`, `/ready`, `POST /v0.6/agent/run`,
  `GET /v0.6/sources/{stable_id}`; the public DTOs are frozen, errors use a closed message
  catalog, and internal traces, ToolResults, token usage or raw model responses are never exposed.
- **Hosted security boundary (WP3)**: rate and concurrency limits, request body cap, exact CORS
  origins, budget rejection and error sanitization; keys exist only server-side and never enter
  logs or responses.
- **Hosted storage safety (WP4)**: an independent hosted SQLite bootstrap with exact schema-v12
  validation, symlink and sidecar rejection; the Local production database is never reused.
- **Deployment packaging (WP5)**: Linux container and non-root (UID/GID 10001) runtime packaging
  with passing integration tests; actual cloud deployment validation remains PAUSED.
- **Local operation unchanged**: the official service binds to `127.0.0.1:8501` only; AI defaults
  to manual mode, and without an API Key every offline core feature keeps working; the Agent
  changes no existing local workflow.

Explicit boundaries: the Agent cannot write, has no autonomous retries and no long-term session
memory; there is no public Agent (WP6A PARTIAL / PAUSED, WP6B NOT STARTED); actual public
deployment is a deferred item (DEFERRED) that resumes only with an explicit maintainer decision.

## v0.5.3 — Audited AI Integration

v0.5.3 builds on the v0.5.2 Knowledge Foundation by adding auditable, citation-constrained, on-demand AI assistance over knowledge that the user explicitly selects or retrieves, together with an AI call ledger, structured exports and legacy backup upgrade.

Version capabilities:

- **ContextItem and KnowledgeContextPackage**: pages, knowledge objects, knowledge memories and evidence are projected into read-only ContextItems and packaged with citations, sources, lifecycle and exclusion information.
- **Two retrieval scopes**: page-material search and personal-knowledge search remain offline-first with unchanged default behavior.
- **Ask AI / RAG Answer**: the user asks on demand, and the AI may answer only from the selected KnowledgeContextPackage.
- **Runtime citation validation**: every citation in an AI answer must belong to the current context package; unknown, forged, out-of-range and empty citations fail closed without showing a half-finished answer.
- **Read-only AI experience organizer**: generates structured experience candidates on demand for preview only; nothing is written to knowledge assets automatically.
- **AI call ledger**: ai_calls records capability, source feature, status, tokens and target references; the ledger is read-only and never stores full prompts, context text or model answers.
- **Knowledge Export**: lossless structured export of knowledge objects, sources, relations, revisions and memories with a manifest, per-file SHA-256 and one Markdown file per object.
- **AI Ledger Export**: a standalone audit export of ai_calls metadata (JSON/JSONL authoritative), without content or credentials.
- **Schema-v8 legacy backup isolated upgrade**: legacy database snapshots are upgraded to the current schema through a dedicated entry point while the original backup stays unchanged.
- **schema v12**: the current database structure hosting both knowledge assets and the AI ledger.
- **Local operation**: the official service binds to `127.0.0.1:8501` only; without an API Key all offline core features keep working.

Explicit boundaries: AI does not write knowledge automatically; an Experience Candidate is not confirmed experience; there is no Agent; no tool calling; no long-term session memory; no automatic scanning of private files; no cloud sync; the AI call ledger does not store full prompts, context or answers; AI is an optional layer, not a dependency of core features.

## Personal Knowledge Workflow

```text
user imports or records on demand
        ↓
Document / Page / Note / Evidence
        ↓
Knowledge Object + Source Fingerprint
        ↓
human review, revision and relation organization
        ↓
Knowledge Memory
        ↓
Page / Object / Memory Retrieval
        ↓
(optional) user selects context → Ask AI / AI experience organizer → read-only result
        ↓
return to sources for verification and reuse
```

The system may extract metadata, render pages, detect text layers, mark pages for review and suggest organization paths, but it never overwrites original materials, modifies user notes, deletes files or turns unconfirmed inference into personal experience.

## Existing Core Capabilities

- import PDFs, detect duplicates by SHA-256, render page PNGs and extract existing text layers;
- explicit local single-page OCR for scanned pages, preserving the "not human-reviewed" boundary;
- browse documents and pages, maintain Markdown, structured notes, tags and projects;
- local full-text search with SQLite FTS5, jieba and field weights;
- create page, text-selection and image-region evidence, then generate citation-grounded prompt packages after human confirmation;
- manage knowledge objects, memories, sources, relations, revisions and source integrity;
- create, validate and restore complete local backups;
- use optional page vectors and hybrid search when explicitly configured; offline core features remain available when AI is unavailable.

## Local-First and Information Boundaries

- Local files and SQLite are the single source of truth; user materials stay on the local machine by default.
- The official service binds to `127.0.0.1:8501` only and is never exposed to the LAN or the public internet.
- The system processes only materials the user imports or explicitly authorizes; it never scrapes private chats or unauthorized third-party material.
- Original PDFs and page images are never overwritten or deleted automatically.
- `ai_mode="manual"` by default; the application starts and core features work without an API Key.
- No registration, login, accounts, passwords, OAuth, JWT, roles, administrators or multi-user permissions.
- No cloud sync; backup location and media are controlled by the user.

## Release Validation

v0.6.0 release validation (candidate audit + final release check):

- Final release check `scripts/release_check.py` (2026-08-29): **PASS (exit 0, 20/20 checks)**,
  including listener acceptance (`127.0.0.1:8501`, health HTTP 200), production database
  integrity / foreign keys / schema v7–v12 invariants, README parity and a verified release
  backup;
- Official release full pytest: **2640 tests, all passed, exit 0** (the 2641 count displayed by
  release_check is a recorded display artifact, non-blocking, deferred as a maintenance item);
- Ruff: **PASS**; `git diff --check`: **clean**;
- Release tag: `v0.6.0` → `bb1a4207af5bf70e0fbf0f5607c396e06bddfc8d`;
- The production database kept an identical SHA-256 and size before and after the release check;
  it was not modified by the release procedure;
- Public deployment status: **DEFERRED** (2026-08-28 maintainer scope decision, not a release
  gate).

These numbers record the release baseline and do not imply equal quality guarantees for every engineering domain, corpus or query.

## Version History

The entries below restore the formal scope and boundaries of each earlier release. The v0.0.x entries follow the
repository's closed milestones; releases from v0.1.0 onward are also cross-checked against tags, the changelog and
release records. These historical entries are not the current v0.6.0 capability statement.

### v0.0.1 — Initial Local Knowledge Base MVP

Established the local PDF workflow: preserve imported originals, render page images, extract text layers, identify
pages needing review, maintain page-level Markdown, browse documents, search locally, generate traceable evidence
packages and persist metadata in SQLite.

### v0.0.2 — Page-Level Knowledge Management and Background Operation

Established page-level organization and the Windows background start, stop, status, PID-validation, health-check and
rotating-log workflow.

### v0.0.3 — Continuous Review Workflow

Added a continuous queue for pending, draft, failed, reviewed and skipped pages, with previous/next navigation,
save-and-continue actions, unsaved-change protection and optional keyboard shortcuts.

### v0.0.4 — Traceable Page Retrieval and Evidence Packages

Made page search traceable to the document, original filename, page number, local source paths, review state and
matched context. Evidence-package generation kept source material separate from user notes and warned on unreviewed pages.

### v0.0.5 — Multi-Page Evidence Collection

Introduced the persistent evidence basket for collecting selections from multiple pages, editing notes, ordering or
removing evidence, returning to the source and exporting single- or multi-document Markdown packages. Source hashes
and pre-generation validation prevent stale material from being exported as current evidence.

### v0.0.6 — Search Filters and State Restoration

Expanded local search with document, project, tag, review-state, matched-field, note and evidence-basket filters;
AND semantics for multiple projects or tags; relevance and recency sorts; facet counts; literal Unicode option search;
and allowlisted URL state restoration.

### v0.0.7 — Search Explainability and Continuous Reading

Added page and document-grouped result views, source-labeled excerpts and literal match counts, on-demand page previews,
full-result and within-document navigation, and restoration of search, filter, sort, panel, preview and result-focus
state without storing complete result ID lists in the URL.

### v0.0.8 — Full Backup, Diagnostics and Release Closure

Completed verified local backups, read-only diagnostics, redacted diagnostic reports, safe restore preflight and
unified release checks, then closed directly into v0.1.0. No v0.0.9 release was created or planned.

### v0.1.0 — First Full Manual Acceptance and Formal Release

Closed the first formal release after full manual acceptance. It included manifest-based complete backups with SQLite
online snapshots and SHA-256 records, stop-before-restore with pre-restore backup and rollback, read-only integrity
diagnostics, redacted reports, usable empty states and a unified release-check entry point.

### v0.1.1 — Stability and Usability Patch

Fixed the production endpoint at `127.0.0.1:8501`, improved Windows diagnostics, added an explicit route from import
results to pending review, and preserved distinct selections on one page while rejecting a duplicate normalized
selection. Schema v4 remained unchanged and no AI, OCR, embedding, semantic-search or network API capability was added.

### v0.1.2 — Batch Organization

Added bounded batch updates for visible search results and the current review batch, with preflight, explicit
confirmation, one-time action tokens, stable selection scope, additive tag/project changes and a single SQLite
transaction. Cross-page or "select all matches" operations were intentionally excluded.

### v0.2.0 — Long and Non-Standard Document Foundation

Introduced isolated single-page processing, deterministic blank/short/landscape/rotated diagnostics, page-level failure
isolation, document diagnostic summaries, a 120-page automated baseline and a 300-page mixed-PDF acceptance run.
Schema v4 remained unchanged; this release did not add OCR, embeddings, semantic search or an external model API.

### v0.2.1 — Default Evidence Basket Concurrency Patch

Moved "find or create the default evidence basket" into one short transaction so concurrent first access returns one
basket instead of creating duplicates. Schema v4 and product scope were unchanged.

### v0.2.2 — Local OCR

Added explicit, offline, single-page OCR for printed text with RapidOCR and ONNX Runtime. OCR text is stored separately,
displayed as an unverified draft, included in local search with a warning, and never overwrites the PDF, rendered page,
text layer, manual Markdown or review state. Handwriting, formulas, structured tables, rotation correction and batch OCR
remain outside scope.

### v0.2.3 — v0.2.x Closure

Closed the long-document foundation with atomic PDF and page-image writes plus recovery after interrupted imports,
stable navigation through large result sets, zero-increment duplicate imports across database and file assets, and
diagnosable failed records when duplicate probing fails. The automated suite recorded 553 passing tests.

### v0.2.4 — Release and Deployment Consistency Patch

Aligned release checks, backup and restore tools, the displayed application version and the formal local deployment
entry point. It added no business capability, schema change or AI feature.

### v0.3.0 — Structured Notes Foundation

Added create, read, update and delete workflows for document, page, text-selection and image-region notes. Text
selections preserve separate source snapshots, user excerpts and personal notes; image regions bind coordinates to the
original PNG and SHA-256. Schema v5 migrated incrementally with a pre-migration backup, and document deletion gained
impact preview, exact-title confirmation, quarantine and two-phase cleanup.

### v0.3.1 — Note Importance and Visual Mapping

Added primary, secondary and normal importance to all four structured-note types, including filtering and customizable
badge backgrounds. Schema v6 was an incremental, pre-backed-up migration; existing notes defaulted to normal importance.

### v0.3.2 — Cross-Document Aggregation and Deletion Lifecycle

Added a read-only, paginated aggregation view organized by project or tag, with importance and note-type filters and
links back to sources or evidence. Document deletion gained impact reporting, per-operation quarantine manifests,
restart reconciliation, conservative preservation of unknown states and explicit high-risk confirmation for evidence removal.

### v0.3.3 — Document Management and Data Safety

Introduced a dedicated document-management page and centralized the deletion confirmation flow. A service-layer
exact-title check prevents side effects on mismatch, while the v0.3.2 quarantine and recovery design remains in force.
Schema v6 was unchanged.

### v0.4.0 — Evidence Objects and Source Model

Unified whole-page, text-selection and image-region evidence under common source location, validation and
human-confirmation semantics. Durable anchors use source-text hashes or original-PNG hashes, dimensions and coordinates.
Schema v7 migrated existing evidence to unconfirmed text selections.

### v0.4.1 — Citation-Grounded Prompt Packages

Added prompt-package generation from confirmed evidence only, with source validation before generation and fail-closed
behavior when any source is invalid. Text selections include source text, pages include clearly labeled current page
text, and image regions include location and coordinates without inventing image contents. User notes remain separate
from source facts, and EKB itself still does not call AI.

### v0.4.2 — Prompt Freshness and Stale-Output Protection

Bound generated prompt packages to the current question and confirmed evidence inputs. Changes to evidence,
confirmation, ordering, notes, page text or source validity invalidate and clear the old package; unrelated tag,
project, review-state or unconfirmed-evidence changes do not. Schema v7 and dependencies were unchanged.

### v0.4.3 — Real-Problem Validation and AI Readiness Gate

Validated real engineering material and questions across source authenticity, human confirmation, cross-document
separation, image-region limits, stale-prompt invalidation and original-page traceability. The CONDITIONAL GO decision
allowed later AI integration work; it did not claim that AI integration already existed or was validated.

### v0.5.0 — AI Foundation and Optional Hybrid Retrieval

Added an optional provider interface, controlled real calls, page-level embedding persistence and freshness, explicit
indexing orchestration, persistent vector recall, and optional hybrid retrieval that unions lexical and vector
candidates through RRF. Production and isolated test data, ports, logs, backups and runtime state remain separated;
manual AI mode and offline fallback remain the defaults.

### v0.5.1 — Retrieval Stabilization

Established a frozen evaluation workflow, made hybrid fallback states explicit, calibrated assumptions with real
embeddings, exposed read-only index coverage and added an honest weak-evidence notice. Production still uses
equal-weight RRF; index completion remains manual, no numeric similarity eligibility threshold was introduced, and the
final displayed-result boundary remains undefined. The release recorded 1,475 passing tests, a successful production
rollout and zero rollout regressions.

### v0.5.2 — Knowledge Foundation

v0.5.2 was a product-direction restructuring: EKB moved beyond a local engineering knowledge base narrated primarily
around page material, evidence and RAG, toward a Knowledge Foundation for accumulating, verifying, retrieving and
reusing long-term personal knowledge assets. RAG and external AI were no longer the product's primary identity; AI was
positioned explicitly as an optional enhancement layer above the knowledge foundation.

The release established Knowledge Objects, Knowledge Memories, typed Relations, stable identifiers, lifecycle states
and append-only Revisions. Source / Provenance links knowledge objects to local documents, pages, notes or evidence,
while canonical SHA-256 Source Fingerprints distinguish valid, changed, missing, corrupt and unknown sources. Schema
v11 added dedicated SQLite FTS5 indexes, synchronization triggers, legacy backfill and deterministic rebuild paths for
Knowledge Objects and Knowledge Memories, creating a personal-knowledge retrieval foundation that leads users back to
local sources for verification.

v0.5.2 did not implement an Agent, tool calling, automatic experience learning, background knowledge rewriting or
cloud synchronization. Agent Foundation enters scope only from v0.6.x. See the dedicated section above for the current
v0.6.0 capabilities and boundaries.

## Roadmap

| Version line | Theme | Status |
| --- | --- | --- |
| **v0.5.x** | Knowledge Foundation | v0.5.3 completes audited AI integration, ledger, exports and backup upgrade. |
| **v0.6.x** | Agent Foundation | **v0.6.0 is officially released (RELEASED / CLOSED, tag `v0.6.0`, 2026-08-29)**; actual public deployment is DEFERRED (WP6A PARTIAL/PAUSED, WP6B NOT STARTED). v0.6.1 Competition Demo Experience is activated and in formal development. Agent autonomy and tool calling begin here only. |
| **v0.7.x** | Personal Experience Agent | Planned, not implemented. Long-term user experience is used from here only. |
| **v0.8.x** | Agent Reliability | Planned, not implemented. Handles Agent misbehavior and reliability. |
| **v0.9.x** | Agent Hardening | Planned, not implemented. Handles long-running operation, cost, context, memory pollution, Eval and engineering hardening. |
| **v1.0.0** | Personal Experience System | Long-term direction, not implemented. The complete personal experience system. |

See [v0.5.x Roadmap](docs/v0.5.x-roadmap.md) for detailed boundaries. The roadmap is not a capability promise and does not promise release dates.

## Limitations

- Currently a Windows-local, single-user system with no multi-user collaboration or permission model.
- No cloud sync, cloud accounts or hosted services; cross-device backup is managed by the user.
- Agent, tool calling and long-term session memory are not implemented; the system does not plan tasks autonomously or act continuously on the user's behalf.
- It does not learn personal experience automatically; knowledge objects and memories require explicit user creation, review or confirmation.
- An Experience Candidate is only an AI-organized draft, not confirmed experience, and is never written into the knowledge base automatically.
- Local OCR targets single-page printed text; it does not support handwriting, formulas, complex table structure or batch OCR.
- Keyword search does not expand synonyms automatically; optional hybrid search is not full semantic understanding and does not replace source verification.
- Search display and batch operations have load-range limits; deep pagination performance on very large knowledge bases still needs observation.
- Complete backups are local directory structures, not encrypted archives; backup media security is the user's responsibility.

## Installation

Requirements: Windows 10/11, PowerShell, Python 3.11 and a Python SQLite build with FTS5 support.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env` is an optional local configuration and must never be committed to Git. See
[Windows recovery and environment rebuild](docs/windows-recovery.md) for the complete new-machine recovery flow. GitHub never stores user databases, imported materials, credentials, logs or caches.

## Start and Stop

- Start: double-click `启动工程知识库.bat`;
- Silent start: double-click `静默启动工程知识库.vbs`;
- Status: double-click `查看运行状态.bat`;
- Stop: double-click `停止工程知识库.bat`.

For development, run in the foreground:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

The official endpoint is <http://127.0.0.1:8501> and the health check is
<http://127.0.0.1:8501/_stcore/health>. Never change it to `0.0.0.0` or expose it to an external network.

## Local Data and Safety

```text
data/
├── raw/            # original PDFs; compatible with existing local data paths
├── pages/          # page PNGs
├── markdown/       # page Markdown
└── database/
    └── knowledge.db
```

`data/`, `backups/`, `logs/`, `runtime/` and local configuration are separated from source code and ignored by Git. Database upgrades use pure incremental migrations, pre-migration backups, transactions, integrity checks and foreign-key checks. Document deletion requires an explicit impact preview and confirmation; the system never deletes original PDFs or page images automatically.

## Quality Checks

```powershell
python -m pytest
python -m ruff check .
git diff --check
```

Unified release check:

```powershell
.\.venv\Scripts\python.exe scripts\release_check.py
```

The release commit and tag closure may use verified-backup and stopped-service modes; every failed check must be handled explicitly before release.

## Documentation

- [CHANGELOG](CHANGELOG.md)
- [v0.5.x Roadmap](docs/v0.5.x-roadmap.md)
- [v0.6.0 Release Notes (Chinese)](docs/v0.6.0-release-notes.md)
- [v0.6.0 Release Notes (English)](docs/v0.6.0-release-notes-en.md)
- [v0.5.3 Release Notes (Chinese)](docs/v0.5.3-release-notes.md)
- [v0.5.3 Release Notes (English)](docs/v0.5.3-release-notes-en.md)
- [Windows recovery and environment rebuild](docs/windows-recovery.md)
- [GitHub Releases](https://github.com/JZ-05T68/engineering-knowledge-base-src/releases)

`README.md` and `README_EN.md` are equivalent official project documents; positioning, capabilities, limitations and roadmap changes must be maintained in sync.
