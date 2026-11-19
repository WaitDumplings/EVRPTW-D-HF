"""Pre-register the city slice of the 17,400-slot panel independently of execution order."""

from datetime import date, timedelta
import hashlib
import json
import pandas as pd
from .common import shared_identity
from .common import (
    CITIES,
    runtime_contract,
    BUDGET,
    DEV,
    PROJECT,
    panel_code_id,
    public_split,
    digest,
    lock,
    read,
    sha,
    write,
)

QUOTA = {
    "train": {100: 1000, 500: 200, 1000: 100},
    "val": {100: 30, 500: 30, 1000: 30},
    "test1": {100: 50, 500: 50, 1000: 50},
    "xs": {2000: 50},
    "test2": {100: 500, 500: 500, 1000: 500},
}
WEEKDAYS = {
    "train": {100: 7143, 500: 1429, 1000: 714},
    "val": dict.fromkeys([100, 500, 1000], 214),
    "test1": dict.fromkeys([100, 500, 1000], 357),
    "test2": dict.fromkeys([100, 500, 1000], 357),
    "xs": {2000: 357},
}


def instance_namespace(plan, plan_sha):
    """Use the original panel identity for immutable instance IDs after a split rename."""
    if plan.get("city") != "jacksonville" or not any(
        t["split"] == "test2" for t in plan["tasks"]
    ):
        return plan_sha
    tasks = [
        dict(t, split="test3") if t["split"] == "test2" else t for t in plan["tasks"]
    ]
    original = dict(plan, tasks=tasks, tasks_sha256=digest(tasks))
    # Match the on-disk preregistration written by stage2.common.write.
    raw = json.dumps(original, sort_keys=True, indent=2, allow_nan=False) + "\n"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def depot_order(loc, seed, city):
    pools = {}
    for bg, group in loc.loc[loc.location_type == "depot"].groupby("bg_geoid"):
        pools[bg] = sorted(
            group.index, key=lambda depot: digest([seed, city, "depot", bg, depot])
        )
    groups = sorted(pools, key=lambda bg: digest([seed, city, "bg", bg]))
    if not groups:
        raise ValueError("No eligible depot with a block group")
    return [
        pools[g][i]
        for i in range(max(map(len, pools.values())))
        for g in groups
        if i < len(pools[g])
    ]


def plan_tasks(city, loc, seed, start, days, custom=None):
    calendar = [date.fromisoformat(start) + timedelta(days=i) for i in range(days)]
    by_day = {
        kind: [
            d.isoformat() for d in calendar if (d.weekday() < 5) == (kind == "weekday")
        ]
        for kind in ["weekday", "weekend"]
    }
    if not all(by_day.values()):
        raise ValueError("Calendar must include weekdays and weekends")
    depots = depot_order(loc, seed, city)
    rows = []
    quotas = QUOTA if custom is None else {"custom": {custom[0]: custom[2]}}
    for split, sizes in quotas.items():
        if custom is None and (city == "jacksonville") != (split == "test2"):
            continue
        # This token is part of the original seed and slot identity.
        stream_split = "test3" if split == "test2" else split
        for n, count in sizes.items():
            total = WEEKDAYS[split][n] if custom is None else (5 * count + 3) // 7
            if split in {"custom", "test2"}:
                weekday = total
            else:
                order = sorted(
                    DEV, key=lambda c: digest([seed, "weekday_remainder", split, n, c])
                )
                weekday = total // 10 + (order.index(city) < total % 10)
            ranked = sorted(
                range(count),
                key=lambda i: digest([seed, city, stream_split, n, "day_type", i]),
            )
            wd = set(ranked[:weekday])
            dayno = {"weekday": 0, "weekend": 0}
            for i in range(count):
                kind = "weekday" if i in wd else "weekend"
                dates = by_day[kind]
                d = dates[dayno[kind] % len(dates)]
                dayno[kind] += 1
                key = [city, stream_split, n, i + 1, depots[i % len(depots)], d, kind]
                rows.append(
                    dict(
                        city=city,
                        split=split,
                        n=n,
                        slot=i + 1,
                        serial=len(rows),
                        date=d,
                        day_type=kind,
                        depot=key[4],
                        depot_bg=str(loc.at[key[4], "bg_geoid"]),
                        seed=seed,
                        task_key=key,
                        cs_budget=BUDGET[n] if custom is None else custom[1],
                    )
                )
    return rows


