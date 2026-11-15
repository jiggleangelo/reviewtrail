# Workbench review fixes

The workbench sends `Referrer-Policy: same-origin`. Browser form submissions retain their same-origin Origin, while outbound privacy links receive no Referer. Exact Origin checks remain in place; opaque `null` origins are rejected.

Global fixed-hour reservations are persisted to `meta/budgets.json` before mutations: 30 workspace creations, 3,000 writes, and 20 MiB of positive storage growth per UTC hour. Empty and sample creation share the creation allowance. Restarting retains reservations. Failed operations may consume reservations. Backwards clock changes do not reset allowances. Corrupt counters fail startup instead of silently resetting protection. These budgets limit unauthenticated resource consumption; they do not establish user identity or guarantee availability against sustained abuse. Run one application process per DATA_DIR.

Sample creation consults an in-memory set rebuilt during startup and maintained during creation, deletion and retention sweeps. It does not load all workspace files per request.

Custom model screening permits digit sequences in model names, including dated releases, while retaining email and secret screening. Workbench task validation recognizes isolated 40-character hexadecimal SHA-1 spellings and preserves the original task and content hash. Explicit secret contexts and known secret prefixes still fail validation. This is a heuristic: arbitrary secrets can resemble commit hashes. The historical validator and demo behavior are unchanged.

JSON restoration records aliases for names that already resolve to a seed or custom model. It only changes a slug on an entry created during that restoration step. Conflicting or duplicate imported slugs return 400 without saving the workspace.

Request-line and header handling retain the normal ten-second deadline. After same-origin validation and admission, import requests receive a 120-second upload/response deadline. The five-second row-processing budget starts after upload and parsing. HTTP/1.0 still closes each connection. At most 20 handlers are accepted; at most two requests may parse bodies or load/render workspace data concurrently. Other data requests return 503 before allocation. Health checks remain available. Workspace files are checked against the existing 5 MiB bound before decoding.

Workspace locks use 256 permanent stripes. A lock is never evicted while holders or waiters reference it. Hash collisions serialize unrelated workspaces safely.

Storage accounting includes payload bytes and a 4 KiB reserve per file and directory entry. Tombstone creation and expiry use the same accounting operations as other mutations. This is an application quota with metadata reserves, not a measurement of physical filesystem allocation. Metadata writes and deletions remain possible when the application quota is full. Atomic writes also require transient filesystem space. Export and delete controls remain available at run capacity; import results no longer offer Continue when the workspace is full.

`tests/test_workbench_review_fixes.py` adds regressions for every reported issue, including a real Chromium form test. The browser test requires Playwright and its Chromium installation; it skips when Playwright is unavailable. The application still uses only the standard library and existing project modules.

No commands, tests, browser checks, or deployment were executed while preparing these files. Run the complete existing suite and the new regressions, including the browser test without a skip, before shipping. Only the explicitly labeled sample workspace contains fabricated data. No model calls or external service integrations were added.
