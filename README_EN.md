[English](README_EN.md) | [简体中文](README.md)

# Engineering Knowledge Base

Engineering Knowledge Base (EKB) is a local-first, single-user knowledge management system for engineering documents on Windows. It turns PDFs that you choose to import into page-level assets that can be read, reviewed, searched, traced to their source, and reused:

**Document → Understanding → Retrieval → Evidence → Reuse → Engineering Capability**

EKB is not a generic AI chatbot, a cloud document service, or an automatic PDF summarizer. Local files and SQLite remain the source of truth. Core document management and lexical search work offline, without an account, API key, VPN, or cloud dependency. Optional AI-assisted retrieval must be explicitly configured and invoked.

![EKB home overview](docs/assets/v0.5.0/home-overview.jpg)

> The existing screenshots were captured from the v0.5.0 interface using isolated, original demonstration material. They do not contain production, staging, customer, or private data. v0.5.1 retains this product foundation and adds retrieval-coverage and weak-evidence status text.

## Current Release

**v0.5.1 — Retrieval Stabilization** makes embedding coverage visible and adds an honest weak-evidence notice without changing the underlying retrieval ranking behavior.

- Embedding coverage is reported from locally persisted page embeddings. Index completion remains an explicit, manual operation.
- Hybrid results can warn when every returned item has lexical evidence only; this is not a numerical relevance guarantee.
- A frozen benchmark and repeatable evaluation workflow now cover lexical, vector, and hybrid behavior. Real-embedding calibration evidence is retained instead of being converted into an unsupported similarity threshold.
- Hybrid fallback states are explicit, so keyword fallback is distinguishable from a successful vector path.
- The final hybrid result count remains a deferred product decision and is not presented as bounded in this release.

The production path still unions lexical and vector candidates using equal-weight reciprocal rank fusion (RRF). v0.5.1 does not add a production `vector_weight`, a separate vector `top_k`, or a numerical similarity eligibility threshold.

