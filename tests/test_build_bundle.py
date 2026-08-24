from __future__ import annotations

import base64
import zipfile
from pathlib import Path

import pytest

from paper_mcp.artifacts import ArtifactStore, token_for
from paper_mcp.pipelines import build_bundle as bb
from paper_mcp.pipelines.build_bundle import build_bundle, bundle_key, load_cached
from paper_mcp.pipelines.marker_client import MarkerBlock, MarkerDoc

_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 32).decode()
# The caller supplies bytes now; the key is their hash, so distinct content
# means a distinct bundle and identical content is a cache hit.
_PDF = b"%PDF-1.7\nattention is all you need"


class _FakeMarker:
    """Stands in for the Marker service; records how it was called."""

    def __init__(self, doc: MarkerDoc) -> None:
        self.doc = doc
        self.calls = 0

    async def extract(
        self,
        pdf_bytes: bytes,
        *,
        max_pages: int | None = None,
        on_progress: object | None = None,
    ) -> MarkerDoc:
        self.calls += 1
        if on_progress is not None:
            on_progress(1, 1)  # a real client reports each page batch
        return self.doc


def _doc() -> MarkerDoc:
    return MarkerDoc(
        blocks=[
            MarkerBlock(block_type="SectionHeader", html="<h1>Introduction</h1>"),
            MarkerBlock(block_type="Text", html="<p>We propose the Transformer.</p>"),
            MarkerBlock(
                block_type="Table",
                html="<table><tr><th>Model</th><th>BLEU</th></tr>"
                "<tr><td>Base</td><td>27.3</td></tr></table>",
            ),
            MarkerBlock(block_type="Equation", latex=r"E = mc^2"),
            MarkerBlock(block_type="Figure", images={"a": _PNG}, caption="Architecture", page=3),
        ]
    )


