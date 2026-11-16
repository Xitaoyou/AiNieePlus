"""Versioned public model catalogue and verified, non-executable asset imports."""

from pathlib import Path
import hashlib
import json
import os
import shutil
import zipfile
import requests
from Backend.application.pipeline_common import PipelineError
from Backend.application.manga_store import atomic_json


CATALOGUE = [
    dict(
        id="manga-panel",
        name="Manga panel YOLO26",
        role="detect",
        core=True,
        files=["model.onnx", "model.json"],
        license="AGPL-3.0",
        size_mb=10,
        url="https://github.com/xulihang/balloon-dataset/releases/download/models/manga-panel.zip",
        source="https://github.com/xulihang/balloon-dataset",
        advice="轻量日漫文字定位；分镜类别与文字类别分别处理",
    ),
    dict(
        id="legacy-balloon-yolov4",
        name="Legacy 气泡 YOLOv4",
        role="detect",
        core=False,
        files=["model.weights", "model.cfg", "model.json"],
        license="需核对原权重来源",
        size_mb=282,
        url="",
        source="https://github.com/xulihang/balloon-dataset",
        advice="与旧版参数一致；从现有 ImageTrans 导入，不随公开主包分发",
    ),
    dict(
        id="mangaocr",
        name="mangaOCR ONNX",
        role="ocr",
        core=True,
        files=["encoder.onnx", "decoder.onnx", "vocab.txt"],
        license="Apache-2.0",
        size_mb=120,
        url="https://github.com/xulihang/mangaocr-onnx/releases/download/models/",
        source="https://github.com/xulihang/mangaocr-onnx",
        advice="日语漫画单框识字，横排竖排均可；不负责定位",
    ),
    dict(
        id="rapidocr",
        name="RapidOCR PP-OCR",
        role="ocr",
        core=True,
        files=["det.onnx", "rec.onnx", "cls.onnx"],
        license="Apache-2.0",
        size_mb=20,
        url="",
        source="https://github.com/RapidAI/RapidOCR",
        advice="通用检测与中英文识别；可导入其他语种识别权重",
    ),
    dict(
        id="lama",
        name="LaMa Manga ONNX",
        role="inpaint",
        core=True,
        files=["model.onnx"],
        license="Apache-2.0",
        size_mb=208,
        url="https://huggingface.co/mayocream/lama-manga-onnx/resolve/main/lama-manga.onnx",
        source="https://huggingface.co/mayocream/lama-manga-onnx",
        advice="复杂背景精准去字；需有效的文字像素蒙版",
    ),
    dict(
        id="manga-with-layout",
        name="Koharu RFDETR 版面分割",
        role="detect",
        core=False,
        files=["model.onnx", "model.json"],
        license="见原模型说明",
        size_mb=85,
        url="",
        source="https://github.com/xulihang/balloon-dataset",
        advice="扩展模型，仅提供来源；暂不支持此模型的推理适配",
        supported=False,
    ),
]


