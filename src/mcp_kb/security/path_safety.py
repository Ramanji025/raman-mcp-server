"""Phase 6: path-traversal-safe filename joining.

Several MCP tools build a file path from an agent-supplied name (ADR
project, snapshot name). Without validation, a name like
`"../../../../etc/passwd"` would let a tool write/read outside its intended
data directory. `safe_join` rejects path separators and `..` outright, then
double-checks the resolved path is still inside `base_dir` as defense in
depth against any encoding trick that slips past the first check.
"""
from __future__ import annotations

import re
from pathlib import Path

# Conservative allowlist: letters, digits, dot, dash, underscore. Rejects
# path separators (both `/` and `\`), null bytes, and any other control/
# special character outright rather than trying to blocklist "bad" ones.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class UnsafeNameError(ValueError):
    """Raised when a caller-supplied name fails path-safety validation."""


def safe_join(base_dir: Path, name: str, *, suffix: str = "") -> Path:
    """Return `base_dir / (name + suffix)`, validated against traversal.

    Raises `UnsafeNameError` if `name` contains path separators, `..`, is
    empty, or the resolved path would escape `base_dir`.
    """
    if not name or not _SAFE_NAME_RE.match(name) or name in (".", ".."):
        raise UnsafeNameError(
            f"'{name}' is not a safe name (letters/digits/dot/dash/underscore only)"
        )
    base_resolved = base_dir.resolve()
    candidate = (base_resolved / f"{name}{suffix}").resolve()
    if candidate != base_resolved and base_resolved not in candidate.parents:
        raise UnsafeNameError(f"'{name}' resolves outside the allowed directory")
    return candidate
