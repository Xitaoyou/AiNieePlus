"""Independent manga transport, reusing image admission and existing job events."""

from typing import Literal

from fastapi import APIRouter, Depends, Header
from fastapi.responses import FileResponse
from pydantic import Field

from Backend.api.queued import run_queued
from Backend.api.schemas import RequestModel
from Backend.application.manga_service import MangaService
from Backend.application.pipeline_common import PipelineError
from Backend.domain.models.manga import MangaSettings, MangaWorkspace


class ReplacementRule(RequestModel):
    src: str = Field(min_length=1, max_length=4096)
    dst: str = Field(default="", max_length=4096)
    regex: bool = False


class PunctuationUpdate(RequestModel):
    enabled: bool
    rows: list[ReplacementRule] | None = Field(default=None, max_length=2000)
    baseline: list[ReplacementRule] | None = Field(default=None, max_length=2000)


class MangaImport(RequestModel):
    paths: list[str] = Field(min_length=1, max_length=2000)
    settings: MangaSettings = Field(default_factory=MangaSettings)
    workspace_id: str | None = None
    recognize: bool = False
    title: str | None = Field(default=None, max_length=200)


class MangaSave(RequestModel):
    workspace: MangaWorkspace
    expected_revision: int = Field(ge=0)


class MangaOptions(RequestModel):
    replace_regions: bool = Field(default=False, strict=True)
    overwrite_source: bool = Field(default=False, strict=True)
    retranslate: bool = Field(default=False, strict=True)


class MangaRun(RequestModel):
    stage: Literal[
        "detect", "ocr", "recognize", "translate", "clean", "render", "pipeline"
    ]
    page_ids: list[str] = Field(default_factory=list)
    region_ids: list[str] = Field(default_factory=list)
    options: MangaOptions = Field(default_factory=MangaOptions)
    expected_revision: int = Field(ge=0)
    operation_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{16,80}$")


class MangaExport(RequestModel):
    output_dir: str = Field(min_length=1)
    mode: Literal[
        "png", "jpeg", "zip", "project", "i18next", "i18next-source", "i18next-target"
    ] = "png"


class ModelImport(RequestModel):
    path: str = Field(min_length=1)


