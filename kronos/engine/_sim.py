"""Shared simulation helpers used by the imbalance planner and the
affinity enforcer.

Both components reason about per-policy host scores and candidate VM
moves.  The math is identical; only the move-generation strategy
differs.  These helpers are internal to the engine package.

``ScoreState`` wraps the per-policy ``PolicyScores`` dict-of-dicts and
maintains an incremental ``(max, min)`` cache.  Speculative methods
(``simulated_*``) score a candidate move in O(1) when the move does
not touch the current extreme host, falling back to an O(H) scan
when it does.  ``apply`` commits a move and refreshes the cache.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kronos.engine.types import MigrationStep, VmProfile
from kronos.policies.models import PolicyConfig

# Per-policy score state: {policy_name: {host: score}}
PolicyScores = dict[str, dict[str, float]]


@dataclass
class _PolicyExtremes:
    """Cached ``max``/``min`` for one policy's host scores."""

    max_score: float
    min_score: float
    max_host: str
    min_host: str


@dataclass
class ScoreState:
    """Per-policy host scores plus an incremental max/min cache.

    Construct with :meth:`from_scores`.  Read scores via :attr:`scores`
    (the underlying dict-of-dicts is the same shape used elsewhere in
    the engine).  Use ``simulated_*`` methods to score candidate moves
    without mutating state, and :meth:`apply` to commit a chosen move.
    """

    scores: PolicyScores
    _extremes: dict[str, _PolicyExtremes] = field(default_factory=dict)

    @classmethod
    def from_scores(cls, scores: PolicyScores) -> ScoreState:
        """Build a cached state from a fully-populated ``PolicyScores`` dict."""
        state = cls(scores=scores)
        for policy_name, host_scores in scores.items():
            state._extremes[policy_name] = _compute_extremes(host_scores)
        return state

    # --- Read helpers ------------------------------------------------

    def policy_imbalance(self, policy_name: str) -> float:
        ex = self._extremes.get(policy_name)
        if ex is None:
            return 0.0
        return ex.max_score - ex.min_score

    def combined_imbalance(self, policies: list[PolicyConfig]) -> float:
        total = 0.0
        for policy in policies:
            ex = self._extremes.get(policy.name)
            if ex is None:
                continue
            total += policy.weight * (ex.max_score - ex.min_score)
        return total

    def all_policies_happy(self, policies: list[PolicyConfig]) -> bool:
        return all(
            self.policy_imbalance(policy.name) <= policy.threshold
            for policy in policies
        )

    # --- Speculative (no mutation) -----------------------------------

    def simulated_policy_imbalance(
        self,
        vm: VmProfile,
        from_host: str,
        to_host: str,
        policy_name: str,
    ) -> float:
        """``policy_imbalance`` after the hypothetical move, no mutation."""
        host_scores = self.scores.get(policy_name)
        ex = self._extremes.get(policy_name)
        if host_scores is None or ex is None or len(host_scores) < 2:
            return 0.0
        weight = vm.weights.get(policy_name, 0.0)
        new_from = host_scores.get(from_host, 0.0) - weight
        new_to = host_scores.get(to_host, 0.0) + weight
        new_max, new_min = _simulated_extremes(
            host_scores, ex, from_host, to_host, new_from, new_to,
        )
        return new_max - new_min

    def simulated_combined_imbalance(
        self,
        vm: VmProfile,
        from_host: str,
        to_host: str,
        policies: list[PolicyConfig],
    ) -> float:
        total = 0.0
        for policy in policies:
            total += policy.weight * self.simulated_policy_imbalance(
                vm, from_host, to_host, policy.name,
            )
        return total

    def simulated_move_hurts_any_policy(
        self,
        vm: VmProfile,
        from_host: str,
        to_host: str,
        policies: list[PolicyConfig],
    ) -> bool:
        """A move is rejected only when it both worsens a policy and
        leaves its imbalance above the policy's threshold.
        """
        for policy in policies:
            before_val = self.policy_imbalance(policy.name)
            after_val = self.simulated_policy_imbalance(
                vm, from_host, to_host, policy.name,
            )
            if after_val > before_val and after_val > policy.threshold:
                return True
        return False

    # --- Commit ------------------------------------------------------

    def apply(
        self,
        vm: VmProfile,
        from_host: str,
        to_host: str,
    ) -> None:
        """Mutate ``scores`` for the move and refresh the extremes cache."""
        for policy_name, host_scores in self.scores.items():
            weight = vm.weights.get(policy_name, 0.0)
            new_from = host_scores.get(from_host, 0.0) - weight
            new_to = host_scores.get(to_host, 0.0) + weight
            host_scores[from_host] = new_from
            host_scores[to_host] = new_to

            ex = self._extremes.get(policy_name)
            if ex is None:
                self._extremes[policy_name] = _compute_extremes(host_scores)
                continue
            self._extremes[policy_name] = _refresh_extremes_after_move(
                host_scores, ex, from_host, to_host, new_from, new_to,
            )

    def combined_host_score(
        self,
        policies: list[PolicyConfig],
        host: str,
    ) -> float:
        """Weighted sum of per-policy scores for one host."""
        return sum(
            policy.weight * self.scores[policy.name].get(host, 0.0)
            for policy in policies
        )


