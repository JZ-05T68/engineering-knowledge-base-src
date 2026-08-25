# Engineering Knowledge Base v0.5.3

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

## v0.5.3 — Audited AI Integration

v0.5.3 builds on the v0.5.2 Knowledge Foundation by adding auditable, citation-constrained, on-demand AI assistance over knowledge that the user explicitly selects or retrieves, together with an AI call ledger, structured exports and legacy backup upgrade.

Current capabilities:

- **ContextItem and KnowledgeContextPackage**: pages, knowledge objects, knowledge memories and evidence are projected into read-only ContextItems and packaged with citations, sources, lifecycle and exclusion information.
- **Two retrieval scopes**: page-material search and personal-knowledge search remain offline-first with unchanged default behavior.
- **Ask AI / RAG Answer**: the user asks on demand, and the AI may answer only from the selected KnowledgeContextPackage.
- **Runtime citation validation**: every citation in an AI answer must belong to the current context package; unknown, forged, out-of-range and empty citations fail closed without showing a half-finished answer.
- **Read-only AI experience organizer**: generates structured experience candidates on demand for preview only; nothing is written to knowledge assets automatically.
- **AI call ledger**: ai_calls records capability, source feature, status, tokens and target references; the ledger is read-only and never stores full prompts, context text or model answers.
- **Knowledge Export**: lossless structured export of knowledge objects, sources, relations, revisions and memories with a manifest, per-file SHA-256 and one Markdown file per object.
- **AI Ledger Export**: a standalone audit export of ai_calls metadata (JSON/JSONL authoritative), without content or credentials.
- **Schema-v8 legacy backup isolated upgrade**: legacy database snapshots are upgraded to the current schema through a dedicated entry point while the original backup stays unchanged.
- **Schema v12**: the current database structure hosting both knowledge assets and the AI ledger.
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

The v0.5.3 release gate is defined by `scripts/release_check.py` and the Phase 7 report, including at least:

- full pytest: **1762+ tests collected, exit 0**;
- Ruff: **PASS**;
- `git diff --check`: **clean**;
- schema v12 backup/restore field-by-field roundtrip;
- production database: `data/database/knowledge.db`;
- production database SHA-256: `59caf2cfc5e80d197ca02a64b702ea6d06b7c4eb66e02c7fefa272403a4c0ad9`;
- official acceptance: `127.0.0.1:8501`, health HTTP 200.

These numbers record the release-candidate baseline and do not imply equal quality guarantees for every engineering domain, corpus or query.

## Roadmap

| Version line | Theme | Status |
| --- | --- | --- |
| **v0.5.x** | Knowledge Foundation | v0.5.3 completes audited AI integration, ledger, exports and backup upgrade. |
| **v0.6.x** | Agent Foundation | Planned, not implemented. Agent autonomy and tool calling begin here only. |
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
- [v0.5.3 Release Notes (Chinese)](docs/v0.5.3-release-notes.md)
- [v0.5.3 Release Notes (English)](docs/v0.5.3-release-notes-en.md)
- [Windows recovery and environment rebuild](docs/windows-recovery.md)
- [GitHub Releases](https://github.com/JZ-05T68/engineering-knowledge-base-src/releases)

`README.md` and `README_EN.md` are equivalent official project documents; positioning, capabilities, limitations and roadmap changes must be maintained in sync.
