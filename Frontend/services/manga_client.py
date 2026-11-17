"""Manga transport; no inference libraries or backend implementation imports in Qt."""

from urllib.parse import quote


class MangaClient:
    def __init__(self, backend):
        self.backend = backend
        self.verified = False

    def request(self, method, path="", **kwargs):
        if not self.verified and path != "/capabilities":
            try:
                info = self.backend.request("GET", "/api/v2/manga/capabilities")
                if info.get("editor_version", 0) < 3:
                    raise RuntimeError("unsupported editor")
            except Exception as error:
                raise RuntimeError(
                    "漫画后端版本不匹配，请完全退出旧版 AiNiee 后重新打开新版。"
                ) from error
            self.verified = True
        return self.backend.request(method, "/api/v2/manga" + path, **kwargs)

    def save(self, workspace):
        return self.request(
            "PATCH",
            "/" + workspace["id"],
            json={"workspace": workspace, "expected_revision": workspace["revision"]},
        )

    def run(self, workspace, stage, page_ids=None, region_ids=None, options=None):
        return self.request(
            "POST",
            f"/{workspace['id']}/run",
            json={
                "stage": stage,
                "expected_revision": workspace["revision"],
                "page_ids": page_ids or [],
                "region_ids": region_ids or [],
                "options": options or {},
            },
            headers={"Prefer": "respond-async"},
        )

    def asset(self, workspace_id, page_id, variant):
        url = f"/api/v2/manga/{quote(workspace_id, safe='')}/pages/{quote(page_id, safe='')}/{variant}"
        response = self.backend.http.get(url, timeout=60)
        if response.is_error:
            raise RuntimeError("当前页原图不可用，请重新打开工程或检查图片文件。")
        return response.content
