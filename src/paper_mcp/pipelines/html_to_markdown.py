"""Render a Marker ``<table>`` HTML fragment as a real markdown table.

# Ported from PaperHub `backend/src/paperhub/pipelines/html_to_markdown.py` @ fd65834.
# Adapted: none of substance — this module already does exactly the job, and
# its reason for existing is the one that matters here.

An LLM reads the bundle's markdown, so a table has to survive as a table:
rows and columns intact, not flattened into a blob of cell text. PaperHub's
earlier `strip_html` path destroyed row/column structure and duplicated cells,
which is why this exists. Pure stdlib; no new dependency.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

_WS_RE = re.compile(r"\s+")


class _TableParser(HTMLParser):
    """Collect `<tr>` rows of cell text from a single `<table>`.

    `<br>` becomes a space; other inline tags are dropped but their text is
    kept. The first row containing any `<th>` is the header; otherwise the
    first row is treated as one.
    """

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self.header_row_index: int | None = None
        self._in_cell = False
        self._cur_cell: list[str] = []
        self._cur_row: list[str] | None = None
        self._row_has_th = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._cur_row = []
            self._row_has_th = False
        elif tag in ("td", "th"):
            self._in_cell = True
            self._cur_cell = []
            if tag == "th":
                self._row_has_th = True
        elif tag == "br" and self._in_cell:
            self._cur_cell.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br" and self._in_cell:
            self._cur_cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th"):
            if self._cur_row is not None:
                self._cur_row.append(_clean_cell("".join(self._cur_cell)))
            self._in_cell = False
            self._cur_cell = []
        elif tag == "tr":
            if self._cur_row:
                if self._row_has_th and self.header_row_index is None:
                    self.header_row_index = len(self.rows)
                self.rows.append(self._cur_row)
            self._cur_row = None

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cur_cell.append(data)


def _clean_cell(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def html_table_to_markdown(table_html: str) -> str:
    """Convert a `<table>` fragment to a markdown table.

    Tolerates missing `<thead>`/`<tbody>`, ragged rows (padded to the widest
    row's column count), empty cells, and `<br>`. Returns an empty
    string when the fragment has no rows, so a caller can fall back rather
    than emit a broken table.
    """
    parser = _TableParser()
    parser.feed(table_html or "")
    parser.close()

    rows = parser.rows
    if not rows:
        return ""

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

    lines = [
        "| " + " | ".join(fit(header)) + " |",
        "| " + " | ".join(["---"] * ncols) + " |",
    ]
    lines.extend(
        "| " + " | ".join(fit(row)) + " |"
        for index, row in enumerate(rows)
        if index != header_idx
    )
    return "\n".join(lines)
