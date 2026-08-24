from __future__ import annotations

import base64
from pathlib import Path

from paper_mcp.pipelines.marker_client import MarkerBlock, MarkerDoc
from paper_mcp.pipelines.marker_to_bundle import marker_doc_to_bundle_parts

_PNG = base64.b64encode(
    b"\x89PNG\r\n\x1a\n" + b"0" * 32
).decode()


def _doc(*blocks: MarkerBlock) -> MarkerDoc:
    return MarkerDoc(blocks=list(blocks))


def test_tables_survive_as_markdown_tables(tmp_path: Path) -> None:
    # The whole reason this pipeline exists. A results table flattened into a
    # blob of cell text is worse than useless — an agent will read numbers
    # against the wrong columns.
    doc = _doc(
        MarkerBlock(
            block_type="Table",
            html=(
                "<table><tr><th>Model</th><th>BLEU</th></tr>"
                "<tr><td>Base</td><td>27.3</td></tr>"
                "<tr><td>Big</td><td>28.4</td></tr></table>"
            ),
        )
    )

    markdown, _figures, warnings = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert "| Model | BLEU |" in markdown
    assert "| --- | --- |" in markdown
    assert "| Base | 27.3 |" in markdown
    assert "| Big | 28.4 |" in markdown
    assert warnings == []


def test_equations_stay_latex(tmp_path: Path) -> None:
    doc = _doc(MarkerBlock(block_type="Equation", latex=r"E = mc^2", html="<p>E = mc2</p>"))

    markdown, _f, _w = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert "$$" in markdown
    assert "E = mc^2" in markdown


def test_equation_latex_is_html_unescaped(tmp_path: Path) -> None:
    # Marker derives `latex` from HTML, so comparisons arrive entity-encoded.
    # Emitted raw, `\lambda &lt; \lambda'` reaches the agent as literal
    # "&lt;" — a renderer shows the entity or errors, and the guarantee that
    # equations stay LaTeX is broken exactly where maths is hardest to guess.
    doc = _doc(
        MarkerBlock(
            block_type="Equation",
            latex=r"a &lt; b \text{ and } c &gt; d \ \&amp;\ e",
        )
    )

    markdown, _f, _w = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert r"a < b \text{ and } c > d \ \&\ e" in markdown
    assert "&lt;" not in markdown and "&gt;" not in markdown and "&amp;" not in markdown


def test_figures_are_extracted_indexed_and_captioned(tmp_path: Path) -> None:
    doc = _doc(
        MarkerBlock(
            block_type="Figure",
            images={"img0": _PNG},
            caption="The Transformer architecture.",
            page=3,
        )
    )

    markdown, figures, _w = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert len(figures) == 1
    assert figures[0].id == "fig-001"
    assert figures[0].caption == "The Transformer architecture."
    assert figures[0].page == 3
    # The image is on disk, so the index entry is a promise that holds.
    assert (tmp_path / figures[0].image_path).is_file()
    # And it is referenced inline, where the surrounding prose explains it.
    assert "![fig-001]" in markdown
    assert "The Transformer architecture." in markdown


def test_a_figure_without_an_image_is_reported_not_indexed(tmp_path: Path) -> None:
    doc = _doc(MarkerBlock(block_type="Figure", caption="Missing pixels", images={}))

    _md, figures, warnings = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert figures == []
    assert any("Missing pixels" in w for w in warnings)


def test_undecodable_image_bytes_do_not_produce_an_index_entry(tmp_path: Path) -> None:
    doc = _doc(MarkerBlock(block_type="Figure", caption="Corrupt", images={"i": "!!!not b64"}))

    _md, figures, warnings = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert figures == []
    assert any("decode" in w for w in warnings)


def test_section_headers_become_markdown_headings(tmp_path: Path) -> None:
    doc = _doc(
        MarkerBlock(block_type="SectionHeader", html="<h1>Introduction</h1>"),
        MarkerBlock(block_type="Text", html="<p>We propose the Transformer.</p>"),
    )

    markdown, _f, _w = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert "## Introduction" in markdown
    assert "We propose the Transformer." in markdown


