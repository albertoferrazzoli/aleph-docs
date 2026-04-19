"""Header-aware Markdown chunker for semantic-memory indexing.

Splits a Markdown body on H2 boundaries (with H3 sub-splits and a sliding
window fallback), preserving fenced code-block integrity, and emits
Chunk objects tagged with stable anchors and sha256 hashes.

See PRD_SEMANTIC_MEMORY.md §3.4 for the algorithm.
"""

from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

# Reuse the canonical MD parsers from the existing indexer — do NOT
# reimplement them.
from indexer import parse_frontmatter, extract_headings  # noqa: E402


MAX_TOKENS = 1500
WINDOW_TOKENS = 800
OVERLAP_TOKENS = 100
WINDOW_CHARS = WINDOW_TOKENS * 4   # 3200
OVERLAP_CHARS = OVERLAP_TOKENS * 4  # 400


_H2_RE = re.compile(r"^##\s+(.+?)\s*$")
_H3_RE = re.compile(r"^###\s+(.+?)\s*$")


@dataclass
class Chunk:
    section_anchor: str
    title: str
    content: str
    hash: str
    token_estimate: int
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tok(s: str) -> int:
    return len(s) // 4


def _slugify(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return s


def _derive_title(frontmatter: dict, headings: list[dict], rel_path: str) -> str:
    if frontmatter.get("title"):
        return str(frontmatter["title"])
    for h in headings:
        if h["level"] == 1:
            return h["text"]
    name = Path(rel_path).stem
    if name == "index":
        name = Path(rel_path).parent.name or name
    return name.replace("-", " ").replace("_", " ").title()


def _split_preserving_fences(lines: list[str], boundary_matcher) -> list[tuple[str | None, list[str]]]:
    """Walk `lines`, split whenever `boundary_matcher(line)` returns a header
    text AND we are NOT inside a fenced code block.

    Returns a list of (header_text_or_None, section_lines). The first section
    (None header) is the preamble before the first matched boundary.
    """
    sections: list[tuple[str | None, list[str]]] = []
    current_header: str | None = None
    current_lines: list[str] = []
    in_fence = False

    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            current_lines.append(line)
            continue

        if not in_fence:
            hdr = boundary_matcher(line)
            if hdr is not None:
                # Flush current section
                sections.append((current_header, current_lines))
                current_header = hdr
                current_lines = []
                continue

        current_lines.append(line)

    sections.append((current_header, current_lines))
    return sections


def _h2_matcher(line: str) -> str | None:
    m = _H2_RE.match(line)
    return m.group(1).strip() if m else None


def _h3_matcher(line: str) -> str | None:
    m = _H3_RE.match(line)
    return m.group(1).strip() if m else None


def _sliding_windows(text: str) -> list[str]:
    """Split `text` into overlapping windows of WINDOW_CHARS chars with
    OVERLAP_CHARS char overlap. Never breaks inside a fenced code block:
    if a window boundary lands inside a fence, extend the window until the
    fence closes.
    """
    if len(text) <= WINDOW_CHARS:
        return [text]

    # Precompute fence state per character position.
    fence_open_at: list[bool] = [False] * (len(text) + 1)
    in_fence = False
    i = 0
    lines_cursor = 0
    # Easier: walk line-by-line accumulating offsets.
    offset = 0
    fence_at_offset: dict[int, bool] = {0: False}
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        offset += len(line)
        fence_at_offset[offset] = in_fence

    def in_fence_at(pos: int) -> bool:
        # Find the largest recorded offset <= pos
        best = 0
        for off in fence_at_offset:
            if off <= pos and off > best:
                best = off
        return fence_at_offset[best]

    def extend_past_fence(pos: int) -> int:
        """If pos falls inside a fence, push it forward until the fence closes."""
        if not in_fence_at(pos):
            return pos
        # Walk forward through lines until we exit the fence.
        # Rebuild by scanning from 0; cheap enough for doc-sized inputs.
        in_f = False
        off = 0
        for line in text.splitlines(keepends=True):
            new_off = off + len(line)
            was_in = in_f
            if line.lstrip().startswith("```"):
                in_f = not in_f
            # If pos is within this line and we're inside a fence, keep going
            if off <= pos < new_off and was_in:
                # continue scanning until fence closes
                pass
            if off > pos and not in_f and was_in is False:
                # we've exited
                return off
            if off >= pos and not in_f:
                return off
            off = new_off
        return len(text)

    windows: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + WINDOW_CHARS, n)
        end = extend_past_fence(end)
        if end <= start:
            end = n
        windows.append(text[start:end])
        if end >= n:
            break
        start = max(end - OVERLAP_CHARS, start + 1)
    return windows