def create_manga_router(runtime, authorized, service=None):
    router = APIRouter(
        prefix="/api/v2/manga", tags=["Manga"], dependencies=[Depends(authorized)]
    )
    service = service or MangaService()
    runtime.manga = service

    from Backend.application.manga_punctuation import get_state, update_rules

    service.punctuation_provider = lambda: get_state(runtime.load_config())

    @router.get("/punctuation")
    def punctuation():
        return get_state(runtime.load_config())

    @router.post("/punctuation")
    def save_punctuation(body: PunctuationUpdate):
        rows = [r.model_dump() for r in body.rows] if body.rows is not None else None
        baseline = (
            [r.model_dump() for r in body.baseline]
            if body.baseline is not None
            else None
        )
        config = runtime.update_config(
            lambda config: update_rules(config, body.enabled, rows, baseline)
        )
        return get_state(config)

    @router.get("/capabilities")
    def capabilities():
        return {"editor_version": 4, "progressive_recognition": True}

    @router.get("/models")
    def models():
        return {"models": service.models.catalogue()}

    @router.post("/models/{model_id}/import")
    async def import_model(
        model_id: str, body: ModelImport, prefer: str | None = Header(default=None)
    ):
        return await run_queued(
            runtime,
            "manga/model/import",
            lambda c: {"models": service.models.import_folder(model_id, body.path)},
            lane="image",
            prefer=prefer,
        )

    @router.post("/models/{model_id}/download")
    async def download_model(model_id: str, prefer: str | None = Header(default=None)):
        return await run_queued(
            runtime,
            "manga/model/download",
            lambda c: {"models": service.models.download(model_id, c)},
            lane="image",
            prefer=prefer,
        )

    @router.post("/models/{model_id}/verify")
    def verify_model(model_id: str):
        return {"path": str(service.models.verify(model_id)), "status": "verified"}

    @router.get("/fonts")
    def fonts():
        from Backend.infrastructure.manga.render import font_files

        return {"fonts": font_files()}

    @router.get("")
    def history():
        return {"workspaces": service.store.history()}

    @router.post("/project/import")
    async def import_project(
        body: ModelImport, prefer: str | None = Header(default=None)
    ):
        return await run_queued(
            runtime,
            "manga/project/import",
            lambda c: {
                "workspace": service.store.import_project(body.path, c).model_dump()
            },
            lane="image",
            prefer=prefer,
        )

    @router.post("/{workspace_id}/translations")
    async def import_translation(
        workspace_id: str, body: ModelImport, prefer: str | None = Header(default=None)
    ):
        return await run_queued(
            runtime,
            "manga/translations/import",
            lambda c: service.import_translation(workspace_id, body.path),
            lane="image",
            prefer=prefer,
            project_id=workspace_id,
        )

    @router.post("/import")
    async def import_images(
        body: MangaImport, prefer: str | None = Header(default=None)
    ):
        def action(context):
            doc = service.store.import_images(
                body.paths, body.settings.model_dump(), body.workspace_id, context
            )
            if body.title:
                doc.title = body.title.strip() or doc.title
            if body.recognize and doc.settings.auto_recognize:
                return service.run(doc.id, "recognize", context=context)
            return {"status": "success", "workspace": doc.model_dump(), "report": {}}

        return await run_queued(
            runtime, "manga/import", action, lane="image", prefer=prefer
        )

    @router.get("/{workspace_id}")
    def load(workspace_id: str):
        return {"workspace": service.store.load(workspace_id).model_dump()}

    @router.delete("/{workspace_id}")
    async def delete(workspace_id: str, prefer: str | None = Header(default=None)):
        service.store.load(workspace_id)
        for job in runtime.scheduler.list(lane="image", limit=1000):
            if job.get("project_id") == workspace_id and job["state"] in {
                "queued",
                "running",
                "cancelling",
            }:
                runtime.scheduler.cancel(job["id"])
        # The same serial image lane lets any cancelled inference finish before
        # deletion, so a late worker cannot recreate the removed project.
        return await run_queued(
            runtime,
            "manga/delete",
            lambda c: service.store.delete(workspace_id) or {"deleted": workspace_id},
            lane="image",
            prefer=prefer,
            project_id=workspace_id,
        )

    @router.patch("/{workspace_id}")
    def save(workspace_id: str, body: MangaSave):
        if workspace_id != body.workspace.id:
            raise PipelineError("INVALID_MANGA_ID", "工程身份不匹配", 422)
        return {
            "workspace": service.store.save(
                body.workspace, body.expected_revision
            ).model_dump()
        }

    @router.post("/{workspace_id}/run")
    async def run(
        workspace_id: str, body: MangaRun, prefer: str | None = Header(default=None)
    ):
        def action(context):
            if service.store.load(workspace_id).revision != body.expected_revision:
                raise PipelineError(
                    "MANGA_CHANGED", "工程在排队期间已更新，请刷新后重试", 409
                )
            return service.run(
                workspace_id,
                body.stage,
                context=context,
                page_ids=body.page_ids,
                region_ids=body.region_ids,
                options=body.options.model_dump(),
            )

        return await run_queued(
            runtime,
            f"manga/{workspace_id}/{body.stage}",
            action,
            lane="image",
            prefer=prefer,
            project_id=workspace_id,
            operation_id=body.operation_id,
        )

    @router.post("/{workspace_id}/export")
    async def export(
        workspace_id: str, body: MangaExport, prefer: str | None = Header(default=None)
    ):
        return await run_queued(
            runtime,
            f"manga/{workspace_id}/export",
            lambda c: service.export(
                workspace_id, body.output_dir, body.mode, context=c
            ),
            lane="image",
            prefer=prefer,
            project_id=workspace_id,
        )

    @router.get("/{workspace_id}/pages/{page_id}/{variant}")
    def asset(
        workspace_id: str,
        page_id: str,
        variant: Literal["source", "clean", "render", "mask", "layer"],
    ):
        return FileResponse(
            service.store.asset(workspace_id, page_id, variant),
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    return router
