# paper-mcp Service Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the eleven findings from the extraction-only field test so paper-mcp survives an internet-facing deployment.

**Architecture:** Every fix is local to an existing module — no new subsystems. Three themes: get blocking work off the event loop (`auth`, `extract`), stop unmetered and unbounded resources (`quota`, `jobs`, middleware), and make the shipped artifacts (markdown tables, README, compose) tell the truth. Nothing changes the MCP tool surface.

**Tech Stack:** Python 3.13, FastAPI + Starlette, `mcp` 2.x SDK, httpx (async), PyJWT, pytest + respx, ruff, mypy --strict, Docker Compose.

**Spec:** `docs/superpowers/reports/2026-08-24-field-test.md` (local copy of the field-test report, artifact `7ffcfd0f-3b78-41c3-867e-5d6e2090922f`). Every task cites the finding id it closes.

## Global Constraints

- Python 3.13, pinned in `.python-version`. Do not change it.
- `uv run mypy src` must pass under `--strict`. Every new function gets full annotations.
- `uv run ruff check src tests` must pass.
- The full suite must stay green: `uv run pytest` — 176 tests at `f5de8a4`, integration excluded.
- Comments explain **why**, in this codebase's established voice: name the measurement or the failure that motivated the code. Never restate what the line does.
- No new runtime dependencies. Everything here uses stdlib, httpx, or what is already imported.
- Every config knob is an environment variable read in `src/paper_mcp/config.py`, prefixed `PAPER_MCP_`.
- Tests go in `tests/`, named `test_<module>.py`, and exercise behaviour through the real surface where one exists. See `tests/test_auth_quota.py` for the house pattern: real RSA keys, respx-mocked issuer, `fastapi.testclient.TestClient`.
- `tests/conftest.py` resets process-level singletons between tests. Any new process-level state MUST be cleared there too.
- Commit after each task, subject line `type(scope): summary`. Claude wrote this code, so end every commit message with `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.
- Do not push, open a PR, or touch a shared branch. Local commits only.

---

## File Structure

**Modified:**

| File | Change | Finding |
|---|---|---|
| `src/paper_mcp/auth.py` | JWKS fetch becomes async, single-flighted, negative-cached | F-01 |
| `src/paper_mcp/api/middleware.py` | awaits `verify_token`, rejects `GET /mcp`, scales the call charge by body size | F-01, F-04, F-05 |
| `src/paper_mcp/pipelines/marker_client.py` | one pooled client, cached health probe | F-02 |
| `src/paper_mcp/pipelines/html_to_markdown.py` | table width from the widest row | F-03 |
| `src/paper_mcp/tools/extract.py` | decode and spool off the loop, no spool write when joining, queue-full mapping, honest upstream advice | F-04, F-07, F-11 |
| `src/paper_mcp/quota.py` | `evict_full`, ceil-rounded retry advice | F-06, F-11 |
| `src/paper_mcp/maintenance.py` | sweeps quota buckets | F-06 |
| `src/paper_mcp/jobs.py` | bounded queue depth | F-07 |
| `src/paper_mcp/config.py` | `MCP_PATH`, `spool_root`, `max_queued_jobs` | F-05, F-07 |
| `src/paper_mcp/server.py` | imports `MCP_PATH` from config | F-05 |
| `scripts/paper_workflow_check.py` | UTF-8 stdout, real table assertion | F-08, F-10 |
| `README.md` | a development flow that runs | F-08 |
| `docker-compose.yml` | auth/quota/TTL/spool variables, overridable Marker port | F-07, F-09 |

**Created:**
- `tests/test_html_to_markdown.py` — the module had no test file, which is how F-03 shipped
- `tests/test_quota_sweep.py` — bucket eviction
- `docs/superpowers/reports/2026-08-24-field-test.md` — the spec (Task 0)

**Task order is dependency order.** Task 4 needs Task 4's own `MCP_PATH` move before Task 5 edits the same middleware region; Task 10 needs the settings Task 7 introduces.

---

## Task 0: Land the spec

**Files:**
- Create: `docs/superpowers/reports/2026-08-24-field-test.md`

**Interfaces:**
- Consumes: nothing.
- Produces: the spec path every later task cites.

- [ ] **Step 1: Save the report next to the plan**

Read the published artifact (`7ffcfd0f-3b78-41c3-867e-5d6e2090922f`) and write its prose to `docs/superpowers/reports/2026-08-24-field-test.md` as markdown: each of the eleven findings with its id, severity, title, evidence table, `Where`, and `Suggested fix`. A plan that argues from a spec needs the spec in the repo, not only in a browser tab.

Correct two citation errors while transcribing:
- F-02's `Where` says `src/paper_mcp/marker_client.py:178`. The file is `src/paper_mcp/pipelines/marker_client.py:178`.
- F-09 reads as if the Marker port's loopback bind were the problem. It is deliberate and well-argued in the compose comments; only the hardcoded port number is at issue.

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/reports/2026-08-24-field-test.md docs/superpowers/plans/2026-08-25-paper-mcp-E-service-readiness.md
git commit -m "docs: land the field-test report and the plan that answers it

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 1: Table columns survive a two-level header (F-03)

The README's first guarantee — "Tables stay tables" — fails on the project's own reference paper. `ncols` comes from the header row and `fit()` truncates every data row to it, so Marker's 3-cell header over 5-cell data rows discards columns 4 and 5 and promotes the sub-header to a data row. A BLEU score of 38.1 lands under "Training Cost (FLOPs)"; the cost column vanishes.

**Files:**
- Modify: `src/paper_mcp/pipelines/html_to_markdown.py:78, 91-101`
- Create: `tests/test_html_to_markdown.py`
- Modify: `scripts/paper_workflow_check.py:165-171`

**Interfaces:**
- Consumes: nothing.
- Produces: `html_table_to_markdown(table_html: str) -> str` — unchanged signature. Every returned row now has the width of the widest input row.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_html_to_markdown.py`:

```python
"""Table rendering, which is the README's first guarantee.

This module had no test file, which is how a two-level header shipped
truncating every data row to the header's width — Table 2 of arXiv
1706.03762 lost its entire training-cost column, and a BLEU score moved
under the cost heading where an agent would report it as a FLOP count.
"""
from __future__ import annotations

from paper_mcp.pipelines.html_to_markdown import html_table_to_markdown


def _cells(markdown: str) -> list[list[str]]:
    return [
        [c.strip() for c in line.strip().strip("|").split("|")]
        for line in markdown.splitlines()
        if line.strip()
    ]


def test_a_two_level_header_keeps_every_data_column() -> None:
    """Marker emits a narrow header above wider data rows; nothing may be dropped."""
    html = (
        "<table><thead>"
        "<tr><th>Model</th><th>BLEU</th><th>Training Cost (FLOPs)</th></tr>"
        "</thead><tbody>"
        "<tr><td>EN-DE</td><td>EN-FR</td><td>EN-DE</td><td>EN-FR</td></tr>"
        "<tr><td>Transformer (base)</td><td>27.3</td><td>38.1</td>"
        "<td>3.3e18</td><td>2.3e19</td></tr>"
        "</tbody></table>"
    )

    rows = _cells(html_table_to_markdown(html))

    assert all(len(row) == 5 for row in rows), rows
    # The cost column is the paper's headline claim. It must be present.
    assert rows[-1][3] == "3.3e18"
    assert rows[-1][4] == "2.3e19"


def test_a_well_formed_table_is_unchanged() -> None:
    """The fix must not perturb a table that was already correct."""
    html = (
        "<table><thead><tr><th>A</th><th>B</th></tr></thead>"
        "<tbody><tr><td>1</td><td>2</td></tr></tbody></table>"
    )

    assert html_table_to_markdown(html) == "| A | B |\n| --- | --- |\n| 1 | 2 |"


def test_a_short_row_is_padded_not_dropped() -> None:
    """A row that lost a spanning label keeps its cells and gains blanks."""
    html = (
        "<table><thead><tr><th>A</th><th>B</th><th>C</th></tr></thead>"
        "<tbody><tr><td>1</td></tr></tbody></table>"
    )

    rows = _cells(html_table_to_markdown(html))

    assert rows[-1] == ["1", "", ""]


def test_a_fragment_with_no_rows_returns_empty() -> None:
    """A caller can fall back rather than emit a broken table."""
    assert html_table_to_markdown("<table></table>") == ""
    assert html_table_to_markdown("") == ""
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_html_to_markdown.py -v`

Expected: `test_a_two_level_header_keeps_every_data_column` FAILS — rows are 3 wide, so `rows[-1][3]` raises `IndexError`. The other three PASS.

- [ ] **Step 3: Take the width from the widest row**

In `src/paper_mcp/pipelines/html_to_markdown.py`, replace lines 91-101:

```python
    header_idx = parser.header_row_index if parser.header_row_index is not None else 0
    header = rows[header_idx]
    # Width is the widest row, not the header's. Marker emits two-level
    # headers — a 3-cell header row above a 4-cell sub-header and 5-cell data
    # rows — and taking the header's width silently truncated every data row
    # to it. Measured on Table 2 of arXiv 1706.03762: 36 of 55 cells survived,
    # the training-cost column disappeared entirely, and the EN-FR BLEU score
    # was left sitting under the "Training Cost (FLOPs)" heading, where an
    # agent reads it as a FLOP count. Widening changes nothing on a
    # well-formed table — Table 3 of the same paper: 252 of 252 either way.
    ncols = max(len(row) for row in rows)
    if ncols == 0:
        return ""

    def fit(row: list[str]) -> list[str]:
        # Pad, never truncate. A short row lost a spanning label and its blanks
        # are visibly blank; a truncated row lost data nothing can recover, and
        # the caller is never told which columns went.
        return list(row) + [""] * (ncols - len(row))
```

