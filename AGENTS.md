# Engineering Knowledge Base — Codex Instructions

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