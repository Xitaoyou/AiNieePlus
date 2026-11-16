"""Verify a silently installed x64 release, independent of a development PATH."""

import argparse
import os
from pathlib import Path
import struct
import subprocess


def machine(path):
    with path.open("rb") as stream:
        header = stream.read(64)
        if header[:2] != b"MZ":
            raise RuntimeError(f"Not PE: {path}")
        stream.seek(struct.unpack_from("<I", header, 60)[0])
        if stream.read(4) != b"PE\0\0":
            raise RuntimeError(f"Bad PE: {path}")
        return struct.unpack("<H", stream.read(2))[0]


def verify(root):
    root = Path(root).resolve()
    if (root / "portable.txt").exists():
        raise RuntimeError("Installer must use standard per-user data paths")
    for relative in ("AiNiee.exe", "backend/AiNieeBackend.exe"):
        if machine(root / relative) != 0x8664:
            raise RuntimeError(f"Not x86-64: {relative}")
    if not (root / "LICENSE").is_file():
        raise RuntimeError("Project license missing")
    backend = root / "backend/AiNieeBackend.exe"
    for check in (
        "--check-media-tools",
        "--check-pdf-runtime",
        "--check-manga-runtime",
    ):
        subprocess.run(
            [str(backend), check],
            cwd=root,
            env={**os.environ, "PATH": ""},
            check=True,
            timeout=180,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    verify(args.directory)
    print("Installed x86-64 release verified")
