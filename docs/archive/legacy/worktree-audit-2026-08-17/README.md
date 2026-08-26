# EKB Historical Worktree Audit Archive

- Created: 2026-08-17 (Asia/Shanghai)
- Scope: local-only evidence preserved before historical Git worktree cleanup
- Git tracking: NO
- Remote upload: NO
- Integrity record: `manifest.json` contains original paths, archive paths, sizes, and SHA-256 hashes

## Archived buckets

- `engineering-kb-retained-evidence`: unique uncommitted documents and launch wrappers copied from the retained historical worktree. The original worktree remains intact.
- `v0.2.1-manual`: smoke-test input, generated three-page library, database, and logs.
- `v0.1.3-overnight-code`: unique overnight development report. The associated commit remains recoverable from the local branch.
- `v0.2.0-foundation`: modified historical README, release history, and the small v0.2.0 manual-test runtime.
- `v0.2.2-nested-wt-parent`: the two reviewed test files; equivalent normalized blobs are recoverable from commit `9fb183f5f98b5d8fedde16de9738060e71bf055b`.
- `v0.2.2-audit-evidence`: compact text/JSON/XML/script/log evidence from the v0.2.2 scale and fault-injection audits.

## Deliberately not archived

- Virtual environments and Python/tool caches.
- Reproducible generated scale-test PDFs, PNG page renders, and transient SQLite databases under `D:\ekb-v0.2.2` (about 1.02 GB total non-cache ignored output).
- The approximately 5.32 GB of historical backups, private/local PDFs, page images, and databases under `D:\Projects\engineering-kb`; that worktree was retained in place for human review.

No code or document from this archive was committed or pushed.
