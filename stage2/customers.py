"""r3 customer activation on one depot's day-specific, road-admitted inventory."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import defaultdict
from functools import lru_cache
from numbers import Integral
from pathlib import Path

import numpy as np
from scipy.special import gammaln

from .frontier import R3, _draw, _stream, grow_footprint, residential_costs_s


PROJECT = Path(__file__).resolve().parents[1]
PROFILE = json.loads((PROJECT / "config/customer_profile.json").read_text())
if PROFILE["method_version"] != R3 or PROFILE["nomination"] != "boundary_parent":
    raise ValueError("customer profile and footprint version disagree")


def admitted_location_mask(
    tracts, outward_fastest_distance_m, outward_fastest_s, return_fastest_s
):
    """Per-location admission. The outward distance/time must come from the same path."""
    tracts = np.asarray(tracts)
    dist, out, back = (
        np.asarray(a, dtype=float)
        for a in (outward_fastest_distance_m, outward_fastest_s, return_fastest_s)
    )
    if tracts.ndim != 1 or any(
        a.shape != tracts.shape or np.isnan(a).any() or (a < 0).any()
        for a in (dist, out, back)
    ):
        raise ValueError("aligned nonnegative road costs or positive infinity required")
    eligible = (
        np.isfinite(out)
        & np.isfinite(back)
        & (dist * PROFILE["kwh_per_km"] / 1000 <= PROFILE["one_way_budget_kwh"])
    )
    qualifying = np.unique(tracts[eligible & (out <= PROFILE["archive_time_limit_s"])])
    return eligible & np.isin(tracts, qualifying)


@lru_cache(maxsize=1)
def _ratio_reference():
    ref = PROFILE["ratio_reference"]
    atoms = PROJECT / "config" / Path(ref["path"]).name
    with atoms.open("rb") as stream:
        if hashlib.sha256(stream.read()).hexdigest() != ref["sha256"]:
            raise ValueError("empirical R reference does not match the r3 input")
    with atoms.open() as stream:
        rows = list(csv.DictReader(stream))
    ratios = np.array([float(r["R"]) for r in rows])
    cdf = np.array([float(r["CDF"]) for r in rows])
    if (
        not len(cdf)
        or not np.isfinite(ratios).all()
        or not np.isfinite(cdf).all()
        or ratios[0] < 1
        or (np.diff(ratios) < 0).any()
        or cdf[0] <= 0
        or (np.diff(cdf) <= 0).any()
        or not math.isclose(cdf[-1], 1, abs_tol=1e-10)
    ):
        raise ValueError("Invalid ordered empirical R support or cumulative weights")
    return ratios, cdf, [r["task_id"] for r in rows]


def _count_prefixes(factors, limit):
    """Log coefficients of prefix products, scaled by count-independent constants."""
    prefixes = [np.zeros(1)]
    for factor in factors:
        prev = prefixes[-1]
        out = np.full(min(limit + 1, len(prev) + len(factor) - 1), -np.inf)
        shorter, longer = sorted((prev, factor), key=len)
        for i in np.flatnonzero(np.isfinite(shorter[: len(out)])):
            width = min(len(longer), len(out) - i)
            block = out[i : i + width]
            np.logaddexp(block, shorter[i] + longer[:width], out=block)
        out -= np.max(out)
        prefixes.append(out)
    return prefixes


def _draw_counts(factors, prefixes, total, rng):
    counts = []
    for factor, prior in reversed(list(zip(factors, prefixes[:-1]))):
        k = np.arange(max(0, total - len(prior) + 1), min(total, len(factor) - 1) + 1)
        log_mass = factor[k] + prior[total - k]
        supported = np.isfinite(log_mass)
        k, log_mass = k[supported], log_mass[supported]
        if not len(k):
            raise ArithmeticError("conditional customer count has empty support")
        if len(k) == 1:
            draw = int(k[0])
        else:
            w = np.exp(log_mass - log_mass.max())
            draw = int(rng.choice(k, p=w / w.sum()))
        counts.append(draw)
        total -= draw
    return counts[::-1]


def sample_footprint_customers(stock, n, p, seed, task_key):
    """Draw exactly n unique addresses with at least one per supplied tract.

    stock maps GEOID to {LOCATION_ID: positive residential units}. Positive
    count polynomials give each valid list probability proportional to its
    product of residential odds. Equal-unit buildings are exchangeable.
    """
    tracts = sorted(stock)
    if not tracts or not len(tracts) <= n <= sum(map(len, stock.values())):
        raise ValueError("require one or more tracts and T <= N <= M")
    if not 0 < p < 1:
        raise ValueError(
            "require a household probability strictly between zero and one"
        )
    limit = n - len(tracts) + 1
    states, positive = [], []
    for t in tracts:
        groups = defaultdict(list)
        for loc, units in sorted(stock[t].items()):
            if not isinstance(units, Integral) or units < 1:
                raise ValueError("residential units must be positive integers")
            groups[units].append(loc)
        if not groups:
            raise ValueError("a selected tract has no residential addresses")
        factors, members = [], []
        for units, ids in sorted(groups.items()):
            a = -units * math.log1p(-p)
            log_odds = a + math.log(-math.expm1(-a))
            k = np.arange(min(len(ids), limit) + 1)
            log_choose = (
                gammaln(len(ids) + 1) - gammaln(k + 1) - gammaln(len(ids) - k + 1)
            )
            factors.append(log_choose + k * log_odds)
            members.append(ids)
        prefixes = _count_prefixes(factors, limit)
        positive_factor = prefixes[-1].copy()
        positive_factor[0] = -np.inf
        positive.append(positive_factor)
        states.append((factors, prefixes, members))
    totals = _draw_counts(
        positive,
        _count_prefixes(positive, n),
        n,
        _stream(seed, task_key, R3, "tract_customer_counts"),
    )
    chosen = []
    for t, count, (factors, prefixes, groups) in zip(tracts, totals, states):
        rng = _stream(seed, task_key, R3, ["customers", t])
        for ids, k in zip(groups, _draw_counts(factors, prefixes, count, rng)):
            if k == len(ids):
                chosen.extend(ids)
            elif k:
                chosen.extend(ids[i] for i in rng.choice(len(ids), k, replace=False))
    if len(set(chosen)) != n:
        raise ValueError("LOCATION_ID must be globally unique")
    return sorted(chosen), dict(zip(tracts, totals))


def activate_customers(
    *,
    city,
    n,
    stock,
    neighbors,
    fastest_times_s,
    seed,
    task_key,
    find_medoid=None,
    center_times=None,
):
    """Generate an r3 footprint and customer list for a specified depot and day.

    stock contains only its admitted locations (positive units, round-trip
    reachability, one-way energy, tract-level P99 already applied). The directed
    fastest_times_s(origins, destinations) callback returns a seconds matrix on
    the task's road graph. Stable string IDs label locations and tracts.
    """
    if not isinstance(n, Integral) or not 100 <= n <= 2000:
        raise ValueError("supported workload is an integer from 100 to 2000")
    city = "-".join(city.replace("_", " ").lower().split())
    probabilities = PROFILE["household_probability"]
    p = probabilities.get(city, probabilities["shared"])
    size = {t: len(ids) for t, ids in stock.items()}
    available = sum(size.values())
    audit = dict(
        profile_version=R3,
        city=city,
        n=int(n),
        p=p,
        available=available,
        root_seed=seed,
        task_key=task_key,
    )

    def failure(reason):
        return dict(**audit, status="unsupported", reason=reason, customer_ids=[])

    if available < n:
        return failure("insufficient_depot_capacity")
    all_ids = {loc for records in stock.values() for loc in records}
    if len(all_ids) != available or any(not isinstance(loc, str) for loc in all_ids):
        raise ValueError("require globally unique string LOCATION_IDs")
    ids, activity, mass = {}, {}, {}
    for t, records in stock.items():
        if (
            not isinstance(t, str)
            or not records
            or any(not isinstance(v, Integral) or v <= 0 for v in records.values())
        ):
            raise ValueError("require a string GEOID with positive residential units")
        ids[t] = sorted(records)
        units = np.array([records[v] for v in ids[t]], dtype=float)
        activity[t] = -np.expm1(units * math.log1p(-p))
        mass[t] = math.fsum(activity[t])
    model = PROFILE["seed_count"]
    lam = float(
        np.logaddexp(
            0, model["intercept"] + model["log_n_over_100"] * math.log(n / 100)
        )
    )
    k = 1 + int(_stream(seed, task_key, R3, "k_seed").poisson(lam))
    audit.update(K_seed=k, seed_intensity=lam)
    if k > min(n, len(stock)):
        return failure("insufficient_tracts_for_seed_count")
    ratio, atom = 1.0, None
    if available > n:
        ratios, cdf, names = _ratio_reference()
        end = int(np.searchsorted(ratios, available / n, side="right"))
        if not end:
            return failure("empty_ratio_support")
        u = _stream(seed, task_key, R3, "ratio").random() * cdf[end - 1]
        index = min(int(np.searchsorted(cdf, u, side="right")), end - 1)
        ratio, atom = float(ratios[index]), names[index]
    target = n * ratio
    audit.update(R=ratio, ratio_atom=atom, M_target=target)
    remaining = mass.copy()
    rng = _stream(seed, task_key, R3, "seed_tracts")
    roots = []
    for _ in range(k):
        t = _draw(remaining, rng)
        del remaining[t]
        roots.append(t)

    def road_times(origins, destinations):
        values = np.asarray(fastest_times_s(origins, destinations), dtype=float)
        if values.shape != (len(origins), len(destinations)):
            raise ValueError(
                "directed time matrix shape must follow the requested addresses"
            )
        return values

    centers = {}
    audit.update(roots=roots, centers=centers)
    for root in roots:
        buildings = ids[root]
        if find_medoid is not None:
            best = find_medoid(buildings, activity[root])
        else:
            best = (math.inf, buildings[0])
            # Chunk queries to avoid allocating a square root-tract matrix.
            for start in range(0, len(buildings), 64):
                chunk = buildings[start : start + 64]
                fw = road_times(chunk, buildings)
                bw = road_times(buildings, chunk).T
                costs = residential_costs_s(fw, bw, activity[root])
                best = min(best, min(zip(costs, chunk)))
        if not math.isfinite(best[0]):
            return failure("root_has_no_bidirectionally_reachable_medoid")
        centers[root] = best[1]

    def cost(root, t):
        if center_times is None:
            center = [centers[root]]
            fw = road_times(center, ids[t])[0]
            bw = road_times(ids[t], center)[:, 0]
        else:
            fw, bw = center_times(centers[root], ids[t])
        return float(residential_costs_s(fw, bw, activity[t]))

    footprint = grow_footprint(
        roots=roots,
        neighbors=neighbors,
        capacity=size,
        mass=mass,
        cost_s=cost,
        n=n,
        target=target,
        seed=seed,
        task_key=task_key,
    )
    audit["footprint"] = footprint
    if footprint["status"] != "ok":
        return failure(footprint["reason"])
    tract_set = {t: stock[t] for t in footprint["tracts"]}
    chosen, counts = sample_footprint_customers(tract_set, n, p, seed, task_key)
    unseen = set(tract_set)
    components = 0
    while unseen:
        stack = [unseen.pop()]
        components += 1
        while stack:
            adjacent = unseen.intersection(neighbors[stack.pop()])
            unseen.difference_update(adjacent)
            stack.extend(adjacent)
    return dict(
        **audit, status="ok", K_geo=components, tract_counts=counts, customer_ids=chosen
    )