def test_heading_depth_follows_marker_hierarchy(tmp_path: Path) -> None:
    doc = _doc(
        MarkerBlock(
            block_type="SectionHeader",
            html="<h2>Attention</h2>",
            section_hierarchy={"1": "/page/0/SectionHeader/0", "2": "/page/1/SectionHeader/3"},
        )
    )

    markdown, _f, _w = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert "### Attention" in markdown


def test_running_heads_are_dropped(tmp_path: Path) -> None:
    # Page headers repeat on every page and interrupt the prose mid-sentence.
    doc = _doc(
        MarkerBlock(block_type="PageHeader", html="<p>Preprint. Under review.</p>"),
        MarkerBlock(block_type="Text", html="<p>Real content.</p>"),
        MarkerBlock(block_type="PageFooter", html="<p>12</p>"),
    )

    markdown, _f, _w = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert "Preprint" not in markdown
    assert "Real content." in markdown


def test_list_items_become_markdown_bullets(tmp_path: Path) -> None:
    doc = _doc(
        MarkerBlock(block_type="ListItem", html="<li>first</li>"),
        MarkerBlock(block_type="ListItem", html="<li>second</li>"),
    )

    markdown, _f, _w = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert "- first" in markdown
    assert "- second" in markdown


def test_html_entities_are_resolved(tmp_path: Path) -> None:
    doc = _doc(MarkerBlock(block_type="Text", html="<p>Wang &amp; Li &lt; 5%</p>"))

    markdown, _f, _w = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert "Wang & Li < 5%" in markdown


def test_an_unrenderable_table_is_flagged_not_flattened(tmp_path: Path) -> None:
    doc = _doc(MarkerBlock(block_type="Table", html="<table></table>"))

    markdown, _f, warnings = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert "table" in " ".join(warnings).lower()
    assert markdown == ""


def test_table_cells_do_not_reappear_as_a_text_blob(tmp_path: Path) -> None:
    """Marker emits a Table and then each of its cells as sibling blocks.

    Shape taken verbatim from a real extraction: `_flatten` appends a block
    and then recurses into its children, so every `TableCell` under a `Table`
    arrives again as its own record. Rendering those as loose text reproduces
    exactly the cell blob this module exists to prevent — the table is already
    rendered in full from the `Table` block's own html.

    Measured on arXiv:1706.03762: every table was followed by its own cell
    values as prose, and Table 2's stripped columns let "38.1" (an EN-FR BLEU
    score) sit under a "Training Cost (FLOPs)" heading.
    """
    doc = _doc(
        MarkerBlock(
            block_type="Table",
            html=(
                "<table><tbody><tr><th>Model</th><th>BLEU</th></tr>"
                "<tr><td>Base</td><td>27.3</td></tr>"
                "<tr><td>Big</td><td>28.4</td></tr></tbody></table>"
            ),
        ),
        MarkerBlock(block_type="TableCell", html="<th>Model</th>"),
        MarkerBlock(block_type="TableCell", html="<th>BLEU</th>"),
        MarkerBlock(block_type="TableCell", html="<td>Base</td>"),
        MarkerBlock(block_type="TableCell", html="<td>27.3</td>"),
        MarkerBlock(block_type="TableCell", html="<td>Big</td>"),
        MarkerBlock(block_type="TableCell", html="<td>28.4</td>"),
    )

    markdown, _figures, warnings = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert "| Base | 27.3 |" in markdown, "the table itself must still render"
    loose = [
        line.strip()
        for line in markdown.splitlines()
        if line.strip() and not line.strip().startswith("|")
    ]
    assert loose == [], f"cell values re-emitted as prose: {loose}"
    assert warnings == []


