# paper-mcp Field Test — Service Readiness Review

**Target:** `extraction-only` @ `f5de8a4`
**Date:** 2026-08-24
**Published as:** artifact `7ffcfd0f-3b78-41c3-867e-5d6e2090922f`

> paper-mcp holds up as a product, and breaks as a service.

The shipped Docker stack was driven end to end with a real MCP client and a real paper, then attacked on the paths a real deployment actually meets: a degraded IdP, an unmetered probe, an oversized upload, a caller that just keeps calling. Extraction is genuinely good. Eleven things stand between it and an internet-facing endpoint.

## Run conditions

| | |
|---|---|
| Stack under test | compose: paper-mcp + marker |
| Document | arXiv 1706.03762 · 15 pp · 2.2 MB |
| Extraction | 45 s · keyless · pdf-layer |
| Gates | 176 tests · ruff · mypy strict |

Everything below was measured against a running service, not a test client — the compose stack on its shipped images for the end-to-end run and container-filesystem checks, and separate short-lived instances for the OIDC, upload-cap and Marker-down cases so the real run was not disturbed.

---

## What already works

Verified on the shipped images. A real MCP client uploaded a real paper to the compose stack and got back usable data in 45 seconds — real markdown tables, LaTeX equations, and a figure index whose URLs download actual image bytes. Nothing below argues with that. Everything below is about what happens when the service is not being used politely.

| Property | Evidence |
|---|---|
| Extraction quality | 39,646 chars of markdown, 26 headings, 10 display-math markers, correct title derived |
| Figure index | 5 figures, 5/5 captioned, images download as real JPEG (67,305 B), zip 364,567 B |
| Progress reporting | Polling showed `extracting page 5/15` then `12/15` — a waiting caller can tell busy from hung |
| Job coalescing | 25 identical uploads produced exactly 1 job and 1 spool file. No duplicate GPU runs |
| Upload-cap layering | Cap + 2 KB got the tool's worded error; well over got a worded `413`, not the SDK's bare one |
| Path traversal | Six encodings — plain, url, double, backslash, absolute, unknown token — all `404` |
| Transport | Hostile `Host` header → `421`. `POST /mcp` answers 200 directly, no redirect |
| Dependency failure | Marker down: health says `down`, the job errors clearly, and a retry genuinely re-queues |
| Call metering | Burst hit `429` at call 56 with a `Retry-After` header. Extraction budget fires on distinct documents |

---

## Stops the service — 2 findings

Both are reachable by an unauthenticated caller, and both take down every endpoint at once — not just the one being abused.

### F-01 · Critical · Verifying a bearer token freezes the entire server

`_fetch_jwks` calls synchronous `httpx.get` from inside async middleware. While it waits on the IdP, the event loop cannot run anything — not another request, not `/health`, not an in-flight extraction's progress callback.

Run in `oidc` mode against a stand-in IdP whose JWKS endpoint takes 8 s, polling `/health` throughout. One single request with one bogus token:

| Measurement | Value |
|---|---|
| token request (2 blocking fetches) | 16.80 s |
| `/health` worst latency during it | 16,613 ms |
| `/health` baseline, same server idle | 248 ms |

Two failure shapes make it worse than a slow path.

**When the IdP is unreachable**, nothing is ever cached, so every request pays it — three consecutive requests took 10.50 s, 10.40 s, 10.39 s. A degraded IdP does not degrade authentication; it takes the whole service offline.

**And it is repeatable on demand.** An unknown `kid` triggers a refetch, rate-limited to once per 30 s — but the rate limit caps how often the freeze happens, not that it happens. No valid token is needed, only a well-formed JWT header:

| Request | Latency |
|---|---|
| kid-0 (cold) | 8.48 s |
| kid-1, kid-2 (inside the 30 s window) | 0.22 s · 0.22 s |
| kid-3 (after waiting 31 s) | 8.40 s |

**Where:** `src/paper_mcp/auth.py:94` — `httpx.get(_jwks_url(issuer), timeout=…)`

