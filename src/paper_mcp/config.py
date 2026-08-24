"""Environment-only configuration (SRS §III-9, twelve-factor)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_ALLOWED_HOSTS = ("localhost", "localhost:8000", "127.0.0.1", "127.0.0.1:8000")


@dataclass(frozen=True)
class Settings:
    auth_mode: str
    public_base_url: str
    log_level: str
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    host: str
    port: int
    marker_url: str
    # VRAM scales with page CONTENT DENSITY, not page count: PaperHub measured
    # a single dense two-column page producing 200+ Surya OCR lines and
    # saturating 6 GB, and a 5-page batch taking 21 minutes. 1 is the safe
    # default on a small card; raise it on a bigger GPU.
    marker_max_pages: int
    # Ceiling on an uploaded PDF. A bound on what reaches the decoder is part
    # of the containment posture (SRS NFR-02), not just politeness — it refuses
    # a file whose only purpose is to exhaust memory.
    #
    # 25 MB looked generous against arXiv preprints (0.5-2 MB) and was wrong
    # for published papers: measured over a real 44-paper library the median
    # is 10.6 MB and the largest is 67 MB, so a quarter of it was refused.
    # 100 MB covers that library with headroom. Measured cost of the largest:
    # an 89 MB JSON body accepted in 1.4 s, ~2.5x the file size held while the
    # job is queued — so this is a ceiling on memory per in-flight extraction,
    # which is what the per-caller quota exists to bound.
    max_upload_bytes: int
    # Extractions allowed to run at once. 1 because VRAM scales with page
    # content density and a second concurrent dense page OOMs a 6 GB card —
    # but that is a statement about the hardware, not the service, and it was
    # hardcoded so no deployment could say otherwise. On a shared endpoint it
    # is also the fairness ceiling: one caller's queue stalls everyone.
    job_concurrency: int
    artifact_root: Path
    artifact_ttl_hours: float
    oidc_issuer: str | None
    oidc_audience: str | None
    # Whether `X-Forwarded-For` names the client. Off unless an operator
    # says a proxy is in front: in open mode the per-IP meter is the only
    # brake, and a header the caller controls is not a rate-limit key.
    trust_forwarded_for: bool
    quota_calls_per_minute: float
    quota_extractions_per_hour: float


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _flag(name: str) -> bool:
    """An opt-in switch. Anything not clearly affirmative stays off.

    An unset compose variable arrives as an empty string, which must read
    as "no" rather than as "the operator configured something".
    """
    return (os.environ.get(name) or "").strip().lower() in _TRUTHY


def _csv(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def settings() -> Settings:
    """Read settings from the environment on every call.

    Deliberately not cached: the process is the unit of configuration, and
    reading a handful of env vars is cheaper than a stale-config bug.
    """
    return Settings(
        auth_mode=os.environ.get("PAPER_MCP_AUTH_MODE", "open"),
        public_base_url=os.environ.get("PAPER_MCP_PUBLIC_BASE_URL", "http://localhost:8000"),
        log_level=os.environ.get("PAPER_MCP_LOG_LEVEL", "INFO"),
        # DNS-rebinding protection. A public deployment MUST set this to its
        # own hostname; the default only covers local development. "*"
        # disables the check entirely and is logged loudly at boot.
        allowed_hosts=_csv("PAPER_MCP_ALLOWED_HOSTS", _DEFAULT_ALLOWED_HOSTS),
        allowed_origins=_csv("PAPER_MCP_ALLOWED_ORIGINS"),
        # Bind address. In the container this is 0.0.0.0:8000 behind a proxy;
        # locally it must be overridable so the service can sit alongside
        # whatever else already owns the port.
        host=os.environ.get("PAPER_MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("PAPER_MCP_PORT", "8000")),
        marker_url=os.environ.get("PAPER_MCP_MARKER_URL", "http://127.0.0.1:8002"),
        marker_max_pages=int(os.environ.get("PAPER_MCP_MARKER_MAX_PAGES", "1")),
        max_upload_bytes=int(os.environ.get("PAPER_MCP_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024))),
        job_concurrency=max(1, int(os.environ.get("PAPER_MCP_JOB_CONCURRENCY", "1"))),
        artifact_root=Path(os.environ.get("PAPER_MCP_ARTIFACT_ROOT", "artifacts")),
        artifact_ttl_hours=float(os.environ.get("PAPER_MCP_ARTIFACT_TTL_HOURS", "24")),
        # This service is a resource server: it validates tokens against an
        # IdP the operator brings, and never issues them.
        oidc_issuer=os.environ.get("PAPER_MCP_OIDC_ISSUER") or None,
        oidc_audience=os.environ.get("PAPER_MCP_OIDC_AUDIENCE") or None,
        trust_forwarded_for=_flag("PAPER_MCP_TRUST_FORWARDED_FOR"),
        quota_calls_per_minute=float(os.environ.get("PAPER_MCP_QUOTA_CALLS_PER_MINUTE", "60")),
        quota_extractions_per_hour=float(
            os.environ.get("PAPER_MCP_QUOTA_EXTRACTIONS_PER_HOUR", "20")
        ),
    )


# Framing around the payload: the JSON-RPC envelope, the method and tool
# names, and a filename. A few hundred bytes in practice; 64 KiB is slack
# bought cheaply, since the number only bounds a rejection.
_ENVELOPE_SLACK = 64 * 1024


def request_body_limit(max_upload_bytes: int) -> int:
    """Transport body limit that admits a PDF at `max_upload_bytes`.

    The MCP SDK defaults to 4 MiB and enforces it *before* a request reaches
    any tool, so the app's own upload cap was unreachable dead code: 25 MiB of
    PDF needs a 33 MiB body. Measured against a real corpus, that default
    rejected 35 of 44 papers — as a bare `413` outside JSON-RPC, with no
    mention of a limit, so `extract_pdf`'s carefully worded size error never
    ran once.

    Equalling the cap would not fix it. `extract_pdf` carries the file as
    base64, which inflates by 4/3, so the body must be bigger than the file it
    is meant to allow. Deriving it here keeps one number configurable
    (`PAPER_MCP_MAX_UPLOAD_BYTES`).

    What deriving it does **not** do is make the transport agree with the tool
    at every size, which an earlier version of this docstring claimed. Body
    size grows with file size, so the two only agree within `_ENVELOPE_SLACK`
    of the cap: measured at a 20 MiB cap, cap+30 KB reached the tool and got
    its worded error while cap+4 MB got the bare 413 again. A limit has to
    exist, so the fix is not a bigger number — it is that `AuthQuotaMiddleware`
    checks `Content-Length` against this and answers with a JSON error that
    names the cap, before the SDK's unworded check ever runs.

    Lives here rather than in `server` so the middleware can reach it without
    importing the app that installs the middleware.
    """
    return max_upload_bytes * 4 // 3 + _ENVELOPE_SLACK
