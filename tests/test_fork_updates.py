import json

from Backend.core.platform.platform_paths import release_api_url as backend_release_api
from Frontend.platform.platform_paths import release_api_url
from Frontend.services.fork_updates import (
    RELEASES_API,
    own_download_url,
    release_version,
)
from Tools.stamp_fork_version import stamp


def test_only_our_repository_is_an_update_source():
    assert release_api_url() == backend_release_api() == RELEASES_API
    assert own_download_url(
        "https://github.com/Xitaoyou/AiNieePlus/releases/download/manga-10-abcdef0/app.zip"
    )
    for url in (
        "https://github.com/NEKOparapa/AiNiee/releases/download/10/app.zip",
        "https://github.com.evil.test/Xitaoyou/AiNieePlus/releases/download/a/b",
        "http://github.com/Xitaoyou/AiNieePlus/releases/download/a/b",
    ):
        assert not own_download_url(url)
    assert release_version("manga-12-abcdef0") == "12"
    assert release_version("v7.4.0") == "0"


def test_ci_stamp_keeps_upstream_version_and_uses_monotonic_build_number(tmp_path):
    path = tmp_path / "Frontend/resources/version/version.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": "AiNiee 7.4.0 dev"}), encoding="utf-8")
    stamp(tmp_path, 17, "revision")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == "AiNieePlus 17"
    assert data["upstream_version"] == "AiNiee 7.4.0 dev"
    stamp(tmp_path, 18, "next")
    assert (
        json.loads(path.read_text(encoding="utf-8"))["upstream_version"]
        == "AiNiee 7.4.0 dev"
    )