@pytest.fixture
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the page count needs faking — nothing fetches anything now.

    The name is kept as a statement: after v1.0 there is no network in this
    path at all, so a test that reached one would be testing a bug.
    """
    monkeypatch.setattr(bb, "page_count", lambda _data: 15)


async def test_builds_a_bundle_with_markdown_and_a_figure_index(
    tmp_path: Path, _no_network: None
) -> None:
    store = ArtifactStore(tmp_path)
    marker = _FakeMarker(_doc())

    bundle = await build_bundle(_PDF, store=store, marker=marker)  # type: ignore[arg-type]

    assert "## Introduction" in bundle.markdown
    assert "| Model | BLEU |" in bundle.markdown  # tables survive as tables
    assert "$$" in bundle.markdown  # equations stay LaTeX
    assert len(bundle.figures) == 1
    assert bundle.extraction.engine == "marker"
    assert bundle.extraction.pages == 15


async def test_figure_urls_resolve_through_the_store(
    tmp_path: Path, _no_network: None
) -> None:
    store = ArtifactStore(tmp_path)

    bundle = await build_bundle(_PDF, store=store, marker=_FakeMarker(_doc()))  # type: ignore[arg-type]

    figure = bundle.figures[0]
    assert figure.image_url is not None
    token = figure.image_url.split("/a/")[1].split("/")[0]
    # The URL is not decoration: it must address a real file.
    assert store.resolve(token, figure.image_path).is_file()


async def test_a_second_call_is_a_cache_hit_and_does_not_re_extract(
    tmp_path: Path, _no_network: None
) -> None:
    store = ArtifactStore(tmp_path)
    marker = _FakeMarker(_doc())

    first = await build_bundle(_PDF, store=store, marker=marker)  # type: ignore[arg-type]
    second = await build_bundle(_PDF, store=store, marker=marker)  # type: ignore[arg-type]

    assert marker.calls == 1  # the GPU ran once
    assert second.bundle_id == first.bundle_id
    assert second.markdown == first.markdown


async def test_the_zip_contains_the_full_markdown_and_figures(
    tmp_path: Path, _no_network: None
) -> None:
    store = ArtifactStore(tmp_path)

    bundle = await build_bundle(_PDF, store=store, marker=_FakeMarker(_doc()))  # type: ignore[arg-type]

    assert bundle.artifact is not None
    zip_path = store.dir_for(bundle.bundle_id) / "bundle.zip"
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
    assert "markdown.md" in names
    assert any(n.startswith("figures/") for n in names)
    assert "bundle.zip" not in names  # never packs itself


async def test_cached_urls_follow_the_current_base_url(
    tmp_path: Path, _no_network: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The cache outlives the deployment. A bundle built behind one origin and
    # replayed behind another must not hand back links to the old one — the
    # files are still there, so the failure is silent and total.
    monkeypatch.setenv("PAPER_MCP_PUBLIC_BASE_URL", "http://old.example:8000")
    store = ArtifactStore(tmp_path)
    await build_bundle(_PDF, store=store, marker=_FakeMarker(_doc()))  # type: ignore[arg-type]

    monkeypatch.setenv("PAPER_MCP_PUBLIC_BASE_URL", "https://new.example")
    cached = load_cached(bundle_key(_PDF), store=store)

    assert cached is not None
    assert cached.figures[0].image_url == (
        f"https://new.example/a/{token_for(cached.bundle_id)}/{cached.figures[0].image_path}"
    )
    assert cached.artifact is not None
    assert cached.artifact.zip_url is not None
    assert cached.artifact.zip_url.startswith("https://new.example/a/")


async def test_no_origin_is_written_to_disk(
    tmp_path: Path, _no_network: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Derive-on-serve only works if nothing stale is stored to begin with.
    monkeypatch.setenv("PAPER_MCP_PUBLIC_BASE_URL", "http://old.example:8000")
    store = ArtifactStore(tmp_path)

    await build_bundle(_PDF, store=store, marker=_FakeMarker(_doc()))  # type: ignore[arg-type]

    raw = (store.dir_for(bundle_key(_PDF)) / "bundle.json").read_text(encoding="utf-8")
    assert "old.example" not in raw


async def test_extraction_warnings_reach_the_caller(
    tmp_path: Path, _no_network: None
) -> None:
    # A figure Marker could not extract must be visible, not silently absent.
    doc = MarkerDoc(blocks=[MarkerBlock(block_type="Figure", caption="ghost", images={})])
    store = ArtifactStore(tmp_path)

    bundle = await build_bundle(_PDF, store=store, marker=_FakeMarker(doc))  # type: ignore[arg-type]

    assert any("ghost" in w for w in bundle.extraction.warnings)


async def test_an_empty_extraction_is_flagged(tmp_path: Path, _no_network: None) -> None:
    store = ArtifactStore(tmp_path)

    bundle = await build_bundle(_PDF, store=store, marker=_FakeMarker(MarkerDoc(blocks=[])))  # type: ignore[arg-type]

    assert any("no text" in w for w in bundle.extraction.warnings)


async def test_a_marker_failure_leaves_no_cache_entry(
    tmp_path: Path, _no_network: None
) -> None:
    # bundle.json is written last, so an interrupted run reads as a miss and
    # is retried rather than served as a truncated paper.
    class _Broken:
        async def extract(
            self,
            pdf_bytes: bytes,
            *,
            max_pages: int | None = None,
            on_progress: object | None = None,
        ) -> MarkerDoc:
            raise RuntimeError("marker died")

    store = ArtifactStore(tmp_path)

    with pytest.raises(RuntimeError):
        await build_bundle(_PDF, store=store, marker=_Broken())  # type: ignore[arg-type]

    assert load_cached(bundle_key(_PDF), store=store) is None


def test_the_key_is_the_hash_of_the_bytes() -> None:
    # Content addressing is what makes a repeat upload free and lets two
    # callers share one extraction without the service knowing about either.
    assert bundle_key(_PDF) == bundle_key(b"%PDF-1.7\nattention is all you need")
    assert bundle_key(_PDF) != bundle_key(b"%PDF-1.7\na different paper")
    assert bundle_key(_PDF).startswith("sha256:")


def test_a_corrupt_cache_entry_reads_as_a_miss(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    entry = store.ensure("arxiv:1")
    (entry / "bundle.json").write_text("{not json", encoding="utf-8")

    assert load_cached("arxiv:1", store=store) is None


async def test_the_bundle_records_which_model_extracted_it(
    tmp_path: Path, _no_network: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whether Marker's accuracy pass ran is a property of the deployment.

    The cache outlives a config change: an operator who adds GEMINI_API_KEY
    keeps serving every already-cached paper from its pre-key extraction,
    where LLMTableProcessor never ran and tables are measurably worse. The
    bundle looks complete either way, so the only defence is recording what
    produced it.
    """
    class _MarkerWithLlm(_FakeMarker):
        async def profile(self) -> dict[str, object]:
            return {"use_llm": True, "llm_model": "gemini-2.5-flash"}

    store = ArtifactStore(tmp_path)

    bundle = await build_bundle(_PDF, store=store, marker=_MarkerWithLlm(_doc()))  # type: ignore[arg-type]

    assert bundle.extraction.llm_model == "gemini-2.5-flash"


async def test_a_keyless_extraction_records_no_model(
    tmp_path: Path, _no_network: None
) -> None:
    store = ArtifactStore(tmp_path)

    bundle = await build_bundle(_PDF, store=store, marker=_FakeMarker(_doc()))  # type: ignore[arg-type]

    assert bundle.extraction.llm_model is None


async def test_page_progress_is_reported_while_extracting(
    tmp_path: Path, _no_network: None
) -> None:
    """A caller polling a job must see movement, not one opaque string.

    Measured on a real run: 18 minutes of `progress: "extracting"` on a
    49-page paper, and 2,268 s of `"queued"` before that, with the GPU at 0%
    because the accuracy pass is network-bound. Indistinguishable from a hang.
    """
    seen: list[tuple[int, int]] = []
    store = ArtifactStore(tmp_path)

    await build_bundle(
        _PDF,
        store=store,
        marker=_FakeMarker(_doc()),  # type: ignore[arg-type]
        on_progress=lambda done, total: seen.append((done, total)),
    )

    assert seen, "the extraction reported no progress at all"
