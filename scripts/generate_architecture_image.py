"""Generates the static architecture diagram PNG.

Draws the fixed module/tool topology of the Tel Aviv Real Estate Listing
Triage Agent using Pillow only (no matplotlib dependency). Every box is
labeled with its *exact* frozen module/tool name, since `GET
/api/model_architecture` serves this image byte-for-byte and those names
must be readable in it.

Run manually whenever the architecture changes:
    python scripts/generate_architecture_image.py
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

# Allow running as a plain script (`python scripts/generate_architecture_image.py`)
# from the repo root, since it imports the `src.listing_agent` package for the
# Phase 16 module-name constants below.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from src.listing_agent.agent.module_names import (  # noqa: E402
    AGENT_ORCHESTRATOR,
    EVIDENCE_BUNDLE_BUILDER,
    EVIDENCE_COLLECTOR,
    GEO_RESOLVER,
    NEARBY_SERVICES,
    PLAN_VALIDATOR,
    PRICE_SIGNAL,
    REPORT_WRITER,
    STEP_TRACER,
    TRIAGE_PLANNER,
)

OUTPUT_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "listing_agent"
    / "static"
    / "model_architecture.png"
)

WIDTH, HEIGHT = 1400, 950
BACKGROUND = "white"
BOX_BORDER = "black"

LLM_FILL = "#dbe9ff"  # light blue: LLM modules
DETERMINISTIC_FILL = "#e2f0d9"  # light green: deterministic modules
TOOL_FILL = "#fde9d9"  # light orange: tools
ORCHESTRATOR_FILL = "#f2d7f2"  # light purple: orchestrator / tracer

BoxSpec = tuple[str, int, int, int, int, str]

# (label, center_x, center_y, width, height, fill)
# Labels are sourced from `agent.module_names` (Phase 16 single source of
# truth for every frozen module/tool name) rather than duplicated literals.
BOXES: list[BoxSpec] = [
    (AGENT_ORCHESTRATOR, 700, 60, 260, 60, ORCHESTRATOR_FILL),
    (TRIAGE_PLANNER, 700, 180, 220, 60, LLM_FILL),
    (PLAN_VALIDATOR, 700, 290, 220, 60, DETERMINISTIC_FILL),
    (EVIDENCE_COLLECTOR, 700, 400, 220, 60, DETERMINISTIC_FILL),
    (GEO_RESOLVER, 300, 530, 220, 60, TOOL_FILL),
    (NEARBY_SERVICES, 700, 530, 220, 60, TOOL_FILL),
    (PRICE_SIGNAL, 1100, 530, 220, 60, TOOL_FILL),
    (EVIDENCE_BUNDLE_BUILDER, 700, 660, 260, 60, DETERMINISTIC_FILL),
    (REPORT_WRITER, 700, 770, 220, 60, LLM_FILL),
    (STEP_TRACER, 1220, 290, 220, 70, ORCHESTRATOR_FILL),
]

# Straight arrows between box labels, in the order the pipeline executes.
ARROWS: list[tuple[str, str]] = [
    (AGENT_ORCHESTRATOR, TRIAGE_PLANNER),
    (TRIAGE_PLANNER, PLAN_VALIDATOR),
    (PLAN_VALIDATOR, EVIDENCE_COLLECTOR),
    (EVIDENCE_COLLECTOR, GEO_RESOLVER),
    (EVIDENCE_COLLECTOR, NEARBY_SERVICES),
    (EVIDENCE_COLLECTOR, PRICE_SIGNAL),
    (GEO_RESOLVER, EVIDENCE_BUNDLE_BUILDER),
    (NEARBY_SERVICES, EVIDENCE_BUNDLE_BUILDER),
    (PRICE_SIGNAL, EVIDENCE_BUNDLE_BUILDER),
    (EVIDENCE_BUNDLE_BUILDER, REPORT_WRITER),
]

# Dashed observational arrows: step_tracer records every completed step.
TRACER_ARROWS: list[str] = [
    TRIAGE_PLANNER,
    PLAN_VALIDATOR,
    EVIDENCE_COLLECTOR,
    EVIDENCE_BUNDLE_BUILDER,
    REPORT_WRITER,
]


def _load_font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - older Pillow without the size kwarg
        return ImageFont.load_default()


def _box_bounds(spec: BoxSpec) -> tuple[int, int, int, int]:
    _, cx, cy, w, h, _ = spec
    return (cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2)


def _draw_box(draw: ImageDraw.ImageDraw, spec: BoxSpec, font: ImageFont.ImageFont) -> None:
    label, cx, cy, _w, _h, fill = spec
    left, top, right, bottom = _box_bounds(spec)
    draw.rounded_rectangle(
        (left, top, right, bottom), radius=10, fill=fill, outline=BOX_BORDER, width=2
    )
    text_bbox = draw.textbbox((0, 0), label, font=font)
    text_w = text_bbox[2] - text_bbox[0]
    text_h = text_bbox[3] - text_bbox[1]
    draw.text(
        (cx - text_w / 2, cy - text_h / 2 - text_bbox[1]),
        label,
        fill="black",
        font=font,
    )


def _draw_arrow(
    draw: ImageDraw.ImageDraw,
    start_label: str,
    end_label: str,
    *,
    dashed: bool = False,
    color: str = "black",
) -> None:
    start_spec = next(spec for spec in BOXES if spec[0] == start_label)
    end_spec = next(spec for spec in BOXES if spec[0] == end_label)
    sx, sy = start_spec[1], start_spec[2]
    ex, ey = end_spec[1], end_spec[2]

    # Clip the line to just outside each box's border instead of its center.
    start_bottom = sy + start_spec[4] // 2
    end_top = ey - end_spec[4] // 2
    if ey > sy:  # end is below start: draw a simple vertical/diagonal drop
        y0, y1 = start_bottom, end_top
    else:
        y0, y1 = sy, ey

    if dashed:
        _draw_dashed_line(draw, (sx, y0), (ex, y1), color=color)
    else:
        draw.line((sx, y0, ex, y1), fill=color, width=2)

    _draw_arrowhead(draw, (sx, y0), (ex, y1), color=color)


def _draw_dashed_line(
    draw: ImageDraw.ImageDraw,
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    color: str,
    dash_length: int = 8,
) -> None:
    (x0, y0), (x1, y1) = start, end
    length = max(((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5, 1)
    steps = int(length // dash_length)
    for i in range(0, steps, 2):
        fx0 = x0 + (x1 - x0) * (i / steps)
        fy0 = y0 + (y1 - y0) * (i / steps)
        fx1 = x0 + (x1 - x0) * (min(i + 1, steps) / steps)
        fy1 = y0 + (y1 - y0) * (min(i + 1, steps) / steps)
        draw.line((fx0, fy0, fx1, fy1), fill=color, width=2)


def _draw_arrowhead(
    draw: ImageDraw.ImageDraw,
    start: tuple[int, int],
    end: tuple[int, int],
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

    title_font = _load_font(22)
    label_font = _load_font(15)
    legend_font = _load_font(13)

    draw.text(
        (40, 15),
        "Tel Aviv Real Estate Listing Triage Agent - Architecture",
        fill="black",
        font=title_font,
    )

    for start_label, end_label in ARROWS:
        _draw_arrow(draw, start_label, end_label)

    for tracer_target in TRACER_ARROWS:
        _draw_arrow(draw, STEP_TRACER, tracer_target, dashed=True, color="gray")

    for spec in BOXES:
        _draw_box(draw, spec, label_font)

    legend_y = HEIGHT - 60
    legend_items = [
        (LLM_FILL, "LLM module"),
        (DETERMINISTIC_FILL, "Deterministic module"),
        (TOOL_FILL, "Tool"),
        (ORCHESTRATOR_FILL, "Orchestrator / tracer"),
    ]
    legend_x = 40
    for fill, text in legend_items:
        draw.rectangle(
            (legend_x, legend_y, legend_x + 18, legend_y + 18), fill=fill, outline="black"
        )
        draw.text((legend_x + 26, legend_y + 1), text, fill="black", font=legend_font)
        legend_x += 260

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG")
    return output_path


if __name__ == "__main__":
    saved_path = generate()
    print(f"Wrote architecture diagram to {saved_path}")