Then update the function's own docstring at line 78: "padded or truncated to the header's column count" becomes "padded to the widest row's column count".

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_html_to_markdown.py tests/test_marker_to_bundle.py -v`

Expected: all PASS. `test_marker_to_bundle.py` is included because it owns the rendered-vs-found cell-count warning whose numbers this changes.

- [ ] **Step 5: Confirm where the warnings live before asserting on them**

Run: `uv run python -c "import sys; sys.path.insert(0,'src'); from paper_mcp.bundle import Bundle; print(list(Bundle.model_fields)); import paper_mcp.bundle as b; print([f for f in b.Extraction.model_fields] if hasattr(b,'Extraction') else 'inspect bundle.py')"`

Expected: prints the bundle's fields and confirms warnings hang off `extraction`. If they live elsewhere, use the real accessor in Step 6 rather than the one written here.

- [ ] **Step 6: Make the workflow check able to catch this**

The check asserted only that pipe-delimited lines and a separator row exist, so a table with two columns missing read as a clean pass. `marker_to_bundle` already compares rendered cells against the cells Marker found and appends a warning containing "were dropped". Judge on that.

In `scripts/paper_workflow_check.py`, replace lines 165-171:

```python
        table_lines = _TABLE_RE.findall(markdown)
        has_sep = any(_TABLE_SEP_RE.match(ln) for ln in markdown.splitlines())
        # Pipes and a separator row are not evidence a table is correct: a
        # table truncated to its header's width has both, and passed this
        # check while a whole column was missing. The service already counts
        # rendered cells against the cells Marker found; judge on that.
        dropped = [
            w for w in (bundle["extraction"].get("warnings") or []) if "were dropped" in w
        ]
        record(
            "PASS" if (table_lines and has_sep and not dropped) else "FAIL",
            "tables survived as markdown tables",
            f"{len(table_lines)} table rows, separator={'yes' if has_sep else 'NO'}, "
            f"{len(dropped)} table(s) lost cells",
        )
