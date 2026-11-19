"""Grow a service footprint on admitted tracts with fixed road-medoid centers."""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_right
from collections import defaultdict
from functools import lru_cache
from itertools import accumulate
from numbers import Integral

import numpy as np
from numpy.random import Generator, PCG64

R3 = "2026-11-18-r3"


def residential_costs_s(outbound_s, inbound_s, activity):
    """Weighted mean road cost, with destinations in the last axis of both arrays."""
    forward = np.asarray(outbound_s, dtype=float)
    backward = np.asarray(inbound_s, dtype=float)
    weights = np.asarray(activity, dtype=float)
    if (
        weights.ndim != 1
        or not weights.size
        or forward.shape != backward.shape
        or forward.ndim not in (1, 2)
        or forward.shape[-1] != weights.size
    ):
        raise ValueError("directed times must align with all residential weights")
    if np.any(~np.isfinite(weights)) or np.any(weights <= 0):
        raise ValueError("activity must be finite and positive")
    if (
        np.isnan(forward).any()
        or np.isnan(backward).any()
        or forward.min() < 0
        or backward.min() < 0
    ):
        raise ValueError("directed times must be nonnegative or positive infinity")
    normalized = weights / math.fsum(weights)
    terms = ((forward + backward) * 0.5) * normalized
    return (
        math.fsum(terms)
        if terms.ndim == 1
        else np.array([math.fsum(row) for row in terms])
    )


def residential_medoid_index(ids, directed_times_s, activity):
    """Index of the fixed root center; matrix rows and columns follow the same IDs."""
    matrix = np.asarray(directed_times_s, dtype=float)
    if (
        not len(ids)
        or len(set(ids)) != len(ids)
        or matrix.shape != (len(ids), len(ids))
    ):
        raise ValueError(
            "root requires distinct residential IDs and a square time matrix"
        )
    costs = residential_costs_s(matrix, matrix.T, activity)
    best = min(range(len(ids)), key=lambda i: (costs[i], ids[i]))
    if not math.isfinite(costs[best]):
        raise ValueError("root has no bidirectionally reachable residential medoid")
    return best


def _reachable(costs):
    if any(math.isnan(c) or c < 0 for c in costs.values()):
        raise ValueError("road times must be nonnegative seconds or positive infinity")
    return sorted(t for t in costs if math.isfinite(costs[t]))


def nomination_weights(costs, half_life_s):
    """Historical road-time rule. Positive infinity is unreachable."""
    if not math.isfinite(half_life_s) or half_life_s < 0:
        raise ValueError("half_life_s must be finite and nonnegative")
    finite = _reachable(costs)
    if not finite:
        return {}
    nearest = min(finite, key=lambda t: costs[t])
    if half_life_s == 0:
        return {nearest: 1.0}
    return {t: 2.0 ** (-(costs[t] - costs[nearest]) / half_life_s) for t in finite}


def _stream(seed, task_key, version, purpose):
    payload = dict(
        root_seed=seed, task_key=task_key, profile_version=version, purpose=purpose
    )
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    sub_seed = int.from_bytes(hashlib.sha256(raw.encode()).digest()[:8], "big")
    return Generator(PCG64(sub_seed))


def _draw(weights, rng):
    items = [(t, weights[t]) for t in sorted(weights) if weights[t] > 0]
    if len(items) == 1:
        return items[0][0]
    u = rng.random()
    cumulative = list(accumulate(weight for _, weight in items))
    return items[min(bisect_right(cumulative, u * cumulative[-1]), len(items) - 1)][0]


def _boundary_nomination(root, parents, neighbors, costs, rng):
    reachable = set(_reachable(costs))
    votes = {}
    for parent in sorted(parents):
        candidates = reachable.intersection(neighbors[parent])
        if candidates:
            votes[parent] = min(candidates, key=lambda t: (costs[t], t))
    record = dict(
        root=root,
        parent=None,
        expandable_parents=len(votes),
        parent_votes=0,
        tract=None,
        probability=None,
        frontier_size=len(reachable),
        cost_s=None,
        min_cost_s=None,
        excess_cost_s=None,
    )
    if votes:
        parents = list(votes)
        parent = (
            parents[int(rng.integers(len(parents)))] if len(parents) > 1 else parents[0]
        )
        tract = votes[parent]
        count = sum(t == tract for t in votes.values())
        best = min(costs[t] for t in reachable)
        record.update(
            parent=parent,
            parent_votes=count,
            tract=tract,
            probability=count / len(votes),
            cost_s=costs[tract],
            min_cost_s=best,
            excess_cost_s=costs[tract] - best,
        )
    return record


