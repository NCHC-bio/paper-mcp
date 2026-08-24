"""Table rendering, which is the README's first guarantee.

This module had no test file, which is how a two-level header shipped
truncating every data row to the header's width — Table 2 of arXiv
1706.03762 lost its entire training-cost column, and a BLEU score moved
under the cost heading where an agent would read it as a FLOP count.
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
