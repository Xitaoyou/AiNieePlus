"""Pixel-measured lettering; no Qt, model engines or application state."""

import math
import os
import sys
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def default_font():
    relative = Path("Backend/resources/manga/fonts/NotoSansCJKjp-Regular.otf")
    for path in (
        Path(__file__).resolve().parents[1] / relative,
        Path(sys.executable).parent / "backend/_internal" / relative,
        Path(sys.executable).parent.parent
        / "Helpers/AiNieeBackend.app/Contents/Resources"
        / relative,
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ):
        if path.is_file():
            return str(path)
    return ""


@lru_cache(maxsize=24)
def font_at(path, size):
    return ImageFont.truetype(path, size) if path else ImageFont.load_default(size=size)


def render_text(region, width=None, height=None, font_path=None):
    rect, style = region["layout_rect"], region["style"]
    width = max(1, round(width if width is not None else rect["width"]))
    height = max(1, round(height if height is not None else rect["height"]))
    padding = min(style["padding"], max(0, (min(width, height) - 1) // 2))
    available_w, available_h = max(1, width - 2 * padding), max(1, height - 2 * padding)
    path = font_path or style.get("font") or default_font()
    stroke = style["stroke_width"] + int(style["bold"])
    text = region.get("target", "")
    spacing = style["char_spacing"]
    vertical = style["vertical"]
    font_missing = False

    def arrange(size):
        nonlocal path, font_missing
        try:
            font = font_at(path, size)
        except OSError:
            path, font_missing = default_font(), True
            font = font_at(path, size)
        boxes = {
            c: font.getbbox(c, stroke_width=stroke) for c in set(text) if c != "\n"
        }
        cell_w = max([size] + [b[2] - b[0] for b in boxes.values()])
        cell_h = max([size] + [b[3] - b[1] for b in boxes.values()])
        advances = {c: max(1, font.getlength(c) + spacing) for c in boxes}
        lines = []
        limit = max(1, int(available_h // max(1, cell_h + spacing)))
        for paragraph in text.split("\n"):
            if vertical:
                lines.extend(
                    [paragraph[i : i + limit] for i in range(0, len(paragraph), limit)]
                    or [""]
                )
            else:
                line, length = "", 0
                for char in paragraph:
                    advance = advances[char]
                    if line and length + advance + 2 * stroke > available_w:
                        if (
                            char in "。，、！？：；）】》」』…,.!?;:)]}"
                            and len(line) > 1
                            and advances[line[-1]] + advance + 2 * stroke <= available_w
                        ):
                            carry = line[-1]
                            lines.append(line[:-1])
                            line, length = carry, advances[carry]
                        else:
                            lines.append(line)
                            line, length = "", 0
                    line += char
                    length += advance
                lines.append(line)
        if vertical:
            pitch = max(cell_w, cell_w * style["line_spacing"])
            tw = cell_w + max(0, len(lines) - 1) * pitch
            th = max(
                [0]
                + [
                    cell_h + max(0, len(line) - 1) * max(1, cell_h + spacing)
                    for line in lines
                ]
            )
        else:
            pitch = max(cell_h, cell_h * style["line_spacing"])
            tw = max(
                [0]
                + [
                    sum(advances[c] for c in line) - spacing + 2 * stroke
                    for line in lines
                ]
            )
            th = cell_h + max(0, len(lines) - 1) * pitch
        return font, boxes, advances, cell_w, cell_h, pitch, lines, tw, th

    size = style["size"]
    if style["autofit"]:
        low, high, best = 1, size, 1
        while low <= high:
            candidate = (low + high) // 2
            *_, tw, th = arrange(candidate)
            if tw <= available_w and th <= available_h:
                best, low = candidate, candidate + 1
            else:
                high = candidate - 1
        size = best
    font, boxes, advances, cw, ch, pitch, lines, tw, th = arrange(size)
    overflow = tw > available_w or th > available_h
    # Extreme tiny frames still contain all text; warn that it is too small.
    logical_w = max(available_w, math.ceil(tw)) if style["autofit"] else available_w
    logical_h = max(available_h, math.ceil(th)) if style["autofit"] else available_h
    content = Image.new("RGBA", (max(1, logical_w), max(1, logical_h)))
    draw = ImageDraw.Draw(content)
    common = dict(
        font=font,
        fill=style["color"],
        stroke_width=stroke,
        stroke_fill=style["stroke_color"] if style["stroke_width"] else style["color"],
    )
    for column, line in enumerate(lines):
        if vertical:
            x = logical_w - cw - column * pitch
            for i, char in enumerate(line):
                box = boxes[char]
                draw.text(
                    (
                        x - box[0] + (cw - (box[2] - box[0])) / 2,
                        i * max(1, ch + spacing) - box[1],
                    ),
                    char,
                    **common,
                )
        else:
            length = (
                sum(advances[c] for c in line) - spacing + 2 * stroke if line else 0
            )
            x = (
                0
                if style["align"] == "left"
                else (logical_w - length) / 2
                if style["align"] == "center"
                else logical_w - length
            )
            for char in line:
                box = boxes[char]
                draw.text(
                    (x + stroke, (logical_h - th) / 2 + column * pitch - box[1]),
                    char,
                    **common,
                )
                x += advances[char]
    if style["autofit"] and overflow:
        content.thumbnail((available_w, available_h), Image.Resampling.LANCZOS)
    if rect.get("rotation"):
        content = content.rotate(
            -rect["rotation"], Image.Resampling.BICUBIC, expand=True
        )
        if style["autofit"]:
            content.thumbnail((available_w, available_h), Image.Resampling.LANCZOS)
    tile = Image.new("RGBA", (width, height))
    tile.alpha_composite(
        content, ((width - content.width) // 2, (height - content.height) // 2)
    )
    return tile, {
        "font_missing": font_missing,
        "font_size": size,
        "overflow": overflow and not style["autofit"],
        "below_minimum": style["autofit"] and (size < style["min_size"] or overflow),
    }