def test_a_consumed_figure_caption_is_not_repeated_as_prose(tmp_path: Path) -> None:
    """The Caption block stays in the list after its text is attached.

    The Marker service copies a sibling Caption's text onto the figure it
    labels, but leaves the Caption block in the stream, so the same sentence
    is rendered twice — once under the image, once as a loose paragraph.
    Seen on arXiv:1706.03762 for all six figures.
    """
    doc = _doc(
        MarkerBlock(
            block_type="Figure",
            images={"a": _PNG},
            caption="Figure 1: The Transformer - model architecture.",
        ),
        MarkerBlock(
            block_type="Caption",
            html="<p>Figure 1: The Transformer - model architecture.</p>",
        ),
    )

    markdown, figures, _w = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert figures[0].caption == "Figure 1: The Transformer - model architecture."
    assert markdown.count("The Transformer - model architecture.") == 1, markdown


def test_a_table_caption_is_still_kept(tmp_path: Path) -> None:
    # Only captions already consumed by a figure are redundant. A table's
    # caption is never attached to anything, so dropping it would strip the
    # label that says what the table below is.
    doc = _doc(
        MarkerBlock(block_type="Caption", html="<p>Table 1: Maximum path lengths.</p>"),
        MarkerBlock(
            block_type="Table",
            html="<table><tr><th>A</th></tr><tr><td>1</td></tr></table>",
        ),
    )

    markdown, _f, _w = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert "Table 1: Maximum path lengths." in markdown


def test_a_table_that_drops_cells_is_reported_not_silently_truncated(
    tmp_path: Path,
) -> None:
    """Suppressing TableCells must not hide data the render lost.

    Measured on arXiv:1706.03762 Table 2: Marker collapsed a five-column
    table with a two-row spanning header down to three columns, so every
    training-cost figure (1.0 · 10^20 …) vanished from the rendered table.
    Those values used to survive in the duplicate cell blob; once the blob is
    suppressed they exist nowhere, and the caller cannot tell a column is
    missing — "27.3 | 38.1" reads as complete.

    The cells arrive as their own blocks, so the count is knowable. Losing
    data silently is the one outcome this module must never produce.
    """
    doc = _doc(
        # renders only 4 of the 6 cells Marker actually found
        MarkerBlock(
            block_type="Table",
            html=(
                "<table><tbody><tr><th>Model</th><th>BLEU</th></tr>"
                "<tr><td>Base</td><td>27.3</td></tr></tbody></table>"
            ),
        ),
        MarkerBlock(block_type="TableCell", html="<th>Model</th>"),
        MarkerBlock(block_type="TableCell", html="<th>BLEU</th>"),
        MarkerBlock(block_type="TableCell", html="<th>Cost</th>"),
        MarkerBlock(block_type="TableCell", html="<td>Base</td>"),
        MarkerBlock(block_type="TableCell", html="<td>27.3</td>"),
        MarkerBlock(block_type="TableCell", html="<td>3.3e18</td>"),
    )

    markdown, _f, warnings = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert "| Base | 27.3 |" in markdown, "the table must still render"
    assert warnings, "dropped cells must be reported"
    joined = " ".join(warnings).lower()
    assert "cell" in joined
    assert "6" in joined and "4" in joined, f"counts should be named: {warnings}"


def test_dropped_cells_are_reported_even_when_a_block_interrupts(
    tmp_path: Path,
) -> None:
    """Cells do not always follow their Table immediately.

    Real order on arXiv:1706.03762 page 7 (Table 2):

        TableGroup -> Caption -> Table -> Reference -> TableCell x55

    A `Reference` sits between the table and its cells. Accounting that
    closes on the first non-TableCell block therefore counts zero cells and
    stays silent — which is exactly how Table 2's missing training-cost
    column went unreported.
    """
    doc = _doc(
        MarkerBlock(
            block_type="Table",
            html=(
                "<table><tbody><tr><th>Model</th><th>BLEU</th></tr>"
                "<tr><td>Base</td><td>27.3</td></tr></tbody></table>"
            ),
        ),
        MarkerBlock(block_type="Reference", html="<span id='ref'></span>"),
        MarkerBlock(block_type="TableCell", html="<th>Model</th>"),
        MarkerBlock(block_type="TableCell", html="<th>BLEU</th>"),
        MarkerBlock(block_type="TableCell", html="<th>Cost</th>"),
        MarkerBlock(block_type="TableCell", html="<td>Base</td>"),
        MarkerBlock(block_type="TableCell", html="<td>27.3</td>"),
        MarkerBlock(block_type="TableCell", html="<td>3.3e18</td>"),
    )

    _md, _f, warnings = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert warnings, "an interrupting block must not silence the accounting"
    assert "6" in " ".join(warnings) and "4" in " ".join(warnings)


