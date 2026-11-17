"""Prepare a normal upstream merge. Conflicts abort; this tool never force-pushes."""

import argparse
import json
import os
import subprocess
from pathlib import Path

UPSTREAM = "https://github.com/NEKOparapa/AiNiee.git"


def git(root, *args, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=check,
    )


def prepare_merge(root, upstream_ref="refs/remotes/upstream/main"):
    if git(root, "status", "--porcelain").stdout.strip():
        raise RuntimeError("同步需要干净的工作区；未覆盖任何本地修改")
    before = git(root, "rev-parse", "HEAD").stdout.strip()
    upstream = git(root, "rev-parse", upstream_ref).stdout.strip()
    if (
        git(
            root, "merge-base", "--is-ancestor", upstream, "HEAD", check=False
        ).returncode
        == 0
    ):
        return {"changed": False, "upstream": upstream, "head": before, "conflicts": []}
    result = git(root, "merge", "--no-ff", "--no-edit", upstream_ref, check=False)
    if result.returncode:
        conflicts = git(
            root, "diff", "--name-only", "--diff-filter=U"
        ).stdout.splitlines()
        git(root, "merge", "--abort", check=False)
        return {
            "changed": False,
            "upstream": upstream,
            "head": before,
            "conflicts": conflicts,
            "error": "上游合并失败，已中止，未推送或覆盖 Fork 的定制功能",
        }
    return {
        "changed": True,
        "upstream": upstream,
        "head": git(root, "rev-parse", "HEAD").stdout.strip(),
        "conflicts": [],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    git(root, "fetch", "--no-tags", UPSTREAM, "main:refs/remotes/upstream/main")
    result = prepare_merge(root)
    args.report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write(f"changed={'true' if result['changed'] else 'false'}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(
                "## AiNiee 上游同步\n\n```json\n"
                + json.dumps(result, ensure_ascii=False, indent=2)
                + "\n```\n"
            )
    if result.get("error"):
        raise SystemExit(2)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
