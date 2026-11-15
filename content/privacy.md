ReviewTrail privacy notice v1.1 — 2026-10-24

Launch gate: do not deploy publicly until the PDPA/DPO pack and this notice are approved and the responsible DPO contact is published. Production startup also requires PRIVACY_APPROVED=1 and DPO_CONTACT.

GitHub identifiers are stored only as keyed pseudonymous hashes. Account creation is coarsened to a month. Reviews display a pseudonym unless the reviewer explicitly opts in to show their GitHub handle. Handle visibility is account-wide. Publishing a later review with the checkbox off restores pseudonymous display.

During login, an encrypted handle claim is placed inside the signed, expiring session cookie. No pending handle is stored in the application database or an in-memory candidate registry. The claim expires with the session and is usable only while that session remains valid. Opting in copies the handle into the account's public display-name field. Account deletion removes the account, reviews and server-side sessions.

Optional proof lines are self-attested, not verified. Their raw text is discarded after validation; only a purpose-separated keyed HMAC-SHA-256 digest is stored. Free text is screened for common personal-data and secret patterns. Do not submit secrets or personal data in review text.

Contact emails are passed to the vault; application tables hold opaque tokens and keyed deduplication digests. The existing vault uses an authenticated stdlib HMAC-stream encryption construction with a separate vault key. This is not a standard, independently audited encryption library. The application has no contact retrieval API, but its running process holds the symmetric key: a fully compromised application process is outside this protection boundary. Filesystem permissions, operational role separation and DPO acceptance remain necessary. The separate offline DPO tool exports contacts. Retention removes expired contact records by rewriting the vault.

The default waitlist retention period is 365 days. The operator must schedule the retention command and handle authenticated contact-access and withdrawal requests through the approved DPO process. Request logs contain only the HTTP method, route template, status and duration; they exclude IP addresses, handles, queries, cookies and form bodies. Review and contact data are not sent to a model.