def _compute_extremes(host_scores: dict[str, float]) -> _PolicyExtremes:
    if not host_scores:
        return _PolicyExtremes(0.0, 0.0, "", "")
    items = host_scores.items()
    max_host, max_score = max(items, key=lambda kv: kv[1])
    min_host, min_score = min(items, key=lambda kv: kv[1])
    return _PolicyExtremes(
        max_score=max_score,
        min_score=min_score,
        max_host=max_host,
        min_host=min_host,
    )


def _simulated_extremes(
    host_scores: dict[str, float],
    ex: _PolicyExtremes,
    from_host: str,
    to_host: str,
    new_from: float,
    new_to: float,
) -> tuple[float, float]:
    """Return ``(max, min)`` after a simulated move, without mutation.

    Fast path (O(1)) when neither endpoint of the move is the current
    extreme.  Slow path (O(H)) when it is — the previous extreme may
    have shifted and the runner-up needs a scan.
    """
    if from_host == ex.max_host:
        new_max = _max_excluding(host_scores, exclude=(from_host, to_host))
        new_max = max(new_max, new_from, new_to)
    else:
        new_max = max(ex.max_score, new_to)

    if to_host == ex.min_host:
        new_min = _min_excluding(host_scores, exclude=(from_host, to_host))
        new_min = min(new_min, new_from, new_to)
    else:
        new_min = min(ex.min_score, new_from)

    return new_max, new_min


def _refresh_extremes_after_move(
    host_scores: dict[str, float],
    ex: _PolicyExtremes,
    from_host: str,
    to_host: str,
    new_from: float,
    new_to: float,
) -> _PolicyExtremes:
    """Like :func:`_simulated_extremes` but also tracks the holding hosts."""
    if from_host == ex.max_host or to_host == ex.max_host:
        max_host, max_score = _argmax(host_scores)
    elif new_to > ex.max_score:
        max_host, max_score = to_host, new_to
    else:
        max_host, max_score = ex.max_host, ex.max_score

    if to_host == ex.min_host or from_host == ex.min_host:
        min_host, min_score = _argmin(host_scores)
    elif new_from < ex.min_score:
        min_host, min_score = from_host, new_from
    else:
        min_host, min_score = ex.min_host, ex.min_score

    return _PolicyExtremes(
        max_score=max_score,
        min_score=min_score,
        max_host=max_host,
        min_host=min_host,
    )


def _max_excluding(
    host_scores: dict[str, float],
    *,
    exclude: tuple[str, ...],
) -> float:
    best = float("-inf")
    excluded = set(exclude)
    for host, score in host_scores.items():
        if host in excluded:
            continue
        if score > best:
            best = score
    return best


def _min_excluding(
    host_scores: dict[str, float],
    *,
    exclude: tuple[str, ...],
) -> float:
    best = float("inf")
    excluded = set(exclude)
    for host, score in host_scores.items():
        if host in excluded:
            continue
        if score < best:
            best = score
    return best


