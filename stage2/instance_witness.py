"""Construct one-customer route certificates and replay full routes independently."""

from collections import Counter
import heapq

import numpy as np


def replay_route(route, nodes, dist, times, vehicle):
    start, end = vehicle["operating_window_s"]
    capacity = vehicle["battery_capacity_kwh"]
    lower = capacity * vehicle["reserve_soc"]
    rate = vehicle["specific_energy_kwh_per_km"]
    if len(route) < 3 or route[0] != 0 or route[-1] != 0 or 0 in route[1:-1]:
        raise ValueError("depot endpoints")
    load = sum(
        nodes[j]["DEMAND_CM3"] for j in route[1:-1] if nodes[j]["TYPE"] == "customer"
    )
    if load > vehicle["cargo_capacity_cm3"]:
        raise ValueError("load")
    clock = float(start)
    soc = capacity * vehicle["initial_soc"]
    events = []
    for i, j in zip(route, route[1:]):
        travel = float(times[i, j])
        distance = float(dist[i, j])
        if not np.isfinite(travel + distance) or min(travel, distance) < 0:
            raise ValueError("road")
        arrival = clock + travel
        soc -= rate * distance / 1000
        if soc < lower:
            raise ValueError("energy")
        row = nodes[j]
        clock = max(arrival, row["READY_S"])
        before = soc
        begin = clock
        if row["TYPE"] == "customer" and begin > row["DUE_S"]:
            raise ValueError("due")
        if row["TYPE"] == "customer":
            clock += row["SERVICE_S"]
            load -= row["DEMAND_CM3"]
        elif row["TYPE"] == "charging":
            clock += 3600 * (capacity - soc) / row["CHARGE_KW"]
            soc = capacity
        if clock > end:
            raise ValueError("shift")
        events.append(
            dict(
                index=j,
                arrival_s=arrival,
                start_s=begin,
                departure_s=clock,
                arrival_kwh=before,
                departure_kwh=soc,
                remaining_load_cm3=load,
            )
        )
    return dict(
        indices=route,
        events=events,
        return_s=clock,
        distance_m=sum(float(dist[i, j]) for i, j in zip(route, route[1:])),
        minimum_arrival_kwh=min(e["arrival_kwh"] for e in events),
    )


def _station_paths(nodes, dist, times, vehicle, stations, reverse):
    """Earliest full-charge prefix / shortest suffix; depot cannot be an interior node."""
    rate = vehicle["specific_energy_kwh_per_km"]
    capacity = vehicle["battery_capacity_kwh"]
    available = capacity * (1 - vehicle["reserve_soc"])
    best = {0: (0.0, (), (0,))}
    heap = [(0.0, (), (0,), 0)]
    while heap:
        elapsed, ids, path, u = heapq.heappop(heap)
        if best[u] != (elapsed, ids, path):
            continue
        for v in stations:
            if v in path:
                continue
            i, j = (v, u) if reverse else (u, v)
            energy = rate * dist[i, j] / 1000
            if not np.isfinite(energy + times[i, j]) or energy > available:
                continue
            weight = times[i, j] + (3600 * energy / nodes[j]["CHARGE_KW"] if j else 0)
            chain = ((v,) + path) if reverse else (path + (v,))
            key = tuple(nodes[k]["LOCATION_ID"] for k in chain)
            value = (float(elapsed + weight), key, chain)
            if v not in best or value < best[v]:
                best[v] = value
                heapq.heappush(heap, (*value, v))
    return {k: v[2] for k, v in best.items()}


def make_witness(nodes, arrays, vehicle):
    dist, times = arrays["shortest_distance_m"], arrays["shortest_distance_time_s"]
    stations = [j for j, r in enumerate(nodes) if r["TYPE"] == "charging"]
    routes = []
    failures = []
    prefixes = suffixes = None
    for i, row in enumerate(nodes):
        if row["TYPE"] != "customer":
            continue
        if row["READY_S"] > row["DUE_S"]:
            failures.append(dict(customer=row["LOCATION_ID"], reason="empty_interval"))
            continue
        try:
            result = replay_route([0, i, 0], nodes, dist, times, vehicle)
        except ValueError:
            if prefixes is None:
                prefixes = _station_paths(nodes, dist, times, vehicle, stations, False)
                suffixes = _station_paths(nodes, dist, times, vehicle, stations, True)
            result = None
            best_key = None
            reasons = set()
            for a, left in prefixes.items():
                for b, right in suffixes.items():
                    try:
                        path = list(left) + [i] + list(right)
                        trial = replay_route(path, nodes, dist, times, vehicle)
                    except ValueError as err:
                        reasons.add(str(err))
                        continue
                    key = (
                        trial["return_s"],
                        tuple(nodes[j]["LOCATION_ID"] for j in path),
                    )
                    if best_key is None or key < best_key:
                        best_key, result = key, trial
            if result is None:
                # A reason is shared only if all paths fail at this or an earlier filter.
                # Keep per-path first failure labels for inspection.
                # Station-only prefixes and suffixes already respect the full-charge arc budget.
                rate = vehicle["specific_energy_kwh_per_km"] / 1000
                available = vehicle["battery_capacity_kwh"] * (
                    vehicle["initial_soc"] - vehicle["reserve_soc"]
                )
                energy_ok = [
                    a
                    for a in prefixes
                    if any(
                        rate * (dist[a, i] + dist[i, b]) <= available for b in suffixes
                    )
                ]
                due_ok = False
                for a in energy_ok:
                    prefix = prefixes[a]
                    elapsed = float(vehicle["operating_window_s"][0])
                    for prev, next_station in zip(prefix, prefix[1:]):
                        elapsed += (
                            times[prev, next_station]
                            + 3600
                            * rate
                            * dist[prev, next_station]
                            / nodes[next_station]["CHARGE_KW"]
                        )
                    if max(row["READY_S"], elapsed + times[a, i]) <= row["DUE_S"]:
                        due_ok = True
                        break
                reason = (
                    "energy" if not energy_ok else ("due" if not due_ok else "shift")
                )
                failures.append(
                    dict(
                        customer=row["LOCATION_ID"],
                        reason=reason,
                        observed_failure_checks=sorted(reasons),
                    )
                )
                continue
        routes.append(result)
    status = (
        "protocol_certified"
        if not failures
        else "proven_infeasible"
        if any(f.get("reason") == "empty_interval" for f in failures)
        else "protocol_uncertified"
    )
    return dict(
        protocol="unlimited_fleet_one_customer_per_route_full_charge_v1",
        status=status,
        routes=routes,
        failures=failures,
    )


