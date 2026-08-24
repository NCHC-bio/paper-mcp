"""Turn Marker blocks into the two things an agent needs: markdown + figures.

This is the whole product. Everything downstream of it — slides, summaries,
literature reviews — is a pipeline the calling agent builds from skills. This
service supplies data, so the only question that matters here is whether the
data is faithful.

Faithful means, concretely:

* tables stay tables (rows and columns), never a blob of cell text
* equations stay LaTeX, never prose approximations of maths
* figures are extracted, indexed, and captioned, never invented

# Block shapes ported from PaperHub `pipelines/marker_to_asset.py` @ fd65834.
# Adapted: emits one markdown document plus a figure index rather than
# PaperHub's PaperAsset, and renders tables through `html_table_to_markdown`
# instead of flattening them.
"""
from __future__ import annotations

import base64
import binascii
import html
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from paper_mcp.bundle import FigureRef
from paper_mcp.pipelines.html_to_markdown import html_table_to_markdown
from paper_mcp.pipelines.marker_client import MarkerBlock, MarkerDoc

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_BLANKS_RE = re.compile(r"\n{3,}")

# Entities can be encoded more than once; two passes covers what Marker emits
# and the bound stops a pathological input from spinning.
_MAX_UNESCAPE_PASSES = 5

# Image magic numbers, so a figure is saved with the extension it actually is
# rather than one we assumed.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),
)


def _image_ext(data: bytes) -> str:
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    return ".png"


def is_group_reference(fragment: str) -> bool:
    """True for a Marker *Group* wrapper that only points at other blocks.

    Marker emits `TableGroup` / `FigureGroup` blocks whose html is a
    `<content-ref src='/page/5/Table/0'>` pointing at the real block, which
    arrives separately. Treating one as a table produced three "table could
    not be rendered" warnings on a real paper whose tables had in fact
    rendered perfectly — a false alarm that tells an agent to distrust good
    data, which is worse than silence.
    """
    return "<content-ref" in fragment and "<table" not in fragment.lower()


def unescape_fully(text: str) -> str:
    """Resolve HTML entities until stable.

    Marker derives equation LaTeX from HTML that is itself already escaped, so
    an `&` in an `aligned` block arrives as `&amp;amp;`. A single unescape
    turns that into `&amp;`, which still reaches the agent as an entity inside
    `$$…$$` — the alignment operator of a central equation rendered as markup.
    `html.unescape` is idempotent once no entities remain, so this terminates.
    """
    for _ in range(_MAX_UNESCAPE_PASSES):
        resolved = html.unescape(text)
        if resolved == text:
            return text
        text = resolved
    return text


@dataclass
class _PendingTable:
    """Cell accounting for the table currently being closed over."""

    rendered: int
    found: int
    name: str
    html: str


# Blocks that legitimately interleave a table's own cell run. A real page ran
# `Table -> Reference -> TableCell x55`, so an interrupting block must not end
# the tally — but prose must, or cells from a later table accrue to this one
# and a complete 2x5 table gets reported as "10 of 177 cells found".
_CELL_RUN_KINDS = frozenset({"TableCell", "Reference", "Caption", "Footnote"})


def _keep_table_source(html: str, *, asset_dir: Path, index: int) -> str:
    """Write a damaged table's source html into the bundle; return its path."""
    tables_dir = asset_dir / "tables"
    tables_dir.mkdir(parents=True, exist_ok=True)
    name = f"table-{index:03d}.html"
    (tables_dir / name).write_text(html, encoding="utf-8")
    return f"tables/{name}"


def _table_name(table_markdown: str) -> str:
    """A table's header row, as a label a caller can locate it by.

    Warnings that name no table are unactionable: reading one meant
    reimplementing this module's private cell counting to work out which of
    thirteen tables was meant.
    """
    first = next((line for line in table_markdown.splitlines() if line.strip().startswith("|")), "")
    cells = [c.strip() for c in first.strip().strip("|").split("|") if c.strip()]
    return " | ".join(cells)[:60] if cells else "(unnamed)"


def _rendered_cell_count(table_markdown: str) -> int:
    """How many real cells a rendered markdown table contains."""
    total = 0
    for line in table_markdown.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(set(c) <= {"-", ":"} and c for c in cells):
            continue  # separator row
        total += len(cells)
    return total


_FIGURE_KINDS = ("Figure", "Picture", "FigureGroup")
# Layout boxes are approximate; a couple of points of slop keeps a panel that
# sits flush against its parent's edge from reading as "not contained".
_BBOX_SLOP = 2.0


