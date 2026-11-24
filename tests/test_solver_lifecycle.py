"""Solver-level tests for failure semantics and staging hygiene."""
import os
from pathlib import Path

import pytest

from app.solver import BaselineFailure, solve
from app.staging import StagingError

RELEASE = "platform-rel"
NAMESPACE = "platform"


def _simple_chart(tmp_path: Path, name: str = "demo") -> Path:
    chart = tmp_path / name
    chart.mkdir()
    (chart / "Chart.yaml").write_text(
        f"apiVersion: v2\nname: {name}\nversion: 0.1.0\n", encoding="utf-8"
    )
    (chart / "values.yaml").write_text(
        "redundant: default-value\nkeepMe: safe-default\n", encoding="utf-8"
    )
    templates = chart / "templates"
    templates.mkdir()
    (templates / "cm.yaml").write_text(
        """apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Release.Name }}-demo
  namespace: {{ .Release.Namespace }}
data:
  keepMe: {{ .Values.keepMe | quote }}
  redundant: {{ .Values.redundant | quote }}
""",
        encoding="utf-8",
    )
    return chart


def test_staging_directory_is_empty_after_solve(tmp_path):
    chart = _simple_chart(tmp_path)
    values = {"keepMe": "safe-default", "redundant": "default-value"}

    roots: list[Path] = []
    from app import staging as staging_module

    original_mkdtemp = staging_module.tempfile.mkdtemp

    def _track(prefix, *args, **kwargs):
        root = Path(original_mkdtemp(prefix=prefix, *args, **kwargs))
        roots.append(root)
        return str(root)

    staging_module.tempfile.mkdtemp = _track
    try:
        result = solve(
            values,
            [{"label": "only", "chart_dir": str(chart)}],
            RELEASE,
            NAMESPACE,
        )
    finally:
        staging_module.tempfile.mkdtemp = original_mkdtemp

    assert result.verified is True
    assert len(roots) == 1
    # The whole staging tree (renders, sqlite dbs, trial values) is removed.
    assert not roots[0].exists()


def test_trial_files_do_not_accumulate_between_rounds(tmp_path, monkeypatch):
    chart = _simple_chart(tmp_path)
    values = {"keepMe": "safe-default", "redundant": "default-value"}

    seen_counts: list[int] = []
    from app import staging as staging_module

    original_write = staging_module.Staging.write_text

    def _counting_write(self, name, content):
        result_path = original_write(self, name, content)
        # Snapshot everything currently staged after each write.
        entries = [p for p in self.root.rglob("*") if p.is_file()]
        seen_counts.append(len(entries))
        return result_path

    monkeypatch.setattr(staging_module.Staging, "write_text", _counting_write)
    result = solve(
        values,
        [{"label": "only", "chart_dir": str(chart)}],
        RELEASE,
        NAMESPACE,
    )
    assert result.verified is True
    # Trial renders and trial values are deleted before the next round, so the
    # number of concurrently staged files never grows across iterations.
    assert max(seen_counts) <= 4


def test_baseline_nonzero_exit_fails_even_with_identical_output(
    tmp_path, monkeypatch
):
    """A fake helm emitting the same manifests but exiting 1 must fail baseline."""
    chart = _simple_chart(tmp_path)
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    fake_helm = fakebin / "helm"
    # Render a valid ConfigMap to stdout, then exit non-zero on every call.
    fake_helm.write_text(
        """#!/bin/sh
cat <<'YAML'
apiVersion: v1
kind: ConfigMap
metadata:
  name: platform-rel-demo
  namespace: platform
data:
  keepMe: "safe-default"
  redundant: "default-value"
YAML
echo "simulated post-render hook failure" 1>&2
exit 1
""",
        encoding="utf-8",
    )
    os.chmod(fake_helm, 0o755)
    monkeypatch.setenv("PATH", f"{fakebin}:{os.environ['PATH']}")

    values = {"keepMe": "safe-default", "redundant": "default-value"}
    with pytest.raises(BaselineFailure) as exc_info:
        solve(
            values,
            [{"label": "only", "chart_dir": str(chart)}],
            RELEASE,
            NAMESPACE,
        )
    assert "simulated post-render hook failure" in str(exc_info.value)


def test_staging_failure_aborts_without_result(tmp_path, monkeypatch):
    chart = _simple_chart(tmp_path)
    values = {"keepMe": "safe-default", "redundant": "default-value"}

    def _boom(self, name, content):
        raise StagingError("simulated write failure")

    monkeypatch.setattr(
        "app.staging.Staging.write_text", _boom
    )
    with pytest.raises(StagingError):
        solve(
            values,
            [{"label": "only", "chart_dir": str(chart)}],
            RELEASE,
            NAMESPACE,
        )


