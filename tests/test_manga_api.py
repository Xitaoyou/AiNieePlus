from types import SimpleNamespace
import threading
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from PIL import Image
from Backend.api.manga import create_manga_router
from Backend.api.jobs import create_job_router
from Backend.application.job_scheduler import JobScheduler
from Backend.application.manga_service import MangaService
from Backend.application.manga_store import MangaStore
from Backend.application.pipeline_common import PipelineError


def test_authenticated_api_queue_revision_and_assets(tmp_path):
    scheduler = JobScheduler(tmp_path / "jobs.sqlite")

    def submit(name, action, **kwargs):
        kwargs.pop("background", None)
        return scheduler.submit(
            kwargs.pop("lane"),
            name,
            action,
            job_id=kwargs.pop("operation_id", None),
            **kwargs,
        ).to_dict()

    runtime = SimpleNamespace(
        scheduler=scheduler, run_job=submit, pipeline_progress=lambda p: None
    )
    service = MangaService(store=MangaStore(tmp_path / "projects"))

    def auth(authorization: str | None = Header(default=None)):
        if authorization != "Bearer test":
            raise HTTPException(401)

    app = FastAPI()

    @app.exception_handler(PipelineError)
    async def failure(request, error):
        return JSONResponse(
            status_code=error.status_code, content={"error": error.to_dict()}
        )

    app.include_router(create_manga_router(runtime, auth, service))
    app.include_router(create_job_router(runtime, auth))
    try:
        with TestClient(app) as client:
            assert client.get("/api/v2/manga/models").status_code == 401
            client.headers["Authorization"] = "Bearer test"
            source = tmp_path / "page.png"
            Image.new("RGB", (100, 100), "white").save(source)
            result = client.post(
                "/api/v2/manga/import",
                json={"paths": [str(source)], "settings": {"auto_recognize": False}},
            )
            assert result.status_code == 200, result.text
            doc = result.json()["workspace"]
            changed = client.patch(
                "/api/v2/manga/" + doc["id"],
                json={"workspace": doc, "expected_revision": doc["revision"]},
            )
            assert changed.status_code == 200
            assert (
                client.patch(
                    "/api/v2/manga/" + doc["id"],
                    json={"workspace": doc, "expected_revision": doc["revision"]},
                ).status_code
                == 409
            )
            asset = client.get(
                f"/api/v2/manga/{doc['id']}/pages/{doc['pages'][0]['id']}/source"
            )
            assert (
                asset.status_code == 200
                and asset.headers["cache-control"] == "no-store"
            )
            block = threading.Event()
            blocker = scheduler.submit("image", "test-block", lambda c: block.wait(5))
            response = client.post(
                f"/api/v2/manga/{doc['id']}/run",
                headers={"Prefer": "respond-async"},
                json={
                    "stage": "ocr",
                    "expected_revision": changed.json()["workspace"]["revision"],
                },
            )
            assert response.status_code == 202
            job = response.json()["job"]
            assert job["lane"] == "image" and job["state"] == "queued"
            cancel = client.post(f"/api/jobs/{job['id']}/cancel")
            assert cancel.json()["job"]["state"] == "cancelled"
            block.set()
            blocker.future.result(5)
            invalid = client.post(
                f"/api/v2/manga/{doc['id']}/run",
                json={
                    "stage": "ocr",
                    "expected_revision": 1,
                    "options": {"overwrite_source": "false"},
                },
            )
            assert invalid.status_code == 422
            assert (
                client.get("/api/v2/manga/capabilities").json()["editor_version"] == 3
            )
            assert client.delete("/api/v2/manga/" + doc["id"]).status_code == 200
            assert client.get("/api/v2/manga/" + doc["id"]).status_code == 404
            assert source.exists()

    finally:
        service.close()
        scheduler.close()
