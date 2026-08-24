"""The error types every tool returns.

This module used to also carry `PaperRef`, `OpenAccess`, `normalize_paper_id`,
`s2_path_id` and `clamp_max_results` — the shared shape of a paper across
arXiv, Semantic Scholar and DOI resolution, and the helpers for addressing
one. Discovery was removed in v1.0 and nothing has referenced any of it
since; it survived only because its own tests kept it looking alive.

What is left is the error contract, which every remaining tool depends on.
"""
from __future__ import annotations


class ToolError(Exception):
    """Base for every error a tool returns to the caller.

    Errors are part of the contract (SRS I-8 #7): each names what happened
    and what the caller should do next. They are never bare 500s.
    """

    code = "tool_error"

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


class InvalidArgumentError(ToolError):
    code = "invalid_argument"


class NotFoundError(ToolError):
    code = "not_found"


class UpstreamError(ToolError):
    code = "upstream_error"


class RateLimitedError(ToolError):
    code = "rate_limited"
