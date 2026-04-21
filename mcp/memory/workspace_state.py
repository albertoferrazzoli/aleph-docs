"""Persistent active-workspace state.

Stores the name of the currently active workspace in a tiny text file
on the mcp_data volume so it survives container restarts. Reading is
cheap (single open), writing is atomic (write-then-rename).
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger("memory")


def _state_path() -> Path:
    # /data is the canonical mcp volume mount (see docker-compose.yml
    # mcp_data:/data). Falls back to CWD when MCP_STATE_DIR is set to
    # a test path.
    base = Path(os.environ.get("MCP_STATE_DIR", "/data"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "active_workspace"


def read_active() -> Optional[str]:
    p = _state_path()
    if not p.is_file():
        return None
    try:
        name = p.read_text(encoding="utf-8").strip()
    except OSError as e:
        log.warning("[workspace_state] read failed: %s", e)
        return None
    return name or None


def write_active(name: str) -> None:
    """Atomic write (tmpfile + rename) so crash-during-write can't leave
    a half-written file.
    """
    if not name:
        raise ValueError("write_active: name must be non-empty")
    p = _state_path()
    # NamedTemporaryFile on the same dir → same filesystem → atomic os.replace.
    fd, tmp = tempfile.mkstemp(prefix=".active_workspace.", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(name.strip() + "\n")
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


__all__ = ["read_active", "write_active"]
