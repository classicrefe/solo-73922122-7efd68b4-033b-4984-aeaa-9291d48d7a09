"""Deterministic change sampling for quality inspection.

The sampler is a pure function over the change evidence captured by a
published round (see :meth:`Store.run_changes`) plus the three inspector
inputs: the round id, a sample size between 1 and 20, and an integer seed.

Selection rules, in order:

1. When all three change types (withdrawal, correction, addition) have at
   least one record and the sample size allows, each type contributes one
   guaranteed record.  If the size is smaller than the number of types with
   records, the guaranteed slots go by priority: withdrawn → corrected →
   added.
2. Remaining slots are drawn from *all* records not yet picked — across every
   type — ordered by a stable hash of ``(seed, run_id, identifier)``
   (SHA-256), so the same parameters always return the same sample and no
   record repeats.

Nothing here reads time, process state or the database: re-requesting the
same parameters — including after a process restart — yields an identical
sample.
"""

from __future__ import annotations

import hashlib
from typing import Any, Sequence

MIN_SAMPLE_SIZE = 1
MAX_SAMPLE_SIZE = 20

# Display / API order: priority withdrawals first.
CHANGE_TYPES: tuple[str, ...] = ("withdrawn", "corrected", "added")

CHANGE_LABELS: dict[str, str] = {
    "added": "新增",
    "corrected": "更正",
    "withdrawn": "撤回",
}


class SamplingError(ValueError):
    """Raised when the inspector inputs are outside their contract."""


def validate_params(sample_size: int, seed: int) -> None:
    if isinstance(sample_size, bool) or not isinstance(sample_size, int):
        raise SamplingError("样本数必须是 1～20 的整数")
    if sample_size < MIN_SAMPLE_SIZE or sample_size > MAX_SAMPLE_SIZE:
        raise SamplingError("样本数必须在 1～20 之间")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise SamplingError("种子必须是整数")
    if abs(seed) > 2_147_483_647:
        raise SamplingError("种子超出 32 位整数范围")


def _stable_key(run_id: str, identifier: str, seed: int) -> str:
    digest = hashlib.sha256()
    digest.update(str(seed).encode("utf-8"))
    digest.update(b"|")
    digest.update(run_id.encode("utf-8"))
    digest.update(b"|")
    digest.update(identifier.encode("utf-8"))
    return digest.hexdigest()


def select_sample(
    changes: Sequence[dict[str, Any]],
    *,
    run_id: str,
    sample_size: int,
    seed: int,
) -> list[dict[str, Any]]:
    """Return up to ``sample_size`` change records, deterministically.

    Each returned dict is a copy of the input record with two extra keys:

    * ``pick``: ``"guaranteed"`` for the rule-based first picks, or
      ``"seeded"`` for the stable-hash remainder draws;
    * ``order``: the 1-based position in the returned sample.

    When fewer than ``sample_size`` changes exist, every change is returned
    once.
    """
    validate_params(sample_size, seed)

    by_type: dict[str, list[dict[str, Any]]] = {
        change_type: [] for change_type in CHANGE_TYPES
    }
    for change in changes:
        by_type.setdefault(change["change_type"], []).append(change)
    # The guarantee is the first record of each type in the same deterministic
    # order the evidence is stored (seq, identifier); the seeded pool itself
    # is hash-ordered, so the guarantee never depends on dict insertion order.
    for change_type in by_type:
        by_type[change_type].sort(key=lambda item: (item["seq"], item["identifier"]))

    chosen: list[dict[str, Any]] = []
    chosen_ids: set[str] = set()

    # Phase 1: one guaranteed record per type, priority order, while slots and
    # records remain.  A type with no records simply yields no slot.
    for change_type in CHANGE_TYPES:
        if len(chosen) >= sample_size:
            break
        bucket = by_type.get(change_type) or []
        if not bucket:
            continue
        first = bucket[0]
        chosen.append({**first, "pick": "guaranteed"})
        chosen_ids.add(first["identifier"])

    # Phase 2: the rest of the slots come from the whole unpicked pool across
    # every type, ordered solely by the seed-stable hash so different seeds
    # give different, reproducible draws with no type quota remaining.
    pool = [
        item
        for item in changes
        if item["identifier"] not in chosen_ids
    ]
    pool.sort(key=lambda item: _stable_key(run_id, item["identifier"], seed))

    for item in pool:
        if len(chosen) >= sample_size:
            break
        chosen.append({**item, "pick": "seeded"})
        chosen_ids.add(item["identifier"])

    return [
        {**item, "order": index}
        for index, item in enumerate(chosen, start=1)
    ]
