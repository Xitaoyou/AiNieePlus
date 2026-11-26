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
from Backend.infrastructure.manga.models import LEGACY_BALLOON_ID, ModelRegistry


def _png_bytes(image):
    """PDF 内嵌图片要字节流；直接塞 PNG 比转 JPEG 快且无损。"""
    from io import BytesIO

    buffer = BytesIO()
    image.save(buffer, "PNG", optimize=False, compress_level=6)
    return buffer.getvalue()


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
        with self._pool_lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)

    # 主业是切面板的检测器：面板照用，文字框交给行级模型。
    PANEL_ONLY_MODELS = ("manga-panel",)
    # 输出「逐行 / 逐列」小框的检测器，必须先聚合成气泡才能用。
    LINE_LEVEL_MODELS = ("japanese-lines", "ppocr-manga-det", "rfdetr")
    LAYOUT_MODELS = ("manga-with-layout", "manga-with-layout-768")
    # 行级文字框来源，按可靠性排序
    TEXT_LINE_FALLBACKS = ("japanese-lines", "ppocr-manga-det", "comic-speech-bubble")

    @classmethod
    def drop_nested_text_boxes(cls, boxes):
        """Keep the full text block when a detector also reports nested or duplicate lines."""
        text = []
        for index, box in enumerate(boxes):
            if box["class_id"] != 1:
                continue
            x, y, w, h = box["rect"]
            if w > 0 and h > 0:
                text.append((index, x, y, w, h, w * h, box["score"]))
        # Sort by area first. In the same bubble, the single-line prediction
        # often has higher confidence than the box covering the full dialogue.
        text.sort(key=lambda item: (item[5], item[6]), reverse=True)
        kept = []
        dropped = set()
        for index, x, y, w, h, area, _ in text:
            cx, cy = x + w / 2, y + h / 2
            rect = (x, y, w, h)
            for left, top, right, bottom in kept:
                kept_rect = (left, top, right - left, bottom - top)
                if cls.boxes_overlap(kept_rect, rect):
                    dropped.add(index)
                    break
                if not (left <= cx <= right and top <= cy <= bottom):
                    continue
                covered = max(0, min(x + w, right) - max(x, left)) * max(
                    0, min(y + h, bottom) - max(y, top)
                )
                # Use the smaller box's coverage, allowing a few pixels of
                # detector jitter at the edge. IoU misses narrow nested lines.
                if covered / area >= 0.92:
                    dropped.add(index)
                    break
            else:
                kept.append((x, y, x + w, y + h))
        return [box for index, box in enumerate(boxes) if index not in dropped]

    @staticmethod
    def merge_into_bubbles(boxes):
        """把同一气泡内的逐行/逐列框合并成一个框。

        行级检测器（japanese-lines / ppocr-manga-det）会把一个气泡里的
        每一列竖排文字各报一个框，直接拿去翻译就是一个气泡切成两段，
        译文互不相干。这里按「相邻且重叠」聚类：竖排看横向间距 + 纵向
        重叠，横排看纵向间距 + 横向重叠，再取包围盒。
        """
        items = [dict(box) for box in boxes if box.get("class_id") == 1]
        count = len(items)
        if count <= 1:
            return items

        parent = list(range(count))

        def find(index):
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left, right):
            root_left, root_right = find(left), find(right)
            if root_left != root_right:
                parent[root_right] = root_left

        for i in range(count):
            xi, yi, wi, hi = items[i]["rect"]
            for j in range(i + 1, count):
                xj, yj, wj, hj = items[j]["rect"]
                gap_x = max(xi - (xj + wj), xj - (xi + wi))
                gap_y = max(yi - (yj + hj), yj - (yi + hi))
                overlap_x = min(xi + wi, xj + wj) - max(xi, xj)
                overlap_y = min(yi + hi, yj + hj) - max(yi, yj)
                overlap_w = min(wi, wj)
                overlap_h = min(hi, hj)
                # 竖排：同一列的左右相邻框，横向贴近且纵向大半重叠（仅合并竖条框）
                columns = (
                    hi >= wi
                    and hj >= wj
                    and gap_x <= 0.60 * overlap_w
                    and overlap_y >= 0.65 * overlap_h
                )
                # 横排：同一行的上下相邻框，纵向几乎贴住且横向大半重叠（仅合并横条框，避免把上下相邻的竖排气泡误并）
                rows = (
                    wi > hi
                    and wj > hj
                    and gap_y <= 0.25 * overlap_h
                    and overlap_x >= 0.65 * overlap_w
                )
                if columns or rows:
                    union(i, j)

        groups = {}
        for index in range(count):
            groups.setdefault(find(index), []).append(items[index])
        merged = []
        for members in groups.values():
            x = min(box["rect"][0] for box in members)
            y = min(box["rect"][1] for box in members)
            right = max(box["rect"][0] + box["rect"][2] for box in members)
            bottom = max(box["rect"][1] + box["rect"][3] for box in members)
            merged.append(
                {
                    "rect": [x, y, right - x, bottom - y],
                    "score": max(box["score"] for box in members),
                    "class_id": 1,
                }
            )
        return merged

    def detect_text_bubbles(self, context, source, confidence):
        """取整框（已聚合）的文字框，而不是逐列小框。"""
        for model_id in self.TEXT_LINE_FALLBACKS:
            try:
                folder = self.models.folder(model_id)
            except PipelineError:
                continue
            spec = self.models.spec(model_id)
            try:
                boxes = self.compute(
                    context,
                    operation="detect",
                    image_path=str(source),
                    folder=str(folder),
                    model_id=model_id,
                    confidence=spec.get("default_confidence") or confidence,
                )
            except PipelineError as error:
                # 取消与进程崩溃必须冒泡：继续换模型只会拖长等待，
                # 还会把真实原因埋进「本页未检出文字框」。
                if error.code in {"CANCELLED", "MANGA_CHANGED", "MANGA_WORKER_CRASH"}:
                    raise
                continue
            except (OSError, RuntimeError):
                continue
            bubbles = self.merge_into_bubbles(boxes)
            if bubbles:
                return bubbles
        return []

    @classmethod
    def boxes_overlap(cls, left, right, iou_threshold=0.3, cover_threshold=0.6):
        """两个框是否在说同一个气泡。

        只看 IoU 会把「小框整个落在气泡里」判成不重叠（小框面积小，IoU 很低），
        于是同一个气泡被重复加上一次。因此再补一个「交集 / 较小面积」的判据。
        """
        lx, ly, lw, lh = left
        rx, ry, rw, rh = right
        x0, y0 = max(lx, rx), max(ly, ry)
        x1, y1 = min(lx + lw, rx + rw), min(ly + lh, ry + rh)
        if x1 <= x0 or y1 <= y0:
            return False
        intersection = (x1 - x0) * (y1 - y0)
        union = lw * lh + rw * rh - intersection
        if union > 0 and intersection / union >= iou_threshold:
            return True
        smaller = min(lw * lh, rw * rh)
        return smaller > 0 and intersection / smaller >= cover_threshold

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
                if context.is_cancelled():
                    # 子进程里的推理无法从外部打断，这里立刻放弃等待：
                    # 结果会被丢弃，池保留给下一个任务排队。
                    future.add_done_callback(lambda f: f.exception())
                    raise PipelineError(
                        "CANCELLED", "模型任务已取消，本次结果不写入", 409
                    )
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
        # recognize 会按页、按子阶段反复调用 run()，每页两次 store.load
        # 只为确认标点表没变是纯浪费；用上次同步结果挡掉重复读盘。
        fingerprint = (
            workspace_id,
            json.dumps(state.get("rows"), sort_keys=True, ensure_ascii=False),
            bool(state.get("enabled")),
        )
        if getattr(self, "_punctuation_fingerprint", None) == fingerprint:
            return None
        with self.store.lock:
            document = self.store.load(workspace_id)
            if (
                document.settings.punctuation_rules == state["rows"]
                and document.settings.punctuation_enabled == state["enabled"]
            ):
                self._punctuation_fingerprint = fingerprint
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
            result = self.store.save(document, revision)
            self._punctuation_fingerprint = fingerprint
            return result

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
            # 预先声明所有可能出现的聚合键：逐页 sub-report 里 setdefault 出来的
            # 键（empty_pages / skipped_pages / fallback_pages）必须在这里存在，
            # 否则汇总时会被静默丢弃。
            reports = {
                "completed": [],
                "errors": [],
                "warnings": [],
                "empty_pages": [],
                "skipped_pages": [],
                "fallback_pages": [],
            }
            pages = [p for p in document.pages if not page_ids or p.id in page_ids]
            for index, page in enumerate(pages):
                from dataclasses import replace

                def progress(
                    value, index=index, page_id=page.id, page_name=page.name
                ):
                    if context.on_progress:
                        context.on_progress(
                            {
                                **value,
                                "page_id": page_id,
                                "page_index": index + 1,
                                "page_name": page_name,
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
                        # 子阶段只在真正发生时才 setdefault 这些键，缺键要按空处理，
                        # 否则整轮 recognize 会抛 KeyError。
                        values.extend(result["report"].get(key) or ())
                if context.on_progress:
                    context.on_progress(
                        {
                            "stage": "recognize",
                            "page_id": page.id,
                            "page_index": index + 1,
                            "page_name": page.name,
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
                            report.setdefault("skipped_pages", []).append(
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
                        if context.on_progress:
                            context.on_progress(
                                {
                                    "stage": "detect",
                                    "phase": "running",
                                    "model": document.settings.detector,
                                    "page_id": page.id,
                                    "page_index": index + 1,
                                    "page_name": page.name,
                                    "current": index,
                                    "total": len(pages),
                                }
                            )
                        results = self.compute(
                            context,
                            operation="detect",
                            image_path=str(source),
                            folder=str(folder),
                            model_id=document.settings.detector,
                            confidence=document.settings.confidence,
                        )
                        detector = document.settings.detector
                        results = self.drop_nested_text_boxes(results)
                        own_text = [d for d in results if d["class_id"] == 1]
                        own_panels = [d for d in results if d["class_id"] == 0]
                        supplemented = False
                        if detector in self.PANEL_ONLY_MODELS:
                            # 面板模型自己也能报出几个有把握的气泡，别丢；
                            # 只把行级聚合出的、它没覆盖到的气泡补进来；
                            # 若行级聚合出了完整覆盖某窄框的更大整气泡，用完整气泡替换窄框，避免排版字号过小。
                            bubbles = self.detect_text_bubbles(
                                context, source, document.settings.confidence
                            )
                            replaced_own = set()
                            extra = []
                            for bubble in bubbles:
                                bx, by, bw, bh = bubble["rect"]
                                b_area = bw * bh
                                overlapped = [
                                    (idx, own)
                                    for idx, own in enumerate(own_text)
                                    if self.boxes_overlap(bubble["rect"], own["rect"])
                                ]
                                if not overlapped:
                                    extra.append(bubble)
                                    continue
                                if (
                                    len(overlapped) == 1
                                    and b_area > 1.35 * (overlapped[0][1]["rect"][2] * overlapped[0][1]["rect"][3])
                                    and max(
                                        0,
                                        min(bx + bw, overlapped[0][1]["rect"][0] + overlapped[0][1]["rect"][2])
                                        - max(bx, overlapped[0][1]["rect"][0]),
                                    )
                                    * max(
                                        0,
                                        min(by + bh, overlapped[0][1]["rect"][1] + overlapped[0][1]["rect"][3])
                                        - max(by, overlapped[0][1]["rect"][1]),
                                    )
                                    >= 0.70 * (overlapped[0][1]["rect"][2] * overlapped[0][1]["rect"][3])
                                ):
                                    replaced_own.add(overlapped[0][0])
                                    extra.append(bubble)
                            if extra or replaced_own:
                                kept_own = [
                                    own
                                    for idx, own in enumerate(own_text)
                                    if idx not in replaced_own
                                ]
                                results = own_panels + kept_own + extra
                                supplemented = True
                        elif detector in self.LINE_LEVEL_MODELS:
                            # 用户直接选了行级模型：它报的是逐列小框，必须聚合，
                            # 否则一个气泡被切成几段分别翻译，译文互不相干。
                            results = own_panels + self.merge_into_bubbles(own_text)
                        else:
                            if detector == LEGACY_BALLOON_ID:
                                # ImageTrans Legacy 在部分页上会输出重叠的逐列框（如 1/2、3/4），
                                # 需要先做一次同气泡聚合，再判断是否漏检。
                                own_text = self.merge_into_bubbles(own_text)
                                results = own_panels + own_text
                            # 整框级模型（japanese-manga / speech-bubble / Legacy…）
                            # 有时只报一两个小横条或干脆漏检。只有「明显偏少」
                            # 才补一次行级检测，按 IoU 去重后与自身结果合并。
                            covered = sum(
                                box["rect"][2] * box["rect"][3] for box in own_text
                            )
                            sparse = len(own_text) < 2 or covered < (
                                0.015 * page.width * page.height
                            )
                            if detector == LEGACY_BALLOON_ID:
                                sparse = not own_text or (
                                    len(own_text) < 2
                                    and covered < 0.015 * page.width * page.height
                                )
                            elif detector in self.LAYOUT_MODELS:
                                # A page with only one line of dialogue is valid.
                                # Avoid mixing another detector into a layout result.
                                sparse = not own_text
                            if sparse:
                                bubbles = self.detect_text_bubbles(
                                    context, source, document.settings.confidence
                                )
                                extra = [
                                    bubble
                                    for bubble in bubbles
                                    if not any(
                                        self.boxes_overlap(bubble["rect"], own["rect"])
                                        for own in own_text
                                    )
                                ]
                                if extra:
                                    results = own_panels + own_text + extra
                                    supplemented = True
                        if (
                            supplemented
                            or detector in self.PANEL_ONLY_MODELS
                            or detector in self.LINE_LEVEL_MODELS
                            or detector == LEGACY_BALLOON_ID
                        ):
                            # Merging lines or adding fallback boxes can create
                            # new containment or overlaps after the initial detector pass.
                            results = self.drop_nested_text_boxes(results)
                        if supplemented:
                            report.setdefault("fallback_pages", []).append(
                                {
                                    "page_id": page.id,
                                    "model": detector,
                                }
                            )
                            if context.on_progress:
                                context.on_progress(
                                    {
                                        "stage": "detect",
                                        "phase": "fallback",
                                        "event": "fallback",
                                        "model": detector,
                                        "added": len(extra),
                                        "page_id": page.id,
                                        "page_index": index + 1,
                                        "page_name": page.name,
                                        "current": index + 1,
                                        "total": len(pages),
                                    }
                                )
                        if not results:
                            report.setdefault("empty_pages", []).append(page.id)
                            if context.on_progress:
                                context.on_progress(
                                    {
                                        "stage": "detect",
                                        "phase": "empty",
                                        "event": "empty",
                                        "model": detector,
                                        "page_id": page.id,
                                        "page_index": index + 1,
                                        "page_name": page.name,
                                        "current": index + 1,
                                        "total": len(pages),
                                    }
                                )
                            continue
                        if not any(d["class_id"] == 1 for d in results):
                            # 回退也拿不到文字框：保留面板结果，但明确告知没检出文字。
                            report.setdefault("empty_pages", []).append(page.id)
                            if context.on_progress:
                                context.on_progress(
                                    {
                                        "stage": "detect",
                                        "phase": "empty",
                                        "event": "empty",
                                        "model": detector,
                                        "page_id": page.id,
                                        "page_index": index + 1,
                                        "page_name": page.name,
                                        "current": index + 1,
                                        "total": len(pages),
                                    }
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
                            layout_rect = rect.model_copy()
                            style = (
                                page.default_style
                                or document.settings.default_style
                            ).model_copy(deep=True)
                            if (
                                page.default_style is None
                                and not document.settings.default_style.vertical
                                and document.settings.source_language.lower().startswith(
                                    ("ja", "zh")
                                )
                                and h >= w * 1.35
                            ):
                                style.vertical = True
                            # 极窄的单行/单列气泡（如「哇—」「诶？」），检测框往往紧贴笔画，
                            # 适当放宽排版框（不动取字框与去字框），避免自动字号被压到 6~10px。
                            if style.vertical and w < 32 and page.width >= 32:
                                new_w = min(float(page.width), max(w * 1.5, 32.0))
                                new_x = max(0.0, min(float(page.width) - new_w, x - (new_w - w) / 2))
                                layout_rect = Rect(
                                    x=new_x, y=y, width=new_w, height=h
                                )
                            elif not style.vertical and h < 32 and page.height >= 32:
                                new_h = min(float(page.height), max(h * 1.5, 32.0))
                                new_y = max(0.0, min(float(page.height) - new_h, y - (new_h - h) / 2))
                                layout_rect = Rect(
                                    x=x, y=new_y, width=w, height=new_h
                                )
                            region = Region(
                                ocr_rect=rect.model_copy(),
                                erase_rect=rect.model_copy(),
                                layout_rect=layout_rect,
                                confidence=detected["score"],
                                removal=document.settings.removal,
                                style=style,
                            )
                            candidates = [
                                p
                                for p in page.panels
                                if p.rect[0] <= x + w / 2 <= p.rect[0] + p.rect[2]
                                and p.rect[1] <= y + h / 2 <= p.rect[1] + p.rect[3]
                            ]
                            if candidates:
                                region.panel_id = min(
                                    candidates,
                                    key=lambda p: p.rect[2] * p.rect[3],
                                ).id
                            regions.append(region)
                        panel_order = {
                            p.id: i
                            for i, p in enumerate(
                                sorted(
                                    page.panels,
                                    key=lambda p: (
                                        round(p.rect[1] / 50),
                                        -p.rect[0]
                                        if document.settings.right_to_left
                                        else p.rect[0],
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
                        engine_id = options.get("ocr_engine") or document.settings.ocr
                        spec = self.models.spec(engine_id)
                        kind = spec.get("kind", "local")
                        backend = spec.get("backend")
                        folder = ""
                        api_config = {}
                        if kind == "local":
                            folder = str(self.models.folder(engine_id))
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
                            local_engine = backend in {"api_ollama", "api_ocrspace"}
                            if not api_config and not local_engine:
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
                        pending_regions = [
                            r
                            for r in selected
                            if not (
                                r.source_locked
                                or (r.source and not options.get("overwrite_source"))
                            )
                        ]
                        if pending_regions and context.on_progress:
                            context.on_progress(
                                {
                                    "stage": "ocr",
                                    "phase": "running",
                                    "model": engine_id,
                                    "page_id": page.id,
                                    "page_index": index + 1,
                                    "page_name": page.name,
                                    "region_index": 0,
                                    "region_total": len(pending_regions),
                                    "current": index + 1,
                                    "total": len(pages),
                                }
                            )
                        for r_index, region in enumerate(pending_regions):
                            self.cancelled(context)
                            r = region.ocr_rect
                            before = region.model_copy(deep=True)
                            text = self.compute(
                                context,
                                operation="ocr",
                                image_path=str(source),
                                folder=folder,
                                model_id=engine_id,
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
                                    live.translation_cache = {}
                                    live.translation_provider = ""
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
                                        "phase": "running",
                                        "model": engine_id,
                                        "page_id": page_id,
                                        "page_index": index + 1,
                                        "page_name": page.name,
                                        "region_id": before.id,
                                        "region_index": r_index + 1,
                                        "region_total": len(pending_regions),
                                        "empty": not text.strip(),
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
                    if not (
                        operation == "detect"
                        and page.id in (report.get("empty_pages") or [])
                    ):
                        report["completed"].append(
                            {"page_id": page.id, "stage": operation}
                        )
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
                    # 取消、工程变更、模型进程崩溃、模型缺失/坏权重/依赖缺失
                    # 都不是「这一页重试就能好」的问题：逐页重试只会把 1 次
                    # 明确报错拖成 N 次，还让用户以为「部分页失败可以再试」。
                    if (
                        error.code in {"CANCELLED", "MANGA_CHANGED", "MANGA_WORKER_CRASH"}
                        or error.code.startswith("MODEL_")
                    ):
                        raise
                    report["errors"].append(
                        {"page_id": page.id, "stage": operation, **error.to_dict()}
                    )
                if context.on_progress:
                    context.on_progress(
                        {
                            "stage": operation,
                            "model": document.settings.detector
                            if operation == "detect"
                            else document.settings.ocr
                            if operation == "ocr"
                            else "",
                            "page_id": page_id,
                            "page_index": index + 1,
                            "page_name": page.name,
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
        provider_id = options.get("provider_id") or document.settings.provider_id
        provider_key = provider_id or "__default__"
        if provider_id:
            config["provider_id"] = provider_id
        # 角色设定与跨框上下文：漫画的台词经常只写「他」「那家伙」，
        # 不把角色表和相邻框原文送进提示词，人称与语气就会每框一跳。
        settings = document.settings
        if settings.characterization_switch and settings.characters:
            config["characterization_switch"] = True
            config["characterization_data"] = [
                character.to_row() for character in settings.characters
            ]
        if settings.context_neighbors:
            config["pre_line_counts"] = settings.context_neighbors
        from dataclasses import replace

        original = {r.id: r.model_copy(deep=True) for r in selected}
        applied = set()

        def commit_items(items, *_):
            from Backend.core.protocol import to_wire

            items = [to_wire(item) for item in items]
            # 取消时不要再抛 CANCELLED：这个回调由翻译层在汇总循环里调用，
            # 抛出去会被当成「接口异常」记进 operation_errors，
            # 用户主动取消最后反而看到「翻译未完成，请检查接口日志」。
            if context.is_cancelled():
                return
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
                        cache = dict(region.translation_cache or {})
                        cache[provider_key] = {
                            "source": region.source,
                            "target": value,
                        }
                        region.translation_cache = cache
                        region.translation_provider = provider_key
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
                        # 取消时共享层会把每条都记成「操作异常中止」，
                        # 原样显示会让人以为接口坏了。
                        r.translation_error = (
                            "任务已取消"
                            if context.is_cancelled()
                            else failures[r.id].get("message", "未获得有效译文")
                        )
            self.store.save(latest, latest.revision)
        translated = to_wire(result["model"])
        by_id = {
            item["item_id"]: item
            for file in translated["files"].values()
            for item in file["items"]
        }
        if result["status"] not in {"success", "partial"}:
            # 共享翻译层把「用户取消」也记进 operation_errors，status 因此可能
            # 落到 failed。漫画侧只看取消标志：用户点了取消就报取消，不要
            # 提示「请检查原版接口日志」。共享层代码保持原样，避免与上游冲突。
            if result["status"] == "cancelled" or context.is_cancelled():
                raise PipelineError(
                    "CANCELLED", "漫画翻译已取消，本次结果不写入", 409
                )
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
        # 排版警告只留在本次 report 里就没人看得见：关掉任务面板后
        # 「哪一框字号已经掉到 12px」无从查询。写回 region.warnings，
        # 工程里就能持久保存，编辑器表格和导出质检都能读到。
        by_region = {}
        for warning in warnings:
            region_id = warning.get("region_id")
            if region_id:
                by_region.setdefault(region_id, []).append(warning)
        for region in page.regions:
            region.warnings = [
                item.get("message", "") for item in by_region.get(region.id, [])
            ]
        # 每次渲染都会写出一整套带哈希的中间图；不清的话工程目录只涨不降。
        try:
            self.store.prune_assets(document.id)
        except (OSError, ValueError):
            pass

    def export(self, workspace_id, output_dir, mode="png", context=None):
        import shutil
        import tempfile

        context = context or ExecutionContext()
        output = Path(output_dir).expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        published = []
        quality = []
        with tempfile.TemporaryDirectory(prefix=".manga-export-", dir=output) as stage:
            result = self._export_into(workspace_id, stage, mode, context)
            quality = result.get("quality") or []
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
        return {"files": [str(p) for p in published], "quality": quality}

    def _export_into(self, workspace_id, output_dir, mode="png", context=None):
        document = self.sync_punctuation(workspace_id) or self.store.load(workspace_id)
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
        if mode not in {"png", "jpeg", "zip", "cbz", "pdf"}:
            raise PipelineError("INVALID_EXPORT", "未知导出格式", 422)
        files = []
        quality = []
        # Render from current data instead of exporting a stale preview.
        for index, page in enumerate(document.pages):
            self.cancelled(context)
            page_report = {"warnings": []}
            self.render(document, page, document.revision, page_report, context)
            # 排版质检信息以前直接扔掉，导出完成后用户无从知道哪些框字号过小
            for warning in page_report["warnings"]:
                quality.append(
                    {
                        "page_id": page.id,
                        "page_name": page.name,
                        "region_id": warning.get("region_id"),
                        "code": warning.get("code"),
                        "message": warning.get("message"),
                    }
                )
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
        elif mode == "cbz":
            # CBZ 就是「按文件名排序的图片压缩包」，阅读器按名字排页，
            # 所以必须沿用 0001-xxx.png 这个零填充序号，不能只塞原名。
            target = output / f"{document.id}.cbz"
            with zipfile.ZipFile(target, "x", zipfile.ZIP_STORED) as archive:
                for path in files:
                    archive.write(path, path.name)
            for path in files:
                path.unlink(missing_ok=True)
            files = [target]
        elif mode == "pdf":
            target = output / f"{document.id}.pdf"
            if target.exists():
                raise PipelineError(
                    "OUTPUT_EXISTS", "导出文件已存在，请使用新目录", 409
                )
            import fitz

            with fitz.open() as pdf:
                for path in files:
                    with Image.open(path) as image:
                        rgb = image.convert("RGB")
                        width, height = rgb.width, rgb.height
                        page_ = pdf.new_page(width=width, height=height)
                        page_.insert_image(
                            page_.rect,
                            stream=_png_bytes(rgb),
                        )
                    path.unlink(missing_ok=True)
                pdf.save(str(target), deflate=True)
            files = [target]
        return {"files": [str(p) for p in files], "quality": quality}

    def fix_overflow(self, workspace_id, page_ids=None, region_ids=None):
        """把字号过小/溢出的框改成自动适应，并把排版框按需扩大。

        这是「导出前最后一次批处理」：用户看到质检列表后一键处理，
        改完仍走 store.save 的 revision CAS，前端照样能撤销。
        """
        codes = {"TEXT_OVERFLOW", "TEXT_SMALL"}
        with self.store.lock:
            document = self.store.load(workspace_id)
            revision = document.revision
            fixed = []
            for page in document.pages:
                if page_ids and page.id not in page_ids:
                    continue
                for region in page.regions:
                    if region_ids and region.id not in region_ids:
                        continue
                    if not region.target.strip():
                        continue
                    # warnings 里存的是可读消息，codes 用于兼容结构化的旧数据
                    flagged = bool(region.warnings) or any(
                        code in str(w)
                        for w in (region.warnings or [])
                        for code in codes
                    )
                    if not flagged:
                        # 没有渲染过就没有 warning：按几何规则兜底，
                        # 固定字号且字号偏大的框同样是溢出高发户
                        flagged = not region.style.autofit and region.style.size > 24
                    if not flagged:
                        continue
                    region.style.autofit = True
                    rect = region.layout_rect
                    grow = 1.15
                    width = min(page.width - rect.x, rect.width * grow)
                    height = min(page.height - rect.y, rect.height * grow)
                    if width > rect.width or height > rect.height:
                        region.layout_rect = rect.model_copy(
                            update={"width": width, "height": height}
                        )
                    region.warnings = []
                    fixed.append(
                        {"page_id": page.id, "region_id": region.id, "page": page.name}
                    )
            if fixed:
                document = self.store.save(document, revision)
            return {
                "workspace": document.model_dump(),
                "fixed": fixed,
                "count": len(fixed),
            }

    def replace_text(
        self,
        workspace_id,
        pattern,
        replacement="",
        ignore_case=False,
        field="target",
        page_ids=None,
        dry_run=False,
    ):
        """批量正则替换。dry_run 只回报命中数与前几条样例，不动工程。"""
        import re

        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as error:
            raise PipelineError(
                "INVALID_MANGA_PATTERN", f"正则表达式无效：{error}", 422
            ) from error
        with self.store.lock:
            document = self.store.load(workspace_id)
            revision = document.revision
            samples = []
            changed = 0
            for page in document.pages:
                if page_ids and page.id not in page_ids:
                    continue
                for region in page.regions:
                    before = getattr(region, field)
                    if not before:
                        continue
                    after = regex.sub(replacement, before)
                    if after == before:
                        continue
                    changed += 1
                    if len(samples) < 20:
                        samples.append(
                            {
                                "page_id": page.id,
                                "page": page.name,
                                "region_id": region.id,
                                "before": before,
                                "after": after,
                            }
                        )
                    if not dry_run:
                        setattr(region, field, after)
                        if field == "source":
                            region.translation_state, region.translation_error = (
                                "idle",
                                "",
                            )
                            region.translation_cache = {}
                            region.translation_provider = ""
            workspace = document
            if changed and not dry_run:
                workspace = self.store.save(document, revision)
            return {
                "workspace": workspace.model_dump(),
                "changed": changed,
                "samples": samples,
                "dry_run": dry_run,
            }

    def import_translation(self, workspace_id, path):
        path = Path(path)
        if not path.is_file():
            raise PipelineError(
                "INVALID_MANGA_TRANSLATIONS", "译文文件不存在或不可读", 422
            )
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
        with self.store.lock:
            document = self.store.load(workspace_id)
            revision = document.revision
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
