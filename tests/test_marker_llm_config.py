"""The Gemini model name Marker's accuracy pass runs against.

Loaded by path: `marker_service` is a separate package whose `app` module
imports `marker` at module scope, which the service venv does not carry. The
config seam is deliberately free of those imports so it can be tested here.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_SOURCE = Path(__file__).resolve().parents[1] / "marker_service" / "llm_config.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("marker_llm_config", _SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Every model generation Google has since retired or scheduled for shutdown.
# A list rather than a single name, because this test previously guarded only
# `gemini-2.0-flash` and therefore said nothing when the pin that replaced it —
# `gemini-2.5-flash` — was itself deprecated and began answering 404 for new
# projects. Add to this as Google moves; the point is that the guard grows
# with the history rather than tracking one incident.
RETIRED_MODELS = frozenset({
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
})


def test_gemini_model_defaults_to_a_model_google_still_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retired pin costs nothing visible and breaks everything quietly.

    marker-pdf 1.10.2 defaults to `gemini-2.0-flash`, which Google retired.
    Every LLM call then answered 404, Marker logged "LLM did not return a
    valid response" and returned 200, so `use_llm` reported healthy while the
    accuracy pass — including LLMTableProcessor — never ran once.

    That has now happened twice: the model pinned to replace it was itself
    deprecated. This asserts against the whole history, not the last incident.
    """
    monkeypatch.delenv("MARKER_GEMINI_MODEL", raising=False)

    model = _load().gemini_model()

    assert model
    assert model not in RETIRED_MODELS, (
        f"the default model {model!r} has been retired by Google; the accuracy "
        "pass will 404 on every call while /health still reports use_llm: true"
    )


def test_gemini_model_can_be_pinned_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Google retires models on their own schedule; a deployment must be able
    # to move without waiting on a release here.
    monkeypatch.setenv("MARKER_GEMINI_MODEL", "gemini-3.5-flash")

    assert _load().gemini_model() == "gemini-3.5-flash"


def test_a_blank_env_override_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unset variable in compose arrives as "", which must not become the
    # model name — marker would then request a model called "".
    monkeypatch.setenv("MARKER_GEMINI_MODEL", "   ")
    module = _load()

    assert module.gemini_model() == module.DEFAULT_GEMINI_MODEL


def test_ocr_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trusting the PDF text layer is the safe default, not a free one.

    Marker re-OCRs a dense two-column page through Surya's recognition pass,
    which measured ~5.9 GB and crashes a 6 GB card — so the text layer is
    trusted instead. The cost is that maths comes back through Type1 font
    encodings: an integral sign arrives as the character `R`, a product as
    `Q`, and epsilon vanishes, degrading prose to "we can train it to
    predict ." while display equations stay perfect, so spot-checking hides
    it.

    That is the right default on small hardware and the wrong one on a big
    card, which makes it a deployment choice rather than a constant.
    """
    monkeypatch.delenv("MARKER_DISABLE_OCR", raising=False)

    assert _load().ocr_disabled() is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0", False), ("false", False), ("FALSE", False), ("no", False),
     ("1", True), ("true", True), ("", True), ("  ", True)],
)
def test_ocr_can_be_enabled_on_hardware_that_can_afford_it(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    # An unset compose variable arrives as "", which must keep the safe
    # default rather than being read as "false".
    monkeypatch.setenv("MARKER_DISABLE_OCR", value)

    assert _load().ocr_disabled() is expected
