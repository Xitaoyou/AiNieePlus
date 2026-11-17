"""Shared Pillow lettering and lossless layered erasure for preview and export."""

import math
import os
from pathlib import Path

import numpy as np
from PIL import Image, ImageColor

from Backend.application.pipeline_common import PipelineError


def font_files():
    from Backend.core.paths import backend_resource_path

    roots = [
        backend_resource_path("manga", "fonts"),
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
        Path("/usr/share/fonts"),
        Path.home() / "Library/Fonts",
        Path("/System/Library/Fonts"),
    ]
    return sorted(
        {
            str(p)
            for root in roots
            if root.exists()
            for p in root.rglob("*")
            if p.suffix.lower() in {".ttf", ".otf", ".ttc"}
        }
    )


def resolve_font(value=""):
    if value:
        if not Path(value).is_file() or Path(value).suffix.lower() not in {
            ".otf",
            ".ttf",
            ".ttc",
        }:
            raise PipelineError("FONT_MISSING", "所选字体不存在，请重新选择", 409)
        return value
    fonts = font_files()
    for name in (
        "NotoSansCJKjp-Regular.otf",
        "NotoSansCJK-Regular.ttc",
        "msyh.ttc",
        "PingFang.ttc",
        "DejaVuSans.ttf",
    ):
        found = next((p for p in fonts if Path(p).name == name), None)
        if found:
            return found
    raise PipelineError("FONT_MISSING", "没有可用字体，请安装核心字体包", 409)


def text_mask(image, page):
    import cv2

    rgb = np.asarray(image.convert("RGB"))
    union = np.zeros(rgb.shape[:2], np.uint8)
    for region in page.regions:
        if region.removal != "precise":
            continue
        r = region.erase_rect
        x, y, right, bottom = (
            int(r.x),
            int(r.y),
            math.ceil(r.x + r.width),
            math.ceil(r.y + r.height),
        )
        patch = cv2.cvtColor(rgb[y:bottom, x:right], cv2.COLOR_RGB2GRAY)
        if min(patch.shape, default=0) < 3:
            continue
        # Preserve the inferred background rather than inpainting an entire text rectangle.
        inverted = np.median(patch) > 127
        _, binary = cv2.threshold(
            patch,
            0,
            255,
            (cv2.THRESH_BINARY_INV if inverted else cv2.THRESH_BINARY)
            | cv2.THRESH_OTSU,
        )
        count, labels, stats, _ = cv2.connectedComponentsWithStats(binary)
        filtered = np.zeros_like(binary)
        for n in range(1, count):
            cx, cy, w, h, area = stats[n]
            if area < 2 or area > patch.size * 0.3:
                continue
            if (w > patch.shape[1] * 0.9 and h < 4) or (
                h > patch.shape[0] * 0.9 and w < 4
            ):
                continue
            filtered[labels == n] = 255
        filtered = cv2.dilate(filtered, np.ones((3, 3), np.uint8))
        union[y:bottom, x:right] |= filtered
        # Strokes are global page coordinates and override automatic glyph selection.
        for stroke in region.mask_strokes:
            color = 0 if stroke.erase else 255
            points = [(round(px), round(py)) for px, py in stroke.points]
            for point in points:
                cv2.circle(union, point, stroke.radius, color, -1)
            for a, b in zip(points, points[1:]):
                cv2.line(union, a, b, color, stroke.radius * 2)
    return union


def clean_page(source, page, models):
    import cv2

    from Backend.infrastructure.manga.engines import session

    original = np.asarray(source.convert("RGB")).copy()
    result = original.copy()
    mask = text_mask(source, page)
    if mask.any():
        model = models.folder("lama") / "model.onnx"
        engine = session(str(model))
        merged = cv2.dilate(mask, np.ones((31, 31), np.uint8))
        count, _, stats, _ = cv2.connectedComponentsWithStats(merged)
        for n in range(1, count):
            x, y, w, h, _ = stats[n]
            x, y, right, bottom = (
                max(0, x - 32),
                max(0, y - 32),
                min(result.shape[1], x + w + 32),
                min(result.shape[0], y + h + 32),
            )
            crop, hole = original[y:bottom, x:right], mask[y:bottom, x:right]
            ih, iw = crop.shape[:2]
            tensor = (
                cv2.resize(crop, (512, 512)).transpose(2, 0, 1)[None].astype(np.float32)
                / 255
            )
            hole_tensor = (
                cv2.resize(hole, (512, 512), interpolation=cv2.INTER_NEAREST) > 0
            ).astype(np.float32)[None, None]
            inputs = engine.get_inputs()
            output = engine.run(
                None, {inputs[0].name: tensor, inputs[1].name: hole_tensor}
            )[0]
            output = np.asarray(output)
            if output.shape[1] == 3:
                output = output[0].transpose(1, 2, 0)
            else:
                output = output[0]
            if output.max() <= 1.5:
                output = output * 255
            repaired = cv2.resize(np.clip(output, 0, 255).astype(np.uint8), (iw, ih))
            result[y:bottom, x:right] = np.where(
                hole[..., None] > 0, repaired, result[y:bottom, x:right]
            )
    for region in page.regions:
        if region.removal != "fill":
            continue
        r = region.erase_rect
        x, y, right, bottom = (
            int(r.x),
            int(r.y),
            math.ceil(r.x + r.width),
            math.ceil(r.y + r.height),
        )
        patch = original[y:bottom, x:right]
        if not patch.size:
            continue
        if region.fill_color == "auto":
            border = np.concatenate([patch[0], patch[-1], patch[:, 0], patch[:, -1]])
            color = np.median(border, axis=0)
        else:
            color = np.array(ImageColor.getrgb(region.fill_color))
        result[y:bottom, x:right] = (
            result[y:bottom, x:right] * (1 - region.fill_opacity)
            + color * region.fill_opacity
        ).astype(np.uint8)
    return Image.fromarray(result), Image.fromarray(mask)


