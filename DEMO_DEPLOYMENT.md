# Disposable demo deployment

The demo uses made-up reviews, a fake account, and a bounded in-memory contact vault. It takes no payment and makes no outbound network requests. State disappears on restart. The existing privacy approval requirements still apply before publishing a URL.

POST bodies are limited to 8,192 bytes before reading or parsing. The shared review validator checks field validity and length before running PII detection on each field. Invalid fields retain their validation errors. HEAD uses the GET status and representation headers without sending a response body, including on error routes.

## Request deadlines and logging

Each accepted handler has a ten-second total connection deadline, beginning before the request line is read. The demo uses HTTP/1.0 responses and closes the connection after each request. A timer shuts down the socket when the deadline expires, interrupting request-line reads, header reads, buffered body reads and blocked response writes. Receiving another byte does not extend the deadline. The existing ten-second socket inactivity timeout remains as an additional limit. The deadline timer is cancelled and joined when handling completes.

At most 20 handlers run concurrently, each with at most one deadline timer. Timed-out handlers release their concurrency slots through the server's existing finally block. This bounds occupancy from slow clients; it does not prevent a client from repeatedly opening new connections. Ingress connection limits remain useful for broader denial-of-service protection.

Both access logging and socketserver's default exception logging are disabled. The latter would otherwise print visitor IP addresses and tracebacks to stderr. Response header and body writes catch OSError and close the connection. Unexpected handler exceptions are also suppressed by the server's handle_error override so they cannot leak visitor information through the default error path.

Email PII screening scans non-whitespace, non-@ runs in linear time. It retains the previous permissive word-boundary heuristic without its backtracking over repeated dots. This is a personal-data screen, not an RFC email-address validator.

## Client addresses behind an ingress

No deployment manifests or ingress configuration were supplied with this change; real client-IP delivery has not been verified.

For a direct listener receiving real client addresses, leave both proxy environment variables unset. Forwarded headers are ignored in this mode.

For an ingress, configure both variables explicitly. For one trusted proxy that writes the actual client IP into X-Forwarded-For:

```sh
TRUSTED_PROXY_HEADER=X-Forwarded-For TRUSTED_PROXY_HOPS=1 python3 demo.py
```

The proxy must sanitize the configured header. Restrict network access to the demo listener so only the trusted ingress can reach it. Otherwise a direct caller could supply a forged trusted header. Configuration alone cannot establish that network trust boundary.

`TRUSTED_PROXY_HOPS` selects a fixed position counting from the right of the comma-separated address list, matching the WSGI application's convention. With two trusted proxies and a sanitized chain `client, edge`, use 2. Extra untrusted entries on the left cannot change the selected position. Use a fixed ingress path; do not guess the hop count or change it based on request contents.

The configured header must contain comma-separated IP literals, such as X-Forwarded-For. RFC Forwarded syntax is not supported. `HTTP_X_FORWARDED_FOR` is also accepted as a configuration name. Missing headers, duplicate header lines, insufficient chains, empty selected positions, and invalid selected IPs cause POST requests to return 400 before consuming a rate-limit bucket or processing the form. IPv6 addresses are canonicalized; IPv4-mapped IPv6 addresses share the IPv4 bucket.

Before exposing the demo, verify the ingress configuration overwrites or sanitizes visitor-supplied values and blocks direct access to the listener. Exercise the deployed route from two distinct client IPs: exhausting one client's allowance must leave the other able to submit. Changing an untrusted forwarded-header prefix must not reset the first client's allowance. Visitors behind the same actual NAT address still share an IP allowance.

## Regression checks

```sh
python -m unittest discover -s tests -v
python -m unittest tests.test_e2e_demo -v
```

Existing tests cover oversized review fields, validation before PII screening, sensitive-content rejection, field-length boundaries, body limits, proxy-client buckets, forged-prefix resistance, malformed proxy chains, configuration validation, IP normalization, HEAD parity, escaping, banners, bounded state and the browser workflow.

The public-security regressions cover silent server exception handling and slot release, OSError during response headers and body writes, trickling request lines and headers, trickling bodies on both POST routes, recovery of all 20 concurrency slots, successful requests after timeout, and timer cleanup. These tests use local sockets with shortened deadlines; production keeps its ten-second deadline. Email regressions compare short inputs against the previous heuristic, check Unicode and punctuation behavior, sweep the reported 1,000-character adversarial family under a one-second budget, and verify continued rejection in review fields.

The demo retains its existing unauthenticated workflow and CSRF behavior, as explicitly accepted in the review. The findings reporting no XSS, traversal, memory-bound, banner or core-flow defect require no behavior change.
