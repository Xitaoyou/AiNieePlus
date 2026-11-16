"""Exercise real bundled CPU models, independent of Java and remote providers."""

import argparse
from pathlib import Path
import json
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from Backend.application.manga_store import MangaStore
from Backend.application.manga_service import MangaService
from Backend.domain.models.manga import Region, Rect
from Backend.infrastructure.manga.models import ModelRegistry
from Backend.infrastructure.manga.engines import detect, recognize
from Backend.infrastructure.manga.render import clean_page, resolve_font


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("build/manga-smoke"))
    parser.add_argument("--legacy", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    models = ModelRegistry()
    image = Image.new("RGB", (640, 800), "#eeeeee")
    draw = ImageDraw.Draw(image)
    draw.rectangle((15, 15, 625, 785), outline="black", width=5)
    draw.ellipse((320, 70, 610, 340), fill="white", outline="black", width=3)
    draw.text(
        (365, 140),
        "こんにちは\n世界",
        font=ImageFont.truetype(resolve_font(), 32),
        fill="black",
        spacing=8,
    )
    draw.ellipse((40, 390, 330, 660), fill="white", outline="black", width=3)
    draw.text(
        (85, 470),
        "你好 世界",
        font=ImageFont.truetype(resolve_font(), 32),
        fill="black",
    )
    source = args.output / "source.png"
    image.save(source)
    found = detect(image, models.folder("manga-panel"), "manga-panel", 0.15)
    japanese = recognize(
        image.crop((355, 130, 570, 240)), models.folder("mangaocr"), "mangaocr"
    )
    chinese = recognize(
        image.crop((70, 450, 290, 540)), models.folder("rapidocr"), "rapidocr"
    )
    store = MangaStore(args.output / "projects")
    doc = store.import_images([str(source)], {"auto_recognize": False})
    rect = Rect(x=350, y=125, width=235, height=125)
    doc.pages[0].regions = [
        Region(
            ocr_rect=rect,
            erase_rect=rect,
            layout_rect=rect,
            source=japanese,
            target="你好，世界！",
            removal="precise",
        )
    ]
    doc = store.save(doc, doc.revision)
    clean, mask = clean_page(image, doc.pages[0], models)
    outside = np.asarray(mask) == 0
    assert np.array_equal(np.asarray(image)[outside], np.asarray(clean)[outside]), (
        "Inpaint changed unmasked pixels"
    )
    assert np.asarray(mask).any(), "No text mask generated"
    clean.save(args.output / "clean.png")
    mask.save(args.output / "mask.png")
    service = MangaService(store, models)
    # Run the detector in the real spawned worker too, without replacing our manual frame.
    from Backend.application.execution_context import ExecutionContext

    worker_boxes = service.compute(
        ExecutionContext(),
        operation="detect",
        image_path=str(source),
        folder=str(models.folder("manga-panel")),
        model_id="manga-panel",
        confidence=0.15,
    )
    rendered = service.run(doc.id, "render")
    assert not rendered["report"]["errors"], rendered
    exported = service.export(doc.id, str(args.output / "export"), "png")
    report = {
        "manga_panel_boxes": len(found),
        "spawned_worker_boxes": len(worker_boxes),
        "mangaocr": japanese,
        "rapidocr": chinese,
        "unmasked_pixels_unchanged": True,
        "export": exported,
    }
    if args.legacy:
        report["legacy_boxes"] = len(
            detect(image, args.legacy, "legacy-balloon-yolov4", 0.15)
        )
    service.close()
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