def test_shuffled_document_and_key_order_still_matches(tmp_path):
    """Deleting a redundant key may reorder documents/mapping keys in the
    render; as long as resource identities and content are unchanged and list
    order is preserved, the manifests must match the baseline.
    """
    chart = tmp_path / "shuffled"
    chart.mkdir()
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: shuffled\nversion: 0.1.0\n", encoding="utf-8"
    )
    (chart / "values.yaml").write_text(
        # Different chart default: removing the override switches the
        # template branch, which only changes document/key order, not content.
        "layout: baseline\nredundant: other-default\n", encoding="utf-8"
    )
    templates = chart / "templates"
    templates.mkdir()
    # Override present -> branch A; override removed (chart default differs)
    # -> branch B: documents reversed, mapping keys shuffled, list order kept.
    (templates / "resources.yaml").write_text(
        """{{- if eq .Values.redundant "default-value" }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Release.Name }}-first
  namespace: {{ .Release.Namespace }}
data:
  zKey: z-value
  aKey: a-value
  ports:
    - one
    - two
---
apiVersion: v1
kind: Secret
metadata:
  name: {{ .Release.Name }}-second
  namespace: {{ .Release.Namespace }}
stringData:
  zKey: z-value
  aKey: a-value
{{- else }}
apiVersion: v1
kind: Secret
metadata:
  namespace: {{ .Release.Namespace }}
  name: {{ .Release.Name }}-second
stringData:
  aKey: a-value
  zKey: z-value
---
apiVersion: v1
kind: ConfigMap
metadata:
  namespace: {{ .Release.Namespace }}
  name: {{ .Release.Name }}-first
data:
  aKey: a-value
  zKey: z-value
  ports:
    - one
    - two
{{- end }}
""",
        encoding="utf-8",
    )
    values = {"redundant": "default-value"}
    result = solve(
        values,
        [{"label": "only", "chart_dir": str(chart)}],
        RELEASE,
        NAMESPACE,
    )
    # Baseline renders branch A; the trial (key removed) renders branch B with
    # reversed document order and shuffled keys — semantically identical.
    assert result.removed_paths == ["redundant"]
    assert result.verified is True


def test_list_reorder_is_not_accepted_as_match(tmp_path):
    """Same documents/keys, but a list is reordered: must NOT match baseline."""
    chart = tmp_path / "listorder"
    chart.mkdir()
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: listorder\nversion: 0.1.0\n", encoding="utf-8"
    )
    (chart / "values.yaml").write_text(
        # Removing the override falls back to this default, which reorders
        # the list inside the otherwise identical ConfigMap.
        "order: second\n", encoding="utf-8"
    )
    templates = chart / "templates"
    templates.mkdir()
    (templates / "cm.yaml").write_text(
        """{{- if eq .Values.order "first" }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Release.Name }}-cm
  namespace: {{ .Release.Namespace }}
data:
  items:
    - one
    - two
{{- else }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Release.Name }}-cm
  namespace: {{ .Release.Namespace }}
data:
  items:
    - two
    - one
{{- end }}
""",
        encoding="utf-8",
    )
    # `order` is an effective key: removing it flips the default and reorders
    # the list, so it must be retained rather than deleted.
    values = {"order": "first"}
    result = solve(
        values,
        [{"label": "only", "chart_dir": str(chart)}],
        RELEASE,
        NAMESPACE,
    )
    assert result.removed_paths == []
    assert "order" in result.kept_paths


def test_helm_spawn_failure_during_trial_is_counted_not_fatal(tmp_path, monkeypatch):
    """If helm cannot start mid-screening, the trial is a skipped render
    failure; the request still completes with the safe key retained."""
    chart = _simple_chart(tmp_path)
    values = {"keepMe": "safe-default", "redundant": "default-value"}

    from app import helm as helm_module
    from app import solver as solver_module

    original = solver_module.helm_template

    def _flaky(chart_dir, values_file, release_name, namespace, output_file,
               timeout_seconds=60):
        # Baseline and final verification renders succeed; only screening /
        # combination trial renders fail to start.
        if "trial-" in str(values_file):
            raise helm_module.HelmRenderError("helm vanished from PATH")
        return original(
            chart_dir, values_file, release_name, namespace,
            output_file, timeout_seconds,
        )

    monkeypatch.setattr(solver_module, "helm_template", _flaky)
    result = solve(
        values,
        [{"label": "only", "chart_dir": str(chart)}],
        RELEASE,
        NAMESPACE,
    )
    # No key could be proven removable, but the request still completes.
    assert result.removed_paths == []
    assert result.render_failures >= 1
    assert result.verified is True


def test_duplicate_resource_identity_is_baseline_failure(tmp_path):
    chart = tmp_path / "dupes"
    chart.mkdir()
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: dupes\nversion: 0.1.0\n", encoding="utf-8"
    )
    (chart / "values.yaml").write_text("unused: true\n", encoding="utf-8")
    templates = chart / "templates"
    templates.mkdir()
    (templates / "two.yaml").write_text(
        """apiVersion: v1
kind: ConfigMap
metadata:
  name: same-name
  namespace: platform
data:
  variant: one
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: same-name
  namespace: platform
data:
  variant: two
""",
        encoding="utf-8",
    )
    values = {"unused": True}
    with pytest.raises(BaselineFailure) as exc_info:
        solve(
            values,
            [{"label": "only", "chart_dir": str(chart)}],
            RELEASE,
            NAMESPACE,
        )
    assert "same-name" in str(exc_info.value)
