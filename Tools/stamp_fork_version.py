"""Stamp CI artifacts with our monotonically increasing release build number."""

import json
import os
from pathlib import Path


def stamp(root, number, revision):
    path = Path(root) / "Frontend/resources/version/version.json"
    original = json.loads(path.read_text(encoding="utf-8"))
    upstream = original.get("upstream_version", original["version"])
    path.write_text(
        json.dumps(
            {
                "version": f"AiNieePlus {int(number)}",
                "build": int(number),
                "revision": revision,
                "repository": "Xitaoyou/AiNieePlus",
                "upstream_version": upstream,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    stamp(
        Path(__file__).resolve().parents[1],
        os.environ["GITHUB_RUN_NUMBER"],
        os.environ["GITHUB_SHA"],
    )
