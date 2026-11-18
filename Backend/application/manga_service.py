"""Cancellable manga stages with stable region identities and existing LLM translation."""

import concurrent.futures
import hashlib
import json
import multiprocessing
import threading
import zipfile
from pathlib import Path

from PIL import Image

from Backend.application.execution_context import ExecutionContext
from Backend.application.manga_store import MangaStore, atomic_json
from Backend.application.pipeline_common import PipelineError
from Backend.domain.models.manga import Rect, Region, identifier
from Backend.infrastructure.manga.models import ModelRegistry


class StageCancellation:
    """An internal translation failure must not impersonate a user's cancel."""

    def __init__(self, parent):
        self.parent = parent
        self.local = threading.Event()

    def is_set(self):
        return self.local.is_set() or self.parent.is_set()

    def set(self):
        self.local.set()


class MangaService:
    def __init__(self, store=None, models=None):
        self.store = store or MangaStore()
        self.models = models or ModelRegistry()
        self._pool = None
        self._pool_lock = threading.Lock()

    def close(self):
        if self._pool:
            self._pool.shutdown(wait=True, cancel_futures=True)

    def compute(self, context, **kwargs):
        from concurrent.futures.process import BrokenProcessPool

        from Backend.infrastructure.manga.engines import worker

        self.cancelled(context)

        with self._pool_lock:
            if self._pool is None:
                self._pool = concurrent.futures.ProcessPoolExecutor(
                    max_workers=1, mp_context=multiprocessing.get_context("spawn")
                )
            future = self._pool.submit(worker, **kwargs)
        while True:
            try:
                result = future.result(timeout=0.1)
                break
            except concurrent.futures.TimeoutError:
                continue
            except BrokenProcessPool as error:
                with self._pool_lock:
                    self._pool.shutdown(wait=False, cancel_futures=True)
                    self._pool = None
                raise PipelineError(
                    "MANGA_WORKER_CRASH",
                    "模型进程异常退出，已隔离；可切换模型后重试",
                    409,
                ) from error
        if context.is_cancelled():
            raise PipelineError("CANCELLED", "模型任务已取消，未写入本次结果", 409)
        return result

    @staticmethod
    def cancelled(context):
        if context.is_cancelled():
            raise PipelineError("CANCELLED", "漫画任务已取消", 409)

    def sync_punctuation(self, workspace_id):
        provider = getattr(self, "punctuation_provider", None)
        if provider is None:
            return self.store.load(workspace_id)
        state = provider()
        with self.store.lock:
            document = self.store.load(workspace_id)
            if (
                document.settings.punctuation_rules == state["rows"]
                and document.settings.punctuation_enabled == state["enabled"]
            ):
                return document
            revision = document.revision
            document.settings.punctuation_rules = state["rows"]
            document.settings.punctuation_enabled = state["enabled"]
            document.settings.default_style.auto_punctuation = state["enabled"]
            for page in document.pages:
                if page.default_style:
                    page.default_style.auto_punctuation = state["enabled"]
                for region in page.regions:
                    region.style.auto_punctuation = state["enabled"]
            return self.store.save(document, revision)

    def run(
        self,
        workspace_id,
        stage,
        *,
        context=None,
        page_ids=None,
        region_ids=None,
        options=None,
    ):
        context, options = context or ExecutionContext(), options or {}
        self.sync_punctuation(workspace_id)
        stages = {
            "recognize": ["detect", "ocr"],
            "pipeline": ["translate", "render"],
        }.get(stage, [stage])
        if "detect" in stages and region_ids:
            raise PipelineError(
                "MANGA_DETECT_SCOPE", "检测以整页为单位，请选择当前页或全部页面", 422
            )
        if any(
            s not in {"detect", "ocr", "translate", "clean", "render"} for s in stages
        ):
            raise PipelineError("INVALID_MANGA_STAGE", "未知漫画处理阶段", 422)
        # Recognize each page completely before advancing to the next page.
        if stage == "recognize":
            document = self.store.load(workspace_id)
            reports = {"completed": [], "errors": [], "warnings": []}
            pages = [p for p in document.pages if not page_ids or p.id in page_ids]
            for index, page in enumerate(pages):
                from dataclasses import replace

                def progress(value, index=index, page_id=page.id):
                    if context.on_progress:
                        context.on_progress(
                            {
                                **value,
                                "page_id": page_id,
                                "current": index + 1,
                                "total": len(pages),
                            }
                        )

                page_context = replace(context, on_progress=progress)
                for part in ("detect", "ocr"):
                    result = self.run(
                        workspace_id,
                        part,
                        context=page_context,
                        page_ids=[page.id],
                        options=options,
                    )
                    for key, values in reports.items():
                        values.extend(result["report"][key])
                if context.on_progress:
                    context.on_progress(
                        {
                            "stage": "recognize",
                            "page_id": page.id,
                            "current": index + 1,
                            "total": len(pages),
                        }
                    )
            return {
                "status": "partial" if reports["errors"] else "success",
                "workspace": self.store.load(workspace_id).model_dump(),
                "report": reports,
            }
        report = {"completed": [], "errors": [], "warnings": []}
        for operation in stages:
            document = self.store.load(workspace_id)
            pages = [p for p in document.pages if not page_ids or p.id in page_ids]
            if not pages:
                raise PipelineError("MANGA_PAGE_MISSING", "没有匹配的漫画页面", 422)
            if operation == "translate":
                selected = [
                    r
                    for p in pages
                    for r in sorted(p.regions, key=lambda r: r.order)
                    if not region_ids or r.id in region_ids
                ]
                result = self.translate(document, pages[0], selected, context, options)
                if result:
                    report["translation"] = result
                    report["warnings"].extend(result.get("failed_items", []))
                with self.store.lock:
                    current = self.store.load(workspace_id)
                    for live_page in current.pages:
                        if any(p.id == live_page.id for p in pages):
                            live_page.stages["translate"] = {
                                "status": "partial"
                                if result and result.get("failed_items")
                                else "complete"
                            }
                    self.store.save(current, current.revision)
                report["completed"].extend(
                    {"page_id": p.id, "stage": operation} for p in pages
                )
                continue
            for index, page in enumerate(pages):
                self.cancelled(context)
                page_id = page.id
                document = self.store.load(workspace_id)
                page = next((p for p in document.pages if p.id == page_id), None)
                if page is None:
                    raise PipelineError("MANGA_CHANGED", "处理期间页面已删除", 409)
                revision = document.revision
                selected = [
                    r for r in page.regions if not region_ids or r.id in region_ids
                ]
                source = self.store.asset(workspace_id, page.id, "source")
                try:
                    if operation == "detect":
                        if page.regions and not options.get("replace_regions"):
                            report["warnings"].append(
                                {
                                    "page_id": page.id,
                                    "message": "已有文字框，未重复检测；替换需要明确选择",
                                }
                            )
                            continue
                        if any(
                            r.geometry_locked or r.source_locked or r.target_locked
                            for r in page.regions
                        ):
                            raise PipelineError(
                                "MANGA_LOCKED", "页面存在锁定文字框，不能整页替换", 409
                            )
                        folder = self.models.folder(document.settings.detector)
                        results = self.compute(
                            context,
                            operation="detect",
                            image_path=str(source),
                            folder=str(folder),
                            model_id=document.settings.detector,
                            confidence=document.settings.confidence,
                        )
                        page.panels = (
                            [
                                {
                                    "id": identifier(),
                                    "rect": d["rect"],
                                    "score": d["score"],
                                }
                                for d in results
                                if d["class_id"] == 0
                            ]
                            if document.settings.detect_panels
                            else []
                        )
                        regions = []
                        for detected in results:
                            if detected["class_id"] != 1:
                                continue
                            x, y, w, h = detected["rect"]
                            x, y = max(0, x), max(0, y)
                            w, h = min(w, page.width - x), min(h, page.height - y)
                            if min(w, h) <= 0:
                                continue
                            rect = Rect(x=x, y=y, width=w, height=h)
                            region = Region(
                                ocr_rect=rect.model_copy(),
                                erase_rect=rect.model_copy(),
                                layout_rect=rect.model_copy(),
                                confidence=detected["score"],
                                removal=document.settings.removal,
                                style=(
                                    page.default_style
                                    or document.settings.default_style
                                ).model_copy(deep=True),
                            )
                            candidates = [
                                p
                                for p in page.panels
                                if p["rect"][0]
                                <= x + w / 2
                                <= p["rect"][0] + p["rect"][2]
                                and p["rect"][1]
                                <= y + h / 2
                                <= p["rect"][1] + p["rect"][3]
                            ]
                            if candidates:
                                region.panel_id = min(
                                    candidates,
                                    key=lambda p: p["rect"][2] * p["rect"][3],
                                )["id"]
                            regions.append(region)
                        panel_order = {
                            p["id"]: i
                            for i, p in enumerate(
                                sorted(
                                    page.panels,
                                    key=lambda p: (
                                        round(p["rect"][1] / 50),
                                        -p["rect"][0]
                                        if document.settings.right_to_left
                                        else p["rect"][0],
                                    ),
                                )
                            )
                        }
                        regions.sort(
                            key=lambda r: (
                                panel_order.get(r.panel_id, len(panel_order)),
                                round(r.ocr_rect.y / 40),
                                -r.ocr_rect.x
                                if document.settings.right_to_left
                                else r.ocr_rect.x,
                            )
                        )
                        for order, region in enumerate(regions):
                            region.order = order
                        page.regions = regions
                    elif operation == "ocr":
                        spec = self.models.spec(document.settings.ocr)
                        kind = spec.get("kind", "local")
                        backend = spec.get("backend")
                        folder = ""
                        api_config = {}
                        if kind == "local":
                            folder = str(self.models.folder(document.settings.ocr))
                        elif kind == "package":
                            if not self.models.package_available(backend):
                                raise PipelineError(
                                    "MODEL_DEPENDENCY_MISSING",
                                    f"请先安装 {backend} 依赖包后再使用该 OCR",
                                    409,
                                )
                        elif kind == "api":
                            api_config = self.models.api_config(
                                spec.get("api_provider")
                            )
                            if not api_config:
                                raise PipelineError(
                                    "MODEL_API_NOT_CONFIGURED",
                                    "该 OCR 模型需要配置 API：请在模型中心点「配置 API」填写密钥",
                                    409,
                                )
                        elif kind == "system":
                            if not spec.get("supported", False):
                                raise PipelineError(
                                    "MODEL_UNSUPPORTED",
                                    "当前系统不支持该系统 OCR 引擎",
                                    409,
                                )
                        for region in selected:
                            self.cancelled(context)
                            if region.source_locked or (
                                region.source and not options.get("overwrite_source")
                            ):
                                continue
                            r = region.ocr_rect
                            before = region.model_copy(deep=True)
                            text = self.compute(
                                context,
                                operation="ocr",
                                image_path=str(source),
                                folder=folder,
                                model_id=document.settings.ocr,
                                backend=backend,
                                api_config=api_config,
                                rect=[r.x, r.y, r.width, r.height],
                            )
                            with self.store.lock:
                                current = self.store.load(workspace_id)
                                live_page = next(
                                    (p for p in current.pages if p.id == page_id), None
                                )
                                live = (
                                    next(
                                        (
                                            r
                                            for r in live_page.regions
                                            if r.id == before.id
                                        ),
                                        None,
                                    )
                                    if live_page
                                    else None
                                )
                                if (
                                    live
                                    and not live.source_locked
                                    and live.source == before.source
                                    and live.ocr_rect == before.ocr_rect
                                ):
                                    live.source = text
                                    live.warnings = (
                                        []
                                        if text.strip()
                                        else ["OCR 未识别出文字，请检查或重画区域"]
                                    )
                                    current = self.store.save(current, current.revision)
                                document = current
                                revision = current.revision
                                page = next(
                                    (p for p in current.pages if p.id == page_id), None
                                )
                                if page is None:
                                    raise PipelineError(
                                        "MANGA_CHANGED", "处理期间页面已删除", 409
                                    )
                            if context.on_progress:
                                context.on_progress(
                                    {
                                        "stage": "ocr",
                                        "page_id": page_id,
                                        "region_id": before.id,
                                        "current": index + 1,
                                        "total": len(pages),
                                    }
                                )
                    else:
                        self.render(
                            document,
                            page,
                            revision + 1,
                            report,
                            context,
                            erase_untranslated=False,
                            background_only=operation == "clean",
                        )
                    page.stages[operation] = {
                        "status": "complete",
                        "model": document.settings.detector
                        if operation == "detect"
                        else document.settings.ocr
                        if operation == "ocr"
                        else "",
                    }
                    document = self.store.save(document, revision)
                    report["completed"].append({"page_id": page.id, "stage": operation})
                except (OSError, RuntimeError) as error:
                    report["errors"].append(
                        {
                            "page_id": page.id,
                            "stage": operation,
                            "code": "MANGA_ENGINE_FAILED",
                            "message": str(error),
                        }
                    )
                except PipelineError as error:
                    if error.code in {"CANCELLED", "MANGA_CHANGED"}:
                        raise
                    report["errors"].append(
                        {"page_id": page.id, "stage": operation, **error.to_dict()}
                    )
                if context.on_progress:
                    context.on_progress(
                        {
                            "stage": operation,
                            "page_id": page_id,
                            "current": index + 1,
                            "total": len(pages),
                        }
                    )
        return {
            "status": "partial"
            if report["errors"] or report.get("translation", {}).get("failed")
            else "success",
            "workspace": self.store.load(workspace_id).model_dump(),
            "report": report,
        }

    def translate(self, document, page, regions, context, options):
        from Backend.application.translation_service import translate_model
        from Backend.core.protocol import to_wire
        from Backend.domain.languages import map_language_code_to_name

        selected = [
            r
            for r in regions
            if r.source.strip()
            and not r.target_locked
            and (not r.target or options.get("retranslate"))
        ]
        if not selected:
            return {"total": 0, "succeeded": 0, "failed": 0, "failed_items": []}
        with self.store.lock:
            latest = self.store.load(document.id)
            selected_ids = {r.id for r in selected}
            for p in latest.pages:
                for r in p.regions:
                    if r.id in selected_ids:
                        r.translation_state, r.translation_error = "pending", ""
            self.store.save(latest, latest.revision)
        dialogue = {r.id: r.source for r in selected}
        path = self.store.directory(document.id) / "translation-source.json"
        atomic_json(path, dialogue)
        file_id = identifier()
        model = {
            "schema_version": 2,
            "project_id": document.id,
            "project_type": "I18next",
            "project_name": document.title,
            "files": {
                "dialogue.json": {
                    "file_id": file_id,
                    "media_type": "document",
                    "storage_path": "dialogue.json",
                    "file_project_type": "I18next",
                    "source_ref": {
                        "path": str(path.resolve()),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "verified": True,
                    },
                    "items": [
                        {
                            "item_id": r.id,
                            "text_index": i,
                            "source_text": r.source,
                            "locator": {"kind": "document", "index": i},
                            "extra": {
                                "manga_region_id": r.id,
                                "manga_page_id": next(
                                    p.id
                                    for p in document.pages
                                    if any(v.id == r.id for v in p.regions)
                                ),
                            },
                        }
                        for i, r in enumerate(selected)
                    ],
                }
            },
        }
        config = {
            "source_language": map_language_code_to_name(
                document.settings.source_language.lower()
            ),
            "target_language": map_language_code_to_name(
                document.settings.target_language.lower()
            ),
        }
        if document.settings.provider_id:
            config["provider_id"] = document.settings.provider_id
        from dataclasses import replace

        original = {r.id: r.model_copy(deep=True) for r in selected}
        applied = set()

        def commit_items(items, *_):
            from Backend.core.protocol import to_wire

            items = [to_wire(item) for item in items]
            self.cancelled(context)
            with self.store.lock:
                current = self.store.load(document.id)
                live = {r.id: r for p in current.pages for r in p.regions}
                changed = False
                for item in items:
                    key = item.get("item_id")
                    region, before = live.get(key), original.get(key)
                    value = item.get("translated_text")
                    if (
                        region
                        and before
                        and value
                        and not region.target_locked
                        and region.source == before.source
                        and region.target == before.target
                    ):
                        region.target = value
                        applied.add(key)
                        region.translation_state, region.translation_error = (
                            "succeeded",
                            "",
                        )
                        original[key] = region.model_copy(deep=True)
                        changed = True
                if changed:
                    self.store.save(current, current.revision)
            if context.on_batch_complete:
                context.on_batch_complete(items, *_)

        def progress(snapshot):
            if context.on_progress:
                context.on_progress(
                    {
                        **snapshot,
                        "stage": "translate",
                        "unit": "框",
                        "current": snapshot.get("line", 0),
                        "total": snapshot.get("total_line", len(selected)),
                    }
                )

        progress({"line": 0, "total_line": len(selected)})
        stage_context = replace(
            context,
            cancel_event=StageCancellation(context.cancel_event),
            on_batch_complete=commit_items,
            on_progress=progress,
        )
        try:
            result = translate_model(model, config=config, context=stage_context)
        except Exception:
            with self.store.lock:
                latest = self.store.load(document.id)
                for p in latest.pages:
                    for r in p.regions:
                        if r.id in original and r.translation_state == "pending":
                            r.translation_state = (
                                "cancelled" if context.is_cancelled() else "failed"
                            )
                            r.translation_error = (
                                "任务已取消"
                                if context.is_cancelled()
                                else "翻译任务异常中止，请检查接口日志"
                            )
                self.store.save(latest, latest.revision)
            raise
        translation_report = result.get("report", {})
        failures = {f["item_id"]: f for f in translation_report.get("failed_items", [])}
        with self.store.lock:
            latest = self.store.load(document.id)
            for p in latest.pages:
                for r in p.regions:
                    if r.id in failures:
                        r.translation_state = (
                            "cancelled" if context.is_cancelled() else "failed"
                        )
                        r.translation_error = failures[r.id].get(
                            "message", "未获得有效译文"
                        )
            self.store.save(latest, latest.revision)
        translated = to_wire(result["model"])
        by_id = {
            item["item_id"]: item
            for file in translated["files"].values()
            for item in file["items"]
        }
        if result["status"] not in {"success", "partial"}:
            if result["status"] == "cancelled":
                self.cancelled(context)
            raise PipelineError(
                "MANGA_TRANSLATION_FAILED",
                "翻译未完成，请检查原版接口日志",
                409,
                result.get("report"),
            )
        commit_items([item for key, item in by_id.items() if key not in failures])
        with self.store.lock:
            latest = self.store.load(document.id)
            for p in latest.pages:
                for r in p.regions:
                    if r.id in original and r.translation_state == "pending":
                        r.translation_state = "idle"
                        r.translation_error = (
                            "文本翻译规则已跳过此条"
                            if by_id.get(r.id, {}).get("translation_status") == 7
                            else "人工校对或锁定已改变，未覆盖本次编辑"
                        )
            self.store.save(latest, latest.revision)
        translation_report["applied"] = len(applied)
        return translation_report

    def render(
        self,
        document,
        page,
        revision,
        report,
        context=None,
        erase_untranslated=False,
        background_only=False,
    ):
        page_data = page.model_copy(deep=True)
        for region in page_data.regions:
            region.removal = document.settings.removal
        warnings = self.compute(
            context or ExecutionContext(),
            operation="render",
            image_path=str(self.store.directory(document.id) / page.source),
            folder=str(getattr(self.models, "root", "")),
            model_id="",
            render_data={
                "page": page_data.model_dump(),
                "revision": self.store.preview_key(page, document.settings),
                "background_key": self.store.background_key(page, document.settings),
                "directory": str(self.store.directory(document.id)),
                "bundled": str(getattr(self.models, "bundled", "")),
                "erase_untranslated": erase_untranslated,
                "background_only": background_only,
                "punctuation_rules": document.settings.punctuation_rules,
            },
        )
        report["warnings"].extend(warnings)

    def export(self, workspace_id, output_dir, mode="png", context=None):
        import shutil
        import tempfile

        context = context or ExecutionContext()
        output = Path(output_dir).expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        published = []
        with tempfile.TemporaryDirectory(prefix=".manga-export-", dir=output) as stage:
            result = self._export_into(workspace_id, stage, mode, context)
            sources = [Path(p) for p in result["files"]]
            if any((output / p.name).exists() for p in sources):
                raise PipelineError(
                    "OUTPUT_EXISTS", "导出文件已存在，请使用新目录", 409
                )
            self.cancelled(context)
            try:
                for source in sources:
                    destination = output / source.name
                    with destination.open("xb") as stream:
                        published.append(destination)
                        with source.open("rb") as incoming:
                            shutil.copyfileobj(incoming, stream)
            except Exception:
                for path in published:
                    path.unlink(missing_ok=True)
                raise
        return {"files": [str(p) for p in published]}

    def _export_into(self, workspace_id, output_dir, mode="png", context=None):
        document = self.sync_punctuation(workspace_id)
        output = Path(output_dir).expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        if mode in {"i18next", "i18next-source", "i18next-target"}:
            source = mode == "i18next-source"
            values = {
                r.id: r.source if source else r.target
                for p in document.pages
                for r in sorted(p.regions, key=lambda r: r.order)
            }
            target = output / f"{document.id}.{'source' if source else 'target'}.json"
            if target.exists():
                raise PipelineError(
                    "OUTPUT_EXISTS", "导出文件已存在，请使用新目录", 409
                )
            atomic_json(target, values)
            return {"files": [str(target)]}
        if mode == "project":
            target = output / f"{document.id}.manga.zip"
            with zipfile.ZipFile(target, "x", zipfile.ZIP_DEFLATED) as archive:
                archive.write(
                    self.store.directory(workspace_id) / "project.json", "project.json"
                )
                for page in document.pages:
                    archive.write(
                        self.store.asset(workspace_id, page.id, "source"), page.source
                    )
            return {"files": [str(target)]}
        if mode not in {"png", "jpeg", "zip"}:
            raise PipelineError("INVALID_EXPORT", "未知导出格式", 422)
        files = []
        # Render from current data instead of exporting a stale preview.
        for index, page in enumerate(document.pages):
            self.cancelled(context)
            self.render(document, page, document.revision, {"warnings": []}, context)
            name = f"{index + 1:04d}-{Path(page.name).stem}." + (
                "jpg" if mode == "jpeg" else "png"
            )
            path = output / name
            if path.exists():
                raise PipelineError("OUTPUT_EXISTS", f"导出文件已存在：{name}", 409)
            with Image.open(self.store.asset(workspace_id, page.id, "render")) as image:
                image.convert("RGB").save(path, quality=95)
            files.append(path)
        if mode == "zip":
            target = output / f"{document.id}.zip"
            with zipfile.ZipFile(target, "x", zipfile.ZIP_DEFLATED) as archive:
                for path in files:
                    archive.write(path, path.name)
            files = [target]
        return {"files": [str(p) for p in files]}

    def import_translation(self, workspace_id, path):
        document = self.store.load(workspace_id)
        revision = document.revision
        path = Path(path)
        if path.stat().st_size > 32 * 1024 * 1024:
            raise PipelineError("INVALID_MANGA_TRANSLATIONS", "译文文件过大", 422)
        values = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(values, dict) or any(
            not isinstance(v, str) for v in values.values()
        ):
            raise PipelineError(
                "INVALID_MANGA_TRANSLATIONS",
                "需要以文字框 ID 为键的 i18next JSON 字符串对象",
                422,
            )
        regions = {r.id: r for p in document.pages for r in p.regions}
        unknown = set(values) - regions.keys()
        if unknown:
            raise PipelineError(
                "MANGA_TRANSLATION_IDS",
                "译文包含其他工程的文字框 ID，未导入",
                422,
                {"unknown_ids": sorted(unknown)},
            )
        imported = 0
        for key, value in values.items():
            if not regions[key].target_locked:
                regions[key].target = value
                imported += 1
        document = self.store.save(document, revision)
        return {"workspace": document.model_dump(), "imported": imported}