def test_double_encoded_equation_entities_are_fully_resolved(tmp_path: Path) -> None:
    """`&amp;amp;` needs unescaping twice, and one pass leaves `&amp;`.

    Seen on arXiv:1706.03762: the MultiHead equation reached the agent as
    `\\text{MultiHead}(Q, K, V) &amp;= \\text{Concat}(...)` inside `$$…$$`,
    so the alignment operator of one of the paper's central equations was a
    literal HTML entity.
    """
    doc = _doc(
        MarkerBlock(
            block_type="Equation",
            latex=r"\begin{aligned} a &amp;amp;= b \end{aligned}",
        )
    )

    markdown, _f, _w = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert r"a &= b" in markdown
    assert "&amp;" not in markdown


def test_a_figure_contained_in_another_is_not_indexed_twice(tmp_path: Path) -> None:
    """A sub-panel and its parent figure both carry the parent's caption.

    Measured on arXiv:1706.03762 page 3, where Marker emitted the left half
    of Figure 2 *and* the whole of Figure 2. Both were indexed, and both took
    the caption "(left) Scaled Dot-Product Attention. (right) Multi-Head
    Attention" — so an agent asked for Multi-Head Attention could cite the
    crop, which does not contain it. An index entry that resolves to a real
    image of the wrong thing is worse than a missing one: nothing about it
    looks wrong.

    Containment is the signal, not the caption: the inner block sits wholly
    inside the outer one on the same page.
    """
    doc = _doc(
        # inner: the left panel only, emitted first
        MarkerBlock(
            block_type="Figure",
            images={"a": _PNG},
            caption="Figure 2: (left) Scaled Dot-Product. (right) Multi-Head.",
            page=3,
            bbox=[148.0, 65.0, 300.0, 243.0],
        ),
        # outer: the whole figure
        MarkerBlock(
            block_type="Figure",
            images={"b": _PNG},
            caption="Figure 2: (left) Scaled Dot-Product. (right) Multi-Head.",
            page=3,
            bbox=[148.0, 65.0, 470.0, 243.0],
        ),
    )

    markdown, figures, _w = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert len(figures) == 1, f"the crop must not be indexed: {figures}"
    assert figures[0].id == "fig-001"
    assert markdown.count("![fig-") == 1


def test_figures_side_by_side_are_both_kept(tmp_path: Path) -> None:
    # Only containment collapses an entry. Two genuinely separate figures on
    # one page must both survive, or a paper's second panel disappears.
    doc = _doc(
        MarkerBlock(block_type="Figure", images={"a": _PNG}, caption="A", page=3,
                    bbox=[100.0, 60.0, 240.0, 200.0]),
        MarkerBlock(block_type="Figure", images={"b": _PNG}, caption="B", page=3,
                    bbox=[260.0, 60.0, 400.0, 200.0]),
    )

    _md, figures, _w = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert [f.caption for f in figures] == ["A", "B"]


def test_figures_are_numbered_in_document_order(tmp_path: Path) -> None:
    doc = _doc(
        MarkerBlock(block_type="Figure", images={"a": _PNG}, caption="A"),
        MarkerBlock(block_type="Figure", images={"b": _PNG}, caption="B"),
    )

    _md, figures, _w = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert [f.id for f in figures] == ["fig-001", "fig-002"]
    assert [f.caption for f in figures] == ["A", "B"]


