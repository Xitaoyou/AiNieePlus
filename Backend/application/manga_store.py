"""Atomic manga workspace persistence with optimistic revisions and immutable originals."""

import hashlib
import json
import os
import re
import shutil
import threading
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageOps

from Backend.application.pipeline_common import PipelineError
from Backend.domain.models.manga import MangaPage, MangaSettings, MangaWorkspace

EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".pdf", ".cbz"}
ARCHIVE_EXTENSIONS = {".cbz", ".zip"}


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def default_root():
    from Backend.core.paths import project_cache_root

    return project_cache_root() / "manga"


class MangaStore:
    def __init__(self, root=None):
        self.root = Path(root) if root else default_root()
        self.lock = threading.RLock()

    def directory(self, workspace_id):
        if not re.fullmatch(r"[a-f0-9]{32}", workspace_id):
            raise PipelineError("INVALID_MANGA_ID", "漫画工程 ID 无效", 422)
        return self.root / workspace_id

    def load(self, workspace_id):
        with self.lock:
            try:
                document = MangaWorkspace.model_validate_json(
                    (self.directory(workspace_id) / "project.json").read_text(
                        encoding="utf-8"
                    )
                )
            except FileNotFoundError:
                raise PipelineError("MANGA_NOT_FOUND", "漫画工程不存在", 404)
            if document.id != workspace_id:
                raise PipelineError("INVALID_MANGA_ID", "漫画工程身份不匹配", 422)
            return document

    def save(self, document, expected_revision):
        with self.lock:
            current = self.load(document.id)
            if current.revision != expected_revision:
                raise PipelineError(
                    "MANGA_CHANGED",
                    "工程已更新，请刷新后重新保存；人工修改未被覆盖",
                    409,
                )
            # Source identity and dimensions belong to the store, not to clients.
            catalogue = self.directory(document.id) / "sources.json"
            known = (
                json.loads(catalogue.read_text(encoding="utf-8"))
                if catalogue.exists()
                else {p.id: p.model_dump() for p in current.pages}
            )
            for page in document.pages:
                old = known.get(page.id)
                if old is None or any(
                    getattr(page, key) != old[key]
                    for key in ("source", "sha256", "width", "height")
                ):
                    raise PipelineError(
                        "MANGA_SOURCE_CHANGED", "不能通过编辑接口修改原图引用", 422
                    )
            clean = MangaWorkspace.model_validate(document.model_dump())
            clean.revision = current.revision + 1
            directory = self.directory(clean.id)
            atomic_json(directory / "previous.json", current.model_dump())
            atomic_json(directory / "project.json", clean.model_dump())
            return clean

    def history(self):
        if not self.root.exists():
            return []

        def modified(path):
            # 另一个请求可能刚好删掉这个工程；排序键里的 stat 在 try 外面，
            # 抛出来会让 GET /api/v2/manga 整个 500，列表一条都拿不到。
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        result = []
        for path in sorted(
            self.root.glob("*/project.json"), key=modified, reverse=True
        )[:100]:
            try:
                doc = self.load(path.parent.name)
                result.append(
                    {"id": doc.id, "title": doc.title, "pages": len(doc.pages)}
                )
            except (ValueError, OSError):
                continue
        return result

    def delete(self, workspace_id):
        with self.lock:
            self.load(workspace_id)
            directory = self.directory(workspace_id).resolve()
            if directory.parent != self.root.resolve():
                raise PipelineError("INVALID_MANGA_ID", "工程目录无效", 422)
            shutil.rmtree(directory)

    @staticmethod
    def background_key(page, settings):
        values = {
            "sha": page.sha256,
            "removal": settings.removal,
            "version": 3,
            "regions": [
                {
                    k: r.model_dump()[k]
                    for k in (
                        "id",
                        "erase_rect",
                        "fill_color",
                        "fill_opacity",
                        "mask_strokes",
                    )
                }
                for r in page.regions
                if r.target.strip() or r.erase_only
            ],
        }
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def preview_key(page, settings):
        # Only pixel-producing inputs matter; OCR progress and other pages do not.
        values = {
            "source": page.sha256,
            "removal": settings.removal,
            "renderer": 4,
            "punctuation_rules": settings.punctuation_rules,
            "regions": [
                {
                    k: r.model_dump()[k]
                    for k in (
                        "id",
                        "target",
                        "erase_only",
                        "erase_rect",
                        "layout_rect",
                        "style",
                        "fill_color",
                        "fill_opacity",
                        "mask_strokes",
                    )
                }
                for r in page.regions
            ],
        }
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    def import_images(self, paths, settings=None, workspace_id=None, context=None):
        files = []
        for value in paths:
            path = Path(value).expanduser().resolve()
            files.extend(
                p
                for p in path.rglob("*")
                if p.suffix.lower() in EXTENSIONS and p.is_file()
            ) if path.is_dir() else files.append(path)
        files = sorted(
            set(files),
            key=lambda p: [
                int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", str(p))
            ],
        )
        if not files or len(files) > 2000:
            raise PipelineError("INVALID_MANGA_INPUT", "请选择 1 至 2000 张图片", 422)
        document = (
            self.load(workspace_id)
            if workspace_id
            else MangaWorkspace(settings=MangaSettings.model_validate(settings or {}))
        )
        revision = document.revision
        directory = self.directory(document.id)
        directory.mkdir(parents=True, exist_ok=True)
        added = []
        try:

            def append_image(image, name):
                if context and context.is_cancelled():
                    raise PipelineError("CANCELLED", "导入已取消", 409)
                if len(document.pages) >= 2000:
                    raise PipelineError(
                        "INVALID_MANGA_INPUT", "漫画工程最多支持 2000 页", 422
                    )
                if image.width * image.height > 100_000_000:
                    raise PipelineError(
                        "IMAGE_TOO_LARGE", f"图片超过一亿像素：{name}", 422
                    )
                image = ImageOps.exif_transpose(image).convert("RGB")
                page = MangaPage(
                    name=name,
                    source="",
                    sha256="",
                    width=image.width,
                    height=image.height,
                )
                destination = directory / f"{page.id}.source.png"
                added.append(destination)
                image.save(destination)
                page.source = destination.name
                page.sha256 = hashlib.sha256(destination.read_bytes()).hexdigest()
                document.pages.append(page)

            for path in files:
                if context and context.is_cancelled():
                    raise PipelineError("CANCELLED", "图片导入已取消", 409)
                if path.suffix.lower() not in EXTENSIONS or not path.is_file():
                    raise PipelineError(
                        "INVALID_MANGA_INPUT", f"不支持的文件：{path.name}", 422
                    )
                if path.suffix.lower() == ".pdf":
                    import fitz

                    with fitz.open(path) as pdf:
                        if pdf.needs_pass:
                            raise PipelineError(
                                "PDF_PASSWORD_REQUIRED",
                                "PDF 需要密码，请先解密再导入",
                                422,
                            )
                        if len(pdf) + len(document.pages) > 2000:
                            raise PipelineError(
                                "INVALID_MANGA_INPUT", "漫画工程最多支持 2000 页", 422
                            )
                        for index, pdf_page in enumerate(pdf):
                            if context and context.is_cancelled():
                                raise PipelineError("CANCELLED", "PDF 导入已取消", 409)
                            if (
                                pdf_page.rect.width * pdf_page.rect.height * 4
                                > 100_000_000
                            ):
                                raise PipelineError(
                                    "IMAGE_TOO_LARGE", "PDF 页面尺寸过大", 422
                                )
                            pixmap = pdf_page.get_pixmap(
                                matrix=fitz.Matrix(2, 2),
                                colorspace=fitz.csRGB,
                                alpha=False,
                            )
                            image = Image.frombytes(
                                "RGB", (pixmap.width, pixmap.height), pixmap.samples
                            )
                            append_image(image, f"{path.stem}-{index + 1:04d}.png")
                elif path.suffix.lower() in ARCHIVE_EXTENSIONS:
                    import zipfile

                    with zipfile.ZipFile(path) as archive:
                        members = sorted(
                            (
                                m
                                for m in archive.infolist()
                                if not m.is_dir()
                                and Path(m.filename).suffix.lower()
                                in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
                            ),
                            key=lambda m: [
                                int(x) if x.isdigit() else x.lower()
                                for x in re.split(r"(\d+)", m.filename)
                            ],
                        )
                        if not members:
                            raise PipelineError(
                                "INVALID_MANGA_INPUT",
                                f"压缩包里没有图片：{path.name}",
                                422,
                            )
                        if len(members) + len(document.pages) > 2000:
                            raise PipelineError(
                                "INVALID_MANGA_INPUT", "漫画工程最多支持 2000 页", 422
                            )
                        for member in members:
                            if context and context.is_cancelled():
                                raise PipelineError("CANCELLED", "CBZ 导入已取消", 409)
                            if member.file_size > 300 * 1024 * 1024:
                                raise PipelineError(
                                    "IMAGE_TOO_LARGE", "压缩包内单页过大", 422
                                )
                            from io import BytesIO

                            with Image.open(
                                BytesIO(archive.read(member))
                            ) as image:
                                append_image(
                                    image, f"{path.stem}-{Path(member.filename).name}"
                                )
                else:
                    with Image.open(path) as image:
                        append_image(image, path.name)
            if workspace_id:
                # New source records were produced by the store itself.
                with self.lock:
                    if self.load(document.id).revision != revision:
                        raise PipelineError(
                            "MANGA_CHANGED", "导入期间工程已变化，请重试", 409
                        )
                    document.revision += 1
                    catalogue = directory / "sources.json"
                    records = (
                        json.loads(catalogue.read_text(encoding="utf-8"))
                        if catalogue.exists()
                        else {}
                    )
                    records.update({p.id: p.model_dump() for p in document.pages})
                    atomic_json(catalogue, records)
                    atomic_json(directory / "project.json", document.model_dump())
            else:
                document.title = files[0].parent.name or "漫画工程"
                atomic_json(
                    directory / "sources.json",
                    {p.id: p.model_dump() for p in document.pages},
                )
                atomic_json(directory / "project.json", document.model_dump())
            return document
        except Exception:
            for path in added:
                path.unlink(missing_ok=True)
            if not workspace_id:
                shutil.rmtree(directory)
            raise

    def asset(self, workspace_id, page_id, variant):
        document = self.load(workspace_id)
        page = next((p for p in document.pages if p.id == page_id), None)
        if page is None or variant not in {
            "source",
            "clean",
            "render",
            "mask",
            "layer",
        }:
            raise PipelineError("MANGA_ASSET_NOT_FOUND", "漫画图片不存在", 404)
        path = self.directory(workspace_id) / (
            page.source
            if variant == "source"
            else f"{page.id}.{self.background_key(page, document.settings) if variant in {'clean', 'mask'} else self.preview_key(page, document.settings)}.{variant}.png"
        )
        if (
            path.resolve().parent != self.directory(workspace_id).resolve()
            or not path.is_file()
        ):
            raise PipelineError(
                "MANGA_ASSET_NOT_FOUND", "预览尚未生成，请重新渲染", 404
            )
        return path

    def prune_assets(self, workspace_id, keep=6):
        """删掉过期的预览/去字缓存。

        每改一次字号就会写出一整套 {page}.{key}.{variant}.png，旧文件
        永远不会被访问却一直占盘（一个 73 页工程几轮编辑就是几个 GB）。
        按 page id 分组，每组保留最近 keep 个不同 key。
        """
        directory = self.directory(workspace_id)
        if not directory.is_dir():
            return 0
        document = self.load(workspace_id)
        live = set()
        for page in document.pages:
            live.add(page.source)
            for variant in ("clean", "mask"):
                live.add(
                    f"{page.id}.{self.background_key(page, document.settings)}.{variant}.png"
                )
            for variant in ("render", "clean", "mask", "layer"):
                live.add(
                    f"{page.id}.{self.preview_key(page, document.settings)}.{variant}.png"
                )
        groups = {}
        for path in directory.glob("*.png"):
            if path.name in live:
                continue
            # {page_id}.{key}.{variant}.png 或 {page_id}.{key}.{variant}-cache.png
            parts = path.name.split(".")
            if len(parts) < 4 or len(parts[0]) != 32:
                continue
            groups.setdefault(parts[0], []).append(path)
        removed = 0
        for paths in groups.values():
            paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            live_keys = {
                name.split(".")[1]
                for name in live
                if name.startswith(paths[0].name.split(".")[0] + ".")
            }
            dropped = 0
            for path in paths:
                key = path.name.split(".")[1]
                if key in live_keys:
                    continue
                dropped += 1
                if dropped <= keep:
                    continue
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    def import_project(self, path, context=None):
        """Read only named originals; never extract arbitrary archive members."""
        import zipfile

        from Backend.domain.models.manga import identifier

        directory = None
        try:
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo("project.json")
                if info.file_size > 32 * 1024 * 1024:
                    raise ValueError("工程描述过大")
                document = MangaWorkspace.model_validate_json(archive.read(info))
                document.id, document.revision = identifier(), 0
                directory = self.directory(document.id)
                directory.mkdir(parents=True)
                for page in document.pages:
                    if context and context.is_cancelled():
                        raise PipelineError("CANCELLED", "工程导入已取消", 409)
                    if page.source != f"{page.id}.source.png":
                        raise ValueError("原图名称不符合工程格式")
                    member = archive.getinfo(page.source)
                    if member.file_size > 300 * 1024 * 1024:
                        raise ValueError("原图过大")
                    destination = directory / page.source
                    digest = hashlib.sha256()
                    # 300MB 的单页读进内存会让导入 20 页的工程直接 OOM；
                    # 边读边算哈希，落盘后只回读做尺寸校验。
                    with archive.open(member) as incoming, destination.open(
                        "wb"
                    ) as outgoing:
                        while True:
                            chunk = incoming.read(1 << 20)
                            if not chunk:
                                break
                            digest.update(chunk)
                            outgoing.write(chunk)
                    if digest.hexdigest() != page.sha256:
                        raise ValueError("原图校验失败")
                    with Image.open(destination) as image:
                        if (
                            image.size != (page.width, page.height)
                            or image.width * image.height > 100_000_000
                        ):
                            raise ValueError("原图尺寸不匹配")
                        image.verify()
                atomic_json(
                    directory / "sources.json",
                    {p.id: p.model_dump() for p in document.pages},
                )
                atomic_json(directory / "project.json", document.model_dump())
                return document
        except Exception as error:
            if directory and directory.exists():
                shutil.rmtree(directory)
            if isinstance(error, PipelineError):
                raise
            raise PipelineError(
                "INVALID_MANGA_PROJECT", "工程包无效或损坏", 422
            ) from error