def _make_chunk(
    title: str,
    section_path: str,
    section_anchor: str,
    body: str,
    source_path: str,
    level: int,
    extra_meta: dict | None = None,
) -> Chunk:
    prefix = f"# {title}\n## {section_path}\n\n"
    content = prefix + body
    h = hashlib.sha256(content.encode("utf-8")).hexdigest()
    md = {
        "source_path": source_path,
        "section_path": section_path,
        "level": level,
    }
    if extra_meta:
        md.update(extra_meta)
    return Chunk(
        section_anchor=section_anchor,
        title=title,
        content=content,
        hash=h,
        token_estimate=_tok(content),
        metadata=md,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def chunk(rel_path: str, body: str, frontmatter: dict, headings: list[dict]) -> list[Chunk]:
    title = _derive_title(frontmatter, headings, rel_path)
    lines = body.splitlines(keepends=True)

    h2_sections = _split_preserving_fences(lines, _h2_matcher)

    chunks: list[Chunk] = []
    for h2_header, h2_lines in h2_sections:
        h2_text = "".join(h2_lines).strip("\n")
        if not h2_text.strip() and h2_header is None:
            continue

        if h2_header is None:
            h2_anchor = "intro"
            h2_path_part = "Intro"
            level = 0  # preamble
        else:
            h2_anchor = _slugify(h2_header)
            h2_path_part = h2_header
            level = 2

        section_path_h2 = f"{title} > {h2_path_part}"

        # If fits, emit a single H2-level chunk
        if _tok(h2_text) <= MAX_TOKENS:
            if h2_text.strip():
                chunks.append(
                    _make_chunk(
                        title=title,
                        section_path=section_path_h2,
                        section_anchor=h2_anchor,
                        body=h2_text,
                        source_path=rel_path,
                        level=level,
                    )
                )
            continue

        # Too big — sub-split on H3
        h3_sections = _split_preserving_fences(h2_lines, _h3_matcher)
        for h3_header, h3_lines in h3_sections:
            h3_text = "".join(h3_lines).strip("\n")
            if not h3_text.strip():
                continue

            if h3_header is None:
                # Preamble of the H2 before the first H3
                sub_anchor = h2_anchor
                sub_path = section_path_h2
                sub_level = level
            else:
                sub_anchor = f"{h2_anchor}#{_slugify(h3_header)}"
                sub_path = f"{section_path_h2} > {h3_header}"
                sub_level = 3

            if _tok(h3_text) <= MAX_TOKENS:
                chunks.append(
                    _make_chunk(
                        title=title,
                        section_path=sub_path,
                        section_anchor=sub_anchor,
                        body=h3_text,
                        source_path=rel_path,
                        level=sub_level,
                    )
                )
                continue

            # Still too big — sliding window
            windows = _sliding_windows(h3_text)
            for i, w in enumerate(windows):
                chunks.append(
                    _make_chunk(
                        title=title,
                        section_path=sub_path,
                        section_anchor=f"{sub_anchor}-p{i}",
                        body=w,
                        source_path=rel_path,
                        level=sub_level,
                        extra_meta={"window_index": i, "window_total": len(windows)},
                    )
                )

    return chunks


# ---------------------------------------------------------------------------
# Self-test CLI
# ---------------------------------------------------------------------------

def _main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python -m memory.chunker <file.md>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    text = path.read_text(encoding="utf-8", errors="replace")
    fm, body = parse_frontmatter(text)
    headings = extract_headings(body)
    chunks = chunk(str(path), body, fm, headings)
    for c in chunks:
        first = c.content.replace("\n", " ")[:50]
        print(f"{c.section_anchor}\t{c.token_estimate}\t{c.hash[:12]}\t{first}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