**Suggested fix:** Await an `httpx.AsyncClient` (or wrap the call in `asyncio.to_thread`), cache negative results so an unreachable IdP is paid for once per window rather than per request, and single-flight the fetch so concurrent misses share one round trip.

### F-02 · Critical · `/health` is unauthenticated, unmetered, and calls Marker on every hit

`_OPEN_PREFIXES` exempts `/health` from both auth and quota — correct for an orchestrator probe. But the handler calls `marker_client().healthy()`, which opens a fresh `httpx.AsyncClient` and a new TCP connection to Marker **per request**. That turns a free, unmetered endpoint into an amplifier.

300 concurrent requests, compared against a control on the same server that skips the upstream call entirely (a 404 on `/a/<bad-token>`, also middleware-exempt):

| Target | Wall | p50 | max |
|---|---|---|---|
| control — 404, no upstream call | 5.0 s | 1,216 ms | 4,681 ms |
| `/health` — Marker round trip each | 66.5 s | 43,619 ms | 66,094 ms |

A single unauthenticated caller pushes the readiness probe to a **43-second median**. If anything is configured to restart the container on a failing health check, this hands that trigger to anyone who can reach the port.

**Where:** `src/paper_mcp/server.py` — `health()`; `src/paper_mcp/pipelines/marker_client.py:178` — `healthy()`

> Note: the published report cited `src/paper_mcp/marker_client.py:178`. The line number is right; the file is under `pipelines/`.

**Suggested fix:** Hold one long-lived `AsyncClient` instead of building one per call, and cache the Marker probe for a few seconds so N concurrent health checks cost one round trip. Splitting liveness (does the process answer) from readiness (is Marker up) would also stop a Marker outage from reading as a paper-mcp outage.

---

## Wrong answers and open taps — 3 findings

### F-03 · High · A table's columns are truncated to the header row's width, so numbers land under the wrong heading

This is the README's first guarantee — "Tables stay tables… A table flattened to a blob of cell text invites an agent to read numbers against the wrong column" — failing on the project's own reference paper.

`html_table_to_markdown` takes `ncols` from the header row and `fit()` truncates every data row to it. Marker emits a two-level header for Table 2 of *Attention Is All You Need*: a 3-cell header row, a 4-cell sub-header, and 5-cell data rows. Columns 4 and 5 are discarded and the sub-header becomes a data row:

```
| Model | BLEU | Training Cost (FLOPs) |
| --- | --- | --- |
| EN-DE | EN-FR | EN-DE |          <- sub-header, now a data row
...
| Transformer (base model) | 27.3 | 38.1 |    <- 38.1 is the EN-FR BLEU score
| Transformer (big) | 28.4 | 41.8 |           real cost: 3.3 · 10^18
```

An agent reading that bundle will report the base model's training cost as `38.1`. The cost column — the paper's headline claim about training efficiency — is gone entirely. Marker got it right; the markdown renderer dropped it.

| Measurement | Value |
|---|---|
| cells kept, current (`ncols` = header width 3) | 36 of 55 |
| cells kept, `ncols` = max row width 5 | 55 of 55 |
| Table 3, either way (no regression) | 252 of 252 |

The service does detect this and warns in `extraction.warnings`, keeping Marker's HTML in `tables/table-001.html` — good instincts. But the markdown an agent actually reads is silently wrong, and **the workflow check passes it**: it asserts only that pipe-delimited lines and a separator row exist, never that cell counts survived.

**Where:** `src/paper_mcp/pipelines/html_to_markdown.py` — `ncols` / `fit()`; assertion at `scripts/paper_workflow_check.py`

**Suggested fix:** Set `ncols = max(len(r) for r in rows)` and pad the header rather than truncating the body — measured above as 55/55 with no change to a well-formed table. Then make the workflow check compare rendered cell count against Marker's, which is the number the existing warning already computes.

### F-04 · High · Re-uploading an in-flight document is never charged, but still costs a full decode

