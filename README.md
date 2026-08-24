<div align="center">

# 📄 paper-mcp

**Ready-to-use tools that give an LLM agent a paper it can actually work with.**

Hand it a PDF; get the full text as markdown, with real tables, LaTeX equations, and every figure indexed by its caption. Served over MCP, stateless, multi-user.

![Python](https://img.shields.io/badge/python-3.13-3776AB?logo=python&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-Streamable%20HTTP-000000)
![Extraction](https://img.shields.io/badge/extraction-Marker%20(GPU)-FF6F00)
![Auth](https://img.shields.io/badge/auth-OIDC%20resource%20server-2A6DB2)
![Lint](https://img.shields.io/badge/lint-ruff-261230?logo=ruff&logoColor=white)
![Types](https://img.shields.io/badge/types-mypy%20--strict-2A6DB2)
![Tests](https://img.shields.io/badge/tests-205%20unit%20%2B%207%20integration-brightgreen)

</div>

---

Built for external agent clients (Claude Cowork, Claude Desktop, Cursor, any MCP framework). It supplies what those clients lack — faithful extraction — and nothing else: **no accounts, no stored user data, and no agent flows.**

One caveat that belongs here rather than in a footnote: extraction is deterministic **only when the service runs keyless**. Set `GEMINI_API_KEY` and Marker's accuracy pass sends page content to Google's API on every extraction — better tables and maths, at the cost of the document leaving the host. Leave it unset and nothing does. Each bundle records which model produced it in `extraction.llm_model`, so the choice is auditable after the fact rather than assumed.

This is *data-processing functionality*, not an agent. Pipelines built on top — slides, summaries, literature reviews — belong to the calling agent and its own skills. This service's only job is to make each step precise.

## 🧰 The tools

| Tool | What it does | Network scope |
| --- | --- | --- |
| **`extract_pdf`** | **The product.** Your PDF → markdown + a figure index, content-addressed and cached. Returns a job handle while the GPU works | **none** — you supply the bytes |
| `get_job` | Poll an extraction: queue depth while waiting, page reached while running | — |

That is the whole surface. Discovery, paper fetching and LaTeX compilation
were removed in v1.0: an agent already has better ways to find and download a
paper than this service had, and the search endpoints it offered never worked
reliably without a paid API key. What remains is the part an agent cannot do
for itself.

## 📦 What you get

`extract_pdf(content_base64=…)` returns the paper as markdown —

```markdown
## Introduction

The dominant sequence transduction models are based on complex recurrent…

| Model | BLEU |
| --- | --- |
| Base | 27.3 |
| Big  | 28.4 |

$$
\mathrm{Attention}(Q,K,V) = \mathrm{softmax}(QK^T/\sqrt{d_k})V
$$

![fig-001](figures/fig-001.png)

*fig-001: The Transformer architecture.*
```

— alongside the figure index:

```jsonc
"figures": [
  { "id": "fig-001",
    "caption": "The Transformer architecture.",
    "page": 3,
    "image_url": "https://…/a/<token>/figures/fig-001.png" }
]
```

Extraction is **Marker**, and only Marker. Three guarantees, each asserted by tests and by a real-workflow check:

- **Tables stay tables** — rows and columns intact. A table flattened to a blob of cell text invites an agent to read numbers against the wrong column.
- **Equations stay LaTeX** — never prose approximations of maths.
- **Figures are extracted, indexed and captioned** — an index entry exists only when the image really decoded to disk, so citing `fig-001` always refers to something real.

There is no low-fidelity fallback engine. PaperHub shipped crude PyMuPDF extraction once, measured the output as "conference-UNusable", and replaced it with Marker; a service whose value is faithful extraction must not quietly substitute unfaithful extraction. Without Marker, a PDF fetch reports `extraction_unavailable` rather than degrading.

---

## 🚀 Quick start

```bash
git clone https://github.com/whats2000/paper-mcp.git
cd paper-mcp

docker compose up -d --build      # paper-mcp on :8000, Marker on :8002 (loopback)
curl -s http://127.0.0.1:8000/health
```

The first extraction downloads ~2 GB of Surya weights into a named volume, so a rebuild never re-pays for them. The service image itself carries no TeX distribution and no jail — with LaTeX out of scope there is nothing here that executes caller-supplied code.

> [!NOTE]
> **GPU strongly recommended.** Marker runs on CPU but far too slowly to be useful. `PAPER_MCP_MARKER_MAX_PAGES=1` bounds VRAM per call — VRAM scales with page *content density*, not page count, and one dense two-column page can saturate 6 GB. Raise it only on a bigger GPU.

> [!IMPORTANT]
> The default `AUTH_MODE=open` means **every caller is unauthenticated**. Before exposing this anywhere, set the OIDC variables and your own hostname in `PAPER_MCP_ALLOWED_HOSTS` — see [Configuration](#️-configuration).

## 🔌 Connecting a client

The service speaks Streamable HTTP at `/mcp`. A local Claude Code session connects with an `.mcp.json` pointing at your own instance:

```json
{
  "mcpServers": {
    "paper-mcp": { "type": "http", "url": "http://127.0.0.1:8000/mcp" }
  }
}
```

Against a deployment with `AUTH_MODE=oidc`, the client sends a bearer token from your IdP on **every** request — there is no session to authenticate once and reuse:

```json
{
  "mcpServers": {
    "paper-mcp": {
      "type": "http",
      "url": "https://paper-mcp.example.org/mcp",
      "headers": { "Authorization": "Bearer ${PAPER_MCP_TOKEN}" }
    }
  }
}
```

---

## 🗺️ Architecture (one screen)

```
┌──────────────────────┐  Streamable HTTP  ┌─────────────────────────────────────────────┐
│  MCP client          │  ───────────────► │  paper-mcp · POST /mcp                      │
│  Claude Cowork /     │  Bearer <token>   │                                             │
│  Desktop / Cursor /  │                   │   OIDC verify ─► quota ─► allowed-host ─► …  │
│  any MCP framework   │                   │        │                                     │
└──────────────────────┘                   │        ├─ extract_pdf ► spool ─► job ────────┤
           ▲                               │        └─ get_job ◄─── depth · page reached ─┤
           │  GET /a/<token>/…             │                        Marker (GPU, jailed) ◄┘
           └────────────────────────────── │                                             │
              figures · bundle.zip         │   artifacts: content-addressed, TTL-swept    │
                                           └─────────────────────────────────────────────┘
```

**Nothing per-user is stored.** Artifacts are keyed by content, not by caller: two people asking for the same public paper share one entry, which is deduplication of public data rather than a leak. Identity exists only as a salted hash used to meter quota. That is the security architecture, not an omission — a shared public endpoint with no per-user state has nothing to leak between callers.

Full architecture lives in the [SRS](docs/superpowers/specs/2026-08-11-paper-mcp-srs.md).

---

## 🛡️ Security

The service is internet-facing and feeds caller-supplied PDFs to an image decoder it cannot patch, so the controls are verified against a **running container** rather than a test client. That distinction is not pedantry: `TestClient` follows redirects, and that is exactly how a `307` on `POST /mcp` passed the suite while a connector would have broken on it. A property that only holds in-process is a property of a stack nobody is attacking.

The attack surface exercised, against a real IdP:

| Class | Covered |
| --- | --- |
| **Token** | missing · malformed · expired · wrong audience (confused deputy) · wrong issuer · unknown signing key |
| **Forgery** | **`alg=none` signature stripping** · **RS256→HS256 key confusion** — the two a header-trusting verifier accepts |
| **Oracle** | rejection bodies are byte-identical, so a probe cannot learn *which* check failed |
| **Transport** | DNS rebinding via `Host` (`421`) · no redirect on `POST /mcp` |
| **Artifacts** | six traversal encodings — plain, url-encoded, double-encoded, backslash, absolute, unknown token |
| **Abuse** | quota exhaustion answering `429` with `Retry-After` |
| **Decoder** | malformed, truncated and zero-page PDFs refused at the boundary in under 0.1 s, before reaching the decoder or the GPU |

All 26 defended. Two rules that made the result trustworthy, both learned the hard way:

1. **Attack the image you ship, and confirm the *installed* package carries the controls first.** One run reported a total auth bypass that did not exist in the code — the image predated the middleware.
2. **Prove the guards don't break the product.** A service that rejects everyone is trivially secure and useless, so the whole flow is also driven through a real MCP client bearing a real token — resolve → extraction → compiled deck — confirming that **no session identifier is ever issued**: the server is `stateless_http`, so there is no handle to leak, and every request carries its own credential.

### The one untrusted input

With LaTeX out of scope there is nothing here that executes caller-supplied
code — the jail, TeX Live and the `seccomp=unconfined` exemption it required
are all gone, and the service image is a fifth of its former size.

What remains is a PDF fed to an image decoder that **cannot be patched**:
`marker-pdf` pins `pillow<11` at every released version while the current
decode advisories are fixed in 12.3.0. Pre-validation is no answer, because
those advisories are reachable through images that are otherwise perfectly
legitimate, and decoding documents is the product.

So the control is not whether an exploit triggers but what it reaches. Marker
runs as the sandbox boundary — published on loopback only, no capabilities, a
read-only root with a sized tmpfs, memory and pid limits — with an upload
ceiling and a page cap bounding what ever gets that far.

> [!NOTE]
> Egress is deliberately left on. Cutting it is the strongest control compose
> offers, but it also disables the `use_llm` accuracy pass that keeps table
> structure honest, and compose cannot allowlist a single host. A keyless
> deployment should cut it once the Surya weights are cached; one using
> `GEMINI_API_KEY` cannot. Choose deliberately.

---

## ⚙️ Configuration

Environment only (twelve-factor). Nothing is read from a config file.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PAPER_MCP_HOST` / `PAPER_MCP_PORT` | `0.0.0.0` / `8000` | Bind address |
| `PAPER_MCP_ALLOWED_HOSTS` | localhost only | **DNS-rebinding protection.** A public deployment must list its own hostname or every request gets `421`. `*` disables the check and is warned about at boot |
| `PAPER_MCP_ALLOWED_ORIGINS` | derived from allowed hosts | CORS origins for browser clients |
| `PAPER_MCP_AUTH_MODE` | `open` | `open` disables authentication — development only. Anything else requires the OIDC settings below |
| `PAPER_MCP_OIDC_ISSUER` / `PAPER_MCP_OIDC_AUDIENCE` | unset | The IdP to validate bearer tokens against. This service is a resource server: it never issues tokens |
| `PAPER_MCP_SUBJECT_SALT` | per-process | Salt for the HMAC of `sub` used in metering and logs. The raw subject is never logged |
| `PAPER_MCP_QUOTA_CALLS_PER_MINUTE` | `60` | Per-caller call budget |
| `PAPER_MCP_QUOTA_EXTRACTIONS_PER_HOUR` | `20` | Per-caller GPU-extraction budget. Charged on a cache **miss** only — a cache hit costs no GPU time, and the tool's own hint tells a caller to keep calling until the cache is warm |
| `PAPER_MCP_TRUST_FORWARDED_FOR` | off | Whether `X-Forwarded-For` names the client. Turn it on **only** when a proxy you control is in front: in `open` mode the per-IP meter is the only brake there is, and a header the caller sets is not a rate-limit key. Left off, metering uses the peer address, which a peer cannot forge |
| `PAPER_MCP_MARKER_URL` | `http://127.0.0.1:8002` | Marker service. **Required for extraction** — without it `extract_pdf` reports the dependency rather than degrading |
| `PAPER_MCP_MAX_UPLOAD_BYTES` | `104857600` (100 MB) | Largest PDF accepted. Sized from a real library: median paper 10.6 MB, largest 67 MB. The transport limit is derived from this, so the two cannot disagree |
| `PAPER_MCP_JOB_CONCURRENCY` | `1` | Extractions at once. 1 because a second concurrent dense page OOMs a 6 GB card — raise it on a bigger one. On a shared endpoint this is also the fairness ceiling |
| `MARKER_DISABLE_OCR` | `1` | *(on the Marker service)* Trust the PDF's text layer instead of re-reading the page. The default avoids a VRAM spike that crashes a 6 GB card, at the cost of inline maths — an integral arrives as `R`, epsilon vanishes, while display equations stay perfect. Set `0` on a bigger card. Each bundle records which mode ran in `extraction.text_source` |
| `PAPER_MCP_MARKER_MAX_PAGES` | `1` | Pages per Marker call. VRAM scales with page *content density*, not page count: one dense two-column page can saturate 6 GB, and a 5-page batch was measured at 21 minutes. Raise only on a bigger GPU |
| `MARKER_GEMINI_MODEL` | `gemini-2.5-flash` | *(on the Marker service)* Model backing Marker's `use_llm` accuracy pass. marker-pdf carries its own default and Google has already retired it once — every call answered 404 while Marker returned `200`, so the pass stopped running with nothing to show for it. Pin it here when Google moves again |
| `PAPER_MCP_PUBLIC_BASE_URL` | `http://localhost:8000` | Origin the artifact URLs are built from. Nothing is persisted with it — URLs are derived on every serve, so moving hosts does not strand a warm cache |
| `PAPER_MCP_ARTIFACT_ROOT` | `artifacts` | Content-addressed cache for bundles and figure images |
| `PAPER_MCP_ARTIFACT_TTL_HOURS` | `24` | How long artifacts survive before the sweeper reclaims them |
| `PAPER_MCP_LOG_LEVEL` | `INFO` | Log level, applied to uvicorn too. `WARNING` drops per-request access logging: measured 18,222 → 114 bytes over 300 requests. Worth setting for a public deployment — a server whose stdout backs up blocks inside `write()`, and per-request logging is what fills the buffer |

### Why there is no search

Discovery was removed in v1.0 on measurement, not preference. Against the live
API the keyless tier throttled the **search** endpoint so hard it was unusable:
a single `search_papers` call still returned HTTP 429 after **237 seconds** of
paced retries, and a title lookup after **873 seconds**. Across a later
verification session it never once returned a result.

The endpoints that *did* work went with it. A tool surface teaches a calling
agent what a service is for, and a narrow one that is entirely trustworthy is
worth more than a broad one where two tools are excellent and one never
answers. An agent already has better ways to find a paper than this service
had — it does not have a better way to read one.

---

## 🎓 Skills

One starting-point skill ships in `skills/`, also served over `prompts/` so a Claude client surfaces it as a slash command:

| Skill | Shows an agent how to |
| --- | --- |
| `deep-read` | answer questions from the bundle rather than from memory of the paper |

It is an **example, not the product.** The calling agent owns its pipelines and can ignore them entirely — which is exactly why this service ships tools rather than flows.

---

## 🧑‍💻 Development

```bash
uv sync
uv run pytest                    # 205 tests, fast and offline (integration excluded)
uv run pytest -m integration     # spawns a real server, drives it with a real MCP client
uv run ruff check src tests
uv run mypy src                  # --strict

docker compose up -d marker      # required for extraction
uv run paper-mcp
```

The interpreter is pinned in `.python-version` (3.13) so every contributor and the container build agree on one runtime (NFR-07).

**pytest proves the code runs; it cannot prove the product works.** Mocked tests are blind to upstream contracts by construction, so the on-device check is load-bearing:

```bash
uv run python scripts/paper_workflow_check.py path/to/paper.pdf  # the real workflow, judged on content
```

It boots the service through its real entry point, drives it with a real MCP client over the wire, and judges the *output*: does the markdown have tables whose cell counts survived the render, equations as LaTeX, a populated and captioned figure index, and figure URLs that download real image bytes? Slow by nature — Marker takes roughly a minute per dense page. It takes a path to a PDF; acquiring the paper is your job, as it is the calling agent's.

> [!WARNING]
> `scripts/authenticated_client_check.py` and `scripts/security_check.py` **predate v1.0 and do not currently pass.** Both still drive tools that were deleted with discovery and LaTeX compilation — the first fails outright on `resolve_paper`/`search_papers`/`fetch_paper` and asserts a seven-tool surface against today's two; the second's three `compile_latex` probes now pass *vacuously*, reporting a LaTeX sandbox as verified when the tool it sandboxed no longer exists. Do not read either as a green light until they are rewritten against `extract_pdf`.

Between them these have caught defects the unit suite passed clean: a `307` redirect on `POST /mcp` (the in-process test client follows redirects), a Semantic Scholar field name one endpoint accepts and another rejects, a `similar` mode pointed at an endpoint that does not exist, a synchronous arXiv client blocking the event loop (three concurrent calls: 20.5 s → 0.7 s once threaded), and a bundle that persisted absolute artifact URLs — so a warm cache surviving a redeploy handed out figure links to an origin that no longer answered.

<details>
<summary><b>If uv warns <code>VIRTUAL_ENV=… does not match the project environment path .venv</code> →</b></summary>

<br>

That warning — and an editor reporting this project's dependencies as "not installed" — have one cause: **VS Code's selected Python interpreter.**

The Python extension exports the selected interpreter as `VIRTUAL_ENV` into its terminals and into extension-spawned processes (which is how Claude Code's shells inherit it). If that interpreter is a *bare* Python install rather than a virtualenv — a uv-managed `…/uv/python/cpython-3.x…/python.exe`, say, which has no `pyvenv.cfg` — then uv correctly refuses it and Pylance resolves against an interpreter lacking this project's packages.

`.vscode/settings.json` fixes it for this repo by pointing `python.defaultInterpreterPath` at `${workspaceFolder}/.venv`; **reload the window** for it to take effect. To fix it everywhere, repoint the user-level `python.defaultInterpreterPath` in your VS Code settings.

Things that do **not** fix it, tested: pinning `.python-version` (it selects an interpreter; the warning compares environment *paths*), and `UV_NO_ACTIVE=1`. There is no `pyproject.toml` or `uv.toml` key for it. The ad-hoc escape is `uv run --no-active …` — a real flag, though `uv run --help` documents only its `--active` counterpart.

</details>

---

## 📂 Repository layout

```
.
├── src/paper_mcp/
│   ├── server.py         # MCP server + FastAPI app · tools registered here
│   ├── tools/            # extract · get_job — the MCP surface
│   ├── pipelines/        # Marker client → bundle · html → markdown
│   ├── api/              # auth+quota middleware · artifact routes (GET /a/…)
│   ├── artifacts.py      # content-addressed store + TTL sweeper
│   ├── auth.py           # OIDC resource server (JWKS, rotation-aware)
│   └── quota.py          # per-caller token buckets
├── marker_service/       # the Marker extraction service (compose)
├── skills/               # portable skills, also served as MCP prompts
├── scripts/              # on-device and workflow checks
├── tests/                # pytest suite + integration suite
├── docs/superpowers/
│   ├── specs/            # SRS — the single authoritative specification
│   └── plans/            # implementation plans
├── Dockerfile            # the service, and nothing it does not need
└── docker-compose.yml    # paper-mcp + marker
```

`artifacts/` (gitignored) holds the content-addressed cache — derived data only, rebuildable from its key. `spool/` (also gitignored) holds uploads waiting for a worker; it is cleared at startup, since a restart has already forgotten the jobs they belonged to.

---

## 📖 Documentation

- **[Software Requirements Specification](docs/superpowers/specs/2026-08-11-paper-mcp-srs.md)** — the single authoritative document for architecture, scope, and acceptance criteria (**v0.4**).
- **[Implementation plans](docs/superpowers/plans/)** — one per phase, each executed via TDD.

---

## 🧬 Provenance

The extraction and LaTeX pipelines are ported from the [PaperHub](https://github.com/whats2000/PaperHub) proof-of-concept, which validated the end-to-end flow inside a single-user local application. PaperHub proved the *pipeline*; it could not be the *product*, because the delivery target is a remote, multi-user, internet-facing service that executes untrusted input.

Every ported file carries a header naming its source and what was adapted. The dependency arrow points one way — this project imports nothing from PaperHub.
