"""Thin wrapper around the `helm template` CLI.

The rendered manifests are streamed straight to a caller-provided file so the
service's memory footprint does not grow with the number (or total size) of
the emitted documents: helm's stdout is piped in fixed-size chunks to disk
instead of being captured as one in-memory string.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class HelmRenderError(RuntimeError):
    """Raised when helm cannot be started or does not finish in time."""


@dataclass(frozen=True)
class HelmResult:
    """Outcome of a single `helm template` invocation.

    ``stdout_path`` holds the full multi-document render on disk; callers
    re-open it (streaming) instead of receiving a buffered string.
    """

    returncode: int
    stdout_path: Path
    stderr: str


def ensure_helm_available() -> str:
    helm = shutil.which("helm")
    if helm is None:
        raise HelmRenderError(
            "helm executable not found on PATH; install Helm 3 to run this service"
        )
    return helm


def helm_template(
    chart_dir: Path,
    values_file: Path,
    release_name: str,
    namespace: str,
    output_file: Path,
    timeout_seconds: int = 60,
) -> HelmResult:
    """Run `helm template`, streaming stdout into ``output_file``.

    Never raises on helm failure; the caller decides whether a non-zero exit
    is a hard baseline failure or a combination to skip. Even when helm exits
    non-zero (and may have written a partial render), the return code is what
    determines the outcome: identical output bytes never downgrade a failed
    stage into success.

    The output file is truncated when helm produces nothing.
    """
    helm = ensure_helm_available()
    cmd = [
        helm,
        "template",
        release_name,
        str(chart_dir),
        "--namespace",
        namespace,
        "--values",
        str(values_file),
        "--include-crds",
    ]
    try:
        with output_file.open("wb") as out:
            proc = subprocess.run(
                cmd,
                stdout=out,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
                check=False,
            )
    except subprocess.TimeoutExpired as exc:
        # Treat timeouts like a non-zero render failure of the current stage.
        raise HelmRenderError(
            f"helm template timed out after {timeout_seconds}s for {chart_dir}"
        ) from exc
    except OSError as exc:
        raise HelmRenderError(f"failed to run helm template: {exc}") from exc

    return HelmResult(
        returncode=proc.returncode,
        stdout_path=output_file,
        stderr=proc.stderr.decode("utf-8", errors="replace"),
    )