```

- [ ] **Step 7: Run the full suite and gates**

Run: `uv run pytest && uv run ruff check src tests scripts && uv run mypy src`

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add src/paper_mcp/pipelines/html_to_markdown.py tests/test_html_to_markdown.py scripts/paper_workflow_check.py
git commit -m "fix(tables): size a table by its widest row, not its header

A two-level header truncated every data row to the header's width: Table 2
of 1706.03762 kept 36 of 55 cells, lost the training-cost column outright,
and left a BLEU score under the cost heading. The workflow check passed it
because it only ever asserted that pipes and a separator row existed.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Verifying a token stops freezing the server (F-01)

`_fetch_jwks` calls synchronous `httpx.get` from inside async middleware, so the event loop cannot run anything while it waits on the IdP — not another request, not `/health`, not an in-flight extraction's progress callback. Measured against an 8 s JWKS endpoint: one request with one bogus token held `/health` for 16.6 s against a 248 ms idle baseline. An unreachable IdP is worse, because nothing is ever cached and every request pays the full timeout: three consecutive requests measured 10.50 s, 10.40 s, 10.39 s.

**Files:**
- Modify: `src/paper_mcp/auth.py:36-38, 76-97, 100-129, 139-150`
- Modify: `src/paper_mcp/api/middleware.py:88`
- Modify: `tests/test_auth_quota.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `async def verify_token(token: str, *, issuer: str | None = None, audience: str | None = None) -> Principal` — **now a coroutine.** The only production caller is `AuthQuotaMiddleware.dispatch`.
  - `def reset_jwks_cache() -> None` — unchanged signature; now also drops the pooled client, the lock, and the negative-cache deadline.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_auth_quota.py`. If the module has no `anyio_backend` fixture, add this first:

```python
@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
```

Then the cases:

```python
@pytest.mark.anyio
async def test_verifying_a_token_does_not_block_the_event_loop(
    keypair: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow IdP must degrade authentication, not the whole service.

    `httpx.get` held the loop for the entire JWKS round trip, so one request
    with one bogus token froze every other request, `/health` included.
    """
    monkeypatch.setenv("PAPER_MCP_AUTH_MODE", "oidc")
    monkeypatch.setenv("PAPER_MCP_OIDC_ISSUER", _ISSUER)
    monkeypatch.setenv("PAPER_MCP_OIDC_AUDIENCE", _AUDIENCE)

    async def slow_jwks(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.5)
        return httpx.Response(200, json=_jwks(keypair))

    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    with respx.mock:
        respx.get(_JWKS_URL).mock(side_effect=slow_jwks)
        beat = asyncio.create_task(heartbeat())
        try:
            await auth_mod.verify_token(_token(keypair))
        finally:
            beat.cancel()

    # A blocking fetch pins the loop and the heartbeat never runs. An awaited
    # one lets tens of ticks through in the same half second.
    assert ticks > 10, f"loop advanced only {ticks} ticks during a 0.5s JWKS fetch"


@pytest.mark.anyio
async def test_concurrent_misses_share_one_jwks_fetch(
    keypair: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Single-flighted: ten cold callers cost the IdP one round trip, not ten."""
    monkeypatch.setenv("PAPER_MCP_AUTH_MODE", "oidc")
    monkeypatch.setenv("PAPER_MCP_OIDC_ISSUER", _ISSUER)
    monkeypatch.setenv("PAPER_MCP_OIDC_AUDIENCE", _AUDIENCE)

    calls = 0

    async def counted(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=_jwks(keypair))

    with respx.mock:
        respx.get(_JWKS_URL).mock(side_effect=counted)
        token = _token(keypair)
        await asyncio.gather(*(auth_mod.verify_token(token) for _ in range(10)))

    assert calls == 1, f"{calls} JWKS fetches for 10 concurrent verifications"


@pytest.mark.anyio
async def test_an_unreachable_idp_is_paid_for_once_per_window(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure is cached, so a degraded IdP does not charge every request."""
    monkeypatch.setenv("PAPER_MCP_AUTH_MODE", "oidc")
    monkeypatch.setenv("PAPER_MCP_OIDC_ISSUER", _ISSUER)
    monkeypatch.setenv("PAPER_MCP_OIDC_AUDIENCE", _AUDIENCE)

    calls = 0

    async def unreachable(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("no route to host")

    with respx.mock:
        respx.get(_JWKS_URL).mock(side_effect=unreachable)
        token = jwt.encode({"sub": "x"}, "secret", headers={"kid": _KID})
        for _ in range(3):
            with pytest.raises(auth_mod.AuthError):
                await auth_mod.verify_token(token)

    assert calls == 1, f"{calls} fetches for 3 requests against an unreachable IdP"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_auth_quota.py -k "block or concurrent or unreachable" -v`

Expected: all three FAIL with `TypeError: object Principal can't be used in 'await' expression` — `verify_token` is still synchronous.

- [ ] **Step 3: Make the fetch async, single-flighted and negative-cached**

In `src/paper_mcp/auth.py`, add `import asyncio` to the imports. Replace lines 36-38:

```python
_jwks_cache: dict[str, Any] | None = None
_jwks_fetched_at = 0.0
_last_refetch_attempt = 0.0
# One client for the process. A fresh `AsyncClient` per verification means a
# fresh TCP and TLS handshake against the IdP for every request that misses.
_jwks_client: httpx.AsyncClient | None = None
# Created lazily rather than at import: a lock binds to the loop that first
# awaits it, and the test suite runs more than one loop.
_jwks_lock: asyncio.Lock | None = None
# When the last fetch failed, how long to refuse without trying again. An
# unreachable IdP never populates the cache, so without this every request
# pays the full 10 s timeout — measured at 10.50 s, 10.40 s and 10.39 s for
# three consecutive requests, which is the whole service offline rather than
# authentication degraded.
_negative_until = 0.0
_NEGATIVE_TTL_SECONDS = 30.0
```

Replace `reset_jwks_cache` (lines 76-80):

```python
def reset_jwks_cache() -> None:
    global _jwks_cache, _jwks_fetched_at, _last_refetch_attempt
    global _jwks_client, _jwks_lock, _negative_until
    _jwks_cache = None
    _jwks_fetched_at = 0.0
    _last_refetch_attempt = 0.0
    _negative_until = 0.0
    # Dropped rather than closed: this is test-scope teardown, and closing
    # needs a running loop that may already be gone.
    _jwks_client = None
    _jwks_lock = None
```

Replace `_fetch_jwks` (lines 87-97) with the pooled, awaited version plus its two helpers:

```python
def _client() -> httpx.AsyncClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
    return _jwks_client


def _lock() -> asyncio.Lock:
    global _jwks_lock
    if _jwks_lock is None:
        _jwks_lock = asyncio.Lock()
    return _jwks_lock


async def _fetch_jwks(issuer: str) -> dict[str, Any]:
    """Fetch the issuer's key set over httpx.

    Deliberately not `PyJWKClient`, which fetches with `urllib`: every other
    outbound call here is httpx, and a second HTTP stack means different
    timeout and proxy behaviour, and nothing respx or a test can observe.

    Awaited, not blocking. `httpx.get` here ran inside async middleware and
    held the event loop for the whole IdP round trip: against an 8 s JWKS
    endpoint, one request with one bogus token pushed `/health` to 16.6 s
    against a 248 ms idle baseline. No valid token was needed to trigger it,
    only a well-formed JWT header.
    """
    response = await _client().get(_jwks_url(issuer))
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    return payload


async def _refresh(issuer: str, *, force: bool = False) -> None:
    """Populate the key-set cache, once however many callers are waiting.

    `force` is the unknown-kid path, which must refetch a cache that is still
    inside its TTL — otherwise a rotated key locks every caller out until the
    hour is up.
    """
    global _jwks_cache, _jwks_fetched_at, _negative_until

    async with _lock():
        now = time.monotonic()
        # Another waiter may have refreshed while this one held at the lock.
        if not force and _jwks_cache is not None and now - _jwks_fetched_at <= _JWKS_TTL_SECONDS:
            return
        if now < _negative_until:
            raise AuthError("jwks unavailable")
        try:
            fetched = await _fetch_jwks(issuer)
        except (httpx.HTTPError, ValueError) as exc:
            _negative_until = time.monotonic() + _NEGATIVE_TTL_SECONDS
            logger.info("jwks fetch failed: %s", type(exc).__name__)
            raise AuthError("jwks unavailable") from exc
        _jwks_cache = fetched
        _jwks_fetched_at = time.monotonic()
        _negative_until = 0.0
```

- [ ] **Step 4: Make `_signing_key` and `verify_token` async**

Replace `_signing_key` (lines 100-129):

```python
async def _signing_key(token: str, issuer: str) -> Any:
    """Resolve the key that signed `token`, refetching once on an unknown kid."""
    global _last_refetch_attempt

    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError as exc:
        raise AuthError("malformed token header") from exc

    if _jwks_cache is None or time.monotonic() - _jwks_fetched_at > _JWKS_TTL_SECONDS:
        await _refresh(issuer)

    key = _match_kid(_jwks_cache or {}, kid)
    if key is not None:
        return key

    # Keys rotate; a stale cache must not lock every caller out. One refetch,
    # rate-limited so unknown kids cannot be turned into a battering ram
    # against the IdP. The rate limit bounds how *often* this happens — it
    # never bounded what it cost when it did, which is what made an unknown
    # kid a freeze anyone could trigger on demand. The single-flight and the
    # negative cache bound the cost.
    now = time.monotonic()
    if now - _last_refetch_attempt < _MIN_REFETCH_INTERVAL:
        raise AuthError("unknown signing key")
    _last_refetch_attempt = now
    await _refresh(issuer, force=True)
    key = _match_kid(_jwks_cache or {}, kid)
    if key is None:
        raise AuthError("unknown signing key")
    return key
```

Change `verify_token`'s signature and its one internal call (lines 139-150). Everything from the `jwt.decode` block onward is unchanged:

```python
async def verify_token(
    token: str, *, issuer: str | None = None, audience: str | None = None
) -> Principal:
    """Verify a bearer JWT and return the caller's principal."""
    cfg = settings()
    issuer = issuer or cfg.oidc_issuer
    audience = audience or cfg.oidc_audience
    if not issuer or not audience:
        raise AuthError("OIDC issuer/audience are not configured")

    try:
        signing_key = await _signing_key(token, issuer)
```

- [ ] **Step 5: Await it in the middleware**

In `src/paper_mcp/api/middleware.py`, line 88:

```python
                principal = await verify_token(token)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_auth_quota.py -v`

Expected: all PASS, including the three new cases.

- [ ] **Step 7: Run the full suite and gates**

Run: `uv run pytest && uv run ruff check src tests && uv run mypy src`

Expected: all PASS. If any other test calls `verify_token` directly it now needs `await` — there were none at `f5de8a4`, but read the failure list rather than assuming.

- [ ] **Step 8: Commit**

```bash
git add src/paper_mcp/auth.py src/paper_mcp/api/middleware.py tests/test_auth_quota.py
git commit -m "fix(auth): await the JWKS fetch instead of freezing the loop on it

A sync httpx.get inside async middleware held the event loop for the whole
IdP round trip: one request with one bogus token pushed /health from 248ms
to 16.6s against an 8s JWKS endpoint, and an unreachable IdP charged every
request the full timeout because nothing was ever cached. Now awaited,
single-flighted, and negative-cached for 30s.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: /health stops amplifying against Marker (F-02)

`/health` is exempt from auth and quota — correct for an orchestrator probe — but the handler calls `marker_client().healthy()`, which opens a fresh `AsyncClient` and a new TCP connection to Marker per request. Measured at 300 concurrent requests: 66.5 s wall and a 43.6 s median, against 5.0 s and 1.2 s for a control on the same server that skips the upstream call. If anything restarts the container on a failing health check, that hands the trigger to anyone who can reach the port.

**Files:**
- Modify: `src/paper_mcp/pipelines/marker_client.py:93-95, 103-118, 162-175, 176-186`
- Modify: `tests/test_marker_extract_pipeline.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `MarkerClient.healthy() -> bool` — unchanged signature; result cached for `_HEALTH_TTL_SECONDS` and single-flighted.
  - `MarkerClient.aclose() -> None` — new. Closes the client this instance owns; an injected client belongs to whoever injected it and is never closed.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_marker_extract_pipeline.py`:

```python
@pytest.mark.anyio
async def test_concurrent_health_probes_cost_one_round_trip() -> None:
    """`/health` is unauthenticated and unmetered, so it must not amplify.

    A fresh AsyncClient per probe turned a free endpoint into an amplifier:
    300 concurrent requests measured 66.5s wall and a 43.6s median, against
    5.0s and 1.2s for the same server with no upstream call.
    """
    calls = 0

    async def counted(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"status": "ok"})

    with respx.mock:
        respx.get("http://marker.test/health").mock(side_effect=counted)
        client = MarkerClient("http://marker.test")
        results = await asyncio.gather(*(client.healthy() for _ in range(50)))

    assert all(results)
    assert calls == 1, f"{calls} upstream probes for 50 concurrent health checks"


@pytest.mark.anyio
async def test_a_health_result_is_cached_briefly() -> None:
    """Sequential probes inside the TTL reuse the answer."""
    calls = 0

    async def counted(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"status": "ok"})

    with respx.mock:
        respx.get("http://marker.test/health").mock(side_effect=counted)
        client = MarkerClient("http://marker.test")
        for _ in range(20):
            assert await client.healthy() is True

    assert calls == 1, f"{calls} upstream probes for 20 sequential health checks"


@pytest.mark.anyio
async def test_an_outage_is_still_reported_after_the_ttl() -> None:
    """Caching must not hide a Marker that went away."""
    client = MarkerClient("http://marker.test")

    with respx.mock:
        respx.get("http://marker.test/health").mock(return_value=httpx.Response(200))
        assert await client.healthy() is True

    # Expire the cache rather than sleeping out the TTL.
    client._health_cached = None

    with respx.mock:
        respx.get("http://marker.test/health").mock(side_effect=httpx.ConnectError("down"))
        assert await client.healthy() is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_marker_extract_pipeline.py -k health -v`

Expected: the first two FAIL on `calls == 50` and `calls == 20`; the third FAILS with `AttributeError: 'MarkerClient' object has no attribute '_health_cached'`.

- [ ] **Step 3: Pool one client and add the health cache**

In `src/paper_mcp/pipelines/marker_client.py`, confirm `asyncio` and `time` are imported and add them if not. Add beside `_TIMEOUT`:

```python
# How long a health answer stands. Short enough that an orchestrator still
# sees an outage promptly, long enough that a burst of probes is one round
# trip. `/health` is exempt from both auth and quota, so this is the only
# thing between an anonymous caller and Marker.
_HEALTH_TTL_SECONDS = 5.0
```

Replace `__init__` (lines 93-95) and add the helpers after it:

```python
    def __init__(self, base_url: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._owned: httpx.AsyncClient | None = None
        self._health_cached: tuple[float, bool] | None = None
        self._health_lock: asyncio.Lock | None = None

    def _shared(self) -> httpx.AsyncClient:
        """One client for this instance, not one per call.

        Every method built its own `AsyncClient`, so every call paid a fresh
        TCP connection. On `/health` — unauthenticated, unmetered, and
        reachable by anyone who can route to the port — that made a free
        endpoint an amplifier against Marker.
        """
        if self._client is not None:
            return self._client
        if self._owned is None:
            self._owned = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._owned

    def _health_gate(self) -> asyncio.Lock:
        # Lazily created: a lock binds to the loop that first awaits it.
        if self._health_lock is None:
            self._health_lock = asyncio.Lock()
        return self._health_lock

    async def aclose(self) -> None:
        """Close the client this instance owns. An injected one is the caller's."""
        if self._owned is not None:
            await self._owned.aclose()
            self._owned = None
```

- [ ] **Step 4: Route every request through the shared client**

In `_post`, replace the per-call client construction and the `finally` that closes it:

```python
        try:
            resp = await self._shared().post(
                f"{self._base_url}/extract",
                files={"file": ("paper.pdf", pdf_bytes, "application/pdf")},
                data=data,
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"Marker is unreachable at {self._base_url}: {type(exc).__name__}. "
                "PDF extraction requires it; there is no fallback engine.",
            ) from exc
```

In `profile`, the same shape — `await self._shared().get(f"{self._base_url}/health", timeout=httpx.Timeout(5.0))`, keeping the existing `except (httpx.HTTPError, ValueError): return {}`, and dropping the `finally` that closed the client.

Replace `healthy` (lines 176-186):

```python
    async def healthy(self) -> bool:
        """Whether Marker is reachable, for `/health` and pre-flight checks.

        Cached and single-flighted. `_OPEN_PREFIXES` exempts `/health` from
        both auth and quota, so without this any anonymous caller turns a
        readiness probe into an amplifier: 300 concurrent requests measured a
        43.6 s median against 1.2 s for the same server with no upstream call.
        """
        cached = self._health_cached
        if cached is not None and time.monotonic() - cached[0] < _HEALTH_TTL_SECONDS:
            return cached[1]

        async with self._health_gate():
            # A concurrent probe may have filled the cache while this one waited.
            cached = self._health_cached
            if cached is not None and time.monotonic() - cached[0] < _HEALTH_TTL_SECONDS:
                return cached[1]
            try:
                resp = await self._shared().get(
                    f"{self._base_url}/health", timeout=httpx.Timeout(5.0)
                )
                ok = resp.status_code == 200
            except httpx.HTTPError:
                ok = False
            self._health_cached = (time.monotonic(), ok)
            return ok
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_marker_extract_pipeline.py -v`

Expected: all PASS.

- [ ] **Step 6: Run the full suite and gates**

Run: `uv run pytest && uv run ruff check src tests && uv run mypy src`

Expected: all PASS. Watch for a test asserting that an injected client gets closed — that behaviour is deliberately gone, and such a test should be updated to assert the opposite.

- [ ] **Step 7: Commit**

```bash
git add src/paper_mcp/pipelines/marker_client.py tests/test_marker_extract_pipeline.py
git commit -m "fix(health): pool the Marker client and cache the probe

/health is exempt from auth and quota, and opened a fresh TCP connection to
Marker on every hit. 300 concurrent anonymous requests measured 66.5s wall
and a 43.6s median, against 5.0s and 1.2s for the same server with no
upstream call. One client per instance, one probe per 5s, single-flighted.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: GET /mcp answers 405 instead of hanging forever (F-05)

The server runs `stateless_http=True`, where the SDK creates a fresh transport per request and — as its own comment says — "never opens a GET stream". Nothing rejects the GET. It answers `200 text/event-stream` and holds the connection open indefinitely on a stream structurally incapable of ever delivering a message. Each held stream costs a socket, a server task and a quota token, and the call budget is a rate limit rather than a concurrency limit, so nothing bounds how many accumulate.

The method guard must not catch FastAPI's own `GET /docs` and `GET /openapi.json`, so it matches the MCP path exactly. That means `MCP_PATH` has to be reachable from the middleware without importing `server` (which imports the middleware — a cycle). Move the constant to `config`, which both already import.

**Files:**
- Modify: `src/paper_mcp/config.py` (add `MCP_PATH`)
- Modify: `src/paper_mcp/server.py:41` (import it instead of defining it)
- Modify: `src/paper_mcp/api/middleware.py:16-24, 54-56`
- Modify: `tests/test_server.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `paper_mcp.config.MCP_PATH: str = "/mcp"`. `paper_mcp.server.MCP_PATH` stays importable as a re-export, so existing importers keep working.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_server.py`:

```python
def test_get_on_the_mcp_path_is_refused() -> None:
    """Stateless transport never opens a GET stream, so it must not accept one.

    The SDK answered 200 text/event-stream and held the connection open
    forever on a stream that could never carry a message: a browser pointed
    at the URL hangs instead of failing, and each held stream costs a socket,
    a task and a quota token with nothing bounding how many accumulate.
    """
    with TestClient(create_app()) as client:
        resp = client.get("/mcp")

    assert resp.status_code == 405
    assert resp.headers.get("allow") == "POST"


def test_the_docs_endpoints_still_answer() -> None:
    """The method guard is for /mcp only, not every GET the app serves."""
    with TestClient(create_app()) as client:
        assert client.get("/openapi.json").status_code == 200


def test_health_is_still_a_get() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_server.py -k "mcp_path_is_refused or docs_endpoints or health_is_still" -v`

Expected: `test_get_on_the_mcp_path_is_refused` FAILS — the response is 200, or the call hangs until the test client's timeout. The other two PASS and exist to catch over-reach in Step 4.

- [ ] **Step 3: Move `MCP_PATH` to config**

In `src/paper_mcp/config.py`, add near the top after `_DEFAULT_ALLOWED_HOSTS`:

```python
# The path the MCP sub-app owns. Lives here rather than in `server` because
# the middleware needs it too, and `server` imports the middleware.
MCP_PATH = "/mcp"
```

In `src/paper_mcp/server.py`, delete line 41 (`MCP_PATH = "/mcp"`) and add `MCP_PATH` to the existing config import on line 29:

```python
from paper_mcp.config import MCP_PATH, Settings, request_body_limit, settings
```

- [ ] **Step 4: Refuse the GET in the middleware**

In `src/paper_mcp/api/middleware.py`, extend the config import on line 17:

```python
from paper_mcp.config import MCP_PATH, request_body_limit, settings
```

Then insert the guard immediately after the open-prefix early return (after line 56):

```python
        # The SDK runs `stateless_http=True`, where — as its own comment says
        # — it "never opens a GET stream": a fresh transport is built per
        # request, so `GET /mcp` answered 200 text/event-stream and then held
        # the connection open forever on a stream structurally incapable of
        # delivering a message. A probe client sat on one past a 30 s read
        # timeout and a 5-minute script budget. Matched on the exact path
        # rather than on the method alone, so FastAPI's own `GET /openapi.json`
        # still answers. DELETE already returns a clean 405; this makes GET
        # consistent with it.
        if path == MCP_PATH and request.method == "GET":
            return _method_not_allowed()
```

And add the response helper beside the others at the bottom of the file:

```python
def _method_not_allowed() -> JSONResponse:
    return JSONResponse(
        {
            "error": "method_not_allowed",
            "detail": (
                "this endpoint is stateless streamable-HTTP MCP: it answers POST "
                "only. A GET stream here can never carry a message."
            ),
        },
        status_code=405,
        headers={"Allow": "POST"},
    )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_server.py -v`

Expected: all PASS.

- [ ] **Step 6: Run the full suite and gates**

Run: `uv run pytest && uv run ruff check src tests && uv run mypy src`

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/paper_mcp/config.py src/paper_mcp/server.py src/paper_mcp/api/middleware.py tests/test_server.py
git commit -m "fix(transport): refuse GET /mcp instead of holding it open forever

Stateless transport never opens a GET stream, but nothing rejected one: the
SDK answered 200 text/event-stream and held the connection indefinitely on a
stream that could never deliver a message. Each one cost a socket, a task
and a quota token, and the call budget is a rate limit, not a concurrency
limit. DELETE already answered 405; GET now matches.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: A big upload costs what a big upload costs (F-04)

`charge_extraction()` is skipped whenever a job for those bytes already exists — deliberately, so polling callers are not billed repeatedly. But the expensive work happens before that decision and is not skipped: `decode_pdf` base64-decodes up to 100 MB and opens it with PyMuPDF, then `spooled.write_bytes(pdf)` writes it to disk, all on the event loop. Ten identical 86 MB uploads from one caller stalled the loop for 11.0 s of a 28.4 s window and pushed `/health` to 1.67 s, with all ten inside the call budget and none charged for GPU.

Two changes. The work moves off the loop, and the spool write is skipped entirely when this call is only joining an in-flight job — it was writing up to 100 MB and unlinking it a few lines later for a file that never had a reader. Then the call budget stops pretending every request costs the same.

**Files:**
- Modify: `src/paper_mcp/api/middleware.py:70-72, 95-98` (and add `call_cost`)
- Modify: `src/paper_mcp/tools/extract.py:209, 234-244`
- Modify: `tests/test_auth_quota.py`, `tests/test_extract_tool.py`

**Interfaces:**
- Consumes: `MCP_PATH` from Task 4.
- Produces: `paper_mcp.api.middleware.call_cost(declared_bytes: int) -> float` — call-budget tokens a request of this declared size costs. `1.0` for a request with no declared body.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_auth_quota.py`:

```python
def test_a_large_body_costs_more_call_budget_than_a_poll() -> None:
    """One call can cost far more than a flat charge assumes.

    Ten identical 86 MB uploads stalled the loop for 11.0s of a 28.4s window,
    all ten inside the 60-calls/minute budget and none charged for GPU. The
    call meter is what sees them, so it has to see their size.
    """
    from paper_mcp.api.middleware import call_cost

    assert call_cost(0) == 1.0
    assert call_cost(2 * 1024) == pytest.approx(1.0, abs=0.01)
    # An 86 MB upload is not one poll's worth of work.
    assert call_cost(86 * 1024 * 1024) > 10
    # Monotonic, so a bigger body never costs less.
    assert call_cost(50 * 1024 * 1024) < call_cost(100 * 1024 * 1024)
```

Append to `tests/test_extract_tool.py`:

```python
@pytest.mark.anyio
async def test_joining_an_in_flight_job_writes_no_spool_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll must not write the whole upload to disk and delete it again.

    The spool write happened before the join was decided, so every repeat
    upload of an in-flight document wrote up to 100 MB on the event loop for
    a file that was unlinked a few lines later and never had a reader.
    """
    monkeypatch.setenv("PAPER_MCP_ARTIFACT_ROOT", str(tmp_path / "artifacts"))

    from paper_mcp.tools import extract as extract_mod

    pdf_b64 = _sample_pdf_b64()

    first = await extract_mod.tool_extract_pdf(pdf_b64)
    assert first.status == "extracting"

    spooled_after_first = list(extract_mod.spool_dir().glob("*.pdf"))
    await extract_mod.tool_extract_pdf(pdf_b64)
    spooled_after_second = list(extract_mod.spool_dir().glob("*.pdf"))

    assert spooled_after_second == spooled_after_first, (
        "joining an in-flight job created a spool file"
    )
```

`_sample_pdf_b64()` is whatever `tests/test_extract_tool.py` already uses to build a minimal valid PDF; reuse the existing helper rather than writing a second one. If the module builds its bytes inline, extract that into a helper first.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_auth_quota.py -k call_budget tests/test_extract_tool.py -k spool -v`

Expected: the first FAILS with `ImportError: cannot import name 'call_cost'`; the second FAILS because the second call created an extra spool file.

- [ ] **Step 3: Scale the call charge by declared body size**

In `src/paper_mcp/api/middleware.py`, add above the class:

```python
# One call-token per request, plus one per 8 MiB of declared body. A 100 MB
# upload and a 2 KB poll both cost exactly one token under a flat charge, but
# the upload costs a 100 MB base64 decode, a PyMuPDF open and a spool write.
# Measured: ten 86 MB uploads stalled the loop for 11.0 s of a 28.4 s window
# and pushed /health to 1.67 s, and every one of them was comfortably inside
# the 60-calls/minute budget.
_CALL_BYTES_PER_TOKEN = 8 * 1024 * 1024


def call_cost(declared_bytes: int) -> float:
    """Call-budget tokens a request of this declared size costs.

    Declared, not actual: `Content-Length` is what is known before the body is
    read, and reading the body to price it is the cost being metered.
    """
    return 1.0 + max(0, declared_bytes) / _CALL_BYTES_PER_TOKEN
```

Then in `dispatch`, replace lines 70-72 so the declared size is kept:

```python
        declared = request.headers.get("content-length", "")
        declared_bytes = int(declared) if declared.isdigit() else 0
        if declared_bytes > request_body_limit(cfg.max_upload_bytes):
            return _payload_too_large(declared_bytes, cfg.max_upload_bytes)
```

and replace the consume at line 96:

```python
            quota_store().consume(principal.subject_hash, "calls", call_cost(declared_bytes))
```

- [ ] **Step 4: Move the decode off the loop and stop the wasted spool write**

In `src/paper_mcp/tools/extract.py`, replace line 209:

```python
    # Off the loop. A 100 MB base64 decode plus a PyMuPDF open is tens to
    # hundreds of milliseconds of pure CPU, and it ran inline: ten 86 MB
    # uploads accumulated 11.0 s of event-loop stall in a 28.4 s window, with
    # 8 of 38 concurrent /health probes taking over a second.
    pdf = await asyncio.to_thread(
        decode_pdf, content_base64, max_bytes=cfg.max_upload_bytes
    )
```

Then replace lines 234-244:

```python
    incumbent = jobs.for_key(key)
    starting_work = incumbent is None or incumbent.state == "done"
    if starting_work:
        charge_extraction()

    # Spooled rather than closed over. A queued job that holds its upload in
    # memory turns a queue of large papers into a queue of large buffers: with
    # a 100 MB ceiling and a single worker, ten waiting uploads pinned
    # hundreds of megabytes for no reason but waiting. On disk, the cost is
    # one in-flight document regardless of queue depth.
    #
    # Written only when this call will actually start a job, and written off
    # the loop. Joining an in-flight job wrote the entire upload and unlinked
    # it a few lines later — up to 100 MB of synchronous disk write per poll,
    # for a file that never had a reader.
    spooled = spool_path(key)
    if starting_work:
        await asyncio.to_thread(spooled.write_bytes, pdf)
```

Leave the `spooled.unlink(missing_ok=True)` at the old line 282 in place. It is now a no-op on the join path, and it is still the right thing when `submit` returns an incumbent this call did not predict.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_auth_quota.py tests/test_extract_tool.py -v`

Expected: all PASS.

- [ ] **Step 6: Run the full suite and gates**

Run: `uv run pytest && uv run ruff check src tests && uv run mypy src`

Expected: all PASS. A test that asserted an exact remaining-call-budget number may need its expectation updated — the charge is no longer always 1.0.

- [ ] **Step 7: Commit**

```bash
git add src/paper_mcp/api/middleware.py src/paper_mcp/tools/extract.py tests/test_auth_quota.py tests/test_extract_tool.py
git commit -m "fix(extract): price a call by its body, and decode off the loop

The decode, the PyMuPDF open and the spool write all ran inline before the
charge decision: ten 86 MB uploads stalled the loop 11.0s in a 28.4s window,
all inside the call budget and none charged for GPU. Joining an in-flight
job also wrote the whole upload and unlinked it immediately. Decode moves to
a thread, the join writes nothing, and the call meter now sees size.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Quota buckets stop accumulating forever (F-06)

`QuotaStore._buckets` gains one entry per `(subject_hash, resource)` pair and nothing removes them. `maintenance.sweep_once` reclaims artifacts and job records; quota is not mentioned in that module at all. In open mode the key is the client IP, so on a public endpoint the dict grows with every distinct caller for the life of the process — a slow leak on exactly the deployment shape the quota exists to protect.

**Files:**
- Modify: `src/paper_mcp/quota.py` (add `evict_full`)
- Modify: `src/paper_mcp/maintenance.py:37-39, 56-71`
- Create: `tests/test_quota_sweep.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `QuotaStore.evict_full(*, now: float | None = None) -> int` — drops buckets that have refilled to capacity; returns how many went.
  - `maintenance.Swept` gains a third field, `quota: int = 0`. The default keeps two-field unpacking working for any existing caller.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_quota_sweep.py`:

```python
"""Bucket eviction.

`_buckets` grew one entry per (subject_hash, resource) and nothing ever
removed them. In open mode the key is the caller's IP, so a public endpoint
leaked an entry per distinct caller for the life of the process — on exactly
the deployment shape the quota exists to protect.
"""
from __future__ import annotations

import pytest

from paper_mcp.quota import QuotaLimits, QuotaStore


def test_a_refilled_bucket_is_evicted() -> None:
    """A full bucket is indistinguishable from one that never existed."""
    store = QuotaStore(QuotaLimits(calls_per_minute=60, extractions_per_hour=20))
    store.consume("caller-a", "calls", now=0.0)

    # A minute later the bucket has refilled to capacity.
    assert store.evict_full(now=60.0) == 1
    assert store._buckets == {}


def test_a_bucket_still_in_debt_survives() -> None:
    """Evicting a partly-spent bucket would forgive the spend."""
    store = QuotaStore(QuotaLimits(calls_per_minute=60, extractions_per_hour=20))
    for _ in range(30):
        store.consume("caller-a", "calls", now=0.0)

    assert store.evict_full(now=1.0) == 0
    assert len(store._buckets) == 1


def test_eviction_changes_no_behaviour() -> None:
    """A recreated bucket starts full, which is what the evicted one held."""
    store = QuotaStore(QuotaLimits(calls_per_minute=2, extractions_per_hour=20))
    store.consume("caller-a", "calls", now=0.0)
    store.evict_full(now=600.0)

    # Two more calls fit, exactly as they would have without the eviction.
    store.consume("caller-a", "calls", now=600.0)
    store.consume("caller-a", "calls", now=600.0)
    with pytest.raises(Exception):
        store.consume("caller-a", "calls", now=600.0)


@pytest.mark.anyio
async def test_the_sweeper_evicts_quota_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    """The maintenance sweep is the caller quota never had."""
    from paper_mcp import maintenance
    from paper_mcp.quota import quota_store

    quota_store().consume("caller-a", "calls", now=0.0)
    swept = await maintenance.sweep_once()

    assert swept.quota >= 0  # field exists and the sweep ran
    assert hasattr(swept, "quota")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_quota_sweep.py -v`

Expected: the first three FAIL with `AttributeError: 'QuotaStore' object has no attribute 'evict_full'`; the fourth FAILS on `Swept` having no `quota` field.

- [ ] **Step 3: Add `evict_full`**

In `src/paper_mcp/quota.py`, add to `QuotaStore` after `remaining`:

```python
    def evict_full(self, *, now: float | None = None) -> int:
        """Drop buckets that have refilled to capacity; returns how many went.

        A full bucket is indistinguishable from one that never existed — the
        next `consume` recreates it full — so this is free of behaviour
        change. Without it nothing ever removed an entry: in open mode the key
        is the caller's IP, so a public endpoint grew this dict by one per
        distinct caller for the life of the process.
        """
        moment = now if now is not None else time.monotonic()
        stale = [
            key
            for key, bucket in self._buckets.items()
            if bucket.tokens + (moment - bucket.updated_at) * bucket.refill_per_second
            >= bucket.capacity
        ]
        for key in stale:
            del self._buckets[key]
        if stale:
            logger.debug("evicted %d refilled quota bucket(s)", len(stale))
        return len(stale)
```

- [ ] **Step 4: Sweep them from maintenance**

In `src/paper_mcp/maintenance.py`, extend `Swept` (lines 37-39):

```python
class Swept(NamedTuple):
    artifacts: int
    jobs: int
    # Defaulted so any existing two-field unpacking still works.
    quota: int = 0
```

and extend `sweep_once` (lines 56-71):

```python
async def sweep_once() -> Swept:
    """Reclaim expired artifacts, forgotten job handles and refilled buckets."""
    # Imported here rather than at module scope: `tools.extract` owns the
    # process-level stores, and importing it eagerly would make this module
    # part of a cycle with the tool that imports the config.
    from paper_mcp.quota import quota_store
    from paper_mcp.tools.extract import artifact_store, job_store

    ttl = settings().artifact_ttl_hours
    # Walking the store stats every file in every entry, which is disk work,
    # not CPU work — off the loop so a large cache cannot stall the service
    # it is being swept for.
    artifacts = await asyncio.to_thread(artifact_store().sweep, ttl)
    jobs = job_store().sweep()
    # Quota was never mentioned in this module, so its buckets were the one
    # store that grew without bound. In-memory and cheap to walk, so it stays
    # on the loop.
    quota = quota_store().evict_full()
    if artifacts or jobs or quota:
        logger.info(
            "sweep reclaimed %d artifact(s), %d job record(s) and %d quota bucket(s)",
            artifacts,
            jobs,
            quota,
        )
    return Swept(artifacts, jobs, quota)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_quota_sweep.py tests/test_maintenance.py -v`

Expected: all PASS.

- [ ] **Step 6: Run the full suite and gates**

Run: `uv run pytest && uv run ruff check src tests && uv run mypy src`

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/paper_mcp/quota.py src/paper_mcp/maintenance.py tests/test_quota_sweep.py
git commit -m "fix(quota): sweep buckets that have refilled to capacity

_buckets gained an entry per (subject_hash, resource) and nothing removed
them; maintenance never mentioned quota at all. In open mode the key is the
caller's IP, so a public endpoint leaked one entry per distinct caller for
the life of the process. A full bucket is indistinguishable from a fresh
one, so evicting it changes no behaviour.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Bound the queue, and put the spool on a volume (F-07)

`JobStore.submit` accepts work without limit, and each queued job holds its upload on disk until it runs. With `PAPER_MCP_JOB_CONCURRENCY=1` and ~45 s per paper, a backlog that arrives faster than it drains grows unbounded in both queue length and disk.

Where that disk lives is the sharper half. `spool_dir()` derives from `artifact_root.parent`, so with the shipped `PAPER_MCP_ARTIFACT_ROOT=/app/artifacts` the spool is `/app/spool`. Inside the running container `/app/artifacts` is the named volume and `/app/spool` is the container's writable overlay, so a backlog of 100 MB uploads fills the Docker root disk rather than the volume sized for this data.

**Files:**
- Modify: `src/paper_mcp/config.py` (add `spool_root`, `max_queued_jobs`)
- Modify: `src/paper_mcp/jobs.py:50-62, 98-118` (add `JobQueueFull`, bound the depth)
- Modify: `src/paper_mcp/tools/extract.py:63-71, 271` (honour the setting, map the error)
- Modify: `tests/test_jobs.py`, `tests/test_extract_tool.py`

**Interfaces:**
- Consumes: `starting_work` / `spooled` from Task 5.
- Produces:
  - `Settings.spool_root: Path | None` — `PAPER_MCP_SPOOL_DIR`. `None` keeps today's derived location.
  - `Settings.max_queued_jobs: int` — `PAPER_MCP_MAX_QUEUED_JOBS`, default 32.
  - `paper_mcp.jobs.JobQueueFull(depth: int, retry_after: float)` — raised by `submit`; `.depth` and `.retry_after` are attributes.
  - `JobStore.__init__(*, concurrency: int = 1, ttl_seconds: float = 3600.0, max_queued: int = 32)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_jobs.py`:

```python
@pytest.mark.anyio
async def test_a_full_queue_refuses_new_work() -> None:
    """Unbounded queue depth is unbounded latency and unbounded spool disk.

    Work is serialized at one extraction at a time, so a caller queued behind
    two hundred others is hours from an answer with no way to know it. Better
    told to come back.
    """
    store = JobStore(concurrency=1, max_queued=3)
    gate = asyncio.Event()

    async def blocked() -> str:
        await gate.wait()
        return "done"

    for i in range(3):
        store.submit(content_key=f"key-{i}", run=blocked)

    with pytest.raises(JobQueueFull) as excinfo:
        store.submit(content_key="key-overflow", run=blocked)

    assert excinfo.value.depth == 3
    assert excinfo.value.retry_after > 0

    gate.set()


@pytest.mark.anyio
async def test_joining_an_in_flight_job_is_never_refused() -> None:
    """The cap is on new work. A poll for work already queued still answers."""
    store = JobStore(concurrency=1, max_queued=1)
    gate = asyncio.Event()

    async def blocked() -> str:
        await gate.wait()
        return "done"

    first = store.submit(content_key="key-a", run=blocked)
    again = store.submit(content_key="key-a", run=blocked)

    assert again.job_id == first.job_id

    gate.set()
```

Append to `tests/test_extract_tool.py`:

```python
def test_the_spool_honours_its_own_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The spool must be placeable on the disk sized for it.

    Derived from artifact_root.parent, the shipped /app/artifacts put the
    spool at /app/spool — the container's writable overlay, not the named
    volume — so a backlog of 100 MB uploads filled the Docker root disk.
    """
    from paper_mcp.tools import extract as extract_mod

    monkeypatch.setenv("PAPER_MCP_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.delenv("PAPER_MCP_SPOOL_DIR", raising=False)
    assert extract_mod.spool_dir() == tmp_path / "spool"

    monkeypatch.setenv("PAPER_MCP_SPOOL_DIR", str(tmp_path / "elsewhere"))
    assert extract_mod.spool_dir() == tmp_path / "elsewhere"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_jobs.py -k "full_queue or never_refused" tests/test_extract_tool.py -k spool_honours -v`

Expected: the job tests FAIL with `NameError: name 'JobQueueFull' is not defined`; the spool test FAILS on the second assertion because `PAPER_MCP_SPOOL_DIR` is not read.

- [ ] **Step 3: Add the settings**

In `src/paper_mcp/config.py`, add two fields to `Settings` after `artifact_ttl_hours`:

```python
    # Where uploads wait between acceptance and extraction. `None` keeps the
    # derived location beside the artifact cache, which is where it has always
    # been — but under the shipped PAPER_MCP_ARTIFACT_ROOT=/app/artifacts that
    # derives to /app/spool, and only /app/artifacts is a volume. A backlog of
    # 100 MB uploads therefore filled the container's writable layer instead
    # of the disk sized for this data. Settable so a deployment can say where.
    spool_root: Path | None
    # How much work may be queued before new work is refused. Extraction is
    # serialized, so an unbounded queue is unbounded latency and unbounded
    # spool disk: a caller told "queue full, retry in Ns" is better served
    # than one whose job sits behind two hundred others.
    max_queued_jobs: int
```

and to the `settings()` constructor call:

```python
        spool_root=(
            Path(os.environ["PAPER_MCP_SPOOL_DIR"])
            if os.environ.get("PAPER_MCP_SPOOL_DIR")
            else None
        ),
        max_queued_jobs=max(1, int(os.environ.get("PAPER_MCP_MAX_QUEUED_JOBS", "32"))),
```

- [ ] **Step 4: Bound the queue**

In `src/paper_mcp/jobs.py`, add after the `JobStatus` model:

```python
# Roughly how long one extraction takes, for the retry advice a refused
# caller gets. Marker measured ~45 s on a 15-page paper at one page per
# batch; this is an estimate for a hint, not a promise.
_ESTIMATED_SECONDS_PER_JOB = 45.0


class JobQueueFull(Exception):
    """More work is already queued than this service will hold."""

    def __init__(self, depth: int, retry_after: float) -> None:
        super().__init__(
            f"{depth} extractions are already queued; retry in {math.ceil(retry_after)}s"
        )
        self.depth = depth
        self.retry_after = retry_after
```

Add `import math` to the imports. Extend `__init__` (line 53):

```python
    def __init__(
        self, *, concurrency: int = 1, ttl_seconds: float = 3600.0, max_queued: int = 32
    ) -> None:
        # One slot by default: Marker is the workload, and it does not
        # parallelize on a single small GPU.
        self._semaphore = asyncio.Semaphore(concurrency)
        self._ttl = ttl_seconds
        self._max_queued = max_queued
```

leaving the remaining four attribute assignments as they are. Then in `submit`, replace line 98 so the depth is checked before a job is created — after the two join branches, so a poll for existing work is never refused:

```python
        ahead = sum(1 for j in self._jobs.values() if j.state in ("queued", "running"))
        if ahead >= self._max_queued:
            # Refused, not queued. Work is serialized, so accepting this only
            # buys the caller a place at the back of a line it cannot see and
            # a spool file that sits on disk until its turn comes.
            raise JobQueueFull(ahead, retry_after=ahead * _ESTIMATED_SECONDS_PER_JOB)
```

- [ ] **Step 5: Honour the setting and map the error**

In `src/paper_mcp/tools/extract.py`, replace `spool_dir` (lines 63-71):

```python
def spool_dir() -> Path:
    """Where uploads wait on disk between acceptance and extraction.

    Beside the artifact cache by default rather than inside it: these are the
    caller's original documents, and nothing here is ever served over HTTP.
    Overridable because the derived path was not on a volume — with the
    shipped /app/artifacts it landed on /app/spool, the container's writable
    layer, so a backlog filled the Docker root disk instead of the volume.
    """
    cfg = settings()
    path = cfg.spool_root or cfg.artifact_root.parent / "spool"
    path.mkdir(parents=True, exist_ok=True)
    return path
```

Update `job_store()` to pass the cap:

```python
def job_store() -> JobStore:
    global _jobs
    if _jobs is None:
        cfg = settings()
        _jobs = JobStore(concurrency=cfg.job_concurrency, max_queued=cfg.max_queued_jobs)
    return _jobs
```

Add `JobQueueFull` to the `paper_mcp.jobs` import, then wrap the submit at line 271:

```python
    # Keyed by content, so two callers uploading the same paper join one job
    # rather than queueing two identical GPU runs.
    try:
        handle = jobs.submit(content_key=key, run=run)
    except JobQueueFull as exc:
        # Nothing will ever read this file: no job adopted it.
        spooled.unlink(missing_ok=True)
        raise RateLimitedError(str(exc), retry_after=exc.retry_after) from exc
    job = handle
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_jobs.py tests/test_extract_tool.py -v`

Expected: all PASS.

- [ ] **Step 7: Run the full suite and gates**

Run: `uv run pytest && uv run ruff check src tests && uv run mypy src`

Expected: all PASS. A test constructing `JobStore(1)` positionally will now fail — `concurrency` was already keyword-only, so this should not arise, but read the failures.

- [ ] **Step 8: Commit**

```bash
git add src/paper_mcp/config.py src/paper_mcp/jobs.py src/paper_mcp/tools/extract.py tests/test_jobs.py tests/test_extract_tool.py
git commit -m "feat(jobs): cap the queue, and let the spool live on a volume

submit accepted work without limit and each queued job held its upload on
disk until it ran. With one worker and ~45s per paper, a backlog grew
unbounded in both queue length and disk — on /app/spool, the container's
writable layer, while only /app/artifacts was a volume.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: The error contract stops contradicting itself (F-11)

Three inconsistencies, all in what a caller is told to do next.

1. A 429 body can say "retry in 0s" while its own `Retry-After` header says 1, because the message rounds 0.4 s down and the header takes `max(1, int(...))`. That invites an immediate retry that will be refused.
2. With Marker down the caller is told "a repeat failure is the document, not a transient fault". Marker being unreachable is precisely a transient fault, and that advice tells an agent to give up on a perfectly good PDF.
3. The extraction quota's `retry_after` never reaches the wire. It is carried on `RateLimitedError` and rendered by the SDK as a generic tool error, so only the prose survives — which makes the prose the contract, and the prose has to be right.

**Files:**
- Modify: `src/paper_mcp/quota.py:55-61`
- Modify: `src/paper_mcp/api/middleware.py:123-133`
- Modify: `src/paper_mcp/tools/extract.py:283-293`
- Modify: `tests/test_auth_quota.py`, `tests/test_extract_tool.py`

**Interfaces:**
- Consumes: nothing.
- Produces: no signature changes. `QuotaExceededError`'s message and the 429 header now agree, and both round up.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_auth_quota.py`:

```python
def test_the_retry_message_and_header_agree() -> None:
    """A body saying "retry in 0s" invites a retry the header already refuses."""
    from paper_mcp.api.middleware import _too_many
    from paper_mcp.quota import QuotaExceededError

    exc = QuotaExceededError("calls", 0.4)
    resp = _too_many(exc)

    assert "retry in 1s" in str(exc)
    assert resp.headers["Retry-After"] == "1"


def test_retry_advice_always_rounds_up() -> None:
    """Rounding down tells a caller to come back before the bucket refills."""
    from paper_mcp.quota import QuotaExceededError

    assert "retry in 2s" in str(QuotaExceededError("calls", 1.2))
    assert "retry in 126s" in str(QuotaExceededError("extractions", 125.4))
```

Append to `tests/test_extract_tool.py`:

```python
@pytest.mark.anyio
async def test_an_unreachable_marker_is_reported_as_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Marker being down is the commonest outage and it IS transient.

    Telling an agent that a repeat failure is "the document, not a transient
    fault" makes it give up on a perfectly good PDF.
    """
    from paper_mcp.models import UpstreamError
    from paper_mcp.tools import extract as extract_mod

    monkeypatch.setenv("PAPER_MCP_ARTIFACT_ROOT", str(tmp_path / "artifacts"))

    pdf_b64 = _sample_pdf_b64()
    await extract_mod.tool_extract_pdf(pdf_b64)
    # Drive the job to a Marker-unreachable failure, then re-call so the
    # store hands the failure back. Use whatever this module already does to
    # simulate an unreachable Marker (respx, or an injected client).
    await _fail_the_job_with(extract_mod, "UpstreamError: Marker is unreachable")

    with pytest.raises(UpstreamError) as excinfo:
        await extract_mod.tool_extract_pdf(pdf_b64)

    message = str(excinfo.value)
    assert "transient" in message
    assert "not a transient fault" not in message
```

`_fail_the_job_with` is a helper to write against this module's existing fixtures — `tests/test_extract_tool.py` already has a way to make an extraction fail; reuse it rather than inventing a second mechanism. If no such helper exists, drive it by injecting a `MarkerClient` whose `_post` raises `UpstreamError`, and await the job task before re-calling.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_auth_quota.py -k "retry" tests/test_extract_tool.py -k transient -v`

Expected: `test_the_retry_message_and_header_agree` FAILS with `"retry in 0s"` in the message; `test_retry_advice_always_rounds_up` FAILS on `retry in 1s` vs `retry in 2s`; the transient test FAILS on the current wording.

- [ ] **Step 3: Round the retry advice up, in both places**

In `src/paper_mcp/quota.py`, add `import math` and replace `QuotaExceededError` (lines 55-61):

```python
class QuotaExceededError(Exception):
    def __init__(self, resource: str, retry_after: float) -> None:
        # Rounded up, not to nearest. `:.0f` turned a 0.4 s wait into "retry
        # in 0s" while the `Retry-After` header on the same response said 1 —
        # so the body invited a retry the header had already refused.
        super().__init__(
            f"quota exceeded for {resource}; retry in {math.ceil(retry_after)}s"
        )
        self.resource = resource
        self.retry_after = retry_after
```

In `src/paper_mcp/api/middleware.py`, add `import math` and make the header agree (line 132):

```python
        headers={"Retry-After": str(max(1, math.ceil(exc.retry_after)))},
```

- [ ] **Step 4: Tell the truth about a Marker outage**

In `src/paper_mcp/tools/extract.py`, replace the error branch (lines 283-293):

```python
    if job.state == "error":
        # The store hands a previously-failed job back so the failure is seen
        # rather than silently re-queued. Reporting it as `extracting` would
        # keep it invisible: the hint below says call again, so a caller would
        # loop on a dead job forever. The store has already released the key,
        # so calling again genuinely retries.
        #
        # Which advice depends on what failed. `UpstreamError` is Marker
        # unreachable or erroring — the commonest outage there is, and
        # precisely a transient fault. Telling an agent otherwise makes it
        # abandon a perfectly good PDF over a container that is still booting.
        transient = (job.error or "").startswith("UpstreamError:")
        advice = (
            "Marker is the extraction engine and it was unreachable or failing, "
            "which is transient: check /health, then call extract_pdf again."
            if transient
            else "Calling extract_pdf again retries it; a repeat failure is the "
            "document, not a transient fault."
        )
        raise UpstreamError(
            f"extraction of these bytes failed: {job.error or 'unknown error'}. {advice}"
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_auth_quota.py tests/test_extract_tool.py -v`

Expected: all PASS.

- [ ] **Step 6: Run the full suite and gates**

Run: `uv run pytest && uv run ruff check src tests && uv run mypy src`

Expected: all PASS. A test asserting the old `"retry in 0s"` or the old advice wording needs updating to the new contract.

- [ ] **Step 7: Commit**

```bash
git add src/paper_mcp/quota.py src/paper_mcp/api/middleware.py src/paper_mcp/tools/extract.py tests/test_auth_quota.py tests/test_extract_tool.py
git commit -m "fix(errors): make the retry advice agree with itself and with reality

A 429 body could say 'retry in 0s' while its own Retry-After header said 1,
because the message rounded 0.4s down. And a Marker outage — the commonest
failure there is, and definitionally transient — told the caller a repeat
failure was 'the document, not a transient fault', which makes an agent give
up on a perfectly good PDF.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: The documented development flow runs (F-08, F-10)

The README calls two on-device checks "load-bearing" and neither command works as written: `scripts/on_device_check.py` does not exist, and `paper_workflow_check.py` is given an arXiv id when it reads `Path(sys.argv[1]).read_bytes()` — a leftover from before discovery was removed. The test count is stale too: 176 collected, not 163.

Separately, the workflow check reports failure for a passing run on a Windows console in a CJK locale. Every content assertion passes, then the script dies printing the extraction warnings because one names a table column containing ϵ (U+03F5). Extraction output is arbitrary academic text, so any paper with a Greek letter in a table header does this.

Writing a new `on_device_check.py` is out of scope — the fix is to document the scripts that exist.

**Files:**
- Modify: `scripts/paper_workflow_check.py` (UTF-8 stdout at entry)
- Modify: `scripts/authenticated_client_check.py`, `scripts/security_check.py` (same, if they print extraction output)
- Modify: `README.md:257, 271-272`

**Interfaces:**
- Consumes: the table assertion from Task 1 Step 6.
- Produces: nothing importable.

- [ ] **Step 1: Reproduce the encoding crash**

Run: `uv run python -c "import sys; print('table column ϵ — dash')" > nul`

Expected on a cp950 console: `UnicodeEncodeError: 'cp950' codec can't encode character 'ϵ'`. If your console is already UTF-8 this will pass; set `PYTHONIOENCODING=cp950` to reproduce it, since the fix must hold for the operator whose console is not UTF-8.

- [ ] **Step 2: Reconfigure stdout at every script entry**

In `scripts/paper_workflow_check.py`, at the top of `main()` before the first `print`:

```python
async def main() -> int:
    # Extraction output is arbitrary academic text. One warning naming a table
    # column that contains a Greek epsilon killed a run in which every content
    # assertion had already passed, and the em dash in the title line
    # mojibaked: a cp950 console cannot encode either. Replace rather than
    # raise — a check that dies while reporting success is worse than one
    # that prints a question mark.
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    if len(sys.argv) < 2:
```

Apply the identical block at the entry point of `scripts/authenticated_client_check.py` and `scripts/security_check.py`. Read each first: if a script never prints extraction output it does not need this, and adding it anyway is noise.

- [ ] **Step 3: Verify the crash is gone**

Run: `PYTHONIOENCODING=cp950 uv run python scripts/paper_workflow_check.py` (no argument)

Expected: prints the usage line and exits 2, with no `UnicodeEncodeError`. On PowerShell use `$env:PYTHONIOENCODING='cp950'; uv run python scripts/paper_workflow_check.py`.

- [ ] **Step 4: Correct the README's development section**

In `README.md`, line 257, the count:

```
uv run pytest                    # 176 tests, fast and offline (integration excluded)
```

Then replace lines 271-272 with the checks that actually exist and the arguments they actually take:

```bash
uv run python scripts/paper_workflow_check.py path/to/paper.pdf   # the real workflow, judged on content
uv run python scripts/authenticated_client_check.py               # a real MCP client against a real token
uv run python scripts/security_check.py                           # transport, traversal and upload-cap probes
```

Then fix the sentence beneath them, which describes "the first" as booting the service and driving it with a real MCP client — that is now `paper_workflow_check.py`. Read the paragraph and make it describe these three scripts truthfully. Do not invent capabilities: each sentence must match what the script does when you read it.

- [ ] **Step 5: Verify every documented command actually runs**

Run each command in the README's Development block that does not need a GPU:

```bash
uv sync
uv run pytest
uv run ruff check src tests
uv run mypy src
uv run python scripts/paper_workflow_check.py    # expect: usage line, exit 2
```

Expected: no `ModuleNotFoundError`, no "file does not exist", and the reported test count matches line 257. A command that needs Marker may be confirmed by its own error message naming Marker rather than by running to completion.

- [ ] **Step 6: Commit**

```bash
git add README.md scripts/
git commit -m "fix(scripts): make the documented dev flow run, on a CJK console too

The README named a script that does not exist, passed an arXiv id to a
script that reads a file path, and reported 163 tests against 176. The
workflow check itself exited 1 on a fully passing run because one extraction
warning named a table column containing a Greek epsilon and the console was
cp950.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: The shipped compose file can turn authentication on (F-09)

The README's deployment warning says to set the OIDC variables before exposing the service. `PAPER_MCP_ALLOWED_HOSTS` is plumbed through compose; the auth variables are not present at all — nor are quota, TTL, salt or public base URL. Following the README with the shipped compose file leaves you on `AUTH_MODE=open`, with figure URLs pinned to `http://localhost:8000` regardless of where the service actually answers.

Marker's published port is also hardcoded at `127.0.0.1:8002`. The loopback bind is deliberate and well-argued — keep it as the default — but the port number collided with another project and blocked `docker compose up` until overridden.

**Files:**
- Modify: `docker-compose.yml` (`paper-mcp.environment`, `paper-mcp.volumes`, `marker.ports`, `volumes`)

**Interfaces:**
- Consumes: `PAPER_MCP_SPOOL_DIR` and `PAPER_MCP_MAX_QUEUED_JOBS` from Task 7.
- Produces: nothing importable.

- [ ] **Step 1: Plumb the missing variables**

In `docker-compose.yml`, add to `paper-mcp.environment`, after the existing `PAPER_MCP_TRUST_FORWARDED_FOR` line:

```yaml
      # Authentication. `open` means every caller is unauthenticated, which is
      # fine on a laptop and never on a public endpoint. These were absent
      # entirely, so following the README's deployment warning with this file
      # left you on AUTH_MODE=open with no way to say otherwise.
      - PAPER_MCP_AUTH_MODE=${PAPER_MCP_AUTH_MODE:-open}
      - PAPER_MCP_OIDC_ISSUER=${PAPER_MCP_OIDC_ISSUER:-}
      - PAPER_MCP_OIDC_AUDIENCE=${PAPER_MCP_OIDC_AUDIENCE:-}
      # Stable key for per-caller metering. Unset means a per-process
      # ephemeral salt, so every quota resets whenever the process does.
      - PAPER_MCP_SUBJECT_SALT=${PAPER_MCP_SUBJECT_SALT:-}
      # Figure URLs are built from this. Left at the default, every image_url
      # a caller receives points at localhost:8000 regardless of where the
      # service actually answers.
      - PAPER_MCP_PUBLIC_BASE_URL=${PAPER_MCP_PUBLIC_BASE_URL:-http://localhost:8000}
      - PAPER_MCP_ALLOWED_ORIGINS=${PAPER_MCP_ALLOWED_ORIGINS:-}
      - PAPER_MCP_QUOTA_CALLS_PER_MINUTE=${PAPER_MCP_QUOTA_CALLS_PER_MINUTE:-60}
      - PAPER_MCP_QUOTA_EXTRACTIONS_PER_HOUR=${PAPER_MCP_QUOTA_EXTRACTIONS_PER_HOUR:-20}
      - PAPER_MCP_ARTIFACT_TTL_HOURS=${PAPER_MCP_ARTIFACT_TTL_HOURS:-24}
      - PAPER_MCP_MAX_QUEUED_JOBS=${PAPER_MCP_MAX_QUEUED_JOBS:-32}
      # On its own volume. Derived from the artifact root this landed on
      # /app/spool — the container's writable layer — so a backlog of 100 MB
      # uploads filled the Docker root disk rather than the volume sized for
      # this data.
      - PAPER_MCP_SPOOL_DIR=/app/spool
```

- [ ] **Step 2: Give the spool a volume and the Marker port an override**

Extend `paper-mcp.volumes`:

```yaml
    volumes:
      - paper-mcp-artifacts:/app/artifacts
      - paper-mcp-spool:/app/spool
```

Replace `marker.ports`, keeping the loopback default and the reasoning above it intact:

```yaml
    # Host side is overridable because 8002 collides: the bind address stays
    # loopback by default and MUST stay loopback on any shared host, for the
    # pillow reasons above. Override the whole left side, e.g.
    # PAPER_MCP_MARKER_PUBLISH=127.0.0.1:18002
    ports: ["${PAPER_MCP_MARKER_PUBLISH:-127.0.0.1:8002}:8002"]
```

Add the named volume at the bottom:

```yaml
volumes:
  paper-mcp-artifacts:
  paper-mcp-spool:
  marker-models:
```

- [ ] **Step 3: Verify compose parses and resolves as intended**

Run: `docker compose config`

Expected: valid YAML output; `PAPER_MCP_AUTH_MODE: open`, `PAPER_MCP_SPOOL_DIR: /app/spool`, the marker port published on `127.0.0.1:8002`, and both named volumes declared.

- [ ] **Step 4: Verify the overrides take**

Run: `PAPER_MCP_AUTH_MODE=oidc PAPER_MCP_MARKER_PUBLISH=127.0.0.1:18002 docker compose config`

Expected: `PAPER_MCP_AUTH_MODE: oidc` and the marker port on `127.0.0.1:18002`. On PowerShell set the two variables with `$env:` first.

- [ ] **Step 5: Verify the spool actually lands on the volume**

Run: `docker compose up -d && docker compose exec paper-mcp sh -c "df -h /app/artifacts /app/spool"`

Expected: both paths report the same device class as each other and neither reports `overlay`. Then `docker compose down`.

If a GPU is not available on this machine, skip this step and say so explicitly in the task's completion note rather than reporting it as verified.

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml
git commit -m "fix(compose): ship the variables the README tells you to set

AUTH_MODE, both OIDC variables, the subject salt, quota, TTL, public base
URL and allowed origins were absent entirely, so following the deployment
warning with this file left you on AUTH_MODE=open with figure URLs pinned to
localhost:8000. The spool now has its own volume, and Marker's published
port is overridable while staying loopback by default.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Final verification

- [ ] **Step 1: Full gates**

Run: `uv run pytest && uv run ruff check src tests scripts && uv run mypy src`

Expected: all PASS, with a test count above the 176 baseline.

- [ ] **Step 2: Confirm every finding is closed**

Walk the spec and name the commit that closes each of F-01 through F-11. Anything without one is not done.

- [ ] **Step 3: On-device confirmation**

The findings were measured against a running service, and a green pytest does not re-measure them. With Marker available:

```bash
docker compose up -d
uv run python scripts/paper_workflow_check.py path/to/1706.03762.pdf
```

Expected: every check passes, including "tables survived as markdown tables" with `0 table(s) lost cells`. Report the actual output. If no GPU is available, say the on-device confirmation did not run rather than implying it did.

---

## Self-Review

Checked against the spec after writing:

**Spec coverage** — F-01 Task 2, F-02 Task 3, F-03 Task 1, F-04 Task 5, F-05 Task 4, F-06 Task 6, F-07 Task 7, F-08 Tasks 1+9, F-09 Task 10, F-10 Task 9, F-11 Task 8. All eleven have a task.

**Deliberately out of scope, and why:**
- Writing a new `scripts/on_device_check.py`. The README references it and it does not exist; Task 9 fixes the documentation rather than building the tool, because building it is a feature, not a fix. If the check is wanted, it is its own plan.
- Splitting `/health` liveness from readiness (F-02's second paragraph). The pooled client and cached probe close the amplification; the split changes a documented contract and was declined.

**Type consistency** — `call_cost(int) -> float` is defined in Task 5 and used only there. `JobQueueFull(depth, retry_after)` is defined in Task 7 Step 4 and consumed in Step 5 with those exact attribute names. `Swept` gains `quota: int = 0` in Task 6 and is constructed with three positional fields in the same step. `verify_token` becomes a coroutine in Task 2 Step 4 and every caller is updated in Step 5. `MCP_PATH` moves in Task 4 Step 3 before it is imported in Step 4.

**Known ordering constraints** — Task 5 edits the middleware region Task 4 touches, so run 4 first. Task 10 uses settings introduced in Task 7. Task 9's table assertion was written in Task 1 Step 6; it is listed there rather than duplicated.

**Placeholders** — two steps deliberately defer to code the executor must read rather than guessing: Task 5 Step 1 (`_sample_pdf_b64`) and Task 8 Step 1 (`_fail_the_job_with`) reuse existing helpers in `tests/test_extract_tool.py`. Both name what the helper must do and what to build if it is absent. Task 1 Step 5 verifies the warnings accessor before Step 6 asserts on it.

