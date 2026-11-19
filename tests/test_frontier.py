from collections import Counter
import csv
import json
import math
from pathlib import Path

import pytest

from stage2.frontier import (
    R3,
    grow_footprint,
    nomination_weights,
    residential_costs_s,
    residential_medoid_index,
)


def test_residential_centers_and_costs():
    assert residential_costs_s([10, 40], [30, 80], [3, 1]) == 30
    assert math.isinf(residential_costs_s([10, 40], [math.inf, 80], [3, 1]))
    assert residential_medoid_index(["z", "a"], [[0, 10], [30, 0]], [1, 1]) == 1
    assert residential_medoid_index(["z", "a"], [[0, 10], [30, 0]], [2, 1]) == 0


def grow(roots=("a",), neighbors=None, capacity=None, mass=None, costs=None, **kwargs):
    if neighbors is None:
        neighbors = {"a": ["b", "c", "d"], "b": ["a"], "c": ["a"], "d": ["a"]}
    if capacity is None:
        capacity = {t: 100 for t in neighbors}
    if costs is None:
        costs = {"b": 0, "c": 15, "d": 30}
    options = dict(
        n=100,
        target=200,
        half_life_s=15,
        seed=20261103,
        task_key="test",
        profile_version="frontier-local-v1",
    )
    options.update(kwargs)
    return grow_footprint(
        roots=roots,
        neighbors=neighbors,
        capacity=capacity,
        mass=mass or capacity,
        cost_s=lambda root, t: costs[t],
        **options,
    )


def test_nomination_law():
    assert nomination_weights({"d": math.inf, "b": 65, "c": 80, "a": 50}, 15) == {
        "a": 1.0,
        "b": 0.5,
        "c": 0.25,
    }
    assert nomination_weights({"b": 0, "a": 0}, 0) == {"a": 1}
    assert nomination_weights({"b": 0, "a": 0}, 1) == {"a": 1, "b": 1}
    assert nomination_weights({"a": math.inf}, 15) == {}
    with pytest.raises(ValueError):
        nomination_weights({"a": float("nan")}, 15)


def test_draws_replay_and_have_specified_probabilities():
    assert grow() == grow(
        neighbors={"d": ["a"], "c": ["a"], "b": ["a"], "a": ["d", "c", "b"]}
    )
    counts = Counter(grow(task_key=i)["tracts"][-1] for i in range(3000))
    for tract, fraction in zip("bcd", [4 / 7, 2 / 7, 1 / 7]):
        assert abs(counts[tract] / 3000 - fraction) < 0.035
    assert grow(half_life_s=0)["tracts"] == ["a", "b"]


def test_draft_profile_is_replayable():
    path = Path(__file__).resolve().parents[1] / "tests/fixtures/draft_profile.json"
    options = json.loads(path.read_text())["footprint_options"]
    outcome = grow(**options)
    record = outcome["trace"][0]["nominations"][0]
    assert (
        outcome["half_life_s"] == 15
        and outcome["profile_version"] == options["profile_version"]
    )
    assert record["frontier_size"] == 3 and record["min_cost_s"] == 0
    assert record["probability"] == pytest.approx(2 ** (-record["cost_s"] / 15) / 1.75)


def test_shared_nomination_has_one_vote_and_stable_owner():
    adj = {"a": ["x"], "b": ["x"], "c": ["y"], "x": ["a", "b"], "y": ["c"]}
    options = dict(
        roots=["b", "c", "a"], neighbors=adj, target=400, costs={"x": 1, "y": 1}
    )
    samples = [grow(task_key=i, **options) for i in range(1000)]
    assert 0.45 < sum(s["tracts"][-1] == "x" for s in samples) / len(samples) < 0.55
    winner = next(s for s in samples if s["tracts"][-1] == "x")
    assert winner["owners"]["x"] == "a" and len(winner["trace"][0]["nominations"]) == 3
    assert (
        grow_footprint(
            roots=["b", "a"],
            neighbors=adj,
            capacity={t: 100 for t in adj},
            mass={t: 1 for t in adj},
            cost_s=lambda g, t: 5 if g == "a" else 2,
            n=100,
            target=300,
            half_life_s=15,
            seed=1,
            task_key="tie",
            profile_version="frontier-local-v1",
        )["owners"]["x"]
        == "b"
    )


