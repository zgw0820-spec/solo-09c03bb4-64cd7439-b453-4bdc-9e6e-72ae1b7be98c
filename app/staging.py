"""On-disk staging area for per-request working files.

All transient artifacts (baseline/trial values files, streamed helm renders,
the per-version baseline databases) live in one temporary directory which is
removed when the request finishes. Every read/write/delete goes through this
class so that an I/O failure raises :class:`StagingError`; callers must turn
that into a 5xx response and never return a cleanup result.

Trial renders from one round are explicitly removed before the next round, so
staged data never accumulates across the screening/combination iterations.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any


class StagingError(RuntimeError):
    """Raised when the on-disk staging area cannot be read or written."""


class Staging:
    def __init__(self, prefix: str = "helm-cleanup-") -> None:
        try:
            self.root = Path(tempfile.mkdtemp(prefix=prefix))
        except OSError as exc:  # pragma: no cover - depends on host /tmp
            raise StagingError(f"cannot create staging directory: {exc}") from exc

    def _resolve(self, name: str) -> Path:
        path = (self.root / name).resolve()
        if self.root not in path.parents and path != self.root:
            raise StagingError(f"staging path escapes the staging root: {name!r}")
        return path

    def path(self, name: str) -> Path:
        return self._resolve(name)

    def write_text(self, name: str, content: str) -> Path:
        path = self._resolve(name)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise StagingError(f"failed to write staged file {name!r}: {exc}") from exc
        return path

    def read_text(self, name: str) -> str:
        path = self._resolve(name)
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StagingError(f"failed to read staged file {name!r}: {exc}") from exc

    def remove(self, name: str) -> None:
        """Delete one staged file; a missing file is already gone, not an error."""
        path = self._resolve(name)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise StagingError(f"failed to remove staged file {name!r}: {exc}") from exc

    def cleanup(self) -> None:
        try:
            shutil.rmtree(self.root, ignore_errors=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise StagingError(
                f"failed to clean up staging directory {self.root}: {exc}"
            ) from exc

    def __enter__(self) -> "Staging":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.cleanup()
