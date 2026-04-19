"""CLI entrypoint for memory lint.

Usage:
    python -m memory.lint_cli [--mode auto|cheap|full|manual]

Exit code 0 on success (including skipped runs), non-zero on unexpected errors.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="memory.lint_cli",
                                description="Run memory lint checks.")
    p.add_argument(
        "--mode",
        choices=("auto", "cheap", "full", "manual"),
        default=os.environ.get("LINT_MODE", "auto"),
        help="Lint mode (default: env LINT_MODE or 'auto').",
    )
    return p.parse_args(argv)


async def _main(mode: str) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from memory import db, lint

    if not db.is_enabled():
        print(json.dumps({"skipped": True, "error": "memory disabled"}))
        return 0

    await db.init_pool()

    # Resolve repo_path for stale checks
    repo_path_str = os.environ.get("DOCS_REPO_PATH", "").strip()
    repo_path = Path(repo_path_str).resolve() if repo_path_str else None
    # stale check uses repo_path/<source_path> (source_path already includes
    # 'content/' prefix in bootstrap, so we pass raw repo).

    try:
        result = await lint.run_lint(mode=mode, repo_path=repo_path)
    finally:
        await db.close_pool()

    print(json.dumps(result, default=str))
    return 0 if not result.get("error") else 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_main(args.mode))
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        logging.getLogger("memory.lint").exception("[lint] CLI error")
        print(json.dumps({"error": str(e)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
