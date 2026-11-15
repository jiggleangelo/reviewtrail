# reviewtrail

Prototype: reviewtrail: hands-on reviews to choose an LLM API

**Use it free, no account:** https://app-reviewtrail.manykiln.com/  
**Try the demo (made-up data):** https://try-reviewtrail.manykiln.com/  
**Product page:** https://reviewtrail.manykiln.com/

> Built and maintained by AI agents at [Manykiln](https://manykiln.com), a studio whose products are written, tested and operated by AI agents. This is a working prototype, not a finished product. No payment is taken anywhere: any checkout, waitlist or payment code in this project runs against fakes only, and the hosted demo uses made-up data.

- Status: prototype; run the 102 automated tests yourself with `python3 -m unittest discover`
- Runs on the Python 3.12 standard library; run the usable app with `python3 app.py` (see below)
- Licence: MIT. Free to use, fork and improve.
- Issues and pull requests are not monitored yet.

---

# ReviewTrail MVP

ReviewTrail includes its historical public MVP and a separate private workbench for recording real coding-model and agent runs.

## Workbench

The workbench has no accounts, OAuth, cookies, payments, contact capture, analytics, third-party assets, or model calls. Its tables, charts, reports, and possible-shift signals use deterministic arithmetic over data entered into one token-addressed workspace.

Run it with Python 3.12 and the standard library:

```sh
mkdir -p ./reviewtrail-data
APP_HOST=127.0.0.1 \
PORT=8080 \
PUBLIC_BASE_URL=http://127.0.0.1:8080 \
APP_HTTPS_TERMINATED=false \
DATA_DIR=./reviewtrail-data \
APP_DATA_CAP_MB=200 \
python3 app.py
```

Open `http://127.0.0.1:8080`, then choose **Start a workspace** or **Try with sample data**. Bookmark the resulting `/w/<token>` URL: it is the only workspace key and cannot be recovered. Real workspaces are retained until explicitly deleted. Clearly labelled sample workspaces expire after 24 hours.

Optional read-only links can be created, rotated, and revoked. Workspace and share tokens necessarily appear in URL paths and browser history. A hosted operator must configure ingress and reverse-proxy access logs to redact `/w/<token>` and `/s/<token>` paths. The application itself does not write access logs. Use HTTPS and set `APP_HTTPS_TERMINATED=true` when TLS terminates at a trusted ingress; this enables HSTS.

`GET /healthz` always returns `ok`, including when a platform health probe supplies an internal Host header. Application links and CSRF origin checks use `PUBLIC_BASE_URL`, so it must be the exact externally visible origin.

The workbench accepts individual entries, UTF-8 CSV imports (including UTF-8 BOM files), and previous JSON exports. CSV headers are:

```text
model,version,category,task,stack,harness,outcome,retries,cost_usd,date
```

Imports are limited to 1 MB of file or pasted content and 2,000 rows per request. A workspace is limited to 5 MB, 5,000 runs, and 50 custom models. Unknown CSV model names become custom workspace models after the same sensitive-data screening used by the form. JSON restoration preserves exported custom models.

Exact duplicate import rows are skipped. Runs which repeat the same task with a different date, model, outcome, retry count, or cost are retained; this is required for longitudinal tracking. A shared content hash is reported only as a repeated-task warning.

Possible shift is a self-reported heuristic, not a benchmark or confirmed regression. It appears only when consecutive ISO weeks for the same version each contain at least five runs and mean outcome falls by at least 1.0. Small cells remain visible with a `low n` label but are never used for shift detection.

Export regularly. Deleting runs and revoking a share link remain available when the global storage cap has been reached, so users can recover space.

## Tests

```sh
python -m unittest discover -s tests -v
python -m unittest tests.test_e2e_demo -v
```

The regression suite covers retention, exact duplicate handling, repeated-task imports, JSON and CSV custom models, form-value preservation, BOM and encoding errors, malformed CSV rows, bounded continuation requests, storage-full deletion, proxy health checks, HSTS, exact CSRF origin matching, shared navigation, pagination, and bounded write-budget state.

## Seed files

`seed/founder_reviews.json` is an empty placeholder (a JSON empty list): it contains no reviews, real or invented. Nothing in this repository is a real user review.

## Historical MVP

The original modules and `demo.py` remain available for compatibility and existing tests. Their historical OAuth, publication, moderation, waitlist, vault, and maintenance experiments are not exposed by `app.py`.
