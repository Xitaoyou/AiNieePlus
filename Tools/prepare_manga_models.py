"""Prepare redistributable public manga assets before building the full installer.

Usage: python -m Tools.prepare_manga_models --source /path/to/model-folder --download
No Java binaries are read or copied. The Scaled YOLOv4 legacy detector is bundled
from its public release; private third-party weights stay out.
"""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import requests

from Backend.application.manga_store import atomic_json
from Backend.application.pipeline_common import PipelineError
from Backend.infrastructure.manga.models import ModelRegistry, CATALOGUE

ROOT = Path(__file__).resolve().parents[1] / "Backend/resources/manga"
FONT_URL = "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTF/Japanese/NotoSansCJKjp-Regular.otf"
FONT_LICENSE = "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/LICENSE"


def check(root=ROOT):
    registry = ModelRegistry(root=root / "models", bundled=root / "models")
    verified = {}
    lock = json.loads((ROOT / "source-lock.json").read_text(encoding="utf-8"))
    for spec in CATALOGUE:
        if spec["core"]:
            try:
                folder = registry.verify(spec["id"])
                registry.validate_layout(spec["id"], folder)
                verified[spec["id"]] = str(folder)
                actual = json.loads((folder / "checksums.json").read_text(encoding="utf-8"))
                if actual != lock["files"][spec["id"]]:
                    raise PipelineError(
                        "MODEL_UNPINNED",
                        f"Unpinned core model: {spec['id']}",
                        409,
                    )
            except PipelineError as error:
                if (
                    spec["id"] != "legacy-balloon-yolov4"
                    or error.code not in {"MODEL_CORRUPT", "MODEL_UNPINNED"}
                ):
                    raise
                # 本地可能保留用户自备的私有旧版权重；它不参与云端
                # 发布校验（source-lock 锁的是公开下载版），本地构建照常使用。
                folder = registry.folder(spec["id"])
                registry.validate_layout(spec["id"], folder)
                verified[spec["id"]] = str(folder)
                print(
                    "note: legacy-balloon-yolov4 使用本地私有权重；"
                    "云端构建会下载公开 Scaled YOLOv4"
                )
    font = root / "fonts/NotoSansCJKjp-Regular.otf"
    manifest = json.loads((root / "bundle.json").read_text(encoding="utf-8"))
    if (
        not font.is_file()
        or hashlib.sha256(font.read_bytes()).hexdigest() != manifest["font_sha256"]
    ):
        raise RuntimeError(
            "Manga font missing or corrupt; run Tools.prepare_manga_models"
        )
    if manifest["font_sha256"] != lock["font_sha256"]:
        raise RuntimeError("Unpinned core font")
    if not (root / "fonts/OFL.txt").is_file():
        raise RuntimeError("Font license missing")
    return verified


def prepare(sources=(), download=False, root=ROOT):
    registry = ModelRegistry(root=root / "models", bundled=root / "models")
    for spec in CATALOGUE:
        if not spec["core"]:
            continue
        try:
            registry.verify(spec["id"])
            continue
        except ValueError:
            pass
        imported = False
        for source in sources:
            try:
                registry.import_folder(spec["id"], source)
                imported = True
                break
            except ValueError as error:
                if getattr(error, "code", None) != "MODEL_FILES_MISSING":
                    raise
        if imported:
            continue
        if spec["id"] == "rapidocr":
            package = importlib.util.find_spec("rapidocr")
            if package is None:
                raise RuntimeError("Install rapidocr==3.10.0 first")
            models = Path(package.origin).parent / "models"
            with tempfile.TemporaryDirectory() as temp:
                for src, dst in [
                    ("PP-OCRv6_det_small.onnx", "det.onnx"),
                    ("PP-OCRv6_rec_small.onnx", "rec.onnx"),
                    ("ch_ppocr_mobile_v2.0_cls_mobile.onnx", "cls.onnx"),
                ]:
                    shutil.copyfile(models / src, Path(temp) / dst)
                registry.import_folder("rapidocr", temp)
        elif download:
            registry.download(spec["id"])
        else:
            raise RuntimeError(f"Missing {spec['id']}; provide --source or --download")
    fonts = root / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    for name, url in [
        ("NotoSansCJKjp-Regular.otf", FONT_URL),
        ("OFL.txt", FONT_LICENSE),
    ]:
        path = fonts / name
        if not path.exists():
            if not download:
                raise RuntimeError("Core font missing; add --download")
            response = requests.get(url, timeout=120)
            response.raise_for_status()
            path.write_bytes(response.content)
    atomic_json(
        root / "bundle.json",
        {
            "schema_version": 1,
            "font_sha256": hashlib.sha256(
                (fonts / "NotoSansCJKjp-Regular.otf").read_bytes()
            ).hexdigest(),
            "font_source": FONT_URL,
            "models": [
                {k: s[k] for k in ("id", "source", "url", "license")}
                for s in CATALOGUE
                if s["core"]
            ],
        },
    )
    return check(root)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, action="append", default=[])
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            check() if args.check else prepare(args.source, args.download),
            ensure_ascii=False,
            indent=2,
        )
    )