def test_group_wrappers_are_skipped_not_warned_about(tmp_path: Path) -> None:
    """Marker's TableGroup points at a Table block that arrives separately.

    Shape taken verbatim from a real extraction, where treating these as
    tables produced three "could not be rendered" warnings for tables that
    had in fact rendered perfectly. A false warning tells an agent to
    distrust good data.
    """
    doc = _doc(
        MarkerBlock(
            block_type="TableGroup",
            html="<content-ref src='/page/5/Table/0'></content-ref>",
        ),
        MarkerBlock(
            block_type="Table",
            html="<table><tr><th>A</th></tr><tr><td>1</td></tr></table>",
        ),
    )

    markdown, _figures, warnings = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert warnings == []
    assert "| A |" in markdown


def test_figure_group_wrappers_are_skipped(tmp_path: Path) -> None:
    doc = _doc(
        MarkerBlock(
            block_type="FigureGroup",
            html="<content-ref src='/page/2/Figure/0'></content-ref>",
            caption="wrapper",
        ),
        MarkerBlock(block_type="Figure", images={"a": _PNG}, caption="Real figure"),
    )

    _md, figures, warnings = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert [f.caption for f in figures] == ["Real figure"]
    assert warnings == []


def test_a_genuinely_broken_table_still_warns(tmp_path: Path) -> None:
    # The skip must not swallow real failures.
    doc = _doc(MarkerBlock(block_type="Table", html="<table></table>"))

    _md, _f, warnings = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert any("table" in w.lower() for w in warnings)


def test_cells_belonging_to_a_later_table_are_not_charged_to_this_one(
    tmp_path: Path,
) -> None:
    """Cell accounting must not accumulate across unrelated blocks.

    Closing the tally only when the next `Table` arrives let every TableCell
    in between accrue to the previous table. Measured on a real paper: a
    complete 2x5 table was reported as "rendered 10 of the 177 cells Marker
    found" — a false alarm on the one table in that document a reader could
    actually trust, while genuinely broken tables stayed silent.

    Prose ends a table's cell run. An interleaved Reference or Caption does
    not (real pages emit `Table -> Reference -> TableCell...`).
    """
    doc = _doc(
        MarkerBlock(
            block_type="Table",
            html="<table><tbody><tr><th>A</th><th>B</th></tr>"
                 "<tr><td>1</td><td>2</td></tr></tbody></table>",
        ),
        MarkerBlock(block_type="Reference", html="<span/>"),
        MarkerBlock(block_type="TableCell", html="<th>A</th>"),
        MarkerBlock(block_type="TableCell", html="<th>B</th>"),
        MarkerBlock(block_type="TableCell", html="<td>1</td>"),
        MarkerBlock(block_type="TableCell", html="<td>2</td>"),
        # Prose closes the run. Everything after belongs to something else.
        MarkerBlock(block_type="Text", html="<p>Discussion of the results.</p>"),
        *[MarkerBlock(block_type="TableCell", html="<td>x</td>") for _ in range(40)],
    )

    _md, _f, warnings = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert warnings == [], f"the complete table must not be flagged: {warnings}"


def test_a_padded_row_is_reported_as_misaligned(tmp_path: Path) -> None:
    """The dominant real failure preserves total cell count, so counting alone
    cannot see it — but the direction of the mismatch can.

    Marker emits a spanning row-label only on its first row. Later rows lose
    it, every value shifts one column left, and the renderer pads the row back
    out to the header width. Verified on SEDD Table 1, where "SEDD Absorb"
    landed under `Size` and its LAMBADA perplexity read 41.84 when the truth
    was 50.92 — a plausible number, silently wrong.

    Marker's own `TableCell` blocks are the ground truth for how many cells
    existed. Padding invents cells, so `rendered > found`. That direction is
    what distinguishes an invented blank from a genuinely empty cell, which is
    common and must not be flagged (Transformer Table 2 has real blanks where
    a model reports no EN-FR score).
    """
    doc = _doc(
        MarkerBlock(
            block_type="Table",
            html=(
                "<table><tbody>"
                "<tr><th>Size</th><th>Model</th><th>LAMBADA</th></tr>"
                "<tr><td>Small</td><td>GPT-2</td><td>45.04</td></tr>"
                "<tr><td>SEDD</td><td>50.92</td></tr>"
                "</tbody></table>"
            ),
        ),
        # Ground truth: 3 + 3 + 2 = 8 real cells. The render pads to 9.
        *[MarkerBlock(block_type="TableCell", html=f"<td>{i}</td>") for i in range(8)],
    )

    _md, _f, warnings = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    joined = " ".join(warnings)
    assert warnings, "a padded row must be reported"
    assert "align" in joined.lower(), joined
    # Names the table so a caller can act without reimplementing our internals.
    assert "Size" in joined or "LAMBADA" in joined, joined


