"""Disk-backed per-version render baseline with streaming comparison.

Each chart version gets exactly one baseline, built once from the initial
render with the original override. Both the single-key screening and the
final verification compare against this same baseline object.

The baseline persists on disk in a small SQLite database instead of in
Python memory. Manifests are ingested and compared one document at a time,
so the working set is bounded by the largest single document (plus a fixed
SQLite page cache), regardless of how many thousands of documents a render
contains.

Order semantics:
* document order is ignored — documents are keyed by their stable resource
  identity (kind/namespace/name);
* mapping-key order is ignored — each document is stored as canonical YAML
  with sorted keys;
* list order stays significant — lists are serialized in their parsed order
  and compared byte-for-byte.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .normalize import canonical_yaml, doc_identity, iter_documents

# Cap SQLite's own page cache so the database does not grow process memory
# with the number of stored documents.
_PAGE_CACHE_KIB = 2048


class DuplicateIdentityError(RuntimeError):
    """Raised when a render contains two documents with the same identity.

    Order-insensitive comparison needs a one-to-one document mapping, so a
    baseline with duplicate identities cannot be supported.
    """

    def __init__(self, identity: str):
        super().__init__(
            f"manifest contains multiple documents with identity {identity!r}; "
            "order-insensitive comparison requires unique resource identities"
        )
        self.identity = identity


class ManifestBaseline:
    """One immutable-once-built baseline for a single chart version."""

    def __init__(self, db_path: Path, label: str):
        self.label = label
        self.db_path = db_path
        self._conn = sqlite3.connect(str(db_path))
        # Hard-cap the in-memory page cache; the rest spills to the db file.
        self._conn.execute(f"PRAGMA cache_size=-{_PAGE_CACHE_KIB}")
        self._conn.execute("PRAGMA temp_store=FILE")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS manifests (
                kind      TEXT NOT NULL,
                namespace TEXT NOT NULL,
                name      TEXT NOT NULL,
                body      TEXT NOT NULL,
                PRIMARY KEY (kind, namespace, name)
            )
            """
        )
        # Scratch table used to detect duplicate identities inside a trial
        # render without keeping a Python set of all identities in memory.
        self._conn.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS trial_seen (
                kind      TEXT NOT NULL,
                namespace TEXT NOT NULL,
                name      TEXT NOT NULL,
                PRIMARY KEY (kind, namespace, name)
            )
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------------ build
    def ingest_path(self, manifest_path: Path) -> int:
        """Stream every document of a render into the baseline database.

        Returns the number of ingested documents. Raises
        :class:`DuplicateIdentityError` on a repeated identity.
        """
        count = 0
        # The context manager commits on success and rolls back on failure,
        # so a duplicate identity leaves the baseline unchanged.
        with self._conn:
            for doc in iter_documents(manifest_path):
                kind, namespace, name = doc_identity(doc)
                try:
                    self._conn.execute(
                        "INSERT INTO manifests (kind, namespace, name, body) "
                        "VALUES (?, ?, ?, ?)",
                        (kind, namespace, name, canonical_yaml(doc)),
                    )
                except sqlite3.IntegrityError as exc:
                    raise DuplicateIdentityError(
                        f"{kind}/{namespace}/{name}"
                    ) from exc
                count += 1
        return count

    # --------------------------------------------------------------- compare
    def compare_path(self, manifest_path: Path) -> bool:
        """Stream-compare a trial render against this baseline.

        Returns ``True`` only when the trial contains exactly the same
        document identities and every document's canonical YAML matches.
        The comparison runs in a transaction that is rolled back, so the
        baseline remains untouched and reusable for later rounds.
        """
        seen = 0
        try:
            self._conn.execute("BEGIN")
            for doc in iter_documents(manifest_path):
                kind, namespace, name = doc_identity(doc)
                # A repeated identity inside the trial render cannot be
                # matched one-to-one, so it is a mismatch. The uniqueness
                # check is delegated to the on-disk temp table.
                try:
                    self._conn.execute(
                        "INSERT INTO temp.trial_seen (kind, namespace, name) "
                        "VALUES (?, ?, ?)",
                        (kind, namespace, name),
                    )
                except sqlite3.IntegrityError:
                    self._conn.rollback()
                    return False
                row = self._conn.execute(
                    "SELECT body FROM manifests "
                    "WHERE kind = ? AND namespace = ? AND name = ?",
                    (kind, namespace, name),
                ).fetchone()
                if row is None or row[0] != canonical_yaml(doc):
                    self._conn.rollback()
                    return False
                seen += 1
            total = self._conn.execute(
                "SELECT COUNT(*) FROM manifests"
            ).fetchone()[0]
            if seen != total:
                self._conn.rollback()
                return False
            self._conn.rollback()
            return True
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "ManifestBaseline":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
