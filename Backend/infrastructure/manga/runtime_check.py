"""Offline release smoke check: load every core inference graph and font."""

import hashlib
import json
from PIL import ImageFont
from Backend.infrastructure.manga.models import ModelRegistry, CATALOGUE
from Backend.infrastructure.manga.engines import session


def check_runtime(root):
    from importlib import import_module

    import_module("rapidocr")
    registry = ModelRegistry(root=root / "models", bundled=root / "models")
    result = {}
    for spec in CATALOGUE:
        if not spec["core"]:
            continue
        folder = registry.verify(spec["id"])
        registry.validate_layout(spec["id"], folder)
        for name in spec["files"]:
            if name.endswith(".onnx"):
                engine = session(str(folder / name))
                result[spec["id"] + "/" + name] = [i.name for i in engine.get_inputs()]
    font = root / "fonts/NotoSansCJKjp-Regular.otf"
    expected = json.loads((root / "bundle.json").read_text(encoding="utf-8"))[
        "font_sha256"
    ]
    if hashlib.sha256(font.read_bytes()).hexdigest() != expected:
        raise RuntimeError("Font checksum failed")
    ImageFont.truetype(str(font), 28)
    return result
