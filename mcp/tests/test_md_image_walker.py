"""Tests for the markdown-referenced image walker in indexer._extract_image_refs.

Novel to Babel (not present in aleph-docs): indexes screenshots embedded
via `![alt](url)` or `<img src="...">` in the nextra-docs-babel repo.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Import the helper from indexer.py without triggering main().
_THIS = Path(__file__).resolve()
_MCP_ROOT = _THIS.parent.parent
sys.path.insert(0, str(_MCP_ROOT))

import indexer  # noqa: E402


def test_extract_image_refs(tmp_path: Path):
    """Build a fake Nextra repo, assert resolution rules hold."""
    repo = tmp_path
    content_dir = repo / "content" / "guides"
    public_dir = repo / "public" / "images"
    rel_dir = content_dir / "screenshots"
    content_dir.mkdir(parents=True)
    public_dir.mkdir(parents=True)
    rel_dir.mkdir(parents=True)

    # Create three image files on disk (relative, absolute-public, absent
    # external URL skipped). Also a non-image .txt to confirm filtering.
    rel_img = rel_dir / "setup-wizard.jpg"
    abs_img = public_dir / "obfuscation-flow.png"
    rel_img.write_bytes(b"fake-jpg")
    abs_img.write_bytes(b"fake-png")
    (rel_dir / "notes.txt").write_bytes(b"not an image")

    md_path = content_dir / "obfuscation.mdx"
    md_path.write_text(
        "# Obfuscation rules\n"
        "\n"
        "![Obfuscation pipeline diagram](/images/obfuscation-flow.png)\n"
        "\n"
        'See also <img src="./screenshots/setup-wizard.jpg" alt="Setup wizard" />\n'
        "\n"
        "External: ![Logo](https://example.com/logo.png)\n"
        "\n"
        "Data URI: ![inline](data:image/png;base64,AAAA)\n",
        encoding="utf-8",
    )

    refs = indexer._extract_image_refs(
        md_path.read_text(encoding="utf-8"), md_path, repo
    )

    # 2 resolved paths; external URL and data URI both skipped.
    assert len(refs) == 2, refs
    resolved_paths = {p for p, _ in refs}
    assert abs_img.resolve() in resolved_paths
    assert rel_img.resolve() in resolved_paths

    # alt text preserved.
    by_path = {p: alt for p, alt in refs}
    assert by_path[abs_img.resolve()] == "Obfuscation pipeline diagram"
    assert by_path[rel_img.resolve()] == "Setup wizard"


def test_extract_image_refs_rejects_traversal(tmp_path: Path):
    """../ paths escaping repo_root must be filtered out."""
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    (repo / "content").mkdir(parents=True)
    outside.mkdir()
    evil = outside / "evil.png"
    evil.write_bytes(b"x")

    md_path = repo / "content" / "x.mdx"
    md_path.write_text("![e](../../outside/evil.png)\n", encoding="utf-8")

    refs = indexer._extract_image_refs(
        md_path.read_text(encoding="utf-8"), md_path, repo
    )
    assert refs == []


def test_extract_image_refs_strips_title_and_query(tmp_path: Path):
    """![alt](url "title") and url?query#frag must resolve to url."""
    repo = tmp_path
    (repo / "content").mkdir()
    img = repo / "content" / "a.png"
    img.write_bytes(b"x")

    md_path = repo / "content" / "doc.mdx"
    md_path.write_text('![a](./a.png "caption")\n', encoding="utf-8")

    refs = indexer._extract_image_refs(
        md_path.read_text(encoding="utf-8"), md_path, repo
    )
    assert len(refs) == 1
    assert refs[0][0] == img.resolve()
