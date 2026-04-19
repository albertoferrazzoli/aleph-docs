"""Tests for memory.doc_patch — local branch + commit workflow.

These tests build an ephemeral git repo under pytest's tmp_path and monkeypatch
`indexer.REPO_PATH` to point at it. No network, no real docs repo access.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixture: fake docs repo
# ---------------------------------------------------------------------------

def _git(args, cwd, check=True, env=None):
    full_env = {**os.environ, **(env or {}), "GIT_TERMINAL_PROMPT": "0"}
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, env=full_env)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args} failed: {r.stderr}")
    return r.stdout.strip()


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """Create a minimal git repo with a content/ dir and one .md file."""
    repo = tmp_path / "fake-docs"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "test"], cwd=repo)
    _git(["config", "commit.gpgsign", "false"], cwd=repo)

    content = repo / "content" / "guides"
    content.mkdir(parents=True)
    md = content / "example.md"
    md.write_text(
        "---\ntitle: Floating License\n---\n\n"
        "# Floating License\n\n"
        "Intro text.\n\n"
        "## Overview\n\n"
        "Floating licenses are shared.\n\n"
        "## Configuration\n\n"
        "See the config file.\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], cwd=repo)
    _git(["commit", "-q", "-m", "initial"], cwd=repo,
         env={"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
              "GIT_COMMITTER_NAME": "test",
              "GIT_COMMITTER_EMAIL": "test@example.com"})

    # Patch indexer module attributes
    import indexer
    monkeypatch.setattr(indexer, "REPO_PATH", repo)
    monkeypatch.setattr(indexer, "CONTENT_SUBDIR", "content")

    return repo


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_slugify():
    from memory.doc_patch import slugify
    assert slugify("Floating License") == "floating-license"
    assert slugify("  Weird! @#$ text ") == "weird-text"
    assert slugify("") == "topic"
    long = "a" * 100
    assert len(slugify(long, max_len=40)) == 40
    # No trailing dashes
    assert not slugify("hello---").endswith("-")


def test_apply_patch_happy_path(fake_repo):
    from memory.doc_patch import apply_patch

    block = (
        "## Note dal supporto (auto-suggerite)\n\n"
        "- Customer-X hit a race condition _(stability: 1.2, access_count: 3)_\n"
    )
    res = apply_patch(
        topic="floating license",
        target_rel_path="guides/example.md",
        section_anchor="Overview",
        markdown_block=block,
        commit_message_subject="docs: auto-suggestion for floating license",
        commit_message_body="supporting insights: …",
    )
    assert res.status == "committed", res.error
    assert res.branch and res.branch.startswith("docs/mcp-floating-license-")
    assert res.commit_sha

    # Verify log
    log_out = _git(["log", "--oneline", "-n", "2"], cwd=fake_repo)
    assert "docs: auto-suggestion for floating license" in log_out

    # Verify file was modified — block inserted before "## Configuration"
    md = (fake_repo / "content" / "guides" / "example.md").read_text()
    assert "Note dal supporto" in md
    # Order: Overview section → new block → Configuration
    assert md.index("Overview") < md.index("Note dal supporto") < md.index("Configuration")


def test_apply_patch_dirty_repo(fake_repo):
    from memory.doc_patch import apply_patch

    # Introduce an uncommitted change
    stray = fake_repo / "content" / "guides" / "stray.md"
    stray.write_text("dirty\n", encoding="utf-8")

    res = apply_patch(
        topic="floating",
        target_rel_path="guides/example.md",
        section_anchor="Overview",
        markdown_block="## x\n",
        commit_message_subject="docs: x",
        commit_message_body="",
    )
    assert res.status == "error"
    assert "dirty" in (res.error or "").lower()


def test_apply_patch_missing_target(fake_repo):
    from memory.doc_patch import apply_patch

    res = apply_patch(
        topic="nope",
        target_rel_path="guides/does-not-exist.md",
        section_anchor=None,
        markdown_block="## x\n",
        commit_message_subject="docs: x",
        commit_message_body="",
    )
    assert res.status == "error"
    assert "does not exist" in (res.error or "").lower()

    # Branch shouldn't linger
    branches = _git(["branch", "--list"], cwd=fake_repo)
    assert "docs/mcp-" not in branches


def test_apply_patch_branch_exists(fake_repo):
    from memory.doc_patch import apply_patch
    from datetime import datetime

    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M")
    existing = f"docs/mcp-collision-{stamp}"
    _git(["branch", existing], cwd=fake_repo)

    res = apply_patch(
        topic="collision",
        target_rel_path="guides/example.md",
        section_anchor="Overview",
        markdown_block="## Note\n\n- hi\n",
        commit_message_subject="docs: collision",
        commit_message_body="",
    )
    assert res.status == "committed", res.error
    # Should have -2 suffix (may be -2 or higher if rerun in same minute)
    assert res.branch != existing
    assert res.branch.startswith(existing)
    assert res.branch[len(existing):].startswith("-")


def test_apply_patch_dry_run(fake_repo):
    from memory.doc_patch import apply_patch

    res = apply_patch(
        topic="floating",
        target_rel_path="guides/example.md",
        section_anchor="Overview",
        markdown_block="## x\n",
        commit_message_subject="docs: x",
        commit_message_body="",
        dry_run=True,
    )
    assert res.status == "dry_run"
    assert res.branch and res.branch.startswith("docs/mcp-floating-")

    # No branch was actually created
    branches = _git(["branch", "--list"], cwd=fake_repo)
    assert "docs/mcp-" not in branches

    # No new commits
    log_out = _git(["log", "--oneline"], cwd=fake_repo)
    assert log_out.count("\n") == 0  # only the initial commit
