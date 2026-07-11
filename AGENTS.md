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

The default AI mode is manual.

Do not require an API key for the application to start.

Provide a provider interface for future API integration, but do not enable or require any paid API in v0.0.1.

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