def grow_footprint(
    *,
    roots,
    neighbors,
    capacity,
    mass,
    cost_s,
    n,
    target,
    seed,
    task_key,
    profile_version=R3,
    half_life_s=None,
    competition_rng=None,
):
    """Return accepted tracts, owners, capacity, stop reason and per-round nominations.

    Keys are stable string GEOIDs. Capacity and mass cover the admitted domain
    only. neighbors supplies shared-boundary adjacency. cost_s(root, tract)
    supplies the residential activity-weighted bidirectional fastest time from
    the root's fixed medoid to the tract, in seconds. Positive infinity excludes
    that nomination. The caller supplies the target in candidate locations.
    Supply the original task's fresh competition_rng to reproduce an archive
    that already has a group-selection stream. Root draw order is preserved.
    The r3 profile draws a boundary parent uniformly. An explicit historical
    profile retains the nearest or half-life nomination used for older replays.
    """
    roots = list(roots)
    if not isinstance(n, Integral) or n < 1 or not math.isfinite(target) or target < n:
        raise ValueError("require integer n >= 1 and finite target >= n")
    if not roots or len(set(roots)) != len(roots) or not set(roots) <= capacity.keys():
        raise ValueError("roots must be distinct and admitted")
    if capacity.keys() != mass.keys() or any(
        not isinstance(t, str)
        or not isinstance(v, Integral)
        or v <= 0
        or not math.isfinite(mass[t])
        or mass[t] <= 0
        for t, v in capacity.items()
    ):
        raise ValueError(
            "each admitted GEOID requires positive capacity and finite positive mass"
        )
    boundary = profile_version == R3
    if boundary and half_life_s is not None:
        raise ValueError("r3 does not take a half-life")
    if not boundary:
        half_life_s = 0 if half_life_s is None else half_life_s
        nomination_weights({}, half_life_s)
    order = roots.copy()
    owners = {t: t for t in roots}
    total = sum(capacity[t] for t in roots)
    frontier = {t: set(neighbors[t]).intersection(capacity) for t in roots}
    members = {t: [t] for t in roots}
    cost = lru_cache(maxsize=None)(cost_s)
    if competition_rng is None:
        competition_rng = _stream(seed, task_key, profile_version, "frontier")
    trace = []
    if len(roots) > n:
        reason = "seed_count_exceeds_customer_count"
    elif total >= target:
        reason = "initial_seeds_reach_target"
    else:
        reason = "tract_count_limit"
        while len(order) < n:
            step = len(trace) + 1
            nominees = defaultdict(list)
            nominations = []
            for root in sorted(roots):
                costs = {t: cost(root, t) for t in frontier[root].difference(owners)}
                if boundary:
                    vote = _boundary_nomination(
                        root,
                        members[root],
                        neighbors,
                        costs,
                        _stream(
                            seed,
                            task_key,
                            profile_version,
                            ["boundary_parent", step, root],
                        ),
                    )
                    nominations.append(vote)
                    if vote["tract"] is not None:
                        nominees[vote["tract"]].append((vote["cost_s"], root))
                    continue
                weights = nomination_weights(costs, half_life_s)
                if weights:
                    t = (
                        next(iter(weights))
                        if len(weights) == 1
                        else _draw(
                            weights,
                            _stream(
                                seed,
                                task_key,
                                profile_version,
                                ["nominate", step, root],
                            ),
                        )
                    )
                    nominees[t].append((costs[t], root))
                    nominations.append(
                        dict(
                            root=root,
                            tract=t,
                            cost_s=costs[t],
                            frontier_size=sum(map(math.isfinite, costs.values())),
                            min_cost_s=min(costs.values()),
                            probability=weights[t] / math.fsum(weights.values()),
                        )
                    )
                else:
                    nominations.append(
                        dict(
                            root=root,
                            tract=None,
                            cost_s=None,
                            frontier_size=0,
                            min_cost_s=None,
                            probability=None,
                        )
                    )
            if not nominees:
                reason = "frontier_exhausted"
                break
            t = _draw({t: mass[t] for t in nominees}, competition_rng)
            _, owner = min(nominees[t])
            after = total + capacity[t]
            # Compare ratios without logs. An exact tie retains the earlier prefix.
            retain = not (
                after >= target and total >= n and target / total <= after / target
            )
            trace.append(
                dict(
                    step=step,
                    nominations=nominations,
                    tract=t,
                    owner=owner,
                    retained=retain,
                    selection_probability=mass[t]
                    / math.fsum(mass[v] for v in nominees),
                )
            )
            if not retain:
                reason = "target_crossed_choose_previous"
                break
            order.append(t)
            owners[t] = owner
            members[owner].append(t)
            total = after
            if total >= target:
                reason = "target_crossed_choose_current"
                break
            frontier[owner].update(set(neighbors[t]).intersection(capacity))
    if total < n:
        reason = "insufficient_reachable_frontier_capacity"
    supported = total >= n and len(roots) <= n
    result = dict(
        status="ok" if supported else "unsupported",
        reason=reason,
        tracts=order,
        owners=owners,
        M=total,
        T=len(order),
        trace=trace,
        profile_version=profile_version,
        nomination_rule="boundary_parent" if boundary else "road_time",
        shortfall=supported
        and total < target
        and reason in {"tract_count_limit", "frontier_exhausted"},
    )
    if not boundary:
        result["half_life_s"] = half_life_s
    return result