Retrieval quality is still an open engineering problem. v0.5.1 partially mitigates known coverage and weak-evidence issues; it does not claim that semantic retrieval is complete or universally reliable. See the [v0.5.1 release](https://github.com/JZ-05T68/engineering-knowledge-base-src/releases/tag/v0.5.1) and the [final validation record](docs/v0.5.1-phase7-final-validation.md).

## Version History

The entries below preserve the scope and evidence level of each release. Early v0.0.x entries follow the repository's completed milestones; tags begin at v0.1.0, so dates, test counts, and implementation details that cannot be verified from the repository are not added.

### v0.0.1 — Initial Local Knowledge Base MVP

Established the local PDF workflow: preserve imported originals, render page images, extract text layers, identify pages needing review, attach page-level Markdown, browse documents, search locally, generate traceable evidence packages, and persist metadata in SQLite.

### v0.0.2 — Page-Level Knowledge Management and Background Operation

Established page-level organization and the Windows background start, stop, status, PID-validation, health-check, and rotating-log workflow.

### v0.0.3 — Continuous Review Workflow

Added a continuous queue for pending, draft, failed, reviewed, and skipped pages, with previous/next navigation, save-and-continue actions, unsaved-change protection, and optional keyboard shortcuts.

### v0.0.4 — Traceable Page Retrieval and Evidence Packages

Made page search traceable to the document, original filename, page number, local source paths, review state, and matched context. Evidence-package generation kept source material separate from user notes and warned on unreviewed pages.

### v0.0.5 — Multi-Page Evidence Collection

Introduced the persistent evidence basket for collecting selections from multiple pages, editing notes, ordering or removing evidence, returning to the source, and exporting single- or multi-document Markdown packages. Source hashes and validation prevent stale material from being exported as current evidence.

### v0.0.6 — Search Filters and State Restoration

Expanded local search with document, project, tag, review-state, matched-field, note, and evidence-basket filters; AND semantics for multiple projects or tags; relevance and recency sorts; facet counts; literal Unicode option search; and allowlisted URL state restoration.

### v0.0.7 — Search Explainability and Continuous Reading

Added page and document-grouped result views, source-labeled excerpts and literal match counts, on-demand page previews, full-result and within-document navigation, and restoration of search, filter, sort, panel, preview, and result-focus state without storing complete result ID lists in the URL.

### v0.0.8 — Full Backup, Diagnostics, and Release Closure

Completed verified local backups, read-only diagnostics, redacted diagnostic reports, safe restore preflight, and unified release checks, then closed directly into v0.1.0. No v0.0.9 release was created or planned.

### v0.1.0 — First Full Manual Acceptance and Formal Release

Closed the first formal release after full manual acceptance. It included manifest-based complete backups with SQLite online snapshots and SHA-256 records, stop-before-restore with pre-restore backup and rollback, read-only integrity diagnostics, redacted reports, usable empty states, and a unified release-check entry point. See the [v0.1.0 manual test record](docs/v0.1.0-manual-test-results.md).

### v0.1.1 — Stability and Usability Patch

Fixed the production endpoint at `127.0.0.1:8501`, improved Windows diagnostics, added an explicit route from import results to pending review, and preserved distinct selections on one page while rejecting a duplicate normalized selection. Schema v4 remained unchanged and no AI, OCR, embedding, semantic-search, or network API capability was added.

### v0.1.2 — Batch Organization

Added bounded batch updates for visible search results and the current review batch, with preflight, explicit confirmation, one-time action tokens, stable selection scope, additive tag/project changes, and a single SQLite transaction. Cross-page or “select all matches” operations were intentionally excluded. See the [v0.1.2 release notes](docs/v0.1.2-release-notes.md).

### v0.2.0 — Long and Non-Standard Document Foundation

Introduced isolated single-page processing, deterministic blank/short/landscape/rotated diagnostics, page-level failure isolation, document diagnostic summaries, a 120-page automated baseline, and a 300-page mixed-PDF acceptance run. Schema v4 remained unchanged; this release did not add OCR, embeddings, semantic search, or an external model API. See the [v0.2.0 release notes](docs/v0.2.0-release-notes.md).

### v0.2.1 — Default Evidence Basket Concurrency Patch

Moved “find or create the default evidence basket” into one short transaction so concurrent first access returns one basket instead of creating duplicates. Schema v4 and product scope were unchanged. See the [v0.2.1 release notes](docs/v0.2.1-release-notes.md).

### v0.2.2 — Local OCR

Added explicit, offline, single-page OCR for printed text with RapidOCR and ONNX Runtime. OCR text is stored separately, displayed as an unverified draft, included in local search with a warning, and never overwrites the PDF, rendered page, text layer, manual Markdown, or review state. Handwriting, formulas, structured tables, rotation correction, and batch OCR remain outside scope. See the [v0.2.2 release notes](docs/v0.2.2-release-notes.md).

### v0.2.3 — v0.2.x Closure

Closed the long-document foundation with atomic PDF and page-image writes plus recovery after interrupted imports, stable navigation through large result sets, zero-increment duplicate imports across database and file assets, and diagnosable failed records when duplicate probing fails. The automated suite recorded 553 passing tests. See the [v0.2.3 release notes](docs/v0.2.3-release-notes.md).

### v0.2.4 — Release and Deployment Consistency Patch

Aligned release checks, backup and restore tools, the displayed application version, and the formal local deployment entry point. It added no business capability, schema change, or AI feature. See the [v0.2.4 release notes](docs/v0.2.4-release-notes.md).

### v0.3.0 — Structured Notes Foundation

Added create, read, update, and delete workflows for document, page, text-selection, and image-region notes. Text selections preserve separate source snapshots, user excerpts, and personal notes; image regions bind coordinates to the original PNG and SHA-256. Schema v5 migrated incrementally with a pre-migration backup, and document deletion gained impact preview, exact-title confirmation, quarantine, and two-phase cleanup.

### v0.3.1 — Note Importance

Added high, medium, and normal importance to all four structured-note types, including filtering and customizable badge backgrounds. Schema v6 was an incremental, pre-backed-up migration; existing notes defaulted to normal importance.

### v0.3.2 — Cross-Document Aggregation and Deletion Lifecycle

Added a read-only, paginated aggregation view organized by project or tag, with importance and note-type filters and links back to sources or evidence. Document deletion gained impact reporting, per-operation quarantine manifests, restart reconciliation, conservative preservation of unknown states, and explicit high-risk confirmation for evidence removal. See the [v0.3.2 release notes](docs/v0.3.2-release-notes.md).

### v0.3.3 — Document Management and Data Safety

Introduced a dedicated document-management page and centralized the deletion confirmation flow. A service-layer exact-title check prevents side effects on mismatch, while the v0.3.2 quarantine and recovery design remains in force. Schema v6 was unchanged. See the [v0.3.3 release notes](docs/v0.3.3-release-notes.md).

### v0.4.0 — Evidence Objects and Source Model

Unified whole-page, text-selection, and image-region evidence under common source location, validation, and human-confirmation semantics. Durable anchors use source-text hashes or original-PNG hashes, dimensions, and coordinates. Schema v7 migrated existing evidence to unconfirmed text selections. See the [v0.4.0 release notes](docs/v0.4.0-release-notes.md).

### v0.4.1 — Citation-Grounded Prompt Packages

Added prompt-package generation from confirmed evidence only, with source validation before generation and fail-closed behavior when any source is invalid. Text selections include selected source text, pages include clearly labeled current page text, and image regions include location and coordinates without inventing image contents. User notes remain separate from source facts. See the [v0.4.1 release notes](docs/v0.4.1-release-notes.md).

### v0.4.2 — Prompt Freshness and Stale-Output Protection

Bound generated prompt packages to the current question and confirmed evidence inputs. Changes to evidence, confirmation, ordering, notes, page text, or source validity invalidate and clear the old package; unrelated tag, project, review-state, or unconfirmed-evidence changes do not. Schema v7 and dependencies were unchanged. See the [v0.4.2 release notes](docs/v0.4.2-release-notes.md).

### v0.4.3 — Real-Problem Validation and AI Readiness Gate

Validated real engineering material and questions across source authenticity, human confirmation, cross-document separation, image-region limits, stale-prompt invalidation, and original-page traceability. The conditional go decision allowed later AI integration work; it did not claim that AI integration already existed or was validated. See the [v0.4.3 release notes](docs/v0.4.3-release-notes.md) and [decision record](docs/v0.4.3-decision-record.md).

### v0.5.0 — AI Foundation and Optional Hybrid Retrieval

Added an optional provider foundation, controlled real calls, page-level embedding persistence and freshness, explicit indexing orchestration, persistent vector recall, and optional hybrid retrieval that unions lexical and vector candidates through RRF. Production and staging data, ports, logs, backups, and runtime state remain isolated; manual AI mode and offline fallback remain the defaults. See the [v0.5.0 release notes](docs/v0.5.0-release-notes.md).

### v0.5.1 — Retrieval Stabilization

Established a frozen evaluation workflow, made hybrid fallback states explicit, calibrated assumptions with real embeddings, exposed read-only embedding coverage, and added an honest weak-evidence notice. Production still uses equal-weight RRF; index completion remains manual, no numeric similarity eligibility threshold was introduced, and the final displayed-result boundary remains undefined. The release recorded 1,475 passing tests, a successful production rollout, and zero rollout regressions. See the [v0.5.1 release](https://github.com/JZ-05T68/engineering-knowledge-base-src/releases/tag/v0.5.1) and [final validation record](docs/v0.5.1-phase7-final-validation.md).

## What EKB Solves

Engineering knowledge is often buried in long datasheets, manuals, standards, and project notes. Finding a promising page is only part of the job: the result must still be checked against the original document and retained with enough context to be useful later.

EKB provides a local workflow for that loop:

1. Import a PDF while preserving the original file.
2. Render every page to PNG and extract an existing text layer.
3. Flag scanned, handwritten, or otherwise uncertain pages for review; run local, single-page OCR when appropriate.
4. Add page Markdown, structured notes, tags, and projects.
5. Search locally with SQLite FTS5 and trace each result back to its document, filename, page number, and source image.
6. Collect whole pages, text selections, or image regions as evidence and confirm them manually.
7. Generate a citation-grounded prompt package for manual use with an external AI tool.

The prompt-package step does not send documents to an external service. Source validity is checked again before generation, and invalid evidence fails closed.

## Current Capabilities

- SHA-256 duplicate detection, page-by-page PNG rendering, text-layer extraction, and isolated page-failure handling.
- Local single-page OCR for printed text; OCR output is treated as an unverified draft and does not overwrite the source PDF, page image, or manual notes.
- Page reading and review, Markdown editing, structured notes, tags, projects, document management, and cross-document aggregation.
- Offline lexical retrieval with SQLite FTS5, jieba tokenization, filters, sorting, highlighting, and navigation back to the source page.
- Optional page-level embedding persistence and hybrid lexical/vector retrieval with explicit mode selection, local freshness checks, provenance labels, and keyword fallback.
- Evidence objects for pages, text selections, and image regions, with confirmed/unconfirmed boundaries and source-integrity validation.
- Local backup, restore preflight, diagnostics, and production/staging data isolation.

EKB does not perform automatic large-language-model question answering, web search, unattended indexing of private material, handwritten/formula/table OCR, or cloud synchronization. Runtime reranking and chunk-level indexing are not current capabilities.

## Optional AI and Retrieval

AI is an optional enhancement layer. The default is `ai_mode="manual"`; without an API key, PDF import, reading, lexical search, notes, evidence, document management, backup, and restore continue to work locally.

Hybrid mode is available only with no filters and relevance sorting. It combines local lexical candidates with fresh page-level vectors from SQLite and ranks their union with equal-weight RRF. Filters, non-relevance sorting, or an unavailable AI path fall back explicitly to keyword search. A deliberate search triggers at most one query embedding; URL restoration does not trigger a paid call.

Page embeddings are stored in `page_embeddings` and reused only when `source_text_sha256`, model, dimensions, and configuration version remain current. Production does not create embeddings automatically. Coverage visibility is read-only and does not complete the index. v0.5.1 has no production `vector_weight`, separate vector `top_k`, numerical similarity eligibility threshold, runtime reranking, or final displayed-result bound.

## Evidence and Traceability

The core retrieval path is designed around verification:

```text
PDF → page → retrieval result → source check → evidence basket
    → human confirmation → citation-grounded prompt package
```

Every reusable item retains its source context. Original PDFs and rendered page images are never automatically overwritten or deleted. EKB processes only material that the user actively imports or explicitly authorizes.

## Runtime and Data Isolation

| Instance | Endpoint | Data directory | Purpose |
|---|---|---|---|
| Production | `127.0.0.1:8501` | `data/` | Formal local knowledge assets |
| Isolated staging | `127.0.0.1:8502` | `staging-data/` | Controlled AI validation |

The two instances use separate databases, originals, rendered pages, Markdown, logs, backups, and PID paths, and both bind only to the loopback interface. Real paid operations remain subject to staging guards, manual defaults, and explicit user action.

## Technology Stack

- Python 3.11 and Streamlit
- SQLite with FTS5
- PyMuPDF and Pillow
- RapidOCR and ONNX Runtime for local OCR
- pydantic-settings and python-dotenv
- jieba and rapidfuzz
- pytest and Ruff

The application binds to `127.0.0.1:8501` by default. It has no registration, login, user-account, role, OAuth, JWT, or cloud-sync subsystem.

## Installation

Requirements:

- Windows 10 or 11 with PowerShell
- Python 3.11
- Python's SQLite build with FTS5 support

From the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Optionally create a local configuration file from the provided example:

```powershell
Copy-Item .env.example .env
```

For a complete clean-machine procedure, see [Windows recovery and environment reconstruction](docs/windows-recovery.md). GitHub restores the software and configuration instructions, not your `.env`, database, imported documents, logs, or cache.

## Start and Stop

For normal use, double-click `启动工程知识库.bat`. It validates the virtual environment, process state, health endpoint, and port, then starts EKB in the background and opens <http://127.0.0.1:8501>.

Use `停止工程知识库.bat` to stop the validated EKB process and `查看运行状态.bat` to inspect its state. For foreground development:

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Do not change the production bind address to `0.0.0.0` or expose the service to a LAN or the public internet.

## Operations and Data Safety

Optional Windows sign-in startup is disabled by default. The included enable and disable scripts use a current-user scheduled task where permitted and fall back to the current user's Startup folder.

The System Maintenance page creates and verifies complete local backups outside `data/`. Restore requires preflight validation and a stopped production service; the standalone restore script creates a pre-restore backup, rebuilds in a temporary directory, validates before switching, and rolls back if post-switch checks fail. Diagnostics are read-only and downloadable reports are redacted by default. Original PDFs and page images are never deleted automatically; document deletion requires explicit impact review and confirmation.

## Repository Layout

| Path | Purpose |
|---|---|
| `app.py` | Streamlit entry point and home dashboard |
| `pages/` | Import, reader, retrieval, review, organization, maintenance, and evidence UI |
| `src/` | Application services, data access, migrations, retrieval, evidence, backup, and diagnostics |
| `scripts/` | Service management, restore, release checks, and controlled evaluation utilities |
| `tests/` | Automated test suite |
| `benchmarks/` | Frozen retrieval evaluation fixtures and supporting material |
| `docs/` | Release records, validation evidence, recovery instructions, and engineering decisions |
| `data/` | Local production originals, rendered pages, Markdown, and SQLite data; ignored by Git |
| `staging-data/` | Isolated staging data used for controlled validation; ignored by Git |

User data, credentials, logs, runtime files, and backups are kept outside version control. Production uses `data/` on port 8501; controlled staging uses `staging-data/` on port 8502.

## Current Limitations

- Local OCR is single-page and intended for printed text; it does not handle handwriting, formula OCR, structured tables, or batch OCR.
- Lexical search does not automatically expand synonyms. Scanned pages require local OCR or manual notes before their content can be searched.
- Optional hybrid retrieval does not claim comprehensive semantic understanding and does not replace keyword search or source verification.
- Search result cards load at most 100 matches at a time; batch actions are limited to the current visible page or review batch.
- Streamlit restores result focus and state but does not provide reliable pixel-level scroll restoration.
- The UI operates one default evidence basket even though schema v7 supports multiple baskets.
- Complete backups are local directory structures, not encrypted archives or cloud synchronization; the user controls backup-media access.
- Formal restore requires stopping the service. Production remains fixed to `127.0.0.1:8501`.

## Development Checks

```powershell
python -m ruff check .
python -m pytest
```

README-only changes do not require rerunning the complete product test suite. The v0.5.1 release baseline recorded 1,475 passing tests, a successful production rollout on `127.0.0.1:8501`, and zero rollout regressions. Release-specific evidence and remaining limitations are recorded in the linked release and validation documents.

## Documentation and Links

- [Releases](https://github.com/JZ-05T68/engineering-knowledge-base-src/releases)
- [Changelog](CHANGELOG.md)
- [Windows recovery](docs/windows-recovery.md)
- [Repository maintenance rules](docs/repository-maintenance.md)
- [Public product showcase](https://github.com/JZ-05T68/engineering-knowledge-base)

`README.md` and `README_EN.md` are maintained together as official project documentation. Release, capability, positioning, and presentation changes should update both languages. Japanese and Korean documentation is deferred to v1.2; no placeholder files are provided in this release.