`charge_extraction()` is skipped whenever a job for those bytes already exists — deliberately, so polling callers are not billed repeatedly. But the expensive work happens **before** that decision and is not skipped: `decode_pdf` base64-decodes up to 100 MB and parses it with PyMuPDF, then `spooled.write_bytes(pdf)` writes it to disk — all on the event loop.

25 identical uploads of one uncached document, against an extraction budget of 20/hour:

| Measurement | Value |
|---|---|
| accepted | 25 of 25 |
| charged against the extraction budget | 0 |

So the only brake is the 60-calls/minute budget — and one call can cost far more than that budget assumes. Ten identical 86 MB uploads from one caller, while polling `/health`:

| Measurement | Value |
|---|---|
| cumulative event-loop stall | 11.0 s in a 28.4 s window (≈39%) |
| worst `/health` latency | 1,667 ms |
| probes over 1 s | 8 of 38 |

All ten returned `200`, well inside the call budget, none charged for GPU. The quota that meters the expensive resource never sees them.

**Where:** `src/paper_mcp/tools/extract.py` — `decode_pdf`, `charge_extraction`, `spooled.write_bytes`

**Suggested fix:** Move the decode, the PyMuPDF open and the spool write off the loop with `asyncio.to_thread`. Then charge something for a repeat upload — a cheap join is fine to leave free, but the decode is not cheap, so either meter it or check the cache and the in-flight key before decoding the body.

### F-05 · High · `GET /mcp` opens a stream that can never carry data, and never closes

The server runs `stateless_http=True`, where the SDK creates a fresh transport per request and — as its own comment says — "never opens a GET stream". Nothing rejects the GET, though. It answers `200 text/event-stream` and holds the connection open indefinitely on a stream that is structurally incapable of ever delivering a message.

```
HTTP/1.1 200 OK
content-type: text/event-stream
transfer-encoding: chunked
... then nothing, forever
```

A probe client sat on it past a 30 s read timeout and a 5-minute script budget before it was killed. Each held stream costs a socket, a server task and a quota token — and the call budget is a rate limit, not a concurrency limit, so nothing bounds how many accumulate. A browser pointed at the URL, or a client that opens the spec's SSE channel, hangs instead of failing.

**Where:** `src/paper_mcp/server.py` — `create_app()` mounts the MCP app; no method guard on `/mcp`

**Suggested fix:** Return `405` for `GET /mcp` in `AuthQuotaMiddleware` while stateless mode is on — `DELETE` already answers a clean `405`, so this just makes GET consistent with it.

---

## Grows without bound — 2 findings

### F-06 · Medium · Quota buckets are created and never evicted

`QuotaStore._buckets` gains one entry per `(subject_hash, resource)` pair and nothing removes them. `maintenance.sweep_once` reclaims artifacts and job records; quota is not mentioned in that module at all. In open mode the key is the client IP, so on a public endpoint the dict grows with every distinct caller for the life of the process — a slow leak on exactly the deployment shape the quota exists to protect.

**Where:** `src/paper_mcp/quota.py:69` — `self._buckets`; `src/paper_mcp/maintenance.py` — no quota sweep

**Suggested fix:** Drop buckets that have refilled to capacity during the existing sweep — a full bucket is indistinguishable from a fresh one, so eviction is free of behaviour change.

### F-07 · Medium · No cap on queue depth, and the spool sits on the container's writable layer

`JobStore.submit` accepts work without limit, and each queued job holds its upload on disk until it runs. With `PAPER_MCP_JOB_CONCURRENCY=1` and ~45 s per paper, a backlog that arrives faster than it drains grows unbounded in both queue length and disk.

Where that disk lives is the sharper half. `spool_dir()` derives from `artifact_root.parent`, so with the shipped `PAPER_MCP_ARTIFACT_ROOT=/app/artifacts` the spool is `/app/spool`. Inside the running container:

| Path | Device |
|---|---|
| `/app/artifacts` | `/dev/sde` — the named volume |
| `/app/spool` | `overlay` — the container's writable layer |

So a backlog of 100 MB uploads fills the Docker root disk rather than the volume that was sized for this data.

