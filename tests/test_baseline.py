"""Tests for the disk-backed streaming baseline store."""
import textwrap

import pytest

from app.baseline import DuplicateIdentityError, ManifestBaseline


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


# Two renders with the same documents in different order and different
# mapping-key order must compare equal; list order stays significant.
DOCS_ORDER_1 = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: cm-a
  namespace: ns
data:
  z: "1"
  a: "2"
---
apiVersion: v1
kind: Secret
metadata:
  namespace: ns
  name: sec-b
stringData:
  token: x
"""

DOCS_ORDER_2 = """
apiVersion: v1
kind: Secret
metadata:
  namespace: ns
  name: sec-b
stringData:
  token: x  # comments disappear on parse
---
apiVersion: v1
kind: ConfigMap
metadata:
  namespace: ns
  name: cm-a
data:
  a: "2"
  z: "1"
"""

DOCS_LIST_1 = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: cm-list
data:
  items:
    - one
    - two
"""

DOCS_LIST_2 = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: cm-list
data:
  items:
    - two
    - one
"""

DOCS_SCALAR_DIFF = """
apiVersion: v1
kind: ConfigMap
metadata:
  name: cm-a
  namespace: ns
data:
  z: "9"
  a: "2"
---
apiVersion: v1
kind: Secret
metadata:
  namespace: ns
  name: sec-b
stringData:
  token: x
"""


def test_shuffled_documents_and_keys_compare_equal(tmp_path):
    base_file = _write(tmp_path, "base.yaml", DOCS_ORDER_1)
    trial_file = _write(tmp_path, "trial.yaml", DOCS_ORDER_2)
    store = ManifestBaseline(tmp_path / "base.sqlite3", "v1")
    assert store.ingest_path(base_file) == 2
    assert store.compare_path(trial_file) is True
    store.close()


def test_list_order_remains_significant(tmp_path):
    base_file = _write(tmp_path, "base.yaml", DOCS_LIST_1)
    trial_file = _write(tmp_path, "trial.yaml", DOCS_LIST_2)
    store = ManifestBaseline(tmp_path / "base.sqlite3", "v1")
    store.ingest_path(base_file)
    assert store.compare_path(trial_file) is False
    store.close()


def test_scalar_difference_detected(tmp_path):
    base_file = _write(tmp_path, "base.yaml", DOCS_ORDER_1)
    trial_file = _write(tmp_path, "trial.yaml", DOCS_SCALAR_DIFF)
    store = ManifestBaseline(tmp_path / "base.sqlite3", "v1")
    store.ingest_path(base_file)
    assert store.compare_path(trial_file) is False
    store.close()


def test_extra_and_missing_documents_detected(tmp_path):
    base_file = _write(tmp_path, "base.yaml", DOCS_ORDER_1)
    only_one = _write(
        tmp_path,
        "one.yaml",
        """
        apiVersion: v1
        kind: ConfigMap
        metadata:
          name: cm-a
          namespace: ns
        data:
          a: "2"
          z: "1"
        """,
    )
    store = ManifestBaseline(tmp_path / "base.sqlite3", "v1")
    store.ingest_path(base_file)
    # Trial is missing the Secret -> different document set.
    assert store.compare_path(only_one) is False
    # Re-comparing with the full baseline render still works: the baseline is
    # reusable after a rollback inside compare_path.
    assert store.compare_path(base_file) is True
    store.close()


def test_duplicate_identity_rejected_at_ingest(tmp_path):
    duplicated = _write(
        tmp_path,
        "dup.yaml",
        """
        apiVersion: v1
        kind: ConfigMap
        metadata:
          name: same
          namespace: ns
        data:
          v: one
        ---
        apiVersion: v1
        kind: ConfigMap
        metadata:
          name: same
          namespace: ns
        data:
          v: two
        """,
    )
    store = ManifestBaseline(tmp_path / "base.sqlite3", "v1")
    with pytest.raises(DuplicateIdentityError):
        store.ingest_path(duplicated)
    # Failed ingest rolled back: no rows persisted.
    count = store._conn.execute("SELECT COUNT(*) FROM manifests").fetchone()[0]
    assert count == 0
    store.close()


def test_trial_with_duplicate_identities_does_not_match(tmp_path):
    base_file = _write(tmp_path, "base.yaml", DOCS_ORDER_1)
    duplicated = _write(
        tmp_path,
        "dup-trial.yaml",
        """
        apiVersion: v1
        kind: ConfigMap
        metadata:
          name: cm-a
          namespace: ns
        data:
          a: "2"
          z: "1"
        ---
        apiVersion: v1
        kind: ConfigMap
        metadata:
          name: cm-a
          namespace: ns
        data:
          a: "2"
          z: "1"
        """,
    )
    store = ManifestBaseline(tmp_path / "base.sqlite3", "v1")
    store.ingest_path(base_file)
    assert store.compare_path(duplicated) is False
    # Temp table is cleared by the rollback; baseline still reusable.
    assert store.compare_path(base_file) is True
    store.close()


def test_many_documents_are_streamed_one_at_a_time(tmp_path):
    """A render with many documents compares correctly without holding them all."""
    n = 500

    def render(path: object, shuffled: bool) -> None:
        indexes = list(range(n))
        if shuffled:
            indexes.reverse()
        chunks = []
        for i in indexes:
            chunks.append(
                f"apiVersion: v1\nkind: ConfigMap\n"
                f"metadata:\n  name: cm-{i:05d}\n  namespace: ns\n"
                f"data:\n  z: {i}\n  a: same\n"
            )
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("---\n" + "\n---\n".join(chunks))

    base_file = tmp_path / "big.yaml"
    trial_file = tmp_path / "big-shuffled.yaml"
    render(base_file, shuffled=False)
    render(trial_file, shuffled=True)

    store = ManifestBaseline(tmp_path / "big.sqlite3", "v1")
    assert store.ingest_path(base_file) == n
    assert store.compare_path(trial_file) is True

    changed = tmp_path / "changed.yaml"
    render(changed, shuffled=True)
    # Mutate one document; mismatch must be detected.
    text = changed.read_text(encoding="utf-8").replace("z: 250\n", "z: 2500\n", 1)
    changed.write_text(text, encoding="utf-8")
    assert store.compare_path(changed) is False
    store.close()