class ModelRegistry:
    def __init__(self, root=None, bundled=None):
        from Backend.core.paths import user_data_root, backend_resource_path

        self.root = Path(root) if root else user_data_root() / "manga-models"
        self.bundled = (
            Path(bundled) if bundled else backend_resource_path("manga", "models")
        )

    def spec(self, model_id):
        for spec in CATALOGUE:
            if spec["id"] == model_id:
                return spec
        raise PipelineError("UNKNOWN_MANGA_MODEL", "未知漫画模型", 422)

    def folder(self, model_id):
        spec = self.spec(model_id)
        for root in (self.root, self.bundled):
            folder = root / model_id
            if all((folder / f).is_file() for f in spec["files"]):
                return folder
        raise PipelineError(
            "MANGA_MODEL_MISSING", f"请在模型中心安装或导入 {model_id}", 409
        )

    def catalogue(self):
        result = []
        for spec in CATALOGUE:
            row = dict(spec)
            try:
                row["path"] = str(self.folder(spec["id"]))
                row["installed"] = True
            except PipelineError:
                row["installed"] = False
            result.append(row)
        return result

    def verify(self, model_id):
        folder = self.folder(model_id)
        manifest = folder / "checksums.json"
        if not manifest.is_file():
            raise PipelineError(
                "MODEL_NOT_VERIFIED", "模型缺少校验清单，请重新导入或下载", 409
            )
        expected = json.loads(manifest.read_text(encoding="utf-8"))
        for name in self.spec(model_id)["files"]:
            if hashlib.sha256((folder / name).read_bytes()).hexdigest() != expected.get(
                name
            ):
                raise PipelineError(
                    "MODEL_CORRUPT", f"模型文件损坏：{model_id}/{name}", 409
                )
        return folder

    def import_folder(self, model_id, source):
        spec = self.spec(model_id)
        source = Path(source).expanduser().resolve()
        candidates = [source, source / "models" / model_id]
        if model_id == "mangaocr":
            candidates.append(source / "mangaocr")
        source = next(
            (p for p in candidates if all((p / n).is_file() for n in spec["files"])),
            None,
        )
        if source is None:
            raise PipelineError(
                "MODEL_FILES_MISSING", "所选目录缺少该模型的权重或配置文件", 422
            )
        self.validate_layout(model_id, source)
        self.root.mkdir(parents=True, exist_ok=True)
        import uuid

        temporary = self.root / ("." + model_id + "-" + uuid.uuid4().hex)
        temporary.mkdir()
        try:
            hashes = {}
            for name in spec["files"]:
                shutil.copyfile(source / name, temporary / name)
                hashes[name] = hashlib.sha256(
                    (temporary / name).read_bytes()
                ).hexdigest()
            atomic_json(temporary / "checksums.json", hashes)
            target = self.root / model_id
            if target.exists():
                raise PipelineError(
                    "MODEL_EXISTS",
                    "该模型已安装；请先备份并移走旧模型目录再导入新版本",
                    409,
                )
            os.replace(temporary, target)
            return self.catalogue()
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def validate_layout(self, model_id, folder):
        """A generic model.onnx filename is not evidence of model identity."""
        if model_id == "legacy-balloon-yolov4":
            return
        from Backend.infrastructure.manga.engines import session

        names = {
            i.name
            for i in session(str(folder / self.spec(model_id)["files"][0])).get_inputs()
        }
        valid = (
            (model_id == "manga-panel" and names == {"images"})
            or (model_id == "mangaocr" and names == {"pixel_values"})
            or (model_id == "rapidocr" and names == {"x"})
            or (
                model_id == "lama"
                and len(names) == 2
                and any("mask" in n.lower() for n in names)
            )
        )
        if not valid:
            raise PipelineError(
                "MODEL_FILES_MISSING", "该文件的模型类型不匹配，请选择对应模型目录", 422
            )

    def download(self, model_id, context=None):
        spec = self.spec(model_id)
        if not spec["url"] or not spec.get("supported", True):
            raise PipelineError(
                "MODEL_IMPORT_REQUIRED", "此模型请从官方来源下载后使用目录导入", 409
            )
        import tempfile

        with tempfile.TemporaryDirectory(prefix="ainiee-manga-") as temp:
            temp = Path(temp)

            def fetch(url, output):
                cache = self.root / ".downloads"
                cache.mkdir(parents=True, exist_ok=True)
                partial = cache / (hashlib.sha256(url.encode()).hexdigest() + ".part")
                offset = partial.stat().st_size if partial.exists() else 0
                headers = {"Range": f"bytes={offset}-"} if offset else {}
                with requests.get(
                    url, headers=headers, stream=True, timeout=(15, 60)
                ) as response:
                    if response.status_code == 416:
                        # A complete interrupted download is checked against pinned
                        # contents below, never trusted just because sizes match.
                        shutil.copyfile(partial, output)
                        return
                    response.raise_for_status()
                    resume = response.status_code == 206 and response.headers.get(
                        "Content-Range", ""
                    ).startswith(f"bytes {offset}-")
                    if response.status_code == 206 and not resume:
                        raise PipelineError(
                            "MODEL_DOWNLOAD_RANGE", "下载源返回的续传范围无效", 409
                        )
                    received = offset if resume else 0
                    with partial.open("ab" if resume else "wb") as stream:
                        for chunk in response.iter_content(1024 * 1024):
                            if context and context.is_cancelled():
                                raise PipelineError("CANCELLED", "模型下载已取消", 409)
                            stream.write(chunk)
                            received += len(chunk)
                            if received > 1024**3:
                                raise PipelineError(
                                    "MODEL_TOO_LARGE", "模型下载超过 1GB 限制", 422
                                )
                            if context and context.on_progress:
                                context.on_progress(
                                    {
                                        "stage": "model_download",
                                        "model": model_id,
                                        "downloaded_bytes": received,
                                    }
                                )
                shutil.copyfile(partial, output)

            if model_id == "manga-panel":
                archive = temp / "model.zip"
                fetch(spec["url"], archive)
                with zipfile.ZipFile(archive) as z:
                    for name in spec["files"]:
                        if z.getinfo(name).file_size > 1024**3:
                            raise PipelineError(
                                "MODEL_TOO_LARGE", "模型文件超过 1GB 限制", 422
                            )
                        (temp / name).write_bytes(z.read(name))
            elif model_id == "mangaocr":
                for name in spec["files"]:
                    fetch(spec["url"] + name, temp / name)
            else:
                fetch(spec["url"], temp / "model.onnx")
            from Backend.core.paths import backend_resource_path

            lock_path = backend_resource_path("manga", "source-lock.json")
            if lock_path.is_file():
                locked = (
                    json.loads(lock_path.read_text(encoding="utf-8"))
                    .get("files", {})
                    .get(model_id, {})
                )
                for name, expected in locked.items():
                    if (
                        hashlib.sha256((temp / name).read_bytes()).hexdigest()
                        != expected
                    ):
                        raise PipelineError(
                            "MODEL_VERSION_CHANGED",
                            "下载的模型与已验证版本不符，请检查官方更新后手动导入",
                            409,
                        )
            return self.import_folder(model_id, temp)
