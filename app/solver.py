"""Core cleanup solver.

Pipeline:
1. Render every chart version with the original override and build one
   disk-backed :class:`~app.baseline.ManifestBaseline` per version. Any
   baseline failure (including a non-zero helm exit or duplicate resource
   identities) aborts the whole request with an error.
2. Enumerate leaf mapping-key paths from the override (all non-contained).
3. Screen each leaf individually: it is a candidate only when deleting it
   alone still renders successfully on every version AND keeps the manifests
   identical to that version's single baseline. This avoids wasting the
   8-key candidate budget on keys that can never be removed.
4. Cap the individually-removable candidates to 8, then exhaustively search
   removal subsets, largest first. A subset is accepted only when every
   version still renders AND its manifests match the baseline. Combinations
   that fail to render are skipped and other combinations are tried.
5. Re-render the cleaned YAML once and verify it against the same baselines.

Memory characteristics: helm output is streamed to disk and parsed one
document at a time; baselines live in SQLite rather than in Python
collections; each trial's staged files are deleted before the next trial.
With a fixed number of versions and a fixed maximum document size, process
memory therefore does not grow with the total number of rendered documents.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .baseline import DuplicateIdentityError, ManifestBaseline
from .helm import HelmRenderError, helm_template
from .staging import Staging
from .values_tools import (
    dump_values,
    leaf_paths,
    remove_paths,
    render_path,
)

MAX_CANDIDATES = 8


@dataclass
class VersionBaseline:
    label: str
    chart_dir: str
    ok: bool
    stderr: str = ""


@dataclass
class SolveResult:
    cleaned_yaml: str
    removed_paths: list[str]
    candidate_paths: list[str]
    kept_paths: list[str]
    total_leaf_paths: int
    capped: bool
    versions: list[dict[str, Any]] = field(default_factory=list)
    combinations_evaluated: int = 0
    render_failures: int = 0
    verified: bool = False


class BaselineFailure(Exception):
    def __init__(self, failures: list[VersionBaseline]):
        self.failures = failures
        detail = "; ".join(f"{f.label}: {f.stderr.strip()[:300]}" for f in failures)
        super().__init__(f"baseline helm template failed -> {detail}")


def select_candidates(
    removable_leaves: list[tuple[str, ...]], max_candidates: int = MAX_CANDIDATES
) -> tuple[list[tuple[str, ...]], bool]:
    """Cap individually-removable leaves deterministically (override order).

    Leaf paths are non-contained by construction.
    """
    capped = len(removable_leaves) > max_candidates
    return removable_leaves[:max_candidates], capped


def _tail(text: str) -> str:
    if len(text) <= _STDERR_TAIL_CHARS:
        return text
    return "…" + text[-_STDERR_TAIL_CHARS:]


def solve(
    values: dict[str, Any],
    versions: list[dict[str, str]],
    release_name: str,
    namespace: str,
) -> SolveResult:
    all_leaves = leaf_paths(values)
    render_failures = 0
    # Populated on every exit path so version metadata is available for reports.
    baselines: dict[str, ManifestBaseline] = {}

    with Staging() as staging:
        try:
            baseline_values_file = staging.write_text(
                "baseline.values.yaml", dump_values(values)
            )

            # Step 1: one baseline render + one baseline store per version.
            baseline_failures: list[VersionBaseline] = []
            for index, version in enumerate(versions):
                label = version["label"]
                chart_dir = Path(version["chart_dir"])
                if not chart_dir.is_dir():
                    baseline_failures.append(
                        VersionBaseline(
                            label=label,
                            chart_dir=str(chart_dir),
                            ok=False,
                            stderr=f"chart directory not found: {chart_dir}",
                        )
                    )
                    continue
                render_name = f"baseline-{index}.render.yaml"
                render_file = staging.path(render_name)
                try:
                    result = helm_template(
                        chart_dir,
                        baseline_values_file,
                        release_name,
                        namespace,
                        render_file,
                    )
                except HelmRenderError as exc:
                    baseline_failures.append(
                        VersionBaseline(label, str(chart_dir), False, str(exc))
                    )
                    continue
                # A non-zero exit stays a render failure even when helm also
                # wrote output that looks identical to a successful render.
                if result.returncode != 0:
                    baseline_failures.append(
                        VersionBaseline(label, str(chart_dir), False, result.stderr)
                    )
                    continue

                store = ManifestBaseline(
                    staging.path(f"baseline-{index}.sqlite3"), label
                )
                baselines[label] = store
                try:
                    store.ingest_path(render_file)
                except DuplicateIdentityError as exc:
                    baseline_failures.append(
                        VersionBaseline(label, str(chart_dir), False, str(exc))
                    )
                    continue
            if baseline_failures:
                raise BaselineFailure(baseline_failures)

            # Per-round counter guarantees unique staged file names and makes
            # the "remove before next round" behavior explicit.
            _trial_counter = itertools.count()

            def trial_matches(trial_values: dict[str, Any]) -> tuple[bool, bool]:
                """Render all versions with one trial values file and compare.

                Returns (all_versions_rendered_ok, all_manifests_match_baseline).
                Staged trial files are removed before returning so nothing
                carries over into the next round.
                """
                nonlocal render_failures
                values_name = f"trial-{next(_trial_counter)}.values.yaml"
                values_file = staging.write_text(values_name, dump_values(trial_values))
                render_names = [
                    f"trial-{next(_trial_counter)}.{version['label']}.render.yaml"
                    for version in versions
                ]
                render_ok_all = True
                matches_all = True
                try:
                    for version, render_name in zip(versions, render_names):
                        render_file = staging.path(render_name)
                        try:
                            result = helm_template(
                                Path(version["chart_dir"]),
                                values_file,
                                release_name,
                                namespace,
                                render_file,
                            )
                        except HelmRenderError:
                            # helm could not run or timed out: this trial is a
                            # render failure of the current stage, not a
                            # baseline failure; skip it and keep screening.
                            render_ok_all = False
                            matches_all = False
                            render_failures += 1
                            break
                        if result.returncode != 0:
                            # Non-zero exit: never trust the partial output,
                            # even if it happens to look identical.
                            render_ok_all = False
                            matches_all = False
                            render_failures += 1
                            break
                        if not baselines[version["label"]].compare_path(render_file):
                            matches_all = False
                finally:
                    # The previous round's staging must not reach the next one.
                    for name in render_names:
                        staging.remove(name)
                    staging.remove(values_name)
                return render_ok_all, matches_all

            # Step 2/3: screen every leaf individually.
            individually_removable: list[tuple[str, ...]] = []
            for leaf in all_leaves:
                render_ok, matches = trial_matches(remove_paths(values, [leaf]))
                if render_ok and matches:
                    individually_removable.append(leaf)

            candidates, capped = select_candidates(individually_removable)

            # Step 4: exhaustive subset search, largest subset first.
            best: list[tuple[str, ...]] = []
            evaluated = 0
            n = len(candidates)
            for size in range(n, 0, -1):
                found_at_size = False
                for subset in itertools.combinations(candidates, size):
                    subset_list = list(subset)
                    evaluated += 1
                    render_ok, matches = trial_matches(
                        remove_paths(values, subset_list)
                    )
                    if not render_ok:
                        # Combination fails to render: skip and try others.
                        continue
                    if matches:
                        best = subset_list
                        found_at_size = True
                        break
                if found_at_size:
                    break

            # Step 5: final verification re-render against the same baselines.
            cleaned_values = remove_paths(values, best)
            final_values_file = staging.write_text(
                "final.values.yaml", dump_values(cleaned_values)
            )
            verified = True
            version_reports: list[dict[str, Any]] = []
            final_render_names: list[str] = []
            try:
                for index, version in enumerate(versions):
                    render_name = f"final-{index}.render.yaml"
                    final_render_names.append(render_name)
                    result = helm_template(
                        Path(version["chart_dir"]),
                        final_values_file,
                        release_name,
                        namespace,
                        staging.path(render_name),
                    )
                    render_ok = result.returncode == 0
                    same = render_ok and baselines[version["label"]].compare_path(
                        staging.path(render_name)
                    )
                    verified = verified and same
                    version_reports.append(
                        {
                            "label": version["label"],
                            "chart_dir": version["chart_dir"],
                            "render_ok": render_ok,
                            "manifests_match_baseline": same,
                        }
                    )
            finally:
                for name in final_render_names:
                    staging.remove(name)
        finally:
            for store in baselines.values():
                store.close()

    removed = [render_path(p) for p in best]
    candidate_text = [render_path(p) for p in candidates]
    best_set = set(best)
    kept = [render_path(p) for p in all_leaves if p not in best_set]

    return SolveResult(
        cleaned_yaml=dump_values(cleaned_values),
        removed_paths=removed,
        candidate_paths=candidate_text,
        kept_paths=kept,
        total_leaf_paths=len(all_leaves),
        capped=capped,
        versions=version_reports,
        combinations_evaluated=evaluated,
        render_failures=render_failures,
        verified=verified,
    )
