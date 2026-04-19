"""Apply a markdown suggestion to the canonical docs repo as a git branch + commit.

Pure functions (no FastMCP dependency). Used by the `propose_doc_patch` MCP tool
to stage a reviewable local commit inside the `<DOCS_REPO_NAME>` clone, and
(optionally) push + open a GitHub Pull Request.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

log = logging.getLogger("memory")


DEFAULT_OWNER = "alephfornet"
DEFAULT_REPO = "<DOCS_REPO_NAME>"


# ---------------------------------------------------------------------------
# Lazy imports from indexer — avoid importing at module load so tests can
# monkeypatch `indexer.REPO_PATH` after import.
# ---------------------------------------------------------------------------

def _get_repo_path() -> Path:
    import indexer
    return Path(indexer.REPO_PATH)


def _get_content_subdir() -> str:
    import indexer
    return indexer.CONTENT_SUBDIR


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class PatchResult:
    status: str                       # committed | skipped | error | dry_run
    branch: str | None = None
    commit_sha: str | None = None
    commit_message: str | None = None
    diff_preview: str | None = None
    target_path: str | None = None
    error: str | None = None
    pr_url: str | None = None          # populated when open_pr=True succeeded
    pushed: bool = False

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 40) -> str:
    """Produce a branch-name-safe slug from free text."""
    s = (text or "").strip().lower()
    s = _SLUG_RE.sub("-", s).strip("-")
    if not s:
        s = "topic"
    return s[:max_len].rstrip("-") or "topic"


def _run(cmd: list[str], cwd: Path, check: bool = True,
         env: dict | None = None) -> str:
    """Run a subprocess command, capture stdout/stderr, raise on failure."""
    full_env = {**os.environ, **(env or {}), "GIT_TERMINAL_PROMPT": "0"}
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                       env=full_env)
    if check and r.returncode != 0:
        raise RuntimeError(
            f"cmd failed: {' '.join(cmd)}\nstderr: {r.stderr.strip()}"
        )
    return r.stdout.strip()


def _git(args: list[str], cwd: Path, check: bool = True,
         env: dict | None = None) -> str:
    return _run(["git", *args], cwd, check=check, env=env)


# ---------------------------------------------------------------------------
# Repo ops
# ---------------------------------------------------------------------------

def ensure_clean_repo(repo_path: Path, *, allowed_paths: set[str] | None = None) -> None:
    """Fail if the working tree has uncommitted changes outside `allowed_paths`.

    Unlike the indexer's `ensure_repo`, this does NOT fetch/pull — callers may
    want to work offline (tests). The freshness is the caller's concern.
    """
    out = _git(["status", "--porcelain"], repo_path, check=True)
    if not out:
        return
    allowed = allowed_paths or set()
    dirty: list[str] = []
    for line in out.splitlines():
        # e.g. " M content/foo.md" or "?? new.md"
        if len(line) < 3:
            continue
        path = line[3:].strip()
        if path not in allowed:
            dirty.append(path)
    if dirty:
        raise RuntimeError(
            "dirty working tree: uncommitted changes detected in "
            + ", ".join(dirty[:5])
            + ("…" if len(dirty) > 5 else "")
        )


def checkout_main(repo_path: Path) -> None:
    """Checkout main (no fetch). Assumes caller has already synced if needed."""
    _git(["checkout", "main"], repo_path)


def branch_exists(repo_path: Path, branch: str) -> bool:
    out = _git(["branch", "--list", branch], repo_path, check=False)
    return bool(out.strip())


def unique_branch_name(repo_path: Path, base: str) -> str:
    """Return `base`, or `base-2`, `base-3`, … so it doesn't collide."""
    if not branch_exists(repo_path, base):
        return base
    i = 2
    while branch_exists(repo_path, f"{base}-{i}"):
        i += 1
    return f"{base}-{i}"


def create_branch(repo_path: Path, branch_name: str) -> None:
    """`git checkout -b branch_name` from current HEAD (expected: main)."""
    _git(["checkout", "-b", branch_name], repo_path)


# ---------------------------------------------------------------------------
# Markdown section insertion
# ---------------------------------------------------------------------------

_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _normalize_heading(h: str) -> str:
    return re.sub(r"\s+", " ", (h or "").strip().lower())