def verify_events(route, nodes, dist, times, vehicle):
    """Check recorded states by arc conservation; independent of the route constructor."""
    seq = route["indices"]
    if len(seq) < 3 or seq[0] != 0 or seq[-1] != 0 or 0 in seq[1:-1]:
        raise ValueError("Invalid route endpoints")
    events = route["events"]
    dst = np.array(seq[1:])
    if [e["index"] for e in events] != dst.tolist():
        raise ValueError("Route/event sequence differs")
    lengths = dist[seq[:-1], dst]
    travel = times[seq[:-1], dst]
    energy = lengths * vehicle["specific_energy_kwh_per_km"] / 1000
    capacity = vehicle["battery_capacity_kwh"]
    start, end = vehicle["operating_window_s"]
    cols = {
        k: np.array([e[k] for e in events], dtype=float)
        for k in (
            "arrival_s",
            "start_s",
            "departure_s",
            "arrival_kwh",
            "departure_kwh",
            "remaining_load_cm3",
        )
    }
    demand = np.array([nodes[i]["DEMAND_CM3"] for i in dst])
    cargo = float(demand.sum())
    if cargo > vehicle["cargo_capacity_cm3"] or (demand < 0).any():
        raise ValueError("Invalid witness load")
    charging = np.array([nodes[i]["TYPE"] == "charging" for i in dst])
    expected_arrival = np.r_[start, cols["departure_s"][:-1]] + travel
    expected_soc = (
        np.r_[capacity * vehicle["initial_soc"], cols["departure_kwh"][:-1]] - energy
    )
    starts = np.maximum(cols["arrival_s"], [nodes[i]["READY_S"] for i in dst])
    stay = np.array([nodes[i]["SERVICE_S"] for i in dst], dtype=float)
    power = np.array([nodes[i]["CHARGE_KW"] for i in dst])
    stay[charging] = 3600 * (capacity - cols["arrival_kwh"][charging]) / power[charging]
    if (
        (cols["arrival_kwh"] < capacity * vehicle["reserve_soc"]).any()
        or (starts > [nodes[i]["DUE_S"] for i in dst]).any()
        or (cols["departure_s"] > end).any()
    ):
        raise ValueError("Witness violates SOC, time window or shift")
    checks = [
        (cols["arrival_s"], expected_arrival),
        (cols["arrival_kwh"], expected_soc),
        (cols["start_s"], starts),
        (cols["departure_s"], starts + stay),
        (cols["departure_kwh"], np.where(charging, capacity, cols["arrival_kwh"])),
        (cols["remaining_load_cm3"], cargo - np.cumsum(demand)),
        (route["distance_m"], lengths.sum()),
        (route["return_s"], cols["departure_s"][-1]),
        (route["minimum_arrival_kwh"], cols["arrival_kwh"].min()),
    ]
    if not all(np.allclose(a, b, rtol=1e-12, atol=1e-9) for a, b in checks):
        raise ValueError("Saved witness violates road-time, SOC or load conservation")


def check_witness(witness, nodes, arrays, vehicle):
    """Check fixed published routes and their numerical traces without running the search."""
    dist, times = arrays["shortest_distance_m"], arrays["shortest_distance_time_s"]
    seen = Counter()
    for saved in witness["routes"]:
        ix = saved["indices"]
        if any(type(j) != int or not 0 <= j < len(nodes) for j in ix):
            raise ValueError("unknown witness node")
        visits = [nodes[j]["LOCATION_ID"] for j in ix if nodes[j]["TYPE"] == "customer"]
        if len(visits) != 1:
            raise ValueError("protocol route customer count")
        seen.update(visits)
        verify_events(saved, nodes, dist, times, vehicle)
    failed = {f["customer"] for f in witness["failures"]}
    all_ids = {r["LOCATION_ID"] for r in nodes if r["TYPE"] == "customer"}
    if (
        seen.keys() & failed
        or set(seen) | failed != all_ids
        or any(v != 1 for v in seen.values())
    ):
        raise ValueError("duplicate or omitted witness customer")
    empty = {
        row["LOCATION_ID"]
        for row in nodes
        if row["TYPE"] == "customer" and row["READY_S"] > row["DUE_S"]
    }
    labelled = {
        item["customer"]
        for item in witness["failures"]
        if item.get("reason") == "empty_interval"
    }
    if len(failed) != len(witness["failures"]) or empty != labelled:
        raise ValueError("Invalid empty-window diagnosis or duplicate failed customer")
    expected = (
        "proven_infeasible"
        if empty
        else "protocol_uncertified"
        if failed
        else "protocol_certified"
    )
    if witness["status"] != expected:
        raise ValueError("inconsistent witness status")
    return len(seen)
