"""Generates the static architecture diagram PNG.

Draws the current implemented pipeline topology of the Tel Aviv Real Estate
Listing Triage Agent using Pillow only (no matplotlib dependency). Every box
is labeled with its *exact* frozen module/tool name where one exists, since
`GET /api/model_architecture` serves this image byte-for-byte and those
names must be readable in it and consistent with `/api/execute`'s public
`{module, prompt, response}` step shape.

Run manually whenever the architecture changes:
    python scripts/generate_architecture_image.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

# Allow running as a plain script (`python scripts/generate_architecture_image.py`)
# from the repo root, since it imports the `src.listing_agent` package for the
# module-name constants below.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from src.listing_agent.agent.module_names import (  # noqa: E402
    AGENT_ORCHESTRATOR,
    CONTEXT_REUSE,
    EVIDENCE_BUNDLE_BUILDER,
    EVIDENCE_COLLECTOR,
    GEO_RESOLVER,
    NEARBY_SERVICES,
    PLAN_VALIDATOR,
    PRICE_SIGNAL,
    REPORT_WRITER,
    TRIAGE_PLANNER,
)

# Internal helper modules that are part of the implemented pipeline but are
# never surfaced as a `StepTrace.step` value (see `module_names.py`). Named
# here to match their actual package names (`agent.tool_executor`,
# `agent.evidence_mapper`) rather than inventing friendlier labels.
TOOL_EXECUTOR = "tool_executor"
EVIDENCE_MAPPER = "evidence_mapper"

OUTPUT_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "listing_agent"
    / "static"
    / "model_architecture.png"
)

WIDTH, HEIGHT = 1500, 1080
BACKGROUND = "white"
BOX_BORDER = "black"

LLM_FILL = "#dbe9ff"  # light blue: LLM modules
DETERMINISTIC_FILL = "#e2f0d9"  # light green: deterministic modules
TOOL_FILL = "#fde9d9"  # light orange: tools
ENTRY_FILL = "#f2d7f2"  # light purple: entry point / orchestrator
OUTPUT_FILL = "#fff2b2"  # light yellow: final buyer-facing output
NOTE_FILL = "#f5f5f5"  # light gray: side notes (not pipeline steps)

BoxSpec = tuple[str, int, int, int, int, str]

# (label, center_x, center_y, width, height, fill)
MAIN_COL_X = 750

BOXES: list[BoxSpec] = [
    # Entry: GUI -> FastAPI -> orchestrator
    ("GUI / User", 190, 90, 220, 60, ENTRY_FILL),
    ("FastAPI\nPOST /api/execute", 470, 90, 260, 60, ENTRY_FILL),
    (AGENT_ORCHESTRATOR, 750, 90, 240, 60, ENTRY_FILL),
    # Two LLM calls + deterministic pipeline, single vertical column
    (f"Triage Planner\n{TRIAGE_PLANNER} \u00b7 LLM #1", MAIN_COL_X, 200, 300, 70, LLM_FILL),
    (PLAN_VALIDATOR, MAIN_COL_X, 300, 240, 55, DETERMINISTIC_FILL),
    (EVIDENCE_COLLECTOR, MAIN_COL_X, 390, 240, 55, DETERMINISTIC_FILL),
    (TOOL_EXECUTOR, MAIN_COL_X, 480, 240, 55, DETERMINISTIC_FILL),
    (GEO_RESOLVER, 320, 575, 220, 55, TOOL_FILL),
    (NEARBY_SERVICES, 750, 575, 220, 55, TOOL_FILL),
    (PRICE_SIGNAL, 980, 575, 220, 55, TOOL_FILL),
    (EVIDENCE_MAPPER, MAIN_COL_X, 670, 240, 55, DETERMINISTIC_FILL),
    (f"{EVIDENCE_BUNDLE_BUILDER}\nEvidenceBundle", MAIN_COL_X, 762, 280, 65, DETERMINISTIC_FILL),
    (f"Report Writer\n{REPORT_WRITER} \u00b7 LLM #2", MAIN_COL_X, 862, 300, 70, LLM_FILL),
    ("Buyer-facing report\n->  POST /api/execute response", MAIN_COL_X, 960, 380, 65, OUTPUT_FILL),
]

# Straight arrows between box labels, in the order the pipeline executes.
ARROWS: list[tuple[str, str]] = [
    ("GUI / User", "FastAPI\nPOST /api/execute"),
    ("FastAPI\nPOST /api/execute", AGENT_ORCHESTRATOR),
    (AGENT_ORCHESTRATOR, f"Triage Planner\n{TRIAGE_PLANNER} \u00b7 LLM #1"),
    (f"Triage Planner\n{TRIAGE_PLANNER} \u00b7 LLM #1", PLAN_VALIDATOR),
    (PLAN_VALIDATOR, EVIDENCE_COLLECTOR),
    (EVIDENCE_COLLECTOR, TOOL_EXECUTOR),
    (TOOL_EXECUTOR, GEO_RESOLVER),
    (TOOL_EXECUTOR, NEARBY_SERVICES),
    (TOOL_EXECUTOR, PRICE_SIGNAL),
    (GEO_RESOLVER, EVIDENCE_MAPPER),
    (NEARBY_SERVICES, EVIDENCE_MAPPER),
    (PRICE_SIGNAL, EVIDENCE_MAPPER),
    (EVIDENCE_MAPPER, f"{EVIDENCE_BUNDLE_BUILDER}\nEvidenceBundle"),
    (f"{EVIDENCE_BUNDLE_BUILDER}\nEvidenceBundle", f"Report Writer\n{REPORT_WRITER} \u00b7 LLM #2"),
    (
        f"Report Writer\n{REPORT_WRITER} \u00b7 LLM #2",
        "Buyer-facing report\n->  POST /api/execute response",
    ),
]

# Side notes: contextual information that is not itself a pipeline step.
# Drawn with a dashed border to visually distinguish them from the main flow.
NoteSpec = tuple[str, int, int, int, int]

CONTEXT_NOTE_LABEL = (
    f"Conversation / follow-up context\n"
    f"optional \u00b7 {CONTEXT_REUSE} step\n"
    "reuses property facts + cached tool results\n"
    "Process-local in-memory context only\n"
    "(not a database, not durable across instances)"
)

NOTES: list[NoteSpec] = [
    (CONTEXT_NOTE_LABEL, 1290, 260, 400, 120),
    (
        "Tool implementations\nmock  |  live\n(src/listing_agent/tools/*_implementations)",
        1290,
        460,
        360,
        80,
    ),
    (
        "Live upstream providers (TOOLS_MODE=live):\n"
        "geo_resolver, price_signal -> GovMap\n"
        "nearby_services -> Overpass\n"
        "No runtime snapshot fallback",
        1290,
        680,
        400,
        100,
    ),
    (
        "Other submission endpoints:\n"
        "/api/team_info\n/api/agent_info\n/api/model_architecture",
        1290,
        900,
        340,
        100,
    ),
]

# Dashed feedback arrow: optional context reuse feeding into evidence
# collection, never a new pipeline step ordering or a third LLM call.
CONTEXT_ARROW_TARGET = EVIDENCE_COLLECTOR


def _load_font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - older Pillow without the size kwarg
        return ImageFont.load_default()


def _box_bounds(spec: BoxSpec) -> tuple[int, int, int, int]:
    _, cx, cy, w, h, _ = spec
    return (cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2)


def _draw_multiline_text(
    draw: ImageDraw.ImageDraw,
    cx: int,
    cy: int,
    label: str,
    font: ImageFont.ImageFont,
    *,
    fill: str = "black",
) -> None:
    lines = label.split("\n")
    line_heights = []
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_heights.append(bbox[3] - bbox[1])
    line_gap = 4
    total_h = sum(line_heights) + line_gap * (len(lines) - 1)
    y = cy - total_h / 2
    for line, line_h in zip(lines, line_heights, strict=True):
        bbox = draw.textbbox((0, 0), line, font=font)
        text_w = bbox[2] - bbox[0]
        draw.text((cx - text_w / 2, y - bbox[1]), line, fill=fill, font=font)
        y += line_h + line_gap


def _draw_box(draw: ImageDraw.ImageDraw, spec: BoxSpec, font: ImageFont.ImageFont) -> None:
    label, cx, cy, _w, _h, fill = spec
    left, top, right, bottom = _box_bounds(spec)
    draw.rounded_rectangle(
        (left, top, right, bottom), radius=10, fill=fill, outline=BOX_BORDER, width=2
    )
    _draw_multiline_text(draw, cx, cy, label, font)


def _draw_note(draw: ImageDraw.ImageDraw, spec: NoteSpec, font: ImageFont.ImageFont) -> None:
    label, cx, cy, w, h = spec
    left, top, right, bottom = cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2
    draw.rounded_rectangle((left, top, right, bottom), radius=8, fill=NOTE_FILL, outline=None)
    _draw_dashed_rect(draw, (left, top, right, bottom), color="#666666")
    _draw_multiline_text(draw, cx, cy, label, font, fill="#333333")


def _draw_dashed_rect(
    draw: ImageDraw.ImageDraw, bounds: tuple[int, int, int, int], *, color: str
) -> None:
    left, top, right, bottom = bounds
    _draw_dashed_line(draw, (left, top), (right, top), color=color)
    _draw_dashed_line(draw, (right, top), (right, bottom), color=color)
    _draw_dashed_line(draw, (right, bottom), (left, bottom), color=color)
    _draw_dashed_line(draw, (left, bottom), (left, top), color=color)


def _rect_edge_point(
    cx: float, cy: float, w: float, h: float, tx: float, ty: float
) -> tuple[float, float]:
    """Point where the segment from (cx, cy) to (tx, ty) exits the box."""
    dx, dy = tx - cx, ty - cy
    if dx == 0 and dy == 0:
        return (cx, cy)
    scale_candidates = []
    if dx != 0:
        scale_candidates.append((w / 2) / abs(dx))
    if dy != 0:
        scale_candidates.append((h / 2) / abs(dy))
    scale = min(scale_candidates)
    return (cx + dx * scale, cy + dy * scale)


def _draw_arrow(
    draw: ImageDraw.ImageDraw,
    boxes_by_label: dict[str, BoxSpec],
    start_label: str,
    end_label: str,
    *,
    dashed: bool = False,
    color: str = "black",
) -> None:
    start_spec = boxes_by_label[start_label]
    end_spec = boxes_by_label[end_label]
    _, sx, sy, sw, sh, _ = start_spec
    _, ex, ey, ew, eh, _ = end_spec

    start_point = _rect_edge_point(sx, sy, sw, sh, ex, ey)
    end_point = _rect_edge_point(ex, ey, ew, eh, sx, sy)

    if dashed:
        _draw_dashed_line(draw, start_point, end_point, color=color)
    else:
        draw.line((*start_point, *end_point), fill=color, width=2)

    _draw_arrowhead(draw, start_point, end_point, color=color)


def _draw_dashed_line(
    draw: ImageDraw.ImageDraw,
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    color: str,
    dash_length: int = 8,
) -> None:
    (x0, y0), (x1, y1) = start, end
    length = max(((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5, 1)
    steps = max(int(length // dash_length), 1)
    for i in range(0, steps, 2):
        fx0 = x0 + (x1 - x0) * (i / steps)
        fy0 = y0 + (y1 - y0) * (i / steps)
        fx1 = x0 + (x1 - x0) * (min(i + 1, steps) / steps)
        fy1 = y0 + (y1 - y0) * (min(i + 1, steps) / steps)
        draw.line((fx0, fy0, fx1, fy1), fill=color, width=2)


def _draw_arrowhead(
    draw: ImageDraw.ImageDraw,
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    color: str,
    size: int = 8,
) -> None:
    (x0, y0), (x1, y1) = start, end
    angle = math.atan2(y1 - y0, x1 - x0)
    p1 = (
        x1 - size * math.cos(angle - math.pi / 6),
        y1 - size * math.sin(angle - math.pi / 6),
    )
    p2 = (
        x1 - size * math.cos(angle + math.pi / 6),
        y1 - size * math.sin(angle + math.pi / 6),
    )
    draw.polygon([(x1, y1), p1, p2], fill=color)


def generate(output_path: Path = OUTPUT_PATH) -> Path:
    """Render the architecture diagram and write it to `output_path`."""
    image = Image.new("RGB", (WIDTH, HEIGHT), color=BACKGROUND)
    draw = ImageDraw.Draw(image)

    title_font = _load_font(24)
    label_font = _load_font(15)
    note_font = _load_font(13)
    legend_font = _load_font(13)

    # Title sits in its own reserved band (y 0-45); the first row of boxes
    # is centered at y=90 (top edge 60) - well clear of the title text.
    draw.text(
        (40, 18),
        "Tel Aviv Real Estate Listing Triage Agent - Architecture",
        fill="black",
        font=title_font,
    )

    boxes_by_label = {spec[0]: spec for spec in BOXES}
    note_spec_by_label = {spec[0]: spec for spec in NOTES}

    for start_label, end_label in ARROWS:
        _draw_arrow(draw, boxes_by_label, start_label, end_label)

    # Dashed feedback arrow: the context/follow-up note can feed cached
    # results back into evidence collection.
    _, ncx, ncy, nw, nh = note_spec_by_label[CONTEXT_NOTE_LABEL]
    _, tcx, tcy, tw, th, _ = boxes_by_label[CONTEXT_ARROW_TARGET]
    start_point = _rect_edge_point(ncx, ncy, nw, nh, tcx, tcy)
    end_point = _rect_edge_point(tcx, tcy, tw, th, ncx, ncy)
    _draw_dashed_line(draw, start_point, end_point, color="#666666")
    _draw_arrowhead(draw, start_point, end_point, color="#666666")

    for spec in BOXES:
        _draw_box(draw, spec, label_font)

    for note_spec in NOTES:
        _draw_note(draw, note_spec, note_font)

    legend_y = HEIGHT - 45
    legend_items = [
        (LLM_FILL, "LLM module (2 calls total)"),
        (DETERMINISTIC_FILL, "Deterministic module"),
        (TOOL_FILL, "Tool"),
        (ENTRY_FILL, "Entry point / orchestrator"),
        (OUTPUT_FILL, "Final output"),
        (NOTE_FILL, "Side note (not a pipeline step)"),
    ]
    legend_x = 40
    for fill, text in legend_items:
        draw.rectangle(
            (legend_x, legend_y, legend_x + 18, legend_y + 18), fill=fill, outline="black"
        )
        draw.text((legend_x + 26, legend_y + 1), text, fill="black", font=legend_font)
        bbox = draw.textbbox((0, 0), text, font=legend_font)
        legend_x += (bbox[2] - bbox[0]) + 60

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG")
    return output_path


if __name__ == "__main__":
    saved_path = generate()
    print(f"Wrote architecture diagram to {saved_path}")