def _find_section_bounds(text: str, section_anchor: str | None) -> tuple[int, int]:
    """Return (insert_at, next_h2_at) character offsets.

    Heuristic:
      * If `section_anchor` matches an existing H2 (case-insensitive, whitespace-
        collapsed), insert the block just BEFORE the NEXT H2 (or EOF).
      * Else: insert at EOF.

    `insert_at` is the offset where the new block's leading newline should go.
    `next_h2_at` is the same value (kept for clarity / potential future use).
    """
    anchor = _normalize_heading(section_anchor) if section_anchor else ""
    headings = list(_H2_RE.finditer(text))

    target_idx = -1
    if anchor:
        for i, m in enumerate(headings):
            if _normalize_heading(m.group(1)) == anchor:
                target_idx = i
                break

    if target_idx < 0:
        # Fallback: append at EOF
        return len(text), len(text)

    # Insert BEFORE the next H2 after the matched one (or EOF)
    if target_idx + 1 < len(headings):
        pos = headings[target_idx + 1].start()
    else:
        pos = len(text)
    return pos, pos


def insert_into_file(target_rel_path: str, section_anchor: str | None,
                     markdown_block: str) -> tuple[Path, str]:
    """Locate target file under `{REPO_PATH}/{CONTENT_SUBDIR}/{target_rel_path}`
    and insert `markdown_block` after the matched H2 section (or at EOF).

    Returns (absolute_path_modified, short_description).
    """
    repo = _get_repo_path()
    content = _get_content_subdir()

    # Handle both "<section>/<page>.md" and "/<section>/<page>.md"
    rel = target_rel_path.lstrip("/")

    abs_path = (repo / content / rel).resolve()
    # Basic traversal guard
    base = (repo / content).resolve()
    try:
        abs_path.relative_to(base)
    except ValueError:
        raise RuntimeError(f"target path escapes content dir: {target_rel_path}")

    if not abs_path.exists():
        # Be tolerant: if user passed '<section>/<page>' try .md/.mdx
        for ext in (".md", ".mdx"):
            cand = abs_path.with_suffix(ext)
            if cand.exists():
                abs_path = cand
                break
        else:
            raise RuntimeError(f"target file does not exist: {rel}")

    text = abs_path.read_text(encoding="utf-8")
    insert_at, _ = _find_section_bounds(text, section_anchor)

    # Ensure tidy spacing: at least one blank line before the inserted block,
    # one blank line after.
    before = text[:insert_at]
    after = text[insert_at:]
    prefix = "" if before.endswith("\n\n") else ("\n" if before.endswith("\n") else "\n\n")
    if not before:
        prefix = ""
    block = markdown_block if markdown_block.endswith("\n") else markdown_block + "\n"
    suffix = "" if after.startswith("\n") or not after else "\n"

    new_text = before + prefix + block + suffix + after
    abs_path.write_text(new_text, encoding="utf-8")

    desc = (
        f"inserted {len(block.splitlines())} lines into {rel}"
        + (f" after section '{section_anchor}'" if section_anchor else " at EOF")
    )
    return abs_path, desc


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

_AUTHOR_ENV = {
    "GIT_AUTHOR_NAME": "aleph-docs-mcp",
    "GIT_AUTHOR_EMAIL": "mcp@example.com",
    "GIT_COMMITTER_NAME": "aleph-docs-mcp",
    "GIT_COMMITTER_EMAIL": "mcp@example.com",
}