def lettering(page, canvas_size, punctuation_rules=None):
    from Shared.manga_lettering import render_text

    layer = Image.new("RGBA", canvas_size)
    warnings = []
    for region in sorted(page.regions, key=lambda r: r.order):
        if not region.target:
            continue
        tile, info = render_text(
            region.model_dump(),
            font_path=resolve_font(region.style.font),
            punctuation_rules=punctuation_rules,
        )
        if info.get("invalid_rules"):
            warnings.append(
                {
                    "region_id": region.id,
                    "code": "INVALID_REPLACEMENT_RULE",
                    "message": "公共替换表存在无效正则，已跳过，请修正规则",
                }
            )
        if info["overflow"] or info["below_minimum"]:
            warnings.append(
                {
                    "region_id": region.id,
                    "code": "TEXT_OVERFLOW" if info["overflow"] else "TEXT_SMALL",
                    "message": "固定字号超出框，请启用自动适应"
                    if info["overflow"]
                    else f"文字已适应框，实际字号 {info['font_size']}；建议扩大文字框",
                }
            )
        layer.alpha_composite(
            tile, (round(region.layout_rect.x), round(region.layout_rect.y))
        )
    return layer, warnings


def render_page(source, page, models):
    clean, mask = clean_page(source, page, models)
    layer, warnings = lettering(page, source.size)
    composite = Image.alpha_composite(clean.convert("RGBA"), layer).convert("RGB")
    return {"clean": clean, "mask": mask, "layer": layer, "render": composite}, warnings


def materialize(
    page,
    source_path,
    directory,
    revision,
    models,
    background_key=None,
    background_only=False,
    punctuation_rules=None,
):
    """Runs only in the spawned model worker, including LaMa and Pillow fonts."""
    import hashlib
    import json
    import os
    from uuid import uuid4

    def write(image, path):
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        try:
            image.save(temporary, format="PNG")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    key = hashlib.sha256(
        json.dumps(
            {
                "sha": page.sha256,
                "regions": [
                    {
                        k: r.model_dump()[k]
                        for k in (
                            "erase_rect",
                            "removal",
                            "fill_color",
                            "fill_opacity",
                            "mask_strokes",
                        )
                    }
                    for r in page.regions
                ],
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    cached = directory / f"{page.id}.{key}.clean-cache.png"
    cached_mask = directory / f"{page.id}.{key}.mask-cache.png"
    with Image.open(source_path) as image:
        image = image.convert("RGB")
        if cached.exists() and cached_mask.exists():
            with Image.open(cached) as f:
                clean = f.copy()
            with Image.open(cached_mask) as f:
                mask = f.copy()
        else:
            clean, mask = clean_page(image, page, models)
            write(clean, cached)
            write(mask, cached_mask)
        if background_only:
            output_key = background_key or revision
            write(clean, directory / f"{page.id}.{output_key}.clean.png")
            write(mask, directory / f"{page.id}.{output_key}.mask.png")
            return []
        layer, warnings = lettering(page, image.size, punctuation_rules)
        rendered = Image.alpha_composite(clean.convert("RGBA"), layer).convert("RGB")
        for name, result in {
            "render": rendered,
            "clean": clean,
            "mask": mask,
            "layer": layer,
        }.items():
            output_key = (
                background_key
                if background_key and name in {"clean", "mask"}
                else revision
            )
            write(result, directory / f"{page.id}.{output_key}.{name}.png")
    return warnings
