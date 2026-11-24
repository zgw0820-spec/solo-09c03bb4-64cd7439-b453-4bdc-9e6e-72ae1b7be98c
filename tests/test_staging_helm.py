"""Tests for the staging area and streaming helm wrapper."""
import os
from pathlib import Path

import pytest

from app.helm import HelmRenderError, HelmResult, helm_template
from app.staging import Staging, StagingError


def test_staging_write_read_remove_and_cleanup():
    staging = Staging(prefix="staging-test-")
    try:
        path = staging.write_text("a/b.txt", "hello")
        assert path.is_file()
        assert staging.read_text("a/b.txt") == "hello"
        staging.remove("a/b.txt")
        assert not path.exists()
        # Removing again is not an error.
        staging.remove("a/b.txt")
        root = staging.root
    finally:
        staging.cleanup()
    assert not root.exists()


def test_staging_context_manager_cleans_even_after_error():
    root_holder = []
    with pytest.raises(RuntimeError):
        with Staging(prefix="staging-test-") as staging:
            root_holder.append(staging.root)
            staging.write_text("x.txt", "data")
            raise RuntimeError("boom")
    assert not root_holder[0].exists()


def test_staging_rejects_escaping_paths():
    with Staging(prefix="staging-test-") as staging:
        with pytest.raises(StagingError):
            staging.path("../escape.txt")


def test_staging_write_failure_raises_staging_error(monkeypatch):
    with Staging(prefix="staging-test-") as staging:
        def _boom(self, data, *args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "write_text", _boom)
        with pytest.raises(StagingError):
            staging.write_text("x.txt", "data")


def _make_fake_helm(tmp_path: Path, script: str) -> Path:
    helm_dir = tmp_path / "fakebin"
    helm_dir.mkdir()
    helm = helm_dir / "helm"
    helm.write_text(script, encoding="utf-8")
    os.chmod(helm, 0o755)
    return helm_dir


# A fake helm that streams output to its stdout (the wrapper redirects stdout
# to the caller's file) and exits with the code given by $FAKE_HELM_RC.
_FAKE_HELM = """#!/bin/sh
cat <<'YAML'
apiVersion: v1
kind: ConfigMap
metadata:
  name: streamed-cm
  namespace: ns
data:
  hello: world
YAML
echo "fake helm message" 1>&2
exit "${FAKE_HELM_RC:-0}"
"""


def test_helm_template_streams_stdout_to_file(tmp_path, monkeypatch):
    helm_dir = _make_fake_helm(tmp_path, _FAKE_HELM)
    monkeypatch.setenv("PATH", f"{helm_dir}:{os.environ['PATH']}")
    out = tmp_path / "render.yaml"
    result = helm_template(
        tmp_path, tmp_path / "values.yaml", "rel", "ns", out
    )
    assert isinstance(result, HelmResult)
    assert result.returncode == 0
    assert result.stdout_path == out
    # Output never sits on the result object; it is read back from disk.
    assert "streamed-cm" in out.read_text(encoding="utf-8")
    assert "fake helm message" in result.stderr


def test_nonzero_exit_is_failure_even_with_identical_output(tmp_path, monkeypatch):
    helm_dir = _make_fake_helm(tmp_path, _FAKE_HELM)
    monkeypatch.setenv("PATH", f"{helm_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_HELM_RC", "1")
    out_ok = tmp_path / "ok.yaml"
    out_fail = tmp_path / "fail.yaml"
    monkeypatch.delenv("FAKE_HELM_RC", raising=False)
    ok = helm_template(tmp_path, tmp_path / "values.yaml", "rel", "ns", out_ok)
    monkeypatch.setenv("FAKE_HELM_RC", "1")
    failed = helm_template(
        tmp_path, tmp_path / "values.yaml", "rel", "ns", out_fail
    )
    # Same bytes on disk, but the non-zero exit still signals failure.
    assert ok.returncode == 0 and failed.returncode == 1
    assert out_ok.read_bytes() == out_fail.read_bytes()


def test_missing_helm_executable_raises(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent-path")
    with pytest.raises(HelmRenderError):
        helm_template(
            Path("/tmp/chart"),
            Path("/tmp/values.yaml"),
            "rel",
            "ns",
            Path("/tmp/out.yaml"),
        )


def test_helm_timeout_surfaces_as_render_error(tmp_path, monkeypatch):
    helm_dir = _make_fake_helm(tmp_path, "#!/bin/sh\nsleep 5\n")
    monkeypatch.setenv("PATH", f"{helm_dir}:{os.environ['PATH']}")
    with pytest.raises(HelmRenderError):
        helm_template(
            tmp_path,
            tmp_path / "values.yaml",
            "rel",
            "ns",
            tmp_path / "out.yaml",
            timeout_seconds=1,
        )
