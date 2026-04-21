"""Multi-workspace configuration.

A workspace bundles the four pieces of state that must move together
when switching between different corpora:

    * docs_path : the root folder whose files are indexed
    * backend   : the EMBED_BACKEND name (affects embed() calls)
    * dim       : the pgvector column dimension — MUST match the DB
    * pg_db     : the Postgres database name on the shared server
    * hybrid    : HYBRID_MEDIA_EMBEDDING flag
    * local_embed_dim : only used by the Ollama `local` backend

Workspaces are declared in `workspaces.yaml` at the repo root. When
the file is absent the module returns a single workspace named
``default`` built from the legacy .env vars — so nothing breaks for
users who don't opt in to multi-workspace.

Example workspaces.yaml:

    - name: trading_course
      docs_path: /docs/trading
      backend: nomic_multimodal_local
      dim: 768
      pg_db: aleph_trading
      hybrid: true

    - name: company_docs
      docs_path: /docs/acme
      backend: local
      dim: 1024
      pg_db: aleph_acme
      hybrid: false
      local_embed_dim: 1024
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

log = logging.getLogger("memory")


@dataclass(frozen=True)
class Workspace:
    name: str
    docs_path: str
    backend: str = "gemini-001"
    dim: int = 1536
    pg_db: Optional[str] = None
    hybrid: bool = True
    local_embed_dim: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _config_path() -> Path:
    return Path(os.environ.get("WORKSPACES_FILE", "/app/workspaces.yaml"))


def _default_from_env() -> Workspace:
    try:
        dim = int(os.environ.get("EMBED_DIM", "1536"))
    except ValueError:
        dim = 1536
    try:
        led = int(os.environ.get("LOCAL_EMBED_DIM", "1024"))
    except ValueError:
        led = 1024
    return Workspace(
        name="default",
        docs_path=os.environ.get("LOCAL_DOCS_PATH", "/docs"),
        backend=os.environ.get("EMBED_BACKEND", "gemini-001").strip() or "gemini-001",
        dim=dim,
        pg_db=None,
        hybrid=os.environ.get("HYBRID_MEDIA_EMBEDDING", "true").strip().lower() == "true",
        local_embed_dim=led,
    )


def load_workspaces() -> list[Workspace]:
    """Return the list of configured workspaces.

    If `workspaces.yaml` is missing or empty, falls back to a single
    ``default`` workspace built from the legacy .env variables.
    """
    path = _config_path()
    if not path.is_file():
        return [_default_from_env()]
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except Exception as e:  # pragma: no cover
        log.warning("[workspaces] failed to read %s: %s — falling back to .env", path, e)
        return [_default_from_env()]
    if not isinstance(data, list) or not data:
        log.warning("[workspaces] %s is not a non-empty list — falling back to .env", path)
        return [_default_from_env()]

    out: list[Workspace] = []
    seen: set[str] = set()
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            log.warning("[workspaces] entry %d is not a mapping, skipping", i)
            continue
        name = str(entry.get("name") or "").strip()
        if not name or name in seen:
            log.warning("[workspaces] entry %d missing/duplicate name, skipping", i)
            continue
        try:
            ws = Workspace(
                name=name,
                docs_path=str(entry.get("docs_path") or "/docs"),
                backend=str(entry.get("backend") or "gemini-001"),
                dim=int(entry.get("dim") or 1536),
                pg_db=entry.get("pg_db"),
                hybrid=bool(entry.get("hybrid", True)),
                local_embed_dim=(
                    int(entry["local_embed_dim"])
                    if entry.get("local_embed_dim") is not None else None
                ),
            )
        except (TypeError, ValueError) as e:
            log.warning("[workspaces] entry %d (%s) malformed: %s", i, name, e)
            continue
        seen.add(name)
        out.append(ws)

    if not out:
        log.warning("[workspaces] no valid entries — falling back to .env")
        return [_default_from_env()]
    return out


def get_by_name(name: str) -> Optional[Workspace]:
    for ws in load_workspaces():
        if ws.name == name:
            return ws
    return None


__all__ = ["Workspace", "load_workspaces", "get_by_name"]