def test_stopping_and_shortfall():
    assert grow(target=100)["trace"] == []
    old = grow(target=125)
    assert (
        old["tracts"] == ["a"]
        and not old["trace"][-1]["retained"]
        and not old["shortfall"]
    )
    assert grow(capacity=dict(a=81, b=63, c=63, d=63), n=70, target=108)["tracts"] == [
        "a"
    ]
    enough = grow(costs={t: math.inf for t in "bcd"})
    assert enough["shortfall"] and enough["reason"] == "frontier_exhausted"
    assert grow(n=120, costs={t: math.inf for t in "bcd"})["status"] == "unsupported"
    assert grow(n=1)["reason"] == "tract_count_limit"


def test_first_steps_with_complete_chicago_residential_costs():
    path = Path(__file__).resolve().parents[1] / "tests/fixtures/r1_first_frontier.csv"
    with path.open() as stream:
        rows = list(csv.DictReader(stream))
    for slot, top, p in [("0", "17031061800", 0.87450), ("2", "17031260400", 0.83772)]:
        ring = [r for r in rows if r["slot"] == slot]
        w = nomination_weights({r["tract"]: float(r["exact_s"]) for r in ring}, 15)
        assert max(w, key=w.get) == top
        assert w[top] / sum(w.values()) == pytest.approx(p, abs=1e-5)


def test_boundary_parent_votes_and_road_detour():
    # After a, b, c, the parents a and b both nominate x, while c nominates y.
    graph = {
        "a": ["b", "x"],
        "b": ["a", "c", "x"],
        "c": ["b", "y"],
        "x": ["a", "b"],
        "y": ["c"],
    }
    options = dict(
        neighbors=graph,
        n=4,
        target=400,
        costs=dict(b=0, c=1, x=2, y=120),
        profile_version=R3,
        half_life_s=None,
    )
    counts = Counter()
    for i in range(700):
        result = grow(task_key=i, **options)
        assert "half_life_s" not in result and result["tracts"][:2] == ["a", "b"]
        if result["tracts"][2] != "c":
            continue
        vote = result["trace"][2]["nominations"][0]
        assert vote["expandable_parents"] == 3
        expected = "y" if vote["parent"] == "c" else "x"
        counts[expected] += 1
        assert vote["tract"] == expected
        assert vote["probability"] == (1 if expected == "y" else 2) / 3
        assert vote["excess_cost_s"] == (118 if expected == "y" else 0)
    assert counts.total() > 270 and abs(counts["x"] / counts.total() - 2 / 3) < 0.09


def test_r3_canonical_order_blocked_groups_and_rollback():
    adj = {"a": ["x"], "b": ["x"], "c": ["z"], "x": ["a", "b"], "z": ["c"]}
    args = dict(
        roots=["b", "a", "c"],
        neighbors=adj,
        capacity={t: 100 for t in adj},
        mass={t: 1 for t in adj},
        cost_s=lambda g, t: math.inf if t == "z" else 2,
        n=100,
        target=325,
        seed=13,
        task_key="shared",
    )
    result = grow_footprint(**args)
    assert (
        result["nomination_rule"] == "boundary_parent"
        and result["profile_version"] == R3
    )
    assert (
        result["tracts"] == ["b", "a", "c"]
        and result["reason"] == "target_crossed_choose_previous"
    )
    (step,) = result["trace"]
    assert (
        not step["retained"]
        and step["owner"] == "a"
        and step["selection_probability"] == 1
    )
    assert [v["tract"] for v in step["nominations"]] == ["x", "x", None]
    reordered = {k: list(reversed(v)) for k, v in reversed(list(adj.items()))}
    assert grow_footprint(**{**args, "neighbors": reordered}) == result
    with pytest.raises(ValueError, match="half-life"):
        grow_footprint(**args, half_life_s=15)