def _argmax(host_scores: dict[str, float]) -> tuple[str, float]:
    return max(host_scores.items(), key=lambda kv: kv[1])


def _argmin(host_scores: dict[str, float]) -> tuple[str, float]:
    return min(host_scores.items(), key=lambda kv: kv[1])


def combined_imbalance(
    scores: PolicyScores,
    policies: list[PolicyConfig],
) -> float:
    """Weighted sum of per-policy (max - min) imbalance values."""
    total = 0.0
    for policy in policies:
        host_scores = scores[policy.name]
        if len(host_scores) < 2:
            continue
        values = host_scores.values()
        total += policy.weight * (max(values) - min(values))
    return total


def policy_imbalance(scores: PolicyScores, policy_name: str) -> float:
    """Return the current (max - min) imbalance for one policy."""
    host_scores = scores[policy_name]
    if len(host_scores) < 2:
        return 0.0
    return max(host_scores.values()) - min(host_scores.values())


def all_policies_happy(
    scores: PolicyScores,
    policies: list[PolicyConfig],
) -> bool:
    """True when every policy's imbalance is at or below its threshold."""
    return all(
        policy_imbalance(scores, policy.name) <= policy.threshold
        for policy in policies
    )


def move_hurts_any_policy(
    before: PolicyScores,
    after: PolicyScores,
    policies: list[PolicyConfig],
) -> bool:
    """True if the move worsens any policy while leaving it above threshold.

    A move is allowed to slightly worsen a policy as long as it stays
    below that policy's threshold; and it is always allowed to improve
    or leave a policy unchanged.  Rejected only when both:

    * the policy's imbalance increases, AND
    * the new imbalance is above the policy's threshold.
    """
    for policy in policies:
        before_val = policy_imbalance(before, policy.name)
        after_val = policy_imbalance(after, policy.name)
        if after_val > before_val and after_val > policy.threshold:
            return True
    return False


def combined_host_score(
    scores: PolicyScores,
    policies: list[PolicyConfig],
    host: str,
) -> float:
    """Weighted sum of per-policy scores for a single host."""
    return sum(
        policy.weight * scores[policy.name].get(host, 0.0)
        for policy in policies
    )


def combined_vm_weight(
    vm: VmProfile,
    policies: list[PolicyConfig],
) -> float:
    """Weighted sum of per-policy VM weights."""
    return sum(
        policy.weight * vm.weights.get(policy.name, 0.0)
        for policy in policies
    )


def group_vms_by_host(
    vm_profiles: dict[str, VmProfile],
) -> dict[str, list[VmProfile]]:
    """Index VM profiles by current host."""
    by_host: dict[str, list[VmProfile]] = {}
    for vm in vm_profiles.values():
        by_host.setdefault(vm.host, []).append(vm)
    return by_host


def simulate_move(
    scores: PolicyScores,
    vm: VmProfile,
    from_host: str,
    to_host: str,
) -> PolicyScores:
    """Return a new per-policy score state reflecting a VM move."""
    new_scores: PolicyScores = {}
    for policy_name, host_scores in scores.items():
        weight = vm.weights.get(policy_name, 0.0)
        updated = dict(host_scores)
        updated[from_host] = updated.get(from_host, 0.0) - weight
        updated[to_host] = updated.get(to_host, 0.0) + weight
        new_scores[policy_name] = updated
    return new_scores


def apply_move_to_scores(
    scores: PolicyScores,
    new_scores: PolicyScores,
) -> None:
    """Replace the contents of ``scores`` in place with ``new_scores``."""
    for policy_name, host_scores in new_scores.items():
        scores[policy_name] = host_scores


def move_vm_between_hosts(
    vms_by_host: dict[str, list[VmProfile]],
    step: MigrationStep,
) -> None:
    """Update the VM-by-host index after a simulated move."""
    source_vms = vms_by_host.get(step.from_host, [])
    moved_vm: VmProfile | None = None
    for i, vm in enumerate(source_vms):
        if vm.instance_uuid == step.instance_uuid:
            moved_vm = source_vms.pop(i)
            break

    if moved_vm is not None:
        moved_vm.host = step.to_host
        vms_by_host.setdefault(step.to_host, []).append(moved_vm)