**Where:** `src/paper_mcp/jobs.py` — `submit()`; `src/paper_mcp/tools/extract.py` — `spool_dir()`; `docker-compose.yml` volumes

**Suggested fix:** Reject new work past a configurable queue depth with the existing `RateLimitedError` — a caller told "queue full, retry in Ns" is better served than one whose job sits behind two hundred others. And mount the spool on the artifacts volume, or make its location its own setting.

---

## Documentation and operator experience — 4 findings

### F-08 · Low · The documented development flow does not run

The README calls two on-device checks "load-bearing" and gives both commands. Neither works as written.

| Reference | Problem |
|---|---|
| `scripts/on_device_check.py` (README:271) | file does not exist |
| `paper_workflow_check.py 1706.03762` (README:272) | script takes a PDF path |
| "163 tests" (README:257) | vs actual 176 — the badge is right |

The arXiv-id argument looks like a leftover from before discovery was removed; the script reads `Path(sys.argv[1]).read_bytes()`.

**Where:** `README.md` lines 257, 271–272, 312

### F-09 · Low · The shipped compose file cannot turn authentication on

The README's deployment warning says to set the OIDC variables before exposing the service. `PAPER_MCP_ALLOWED_HOSTS` is plumbed through compose; the auth variables are not present at all — nor are quota, TTL, salt or public base URL:

```
absent from docker-compose.yml:
  PAPER_MCP_AUTH_MODE            PAPER_MCP_OIDC_ISSUER
  PAPER_MCP_OIDC_AUDIENCE        PAPER_MCP_SUBJECT_SALT
  PAPER_MCP_QUOTA_*              PAPER_MCP_ARTIFACT_TTL_HOURS
  PAPER_MCP_PUBLIC_BASE_URL      PAPER_MCP_ALLOWED_ORIGINS
```

Following the README with the shipped compose file leaves you on `AUTH_MODE=open`, and figure URLs pinned to the default `http://localhost:8000` regardless of where the service actually answers.

Separately, Marker's published port `127.0.0.1:8002` is hardcoded — it collided with another project and blocked `docker compose up` until overridden.

> Note: the published report reads as if the loopback bind were the problem. It is deliberate and well-argued in the compose comments (pillow CVE containment). Only the hardcoded **port number** is at issue.

**Where:** `docker-compose.yml` — `paper-mcp.environment`, `marker.ports`

### F-10 · Low · The workflow check reports failure for a passing run on a non-UTF-8 console

Every content assertion passed — markdown, headings, tables, equations, figures, image download — and then the script died printing the extraction warnings, because one names a table column containing `ϵ` (U+03F5):

```
[  ok  ] figure image downloads and is a real image
UnicodeEncodeError: 'cp950' codec can't encode character 'ϵ'
... exited with code 1
```

The title line also mojibakes its em dash. Since extraction output is arbitrary academic text, any paper with a Greek letter in a table header will do this on a Windows console in a CJK locale.

**Where:** `scripts/paper_workflow_check.py` — `record()`; same crash in `authenticated_client_check.py`-style output paths

**Suggested fix:** `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` at entry.

### F-11 · Low · Three inconsistencies in the error contract

1. **The extraction quota loses its `Retry-After`.** The call quota answers a proper `429` with the header; the extraction quota raises `RateLimitedError` inside the tool, which the SDK renders as a generic tool error. The `retry_after` the exception carries never reaches the wire — only the prose "retry in 125s" survives.

2. **Retry advice is wrong for the commonest outage.** With Marker down, the caller is told: *"a repeat failure is the document, not a transient fault."* Marker being unreachable is precisely a transient fault, and the advice tells an agent to give up on a perfectly good PDF.

3. **A `429` body can say "retry in 0s"** while its own `Retry-After` header says `1` — the message rounds 0.4 s down, inviting an immediate retry that will be refused.

**Where:** `src/paper_mcp/tools/extract.py` — `charge_extraction`, `UpstreamError` message; `src/paper_mcp/quota.py` — `QuotaExceededError`

---

## Teardown

The stack started for this review has been stopped and the working tree is clean; pre-existing containers on the machine were left untouched.