def save_plan(shared, path, seed, start, days, custom=None):
    city = read(shared / "shared.json")["city"]
    loc = pd.read_parquet(shared / "locations.parquet")
    plan = dict(
        city=city,
        version="2026-11-18-r3",
        calendar_start=start,
        calendar_days=days,
        root_seed=seed,
        runtime=runtime_contract(),
        code_sha256=panel_code_id(),
        shared_sha256=shared_identity(shared),
        input_vintage=read(shared / "shared.json").get("vintage"),
        calibration_sha256=read(shared / "shared.json").get("calibration"),
        resource_sha256={
            f.name: sha(f)
            for f in sorted((PROJECT / "config").iterdir())
            if f.is_file()
        },
        tasks=plan_tasks(city, loc, seed, start, days, custom),
    )
    plan["tasks_sha256"] = digest(plan["tasks"])
    with lock(path.with_suffix(".lock")):
        if path.exists():
            existing = read(path)
            if existing["tasks_sha256"] == digest(existing["tasks"]):
                for task in existing["tasks"]:
                    task["split"] = public_split(city, task["split"])
                existing["tasks_sha256"] = digest(existing["tasks"])
            if existing != plan:
                raise ValueError(
                    "A different preregistered panel exists; choose a fresh path"
                )
            return len(plan["tasks"])
        write(path, plan)
    return len(plan["tasks"])


def city_contract(city, panel):
    """Validate one release slice using only the panel and the published design."""
    registered = read(PROJECT / "config/release_panel.json")
    if panel["city"] != city or panel["tasks_sha256"] != digest(panel["tasks"]):
        raise ValueError("Wrong city or changed task list: " + city)
    if any(panel[k] != value for k, value in registered.items()) or not all(
        panel[k] for k in ("input_vintage", "calibration_sha256")
    ):
        raise ValueError(
            "Incompatible release plan or missing city calibration: " + city
        )
    quotas = {
        (s, n): count
        for s, sizes in QUOTA.items()
        if (city == "jacksonville") == (s == "test2")
        for n, count in sizes.items()
    }
    slots = set()
    for t in panel["tasks"]:
        split, n = public_split(city, t["split"]), t["n"]
        key = (split, n, t["slot"])
        if (
            t["city"] != city
            or key in slots
            or not 1 <= t["slot"] <= quotas.get((split, n), 0)
            or t["cs_budget"] != BUDGET[n]
        ):
            raise ValueError("Invalid release slot: " + city)
        slots.add(key)
    if len(slots) != sum(quotas.values()):
        raise ValueError("Release quotas differ: " + city)
    return {k: panel[k] for k in (*registered, "code_sha256", "resource_sha256")}


def seal_plans(folder):
    """Collect independent city plans after production."""
    common = None
    hashes, city_inputs = {}, {}
    total = 0
    for city in CITIES:
        path = folder / (city + ".json")
        panel = read(path)
        fields = city_contract(city, panel)
        if common is not None and common != fields:
            raise ValueError("Incompatible release plan: " + city)
        common = fields
        city_inputs[city] = {
            k: panel[k]
            for k in ("runtime", "input_vintage", "calibration_sha256", "shared_sha256")
        }
        hashes[city] = sha(path)
        total += len(panel["tasks"])
    result = dict(
        schema="evrptwd_city_panel_v2",
        contract=common,
        city_inputs=city_inputs,
        plans_sha256=hashes,
        task_count=total,
    )
    with lock(folder / "release.lock"):
        dest = folder / "release.json"
        if dest.is_file() and read(dest) != result:
            raise ValueError("A different release index already exists")
        write(dest, result)
    return result
