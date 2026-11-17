"""AiNieePlus release identity; upstream is never an application update source."""

import re
from urllib.parse import urlsplit

REPOSITORY = "Xitaoyou/AiNieePlus"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
RELEASES_API = f"https://api.github.com/repos/{REPOSITORY}/releases"
WINDOWS_ASSET = "AiNiee-Beta-Windows-Portable.zip"


def own_download_url(url):
    parsed = urlsplit(url or "")
    return (
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.path.lower().startswith(f"/{REPOSITORY}/releases/download/".lower())
    )


def release_version(tag):
    match = re.fullmatch(r"manga-(\d+)-[a-f0-9]{7,40}", tag or "")
    return match.group(1) if match else "0"
