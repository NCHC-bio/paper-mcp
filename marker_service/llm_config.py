"""Which Gemini model Marker's `use_llm` accuracy pass calls.

Deliberately free of `marker` imports so it can be unit-tested without the
service's heavyweight dependency tree.

Why this exists rather than a literal in `app.py`: marker-pdf pins its own
default (`gemini-2.0-flash` at 1.10.2), Google retires models on a schedule
nobody here controls, and the failure is silent. When the model went away
every LLM call answered 404; Marker logged "LLM did not return a valid
response", returned 200, and `/health` went on reporting `use_llm: true`
while LLMTableProcessor — the pass that keeps table structure honest — never
ran. Naming the model here makes the version explicit and lets a deployment
move without waiting on a release.

That has now happened twice. Pinning a name buys time; it does not stop the
next retirement, and nothing here detects one — `/health` still reports
`use_llm: true` on a model that 404s. `tests/test_marker_llm_config.py`
carries a list of retired names so a stale pin fails the suite rather than a
deployment, but the durable fix is a boot-time probe of the configured model
that makes `/health` tell the truth. That is not built yet.
"""
from __future__ import annotations

import os

# Current stable Flash tier. `gemini-2.5-flash` sat here until Google
# deprecated it too — retirement 2026-10-16, and already answering 404 for
# newly created projects well before that date — which is the second time
# this constant has gone stale underneath a running deployment.
#
# The previous-generation Flash rather than the newest: Marker's accuracy
# pass is a bounded multimodal call with structured output, run once per
# table, and `gemini-3.7-flash` is tuned for agentic multi-step work this
# does not do. Move it with `MARKER_GEMINI_MODEL` rather than editing here.
DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"

_ENV_VAR = "MARKER_GEMINI_MODEL"


def gemini_model() -> str:
    """The model name to hand Marker, honouring `MARKER_GEMINI_MODEL`.

    An unset compose variable arrives as an empty string, which must fall back
    to the default rather than ask Gemini for a model named "".
    """
    return (os.environ.get(_ENV_VAR) or "").strip() or DEFAULT_GEMINI_MODEL


_FALSEY = frozenset({"0", "false", "no", "off"})


def ocr_disabled() -> bool:
    """Whether to trust the PDF's text layer instead of re-reading the page.

    True by default, and that default is a hardware constraint rather than a
    preference. Marker flags a dense two-column page for Surya's line
    RECOGNITION pass, which measured ~5.9 GB and crashes a 6 GB card; with
    OCR disabled `provider_lines_good` is set for every page, so recognition
    is skipped while line detection, layout analysis and the
    figure/equation/table processors all still run.

    The cost is real and worth naming: the embedded text layer of a
    LaTeX-produced PDF encodes maths through Type1 font tables, so an
    integral arrives as the character `R`, a product as `Q`, and epsilon
    disappears — degrading prose to "we can train it to predict ." while
    display equations, which carry their own LaTeX, stay perfect. Inline
    maths is therefore unreliable in this mode and spot-checking equations
    will not reveal it.

    On a card with room to spare, `MARKER_DISABLE_OCR=0` buys correct inline
    maths at the price of that VRAM. An unset compose variable arrives as an
    empty string and must keep the safe default.
    """
    raw = (os.environ.get("MARKER_DISABLE_OCR") or "").strip().lower()
    return raw not in _FALSEY if raw else True
