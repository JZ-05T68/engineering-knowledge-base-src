[简体中文](README.md) | [English](README_EN.md)

# Engineering Knowledge Base v0.5.2

**Engineering Knowledge Base (EKB)**

A local-first personal knowledge system for accumulating, organizing, validating, and reusing long-term knowledge assets.

EKB is designed for individuals who need to preserve engineering experience over time. It organizes material the user
actively imports, experience the user records, and verifiable sources into local knowledge assets. It is not merely a
PDF summarizer, a RAG tool, or a general-purpose chatbot.

## Vision

General-purpose AI can generate answers, but it does not inherently preserve a person's long-term experience. It does
not automatically know why a project made a particular decision, how a failure was debugged, or why an unsuccessful
approach should not be repeated.

EKB focuses on preserving:

- project decisions and their context;
- debugging and troubleshooting paths;
- errors, failures, and rework lessons;
- learning records and conceptual understanding;
- reusable engineering practices.

Sources, revisions, fingerprints, evidence, and retrieval turn this material into traceable, verifiable, and reusable
personal knowledge assets:

```text
Material → Understanding → Knowledge Object → Source Validation
         → Knowledge Memory → Retrieval → Reuse → Engineering Capability
```

## Core Architecture

```text
Source ── Fingerprint ──> Knowledge Object ── Revision ──> Auditable evolution
  │                              │
  ├── Document / Page            ├── Relation
  ├── Note / Evidence            └── Knowledge Memory
  │                                      │
  └──────── Provenance ──────────────────┤
                                         v
                       Retrieval (Page / Object / Memory)
```

| Component | Role |
| --- | --- |
| **Knowledge Object** | Organizes concepts, facts, principles, experiences, problems, and decisions into manageable knowledge units. |
| **Source** | Connects a knowledge object to a local document, page, note, or evidence item. |
| **Revision** | Preserves knowledge-object changes as append-only history instead of silently overwriting their evolution. |
| **Fingerprint** | Captures a canonical SHA-256 source fingerprint to identify valid, changed, missing, or unknown source states. |
| **Evidence** | Retains the source location and human-confirmation state of a full page, text selection, or image region. |
| **Knowledge Memory** | Manually records problem solving, experience, and decisions while retaining links to knowledge objects or other local sources. |
| **Retrieval** | Provides local search and source trace-back across page material, knowledge objects, and knowledge memories. |

RAG and external AI are not EKB's primary identity. Optional AI is an explicitly enabled enhancement layer; it cannot
replace local sources of truth, human confirmation, or engineering judgment.

## v0.5.2 — Knowledge Foundation

v0.5.2 closes the first stage of the personal knowledge-asset foundation:

- **Knowledge Foundation**: knowledge objects, typed relations, knowledge memories, stable identifiers, lifecycle states, and append-only revisions.
- **Source Integrity**: knowledge-object sources link to local documents, pages, notes, or evidence and use Source Fingerprints to evaluate integrity at read time.
- **FTS v11 Retrieval**: schema v11 adds local SQLite FTS5 indexes, synchronization triggers, and safe rebuild paths for knowledge objects and knowledge memories.
- **Knowledge Object Search**: searches object titles, summaries, content, and tags while retaining status and source information.
- **Knowledge Memory Search**: searches personal problem-solving, experience, and decision records.
- **Provenance-aware retrieval**: page results, knowledge objects, and knowledge memories use explicit provenance anchors so users can return to local sources for verification.
- **Local-first operation**: core knowledge management and retrieval require no account, cloud service, VPN, or API key.

v0.5.2 does not implement a Personal Context Agent, automatic experience learning, background knowledge rewriting, or cloud synchronization.

## Personal Knowledge Workflow

```text
User actively imports or records
        ↓
Document / Page / Note / Evidence
        ↓
Knowledge Object + Source Fingerprint
        ↓
Human review, revision, and relationship organization
        ↓
Knowledge Memory
        ↓
Page / Object / Memory Retrieval
        ↓
Return to the source, verify, and reuse
```

The system can extract metadata, render pages, detect text layers, mark pages for review, and suggest organization
paths. It does not automatically overwrite originals, modify user notes, delete files, or turn unconfirmed inferences
into personal experience.

## Existing Core Capabilities

- Imports PDFs, detects duplicates with SHA-256, renders page PNGs, and extracts existing text layers.
- Runs explicit, local, single-page OCR for scanned pages while preserving the “not human-verified” boundary.
- Browses documents and pages and manages Markdown, structured notes, tags, and projects.
- Performs local full-text retrieval with SQLite FTS5, jieba, and field weighting.
- Creates page, text-selection, and image-region evidence and generates citation-grounded prompt packages after human confirmation.
- Manages knowledge objects, knowledge memories, sources, relations, revisions, and source integrity.
- Creates, validates, and restores complete local backups.
- Supports optional page-vector and hybrid retrieval when explicitly configured; offline core functions remain available when AI is unavailable.

