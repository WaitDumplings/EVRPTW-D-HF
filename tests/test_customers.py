from collections import Counter
from itertools import combinations
import math

import numpy as np
import pytest

from stage2.customers import (
    _count_prefixes,
    activate_customers,
    admitted_location_mask,
    sample_footprint_customers,
)
from stage2.frontier import R3


def test_tract_time_support_and_individual_energy():
    mask = admitted_location_mask(
        ["a", "a", "a", "b", "c", "d"],
        [2000, 2000, 300000, 10, 10, 10],
        [4566.3, 7000, 400, 5000, 200, 100],
        [300, 300, 300, 100, np.inf, 12],
    )
    assert mask.tolist() == [True, True, False, False, False, True]


def test_positive_tract_joint_law_against_enumerated_subsets():
    stock = {"x": {"a": 1, "b": 3, "c": 2}, "y": {"d": 1, "e": 2}}
    units = stock["x"] | stock["y"]
    expected = {}
    for subset in combinations(sorted(units), 3):
        if "d" in subset or "e" in subset:
            expected[subset] = math.prod(0.8 ** (-units[i]) - 1 for i in subset)
    total = sum(expected.values())
    # Independently enumerate each admissible tract allocation as well as each subset.
    factors = []
    for records in stock.values():
        address_factors = [
            np.array([0.0, math.log(0.8 ** (-u) - 1)]) for u in records.values()
        ]
        coeff = _count_prefixes(address_factors, 3)[-1]
        coeff[0] = -np.inf
        factors.append(coeff)
    weights = np.exp([factors[0][1] + factors[1][2], factors[0][2] + factors[1][1]])
    exact_one_in_x = (
        sum(
            w
            for subset, w in expected.items()
            if len(set(subset) & set(stock["x"])) == 1
        )
        / total
    )
    assert weights[0] / weights.sum() == pytest.approx(exact_one_in_x, abs=1e-14)
    obs = Counter()
    for key in range(1600):
        chosen, counts = sample_footprint_customers(stock, 3, 0.2, 1, key)
        assert min(counts.values()) >= 1
        obs[tuple(chosen)] += 1
    assert set(obs) == set(expected)
    for subset, w in expected.items():
        assert abs(obs[subset] / 1600 - w / total) < 0.029


def test_large_log_odds_and_one_address_per_tract():
    stock = {"x": {"a": 100000, "b": 100001}, "y": {"c": 1}}
    chosen, counts = sample_footprint_customers(stock, 2, 0.05, 1, "heavy")
    assert len(chosen) == 2 and "c" in chosen and counts == {"x": 1, "y": 1}


@pytest.mark.parametrize("n", [100, 300, 500, 1000, 2000])
def test_admitted_stock_to_addresses(n):
    # Bidirectional but asymmetric travel times and a shared-boundary tract chain.
    stock = {
        f"t{j}": {f"{j * 275 + i:05d}": 1 + i % 3 for i in range(275)}
        for j in range(10)
    }
    neighbors = {
        t: [s for s in stock if abs(int(s[1:]) - int(t[1:])) == 1] for t in stock
    }

    def times(a, b):
        delta = np.asarray(b, dtype=int)[None, :] - np.asarray(a, dtype=int)[:, None]
        return np.abs(delta) + np.maximum(0, delta) * 0.5

    args = dict(
        city="chicago",
        n=n,
        stock=stock,
        neighbors=neighbors,
        fastest_times_s=times,
        seed=20261103,
        task_key=["fixture", n],
    )
    run = activate_customers(**args)
    assert run["status"] == "ok" and run["profile_version"] == R3
    assert len(set(run["customer_ids"])) == n and all(run["tract_counts"].values())
    assert run["K_geo"] <= run["K_seed"] and run["M_target"] == n * run["R"]
    assert run["footprint"]["nomination_rule"] == "boundary_parent"
    assert sum(run["tract_counts"].values()) == n
    reversed_stock = {
        t: dict(reversed(list(v.items()))) for t, v in reversed(list(stock.items()))
    }
    assert run == activate_customers(**{**args, "stock": reversed_stock})


def test_empty_ratio_support_preserves_task():
    run = activate_customers(
        city="dallas",
        n=100,
        stock={str(i): {str(i): 1} for i in range(101)},
        neighbors={},
        fastest_times_s=None,
        seed=1,
        task_key="no-R",
    )
    assert run["reason"] == "empty_ratio_support" and not run["customer_ids"]