def commit_and_return(repo_path: Path, commit_message_subject: str,
                      commit_message_body: str) -> tuple[str, str]:
    """`git add -A` + `git commit`. Returns (sha, diff_preview)."""
    _git(["add", "-A"], repo_path)

    # Truncate body if >4000 chars
    body = commit_message_body or ""
    if len(body) > 4000:
        body = body[:3990] + "\n…[truncated]"

    msg = commit_message_subject.strip()
    if body.strip():
        msg = f"{msg}\n\n{body.strip()}"

    _git(["commit", "-m", msg], repo_path, env=_AUTHOR_ENV)
    sha = _git(["rev-parse", "HEAD"], repo_path)

    # Capture diff preview: first 40 lines of `git show --stat + patch` vs parent
    diff_full = _git(["show", "--format=", "HEAD"], repo_path, check=False)
    diff_lines = diff_full.splitlines()[:40]
    return sha, "\n".join(diff_lines)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def apply_patch(topic: str,
                target_rel_path: str,
                section_anchor: str | None,
                markdown_block: str,
                commit_message_subject: str,
                commit_message_body: str,
                dry_run: bool = False,
                branch_prefix: str = "docs/mcp-") -> PatchResult:
    """Orchestrate branch + insert + commit.

    Safety:
      * refuses on dirty working tree
      * refuses if target file does not exist (v1)
      * auto-suffixes branch name if it already exists
      * never pushes
    """
    try:
        repo = _get_repo_path()
        if not repo.exists():
            return PatchResult(status="error", error=f"repo not found: {repo}")

        # Stamp branch name
        stamp = datetime.utcnow().strftime("%Y%m%d-%H%M")
        base_branch = f"{branch_prefix}{slugify(topic)}-{stamp}"

        if dry_run:
            # No filesystem writes, no git ops — just describe what would happen.
            return PatchResult(
                status="dry_run",
                branch=base_branch,
                target_path=target_rel_path,
                commit_message=commit_message_subject,
                diff_preview=markdown_block[:2000],
            )

        # Check clean state BEFORE branching
        try:
            ensure_clean_repo(repo)
        except RuntimeError as e:
            return PatchResult(status="error", error=str(e))

        # Make sure we start from main
        try:
            checkout_main(repo)
        except RuntimeError as e:
            return PatchResult(status="error",
                               error=f"could not checkout main: {e}")

        branch = unique_branch_name(repo, base_branch)
        try:
            create_branch(repo, branch)
        except RuntimeError as e:
            return PatchResult(status="error",
                               error=f"branch creation failed: {e}")

        try:
            _, desc = insert_into_file(target_rel_path, section_anchor,
                                       markdown_block)
            log.info("doc_patch: %s", desc)
        except RuntimeError as e:
            # Rollback: abort the branch — checkout main, delete new branch
            _git(["checkout", "main"], repo, check=False)
            _git(["branch", "-D", branch], repo, check=False)
            return PatchResult(status="error", error=str(e))

        try:
            sha, diff_preview = commit_and_return(
                repo, commit_message_subject, commit_message_body
            )
        except RuntimeError as e:
            _git(["checkout", "main"], repo, check=False)
            _git(["branch", "-D", branch], repo, check=False)
            return PatchResult(status="error",
                               error=f"commit failed: {e}")

        return PatchResult(
            status="committed",
            branch=branch,
            commit_sha=sha,
            commit_message=commit_message_subject,
            diff_preview=diff_preview,
            target_path=target_rel_path,
        )
    except Exception as e:  # pragma: no cover - defensive
        log.exception("apply_patch unexpected error")
        return PatchResult(status="error", error=f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# GitHub push + PR (optional, gated by DOCS_WRITE_TOKEN env var)
# ---------------------------------------------------------------------------

def _get_token() -> str:
    return (os.getenv("DOCS_WRITE_TOKEN") or "").strip()


def push_branch(repo_path: Path, branch: str,
                owner: str = DEFAULT_OWNER,
                repo: str = DEFAULT_REPO) -> None:
    """git push the branch using DOCS_WRITE_TOKEN as x-access-token.

    Raises RuntimeError if the token is missing or the push fails.
    The token is never logged; only the URL with a placeholder is echoed
    back to the caller's error message.
    """
    token = _get_token()
    if not token:
        raise RuntimeError("DOCS_WRITE_TOKEN not set — cannot push")
    url = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
    try:
        subprocess.run(
            ["git", "push", "-u", url, branch],
            cwd=str(repo_path), check=True,
            capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        # scrub the token from any error output before surfacing
        stderr = (e.stderr or "").replace(token, "<redacted>")
        raise RuntimeError(f"git push failed: {stderr.strip()}") from e


def open_pull_request(branch: str, title: str, body: str,
                      owner: str = DEFAULT_OWNER,
                      repo: str = DEFAULT_REPO,
                      base: str = "main") -> str:
    """Open a GitHub PR via the REST API. Returns the html_url.

    Raises RuntimeError on failure (token missing, non-2xx response, etc.).
    """
    token = _get_token()
    if not token:
        raise RuntimeError("DOCS_WRITE_TOKEN not set — cannot open PR")

    payload = json.dumps({
        "title": title[:250],
        "body": body[:60_000],
        "head": branch,
        "base": base,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/pulls",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "aleph-docs-mcp",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body_data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode(errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"GitHub PR create failed HTTP {e.code}: {detail[:400]}") from e
    except Exception as e:
        raise RuntimeError(f"GitHub PR create error: {type(e).__name__}: {e}") from e
    url = body_data.get("html_url")
    if not url:
        raise RuntimeError(f"GitHub PR response missing html_url: {body_data!r}")
    return url