## Local-First and Information Boundaries

- Local files and SQLite are the sole source of truth; user material stays on the local machine by default.
- The formal service binds to `127.0.0.1:8501` and is not exposed to a LAN or the public internet.
- The system processes only material the user actively imports or explicitly authorizes; it does not crawl private chats or unauthorized third-party material.
- Original PDFs and page images are never automatically overwritten or deleted.
- The default is `ai_mode="manual"`; the application and its core features work without an API key.
- There is no registration, login, account, password, OAuth, JWT, role, administrator, or multi-user permission subsystem.
- There is no cloud synchronization; the user controls backup locations and media.

## Release Validation

The v0.5.2 CLOSED baseline records:

- Full pytest: **1646 passed in 975.32s**;
- Retrieval benchmark: **45 passed**;
- Focused regression: **279 passed**;
- Ruff: **PASS**;
- `git diff --check`: **clean**;
- Production database: `data/database/knowledge.db`, 327680 bytes;
- Production database SHA-256: `6a3ab3542c6865007c1fab3c739228f97d2120b1527dbb6cdefa26834e8b9c91`;
- CLOSED runtime acceptance: `127.0.0.1:8501`, health HTTP 200.

These figures describe the frozen release baseline. They do not guarantee equal retrieval quality for every engineering
domain, corpus, or query.

## Roadmap

| Version line | Theme | Status |
| --- | --- | --- |
| **v0.5.x** | Knowledge Foundation | v0.5.2 completes the knowledge-object, source-integrity, revision, knowledge-memory, and local-retrieval foundation. |
| **v0.6.x** | Personal Context Agent | Future roadmap; not implemented. The goal is user-controlled organization of personal context. |
| **v0.7.x** | Experience Memory | Future roadmap; not implemented. The goal is stronger confirmation, evolution, and reuse of long-term experience. |
| **v1.0** | Personal Experience System | Long-term direction; not implemented. The goal is a complete personal experience system. |

See the [v0.5.x Roadmap](docs/v0.5.x-roadmap.md) for boundaries. The roadmap is not a statement of current capability
and does not promise release dates.

## Limitations

- EKB is currently a local, single-user Windows system with no multi-user collaboration or permission model.
- There is no cloud synchronization, cloud account, or hosted service; users manage cross-device backups themselves.
- The Personal Context Agent is not complete; EKB does not autonomously plan tasks or act continuously on a user's behalf.
- EKB does not automatically learn personal experience; users actively create, review, or confirm knowledge objects and memories.
- Local OCR is single-page and intended for printed text; it does not support handwriting, formula OCR, structured complex tables, or batch OCR.
- Keyword retrieval does not automatically expand synonyms. Optional hybrid retrieval does not imply comprehensive semantic understanding or replace source verification.
- Search display and batch operations have loading limits; deep-pagination performance for very large knowledge bases remains under observation.
- Complete backups are local directory structures, not encrypted archives; users are responsible for backup-media security.

## Installation

Requirements: Windows 10 or 11, PowerShell, Python 3.11, and a Python SQLite build with FTS5 support.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env` is optional local configuration and must not be committed. See
[Windows recovery and environment reconstruction](docs/windows-recovery.md) for a complete clean-machine workflow.
GitHub does not store the user's database, imported material, credentials, logs, or cache.

## Start and Stop

- Start: double-click `启动工程知识库.bat`.
- Silent start: double-click `静默启动工程知识库.vbs`.
- Inspect status: double-click `查看运行状态.bat`.
- Stop: double-click `停止工程知识库.bat`.

For foreground development:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

The formal endpoint is fixed at <http://127.0.0.1:8501>; the health endpoint is
<http://127.0.0.1:8501/_stcore/health>. Do not bind to `0.0.0.0` or expose EKB to an external network.

## Local Data and Safety

```text
data/
├── raw/            # Original PDFs; retained for compatibility with existing local data
├── pages/          # Page PNGs
├── markdown/       # Page Markdown
└── database/
    └── knowledge.db
```

`data/`, `backups/`, `logs/`, `runtime/`, and local configuration remain separate from application source and are
ignored by Git. Database upgrades use additive migrations, pre-migration backups, transactions, integrity checks, and
foreign-key checks. Document deletion requires an explicit impact preview and confirmation; original PDFs and page
images are never deleted automatically.

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

Release-commit and tag closure can reuse a verified backup in stopped-service mode. Every failed check must be handled
explicitly before publication.

## Documentation

- [CHANGELOG](CHANGELOG.md)
- [v0.5.x Roadmap](docs/v0.5.x-roadmap.md)
- [Windows recovery and environment reconstruction](docs/windows-recovery.md)
- [GitHub Releases](https://github.com/JZ-05T68/engineering-knowledge-base-src/releases)

`README.md` and `README_EN.md` are equivalent official project documents. Product positioning, capabilities,
limitations, and roadmap changes must be maintained in both languages.
