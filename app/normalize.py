"""Manifest normalization and semantic comparison.

Comparison rules (per spec):
* YAML comments are ignored (we parse into Python objects, comments never survive).
* Document order is ignored (documents are keyed by a stable resource identity).
* Mapping key order is ignored (canonical YAML is emitted with sorted keys).
* List order is significant (lists are emitted and compared element by element).

The module offers two layers:

* :func:`manifests_equal` / :func:`normalize_manifests` — in-memory helpers for
  small strings, kept for unit-level use.
* :func:`iter_documents`, :func:`doc_identity`, :func:`canonical_yaml` — the
  streaming building blocks used by the solver. A manifest file is parsed one
  document at a time (PyYAML reads in 4 KB chunks and discards consumed
  input), so peak memory is bounded by the largest single document rather
  than by the total number of documents.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import yaml

# Resource identity tuple: (kind, namespace, name). Falls back to a "raw"
# marker plus the canonical content for documents that are not mappings.
Identity = tuple[str, str, str]


def parse_documents(rendered: str) -> list[Any]:
    """Parse a multi-document helm template output, dropping empty documents."""
    docs: list[Any] = []
    for doc in yaml.safe_load_all(rendered):
        if doc is None:
            continue
        docs.append(doc)
    return docs


def iter_documents(path: Any) -> Iterator[Any]:
    """Yield parsed documents from a manifest file one at a time.

    Only one document is held in memory at once; the underlying parser reads
    the file incrementally and releases the consumed text buffer.
    """
    with open(path, "rb") as stream:
        for doc in yaml.safe_load_all(stream):
            if doc is not None:
                yield doc


def doc_identity(doc: Any) -> Identity:
    """Stable resource identity (kind/namespace/name) for a manifest document.

    Non-mapping documents (rare in helm output) get a content-derived identity
    so two renders still compare deterministically.
    """
    if isinstance(doc, dict):
        kind = str(doc.get("kind", ""))
        meta = doc.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        name = str(meta.get("name", ""))
        namespace = str(meta.get("namespace", ""))
        return kind, namespace, name
    return "raw", "", canonical_yaml(doc)


def canonical_yaml(doc: Any) -> str:
    """Deterministic per-document YAML: sorted mapping keys, list order kept."""
    return yaml.safe_dump(
        doc,
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
        width=1000,
    )


def _doc_identity_key(doc: Any) -> str:
    kind, namespace, name = doc_identity(doc)
    return f"{kind}/{namespace}/{name}"


def normalize_manifests(rendered: str) -> list[Any]:
    """Canonical form: documents sorted by identity; list order preserved."""
    docs = parse_documents(rendered)
    return sorted(docs, key=_doc_identity_key)


def manifests_equal(left: str, right: str) -> bool:
    """Semantic equality under the rules in the module docstring."""
    return normalize_manifests(left) == normalize_manifests(right)
