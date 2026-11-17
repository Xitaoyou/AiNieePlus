from Tools.sync_upstream import git, prepare_merge


def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "base").write_text("base", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    git(root, "branch", "upstream-test")
    return root


def test_upstream_merge_keeps_custom_files_and_noop_does_not_commit(tmp_path):
    root = repository(tmp_path)
    (root / "custom").write_text("manga + own updater", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "custom")
    git(root, "checkout", "upstream-test")
    (root / "upstream-new").write_text("upstream feature", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "upstream")
    git(root, "checkout", "main")
    result = prepare_merge(root, "upstream-test")
    assert result["changed"] and not result["conflicts"]
    assert (root / "custom").read_text(encoding="utf-8") == "manga + own updater"
    assert (root / "upstream-new").is_file()
    again = prepare_merge(root, "upstream-test")
    assert not again["changed"] and again["head"] == result["head"]


def test_conflict_aborts_without_overwriting_customization(tmp_path):
    root = repository(tmp_path)
    (root / "base").write_text("our fork", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "ours")
    before = git(root, "rev-parse", "HEAD").stdout.strip()
    git(root, "checkout", "upstream-test")
    (root / "base").write_text("upstream", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "theirs")
    git(root, "checkout", "main")
    result = prepare_merge(root, "upstream-test")
    assert result["error"] and result["conflicts"] == ["base"]
    assert git(root, "rev-parse", "HEAD").stdout.strip() == before
    assert (root / "base").read_text(encoding="utf-8") == "our fork"
    assert not git(root, "status", "--porcelain").stdout.strip()
