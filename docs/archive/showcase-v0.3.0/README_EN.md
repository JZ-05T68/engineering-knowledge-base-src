[English](README_EN.md) | [简体中文](README.md)

# Engineering Knowledge Base

Engineering Knowledge Base (EKB) is a local-first Windows application for turning personal engineering documents into knowledge that can be found, checked, and reused.

Technical work often depends on long PDFs: datasheets, manuals, standards, application notes, and project records. A useful detail may sit somewhere inside hundreds or thousands of pages. Ordinary file search can miss it, while an answer without a page-level source is difficult to trust.

EKB keeps the workflow grounded in the material you provide:

**Document → Understanding → Retrieval → Evidence → Reuse**

> This is the public product and demonstration repository. Source code, dependencies, startup scripts, and recovery instructions are maintained in [engineering-knowledge-base-src](https://github.com/JZ-05T68/engineering-knowledge-base-src). A standalone installer is not currently provided.

## A Practical Scenario

Suppose you need to confirm an electrical limit, interface requirement, or failure condition across a large collection of technical documentation. With EKB, you can:

1. Import the PDFs you already own or are authorized to use.
2. Browse rendered pages alongside extracted text and your own notes.
3. Search locally to narrow a large document collection to candidate pages.
4. Open each result at the original page and verify it against the page image.
5. Collect a whole page, text selection, or image region as evidence.
6. Confirm the evidence and generate a citation-grounded prompt package for manual reuse.

The goal is not to replace engineering judgment. It is to shorten the path from “I know this is documented somewhere” to a source you can inspect and cite.

## Product Principles

### Local first

Imported PDFs, rendered pages, notes, search data, and metadata remain local. Local files and SQLite are the source of truth. Core document management and keyword search work offline and do not require an account, API key, VPN, cloud storage, or external AI service.

### Evidence before answers

Search results lead back to the original document, filename, page number, and rendered page. Evidence remains distinct from user notes, must be confirmed by the user, and is checked for source validity before a prompt package is generated.

### User-controlled automation

EKB processes only documents that the user actively imports or explicitly authorizes. It does not crawl private messages, collect unauthorized third-party material, overwrite original documents, or silently send the knowledge base to an AI provider.

## Core Workflow

- Import PDFs with SHA-256 duplicate detection and preserve the originals.
- Render pages to PNG and extract available text layers.
- Flag uncertain pages for review and optionally run local, single-page OCR for printed text.
- Add page Markdown, structured notes, tags, and projects.
- Use offline keyword search; optionally enable page-level hybrid retrieval when it has been explicitly configured.
- Return from every candidate result to the source page.
- Build an evidence basket from pages, text selections, and image regions.
- Generate a citation-grounded package from confirmed evidence for manual use elsewhere.
- Back up, diagnose, and restore the local knowledge base under explicit user control.

## Product Tour

The screenshots below were captured from the actual v0.5.0 interface and represent the product foundation retained in v0.5.1. The new coverage and weak-evidence status text is not shown through invented replacement images. The demonstrations use isolated synthetic engineering material and contain no real user documents, private filenames, credentials, or production data.

### Home overview

![EKB home overview](assets/home-overview.jpg)

### Read pages and organize Markdown notes

![Two-column page reader](assets/page-reader.jpg)

### Find keywords and inspect why a page matched

![Keyword search results](assets/keyword-search-results.jpg)

### Explicitly gated hybrid retrieval

![Hybrid retrieval mode](assets/hybrid-retrieval-mode.jpg)

### Collect and confirm traceable evidence

![Evidence and source workflow](assets/evidence-source-workflow.jpg)

### Manage documents within clear deletion boundaries

![Document management](assets/document-management.jpg)

## Current Status

The current source release is **[v0.5.1 — Retrieval Stabilization](https://github.com/JZ-05T68/engineering-knowledge-base-src/releases/tag/v0.5.1)**.

v0.5.1 exposes local embedding-index coverage and adds a weak-evidence notice for hybrid results. These changes improve visibility and set a clearer product boundary; they do not make semantic retrieval universally reliable. Index completion remains explicit and manual, weak-evidence handling is only partially mitigated, and the final hybrid result-count boundary remains an open product decision.

The current product includes local PDF import, page reading and review, local OCR for printed text, notes and knowledge organization, SQLite FTS5 keyword retrieval, optional page-level hybrid retrieval, evidence collection, citation-grounded prompt packages, and local backup and recovery.

It is a single-user Windows application bound to `127.0.0.1`. It is not a cloud platform or a generic chatbot, and it does not currently provide chunk-level indexing, runtime reranking, automatic large-language-model Q&A, handwritten/formula/table OCR, or a standalone installer.

## Explore the Project

- [Source repository](https://github.com/JZ-05T68/engineering-knowledge-base-src)
- [v0.5.1 release](https://github.com/JZ-05T68/engineering-knowledge-base-src/releases/tag/v0.5.1)
- [Windows setup and startup instructions](https://github.com/JZ-05T68/engineering-knowledge-base-src/blob/main/README_EN.md#installation)
- [Windows recovery guide](https://github.com/JZ-05T68/engineering-knowledge-base-src/blob/main/docs/windows-recovery.md)
- [Previous public release summary: v0.5.0](docs/v0.5.0-release-summary.md)

`README.md` and `README_EN.md` are maintained together as official project documentation. Release, capability, positioning, and presentation changes should update both languages. Japanese and Korean documentation is deferred to v1.2; no placeholder files are provided in this release.
