"""The error contract every tool returns against.

The rest of this file tested `PaperRef`, `normalize_paper_id`, `s2_path_id`
and `clamp_max_results` — all of which belonged to the discovery tools
removed in v1.0. Those tests passed, which is exactly why the dead code
looked alive for a whole release.
"""
from __future__ import annotations

from paper_mcp.models import (
    InvalidArgumentError,
    NotFoundError,
    RateLimitedError,
    ToolError,
    UpstreamError,
)


def test_tool_errors_carry_a_code_and_optional_retry_after() -> None:
    err = RateLimitedError("slow down", retry_after=7.0)

    assert err.code == "rate_limited"
    assert err.retry_after == 7.0
    assert str(err) == "slow down"


def test_every_error_is_a_tool_error_with_its_own_code() -> None:
    """A caller branches on `code`, so the codes must stay distinct.

    Each names a different mistake and a different next step: fix the
    argument, stop asking for a thing that is not there, retry the upstream,
    or wait. Collapsing two of them would tell a caller to do the wrong one.
    """
    errors = [InvalidArgumentError, NotFoundError, UpstreamError, RateLimitedError]

    assert all(issubclass(e, ToolError) for e in errors)
    assert len({e.code for e in errors}) == len(errors)


def test_retry_after_is_absent_unless_the_error_knows_one() -> None:
    assert UpstreamError("marker is unreachable").retry_after is None