def _area(bbox: list[float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _contains(outer: list[float], inner: list[float]) -> bool:
    return (
        outer[0] <= inner[0] + _BBOX_SLOP
        and outer[1] <= inner[1] + _BBOX_SLOP
        and outer[2] >= inner[2] - _BBOX_SLOP
        and outer[3] >= inner[3] - _BBOX_SLOP
    )


def suppressed_figures(blocks: list[MarkerBlock]) -> set[int]:
    """Indices of figure blocks wholly inside another figure on the same page.

    Marker can emit a sub-panel *and* the whole figure that contains it, and
    the service gives both the same caption because both take it from the one
    Caption block. Indexing both hands the agent two ids promising the same
    content, one of which is a crop missing half of it — a citation that
    resolves to a real image of the wrong thing. Keep the figure that holds
    the whole caption's subject: the outer one.
    """
    boxed = [
        (i, b)
        for i, b in enumerate(blocks)
        if b.block_type in _FIGURE_KINDS and b.images and len(b.bbox) == 4
    ]
    drop: set[int] = set()
    for i, inner in boxed:
        for j, outer in boxed:
            if i == j or inner.page != outer.page:
                continue
            if _contains(outer.bbox, inner.bbox) and _area(outer.bbox) > _area(inner.bbox):
                drop.add(i)
                break
    return drop


def strip_html(fragment: str) -> str:
    """Plain text from a non-table HTML fragment, entities resolved."""
    text = _TAG_RE.sub(" ", fragment or "")
    text = html.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _heading(block: MarkerBlock) -> str:
    """Render a section header, keeping Marker's own hierarchy depth.

    Depth comes from how deep the header sits in `section_hierarchy`; an agent
    reading the markdown gets the paper's real structure rather than a flat
    list of `##`.
    """
    name = strip_html(block.html)
    if not name:
        return ""
    level = min(len(block.section_hierarchy) + 1, 6) if block.section_hierarchy else 2
    return f"\n\n{'#' * level} {name}\n"


def marker_doc_to_bundle_parts(
    doc: MarkerDoc, *, asset_dir: Path
) -> tuple[str, list[FigureRef], list[str]]:
    """Render `(markdown, figures, warnings)` from a Marker document.

    Figure images are written under `asset_dir/figures/`. A figure enters the
    index only when its bytes decoded successfully — an index entry is a
    promise that the image exists.
    """
    figures_dir = asset_dir / "figures"
    parts: list[str] = []
    figures: list[FigureRef] = []
    warnings: list[str] = []
    # Captions already printed under a figure. The service attaches a sibling
    # Caption's text to the figure it labels but leaves the Caption block in
    # the stream, so without this the same sentence lands twice.
    consumed_captions: set[str] = set()
    # Cells the last rendered table produced, against the cells Marker
    # actually found. Suppressing TableCell blocks is only safe while the
    # rendered table still carries them; when the render collapses columns,
    # those blocks are the sole surviving copy and dropping them loses the
    # data outright. Compare, and say so.
    pending_table: _PendingTable | None = None
    kept_tables: list[str] = []

    def _close_pending_table() -> None:
        """Report a table whose rendered cell count disagrees with Marker's.

        Both directions matter, and they mean opposite things:

        * `found > rendered` — cells were dropped. The table is incomplete.
        * `rendered > found` — cells were *invented* as padding, which is the
          signature of a row that lost a spanning label and shifted left. The
          count is intact, so counting alone cannot see it; only the direction
          can. Measured on SEDD Table 1, where every value moved one column
          and a perplexity of 50.92 read as 41.84.

        With no `TableCell` blocks there is no ground truth, so nothing is
        claimed — silence beats a guess.
        """
        nonlocal pending_table
        if pending_table is None:
            return
        table = pending_table
        pending_table = None
        rendered, found, name = table.rendered, table.found, table.name
        if found == 0 or rendered == found:
            return

        # Keep the source only for a table the caller is being told to
        # distrust. Telling someone their numbers are unreliable and offering
        # no way to the real ones is half a feature; keeping every table's
        # html would double the zip for the tables that are fine.
        source = _keep_table_source(table.html, asset_dir=asset_dir, index=len(kept_tables) + 1)
        kept_tables.append(source)
        where = f" Marker's own html for it is in {source} inside the artifact zip."

        if found > rendered:
            warnings.append(
                f"table {name!r} rendered {rendered} of the {found} cells Marker "
                f"found; {found - rendered} were dropped — treat this table as "
                f"incomplete.{where}"
            )
        else:
            warnings.append(
                f"table {name!r} rendered {rendered} cells from the {found} Marker "
                f"found, so {rendered - found} were padded — a row lost a cell and "
                f"its values are probably shifted; treat the column alignment as "
                f"unreliable.{where}"
            )

    # Computed up front: a crop can arrive before the figure containing it,
    # so the decision cannot be made from the blocks seen so far.
    crops = suppressed_figures(doc.blocks)

    for position, block in enumerate(doc.blocks):
        kind = block.block_type

        # Anything that is not part of a table's own cell run ends the tally.
        if pending_table is not None and kind not in _CELL_RUN_KINDS:
            _close_pending_table()

        if position in crops:
            continue  # a panel of a figure indexed in full elsewhere

        # Group wrappers reference blocks that arrive separately; rendering
        # them would duplicate content and warn about tables that are fine.
        if is_group_reference(block.html):
            continue

        if kind == "SectionHeader":
            parts.append(_heading(block))
            continue

        if kind in ("Figure", "Picture", "FigureGroup"):
            caption = block.caption if block.caption is not None else strip_html(block.html)
            if not block.images:
                # Marker saw a figure but produced no image. Say so rather
                # than emit an index entry pointing at nothing.
                if caption:
                    warnings.append(f"figure without an extracted image: {caption[:80]}")
                continue
            raw = next(iter(block.images.values()))
            try:
                data = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError):
                warnings.append(f"figure image failed to decode: {caption[:80]}")
                continue
            index = len(figures) + 1
            fid = f"fig-{index:03d}"
            name = f"{fid}{_image_ext(data)}"
            figures_dir.mkdir(parents=True, exist_ok=True)
            (figures_dir / name).write_bytes(data)
            figures.append(
                FigureRef(
                    id=fid,
                    caption=caption,
                    page=block.page,
                    image_path=f"figures/{name}",
                )
            )
            # Reference the figure inline so the agent meets it in context,
            # where the surrounding prose explains it.
            parts.append(f"\n\n![{fid}](figures/{name})\n\n*{fid}: {caption}*\n")
            if caption:
                consumed_captions.add(caption)
            continue

        if kind in ("Table", "TableGroup"):
            # Cells belong to the most recent table, and other blocks can sit
            # between the two (a real page ran Table -> Reference -> cells),
            # so only a new table closes the previous one's accounting.
            _close_pending_table()
            table = html_table_to_markdown(block.html)
            if table:
                parts.append(f"\n\n{table}\n")
                pending_table = _PendingTable(
                    rendered=_rendered_cell_count(table),
                    found=0,
                    name=_table_name(table),
                    html=block.html,
                )
            else:
                # Falling back to stripped text would produce the cell-blob
                # this pipeline exists to avoid, so flag it instead.
                # Carry the shape that defeated the renderer: a bare "could
                # not render" is undiagnosable without another GPU run.
                head = re.sub(r"\s+", " ", block.html or "")[:200]
                warnings.append(f"table not rendered as markdown; html head: {head!r}")
            continue

        if kind == "TableCell":
            if pending_table is not None:
                pending_table.found += 1
            # Already rendered. Marker's flatten appends a block and then
            # recurses into its children, so every cell of the Table above
            # arrives again as its own record — and the Table's own html
            # already carries all of them. Emitting these as loose text
            # rebuilds, verbatim, the cell blob this module exists to prevent.
            # An unrenderable table warns (see above) rather than falling back
            # to its cells, so dropping them here loses nothing that is not
            # already reported.
            continue

        if kind == "Equation":
            # Marker derives `latex` from HTML, so comparisons arrive
            # entity-encoded: `\lambda &lt; \lambda'`. `strip_html` unescapes
            # for every other block type, and this branch skipped it, sending
            # the entity through to the agent inside `$$…$$`.
            latex = unescape_fully(block.latex or "").strip()
            if latex:
                parts.append(f"\n\n$$\n{latex}\n$$\n")
            else:
                text = strip_html(block.html)
                if text:
                    parts.append(f"\n\n{text}\n")
            continue

        if kind == "ListItem":
            text = strip_html(block.html)
            if text:
                parts.append(f"\n- {text}")
            continue

        if kind in ("PageHeader", "PageFooter"):
            continue  # running heads add nothing and interrupt the prose

        if kind in ("Caption", "Footnote") and strip_html(block.html) in consumed_captions:
            continue  # already printed under its figure

        text = strip_html(block.html)
        if text:
            parts.append(f"\n\n{text}\n")

    _close_pending_table()  # a table whose cells run to the end still reports

    markdown = _BLANKS_RE.sub("\n\n", "".join(parts)).strip()
    return markdown, figures, warnings
