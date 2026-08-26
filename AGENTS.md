# Engineering Knowledge Base — Codex Instructions

## Product Philosophy（最高优先级）

The principles in this section have the highest priority and govern all product, architecture and implementation decisions.

### Local First（本地优先）

- The user's engineering knowledge assets belong to the user.
- User materials must be stored locally by default.
- Local storage is the single source of truth.
- Cloud services must never become required dependencies.
- External AI services may only be offered as optional enhancements.
- Core knowledge management functions must support offline operation.

This project is not a data collection platform. It exists to help users manage, understand and reuse their own engineering knowledge assets.

### Respect Information Boundaries（尊重信息资源边界）

The system may process only information that the user actively provides or explicitly authorizes.

Allowed information includes:

- the user's own PDFs;
- the user's own notes;
- the user's own project files;
- materials that the user actively selects for import.

The project must never:

- automatically crawl private information;
- scan private chat records;
- collect unauthorized third-party materials;
- design features that bypass information ownership or authorization boundaries.

AI should enhance the user's existing knowledge assets, not obtain information through unauthorized means.

### Personal Engineering Knowledge Asset（个人工程知识资产）

This project is not:

- a PDF summarization tool;
- an AI chatbot wrapper;
- a cloud document platform;
- an information collection platform.

This project is a personal engineering knowledge management system.

Its long-term goal is:

**Document → Understanding → Retrieval → Reuse → Engineering Capability**

## Product boundary

This repository is a local, single-user engineering knowledge base.

The project must never implement:

- user registration
- user login
- user accounts
- passwords
- email or SMS verification
- OAuth
- JWT
- user roles
- administrator accounts
- multi-user permissions
- cloud account synchronization

Do not create users tables, authentication modules, login pages or account-related placeholders.

The application must bind to 127.0.0.1 by default and must not be exposed to the local network.

## Automation Boundary（自动化边界）

Automation should reduce repetitive work without replacing user control.

Allowed automation includes:

- extracting document metadata;
- detecting text layers;
- rendering pages;
- classifying page status;
- suggesting tags.

The project must never automate:

- overwriting original user materials;
- modifying user notes without explicit confirmation;
- deleting files automatically;
- performing irreversible operations without user confirmation.

## Data Management（数据管理）

User data must be separated from application source code.

Rules:

- Never commit user materials to Git.
- Never automatically delete original PDFs.
- Never automatically delete page images.
- Keep application metadata separate from user files.

## Version v0.0.1

The initial version must support:

1. Importing PDF documents.
2. Rendering each PDF page to a PNG image.
3. Extracting text when a text layer exists.
4. Marking scanned or handwritten pages as pending review.
5. Importing manually prepared Markdown for individual pages.
6. Preserving document title, filename, page number and source image.
7. Browsing documents and pages.
8. Local full-text search.
9. Generating a citation-grounded prompt package for external AI tools.
10. Persisting metadata locally using SQLite.

## AI mode

The default AI mode is manual (`ai_mode = "manual"`). Since v0.5.0 an optional AI capability exists, but it is disabled by default.

Do not require an API key for the application to start. Without an API key, every existing offline feature (PDF import, search, reading, notes, evidence, deletion/recovery, backup) must keep working unchanged.

The official runtime AI provider is Qwen (Aliyun Bailian / DashScope). Business services must depend only on the vendor-neutral contracts in `src/ai/provider.py`; vendor-specific endpoints, payloads, response parsing and error mapping live in `src/ai/qwen_client.py` only.

API keys must never be hard-coded, committed to Git, written to logs, diagnostics or backups, or leaked into test snapshots. They are read via `EKB_AI_API_KEY` / `.env` into the `SecretStr` settings field.

The AI provider must never become a startup dependency of the existing PDF / search / reading / notes / evidence / data-safety paths.

No unbounded retries, no agent loops, and no automatic repeated paid calls because a semantic answer looks unsatisfying. Only bounded transport-level retries are allowed (by default at most 2 extra attempts, only for network errors, HTTP 429 and 5xx).

Development-tool models (the coding agent) and the EKB runtime model are fully decoupled; never conflate them.

## Technology

- Python 3.11
- Streamlit
- SQLite and SQLite FTS5
- PyMuPDF
- Pillow
- python-dotenv
- pydantic-settings
- jieba
- rapidfuzz
- pytest
- ruff

Do not add LangChain, LlamaIndex, React, Docker, Redis, PostgreSQL or cloud services in v0.0.1.

## Engineering rules

- Use type hints.
- Add useful docstrings.
- Keep modules small.
- Use pathlib instead of hard-coded path strings.
- Never store secrets in source code.
- Never delete original PDFs or page images automatically.
- Detect duplicate files using SHA-256.
- Use database migrations or safe initialization logic.
- Log errors clearly.
- Display useful Chinese error messages in the UI.
- Avoid silent exception handling.
- Keep Windows PowerShell compatibility.

## Validation

After modifying code:

1. Run ruff check.
2. Run pytest.
3. Report which files changed.
4. Report which commands were run.
5. Report any remaining known problems.

Never claim that a test passed unless it was actually executed.

## Product constraints

The MVP must not require:

- VPN access;
- external network access;
- cloud accounts.

**Core knowledge management functions must always work locally.**

AI features, if introduced later, must remain optional enhancements.