def test_a_genuinely_empty_cell_is_not_reported_as_misaligned(tmp_path: Path) -> None:
    # Real tables have real blanks. Transformer Table 2 leaves EN-FR empty for
    # models that never reported it; flagging those would train a caller to
    # ignore the warning, which is how a guard becomes worse than none.
    doc = _doc(
        MarkerBlock(
            block_type="Table",
            html=(
                "<table><tbody>"
                "<tr><th>Model</th><th>EN-DE</th><th>EN-FR</th></tr>"
                "<tr><td>ByteNet</td><td>23.75</td><td></td></tr>"
                "</tbody></table>"
            ),
        ),
        # Marker saw the empty cell too, so nothing was invented.
        *[MarkerBlock(block_type="TableCell", html="<td/>") for _ in range(6)],
    )

    _md, _f, warnings = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert warnings == []


def test_a_fully_populated_table_is_not_flagged_as_misaligned(tmp_path: Path) -> None:
    # The guard must stay quiet on good tables, or it teaches callers to
    # ignore it — which is how a warning becomes worse than none at all.
    doc = _doc(
        MarkerBlock(
            block_type="Table",
            html=(
                "<table><tbody>"
                "<tr><th>Model</th><th>BLEU</th></tr>"
                "<tr><td>Base</td><td>27.3</td></tr>"
                "<tr><td>Big</td><td>28.4</td></tr>"
                "</tbody></table>"
            ),
        ),
    )

    _md, _f, warnings = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert warnings == []


def test_a_damaged_table_keeps_its_source_for_recovery(tmp_path: Path) -> None:
    """A warning without a recovery path is only half a feature.

    When the render drops or pads cells the caller is told the table is
    unreliable and then offered nothing better: the artifact zip holds the
    same degraded markdown, so the numbers are unrecoverable from anything
    the service provides. Marker's own html for that table still has them,
    and it costs a few kilobytes to keep.
    """
    doc = _doc(
        MarkerBlock(
            block_type="Table",
            html=(
                "<table><tbody>"
                "<tr><th>Size</th><th>Model</th><th>LAMBADA</th></tr>"
                "<tr><td>Small</td><td>GPT-2</td><td>45.04</td></tr>"
                "<tr><td>SEDD</td><td>50.92</td></tr>"
                "</tbody></table>"
            ),
        ),
        *[MarkerBlock(block_type="TableCell", html=f"<td>{i}</td>") for i in range(8)],
    )

    _md, _f, warnings = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    source = tmp_path / "tables" / "table-001.html"
    assert source.is_file(), "the damaged table's source was not kept"
    assert "50.92" in source.read_text(encoding="utf-8")
    # And the warning points at it, or the caller cannot find it.
    assert any("table-001.html" in w for w in warnings), warnings


def test_an_undamaged_table_does_not_litter_the_bundle(tmp_path: Path) -> None:
    # Keeping every table's html would double the zip for no reason. Only a
    # table the caller was told to distrust needs a fallback.
    doc = _doc(
        MarkerBlock(
            block_type="Table",
            html=(
                "<table><tbody><tr><th>Model</th><th>BLEU</th></tr>"
                "<tr><td>Base</td><td>27.3</td></tr></tbody></table>"
            ),
        ),
        *[MarkerBlock(block_type="TableCell", html=f"<td>{i}</td>") for i in range(4)],
    )

    _md, _f, warnings = marker_doc_to_bundle_parts(doc, asset_dir=tmp_path)

    assert warnings == []
    assert not (tmp_path / "tables").exists()
