"""Route on frozen paired policies and select stations using directed tree legs."""

import numpy as np
from .common import MethodFailure


def reference_tree(cost):
    """Root at depot; node order is stable location ID within each role."""
    size = len(cost)
    used = np.zeros(size, dtype=bool)
    used[0] = True
    best = cost[0].copy()
    parent = np.zeros(size, dtype=int)
    edges = []
    for _ in range(size - 1):
        child = int(np.argmin(np.where(used, np.inf, best)))
        edges.append((int(parent[child]), child))
        used[child] = True
        update = (cost[child] < best) | ((cost[child] == best) & (child < parent))
        best[update] = cost[child, update]
        parent[update] = child
    return np.asarray(edges)


def route_arrays(router, bound, ids, vehicle):
    bundle = router.route(
        [bound[k] for k in ids],
        specific_energy_kwh_per_km=vehicle["specific_energy_kwh_per_km"],
    )
    return dict(
        shortest_distance_m=bundle.distance_shortest_path_distance,
        shortest_distance_time_s=bundle.distance_shortest_path_time,
        fastest_time_distance_m=bundle.time_shortest_path_distance,
        fastest_time_s=bundle.time_shortest_path_time,
    ), bundle.metadata


def station_selection(
    distance_m, seconds, ids, customers, power, vehicle, budget, *, from_stations=None
):
    n = len(customers) + 1
    if customers != sorted(customers) or len(ids) != len(power):
        raise ValueError("MST requires stable customer order and station identities")
    distance = distance_m / 1000
    local = distance[:n, :n]
    if not np.isfinite(local).all():
        raise MethodFailure("stations", "unreachable_core_pair")
    tree = reference_tree((local + local.T) / 2)
    pairs = np.concatenate([tree, tree[:, ::-1]])
    i, j = pairs.T
    if from_stations is None:
        reverse_d, reverse_t = distance_m[n:, :n], seconds[n:, :n]
    else:
        reverse_d, reverse_t = from_stations
    d1, d2 = distance[i, n:], reverse_d[:, j].T / 1000
    t1, t2 = seconds[i, n:] / 60, reverse_t[:, j].T / 60
    rate = vehicle["specific_energy_kwh_per_km"]
    usable = vehicle["battery_capacity_kwh"] * (1 - vehicle["reserve_soc"])
    allowed = np.isfinite(d1 + d2 + t1 + t2) & (power > 0)
    eligible = allowed & (rate * d1 <= usable) & (rate * d2 <= usable)
    use = eligible.any(axis=1)
    if not use.any():
        return [], dict(
            directions=len(i), coverable=0, covered=0, all_legs_eligible=False, steps=[]
        )
    km = (d1 + d2 - local[i, j, None])[use]
    time = (t1 + t2 - seconds[i, j, None] / 60 + 60 * rate * d1 / power)[use]
    allowed, eligible = allowed[use], eligible[use]

    def normalize(x):
        lo = np.where(allowed, x, np.inf).min(axis=1, keepdims=True)
        width = np.where(allowed, x, -np.inf).max(axis=1, keepdims=True) - lo
        return np.divide(x - lo, width, out=np.zeros_like(x), where=width > 0)

    score = np.where(eligible, (normalize(km) + normalize(time)) / 2, np.inf)
    detour = np.where(eligible, km, np.inf)
    best_km = np.where(allowed, km, -np.inf).max(axis=1)
    best = np.ones(len(score))
    covered = np.zeros(len(score), dtype=bool)
    available = eligible.any(axis=0)
    own_detour = detour.min(axis=0)
    selected, steps = [], []
    for _ in range(min(budget, int(available.sum()))):
        cover = (covered[:, None] | eligible).sum(axis=0)
        tied = np.flatnonzero(available & (cover == cover[available].max()))
        joint = np.minimum(best[:, None], score[:, tied]).mean(axis=0)
        tied = tied[joint <= joint.min() + 1e-12]
        lengths = np.minimum(best_km[:, None], detour[:, tied]).mean(axis=0)
        tied = tied[lengths <= lengths.min() + 1e-12]
        pick = min(tied, key=lambda k: (own_detour[k], -power[k], ids[k]))
        updated = np.minimum(best, score[:, pick])
        steps.append(
            dict(
                station=ids[pick],
                newly_covered=int((~covered & eligible[:, pick]).sum()),
                marginal_joint_gain=float((best - updated).mean()),
                mean_joint_cost=float(updated.mean()),
            )
        )
        best = updated
        best_km = np.minimum(best_km, detour[:, pick])
        covered |= eligible[:, pick]
        available[pick] = False
        selected.append(ids[pick])
    return selected, dict(
        directions=len(i),
        coverable=int(use.sum()),
        covered=int(covered.sum()),
        all_legs_eligible=bool(eligible.all() and use.all()),
        steps=steps,
    )
