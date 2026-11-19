"""Diagnose unsupported addresses and replay a route that has to charge on both sides."""

import numpy as np
import pytest

from stage2.instance_witness import make_witness, check_witness


def case():
    vehicle = dict(
        operating_window_s=[0, 10000],
        battery_capacity_kwh=100,
        initial_soc=1,
        reserve_soc=0.1,
        specific_energy_kwh_per_km=1,
        cargo_capacity_cm3=100,
    )
    nodes = [
        dict(
            LOCATION_ID=k,
            TYPE=k,
            DEMAND_CM3=int(k == "customer"),
            READY_S=0,
            DUE_S=10000,
            CHARGE_KW=100 if k == "charging" else 0,
            SERVICE_S=0,
        )
        for k in ["depot", "customer", "charging"]
    ]
    d = 1000 * np.array([[0, 70, 45], [70, 0, 25], [45, 25, 0]], dtype=float)
    arrays = {"shortest_distance_m": d, "shortest_distance_time_s": d / 100}
    return nodes, arrays, vehicle


def test_full_charge_witness_and_replay():
    nodes, arrays, vehicle = case()
    w = make_witness(nodes, arrays, vehicle)
    assert (
        w["routes"][0]["indices"] == [0, 2, 1, 2, 0]
        and w["status"] == "protocol_certified"
    )
    assert check_witness(w, nodes, arrays, vehicle) == 1


@pytest.mark.parametrize("reason", ["energy", "due", "empty_interval", "shift"])
def test_failure_filter(reason):
    nodes, arrays, vehicle = case()
    if reason == "energy":
        arrays["shortest_distance_m"][2, 1] = 100000
    elif reason == "due":
        nodes[1]["DUE_S"] = 50
    elif reason == "empty_interval":
        nodes[1].update(READY_S=200, DUE_S=100)
    else:
        vehicle["operating_window_s"][1] = 2000
    w = make_witness(nodes, arrays, vehicle)
    assert w["failures"][0]["reason"] == reason
    assert w["status"] == (
        "proven_infeasible" if reason == "empty_interval" else "protocol_uncertified"
    )


def test_independent_replay_rejects_damaged_soc_trace_and_false_infeasibility():
    nodes, arrays, vehicle = case()
    w = make_witness(nodes, arrays, vehicle)
    w["routes"][0]["events"][0]["arrival_kwh"] += 2
    with pytest.raises(ValueError):
        check_witness(w, nodes, arrays, vehicle)
    nodes[1]["DUE_S"] = 1
    w = make_witness(nodes, arrays, vehicle)
    w["failures"][0]["reason"] = "empty_interval"
    w["status"] = "proven_infeasible"
    with pytest.raises(ValueError):
        check_witness(w, nodes, arrays, vehicle)


def test_energy_screen_and_zero_gain_station_padding():
    from stage2.stations import station_selection

    v = case()[2]
    matrix = np.full((5, 5), 1000.0)
    np.fill_diagonal(matrix, 0)
    matrix[2, :2] = matrix[:2, 2] = 200000.0
    ids, report = station_selection(
        matrix, matrix / 10, ["a", "b", "c"], ["cust"], np.array([90, 90, 90]), v, 3
    )
    assert ids == ["b", "c"] and report["steps"][-1]["marginal_joint_gain"] == 0
    assert report["covered"] == 2
