"""Pixel-measured lettering; no Qt, model engines or application state."""

import math
import os
import re
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


# 禁则处理（避头尾）：这些字符不能出现在行首
KINSOKU_HEAD = "。、，．,」』）】》〉〕｝｠…‥！？!?：；:;ー〜～ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮ"
# 这些字符不能出现在行尾
KINSOKU_TAIL = "「『（【《〈〔｛｟"


def apply_kinsoku(lines, vertical):
    """把不该出现在行首/行尾的标点挪到相邻行（避头尾）。

    竖排的行是「一列从上到下」，行首＝列顶；横排的行是「一行从左到右」，
    行首＝行左。连续标点（如 」。」）要整体处理，否则只移走第一个，
    剩下的仍然顶在行首。
    """
    lines = [line for line in lines if line]
    if vertical:
        index = 1
        while index < len(lines):
            # 该列开头连续的禁则标点整体挂到上一列末尾。
            # 注意每个元素可能是 "..." 这种多字符 token，所以按元素挪，
            # 不能 join 成字符串后逐字处理。
            length = len(lines[index])
            moved = 0
            while moved < length and lines[index][moved] in KINSOKU_HEAD:
                moved += 1
            if moved:
                lines[index - 1] = lines[index - 1] + lines[index][:moved]
                lines[index] = lines[index][moved:]
                if not lines[index]:
                    lines.pop(index)
                    continue
            index += 1
        return lines
    for index in range(len(lines) - 1):
        # 开引号/开括号不能留在行尾，逐个推到下一行开头
        while lines[index] and lines[index][-1] in KINSOKU_TAIL:
            lines[index + 1] = lines[index][-1] + lines[index + 1]
            lines[index] = lines[index][:-1]
    return [line for line in lines if line]


DEFAULT_PUNCTUATION_RULES = [
    {"src": source, "dst": target, "regex": False}
    for source, target in [
        ("．．．", "..."),
        ("︙", "..."),
        ("⋮", "..."),
        ("…", "..."),
        ("‥", ".."),
        ("︱", "—"),
        ("︲", "—"),
        ("｜", "—"),
        ("│", "—"),
    ]
]


def normalize_punctuation(text, rules=None, errors=None):
    for rule in DEFAULT_PUNCTUATION_RULES if rules is None else rules:
        source, target = rule.get("src", ""), rule.get("dst", "")
        if source:
            try:
                text = (
                    re.sub(source, target, text)
                    if rule.get("regex")
                    else text.replace(source, target)
                )
            except (re.error, IndexError, TypeError) as error:
                if errors is not None:
                    errors.append(str(error))
    return text


def render_text(
    region, width=None, height=None, font_path=None, punctuation_rules=None
):
    rect, style = region["layout_rect"], region["style"]
    width = max(1, round(width if width is not None else rect["width"]))
    height = max(1, round(height if height is not None else rect["height"]))
    short_side = min(width, height)
    max_pad = max(1, short_side // 10) if short_side < 40 else max(0, (short_side - 1) // 2)
    padding = min(style["padding"], max_pad)
    available_w, available_h = max(1, width - 2 * padding), max(1, height - 2 * padding)
    path = font_path or style.get("font") or default_font()
    stroke = style["stroke_width"] + int(style["bold"])
    text = region.get("target", "").strip()
    invalid_rules = []
    if style.get("auto_punctuation"):
        text = normalize_punctuation(text, punctuation_rules, invalid_rules)
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
        tokens = lambda paragraph: (
            re.findall(r"\.{3}|.", paragraph)
            if vertical and style.get("auto_punctuation")
            else list(paragraph)
        )
        glyphs = {c for paragraph in text.split("\n") for c in tokens(paragraph)}
        boxes = {c: font.getbbox(c, stroke_width=stroke) for c in glyphs}
        cell_w = max([size] + [b[2] - b[0] for b in boxes.values()])
        cell_h = max([size] + [b[3] - b[1] for b in boxes.values()])
        advances = {c: max(1, font.getlength(c) + spacing) for c in boxes}
        lines = []
        limit = max(1, int(available_h // max(1, cell_h + spacing)))
        for paragraph in text.split("\n"):
            if not paragraph.strip():
                continue
            if vertical:
                paragraph = tokens(paragraph.strip())
                # 保持 token 列表（"..." 是一个元素），渲染阶段按元素取字形，
                # join 成字符串会让 "..." 被拆成三个 '.' 而 boxes 里没有。
                columns = [
                    paragraph[i : i + limit] for i in range(0, len(paragraph), limit)
                ] or [[]]
                lines.extend(apply_kinsoku(columns, True))
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
        if not vertical:
            # 横向断行结束后统一过一遍禁则：放在循环里会每个段落
            # 都把已排好的行重扫一次，多段落时白付 O(n²)。
            lines = apply_kinsoku(lines, False)
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
        low, high, best = 1, 512, 1
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
    rtl = style.get("vertical_rtl", True)
    for column, line in enumerate(lines):
        if vertical:
            x = (
                logical_w - cw - column * pitch
                if rtl
                else column * pitch
            )
            for i, char in enumerate(line):
                box = boxes[char]
                if style.get("auto_punctuation") and char in {
                    "...",
                    "—",
                    "―",
                    "ー",
                    "─",
                    "-",
                }:
                    glyph = Image.new(
                        "RGBA", (max(1, box[2] - box[0]), max(1, box[3] - box[1]))
                    )
                    ImageDraw.Draw(glyph).text((-box[0], -box[1]), char, **common)
                    glyph = glyph.rotate(90, expand=True)
                    glyph.thumbnail(
                        (max(1, round(cw)), max(1, round(ch))), Image.Resampling.LANCZOS
                    )
                    content.alpha_composite(
                        glyph,
                        (
                            round(x + (cw - glyph.width) / 2),
                            round(i * max(1, ch + spacing) + (ch - glyph.height) / 2),
                        ),
                    )
                else:
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
        "invalid_rules": invalid_rules,
        "font_missing": font_missing,
        "font_size": size,
        # 断行结果对外暴露：调用方（编辑器预览、质检、测试）不必再重算一遍
        # 对外统一成字符串：竖排内部按 token 列表排版，但调用方不该关心这事
        "lines": ["".join(line) if vertical else line for line in lines],
        "overflow": overflow and not style["autofit"],
        "below_minimum": style["autofit"] and (size < style["min_size"] or overflow),
    }
