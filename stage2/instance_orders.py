"""Realize whole address orders from the r2 full-training catalog on fixed r3 customers."""

import json
from fractions import Fraction
from functools import lru_cache

import numpy as np
import pandas as pd

PROFILE_READY_END_S = 6 * 3600

from .frontier import _stream
from .common import PROJECT, MethodFailure, read, sha
from pathlib import Path
from evrptw_scenarios.calibration import service_curve
from evrptw_scenarios.demand import active_location_count


@lru_cache(maxsize=1)
def _load(catalog):
    method = json.loads((PROJECT / "config/customer_profile.json").read_text())
    profile_file = PROJECT / "config/order_profile.json"
    if sha(profile_file) != method["order_profile"]["sha256"]:
        raise ValueError("Order profile digest mismatch")
    profile = json.loads(profile_file.read_text())
    if (
        sha(PROJECT / "config/manifest.json")
        != profile["sources"]["input_manifest"]["sha256"]
    ):
        raise ValueError("Changed Amazon reference")
    receipt = read(catalog / "receipt.json")
    from sources.orders import SOLVER_COLUMNS

    for name in (*SOLVER_COLUMNS, "solver_fields.json"):
        if sha(catalog / name) != receipt["outputs"][name]:
            raise ValueError("Changed Amazon catalog file: " + name)
    if read(catalog / "solver_fields.json") != read(
        PROJECT / "config/amazon_solver_fields.json"
    ):
        raise ValueError("Amazon solver fields differ from the registered reference")
    donors = pd.read_parquet(catalog / "service.parquet")
    windows = pd.read_parquet(catalog / "windows.parquet").set_index("donor_id")
    bundles = pd.read_parquet(
        catalog / "bundles.parquet", columns=["donor_id", "parcel_rows"]
    ).set_index("donor_id")
    parcels = pd.read_parquet(
        catalog / "parcels.parquet", columns=["volume_cm3", "source_parcel_ref"]
    )
    volumes, parcel_ids = (
        parcels.volume_cm3.to_numpy(),
        parcels.source_parcel_ref.to_numpy(),
    )
    by_count = donors.groupby("package_count").indices
    days = {k: set(donors.source_station_day_id.iloc[ix]) for k, ix in by_count.items()}
    params = profile["orders"]

    @lru_cache(maxsize=None)
    def choices(count):
        groups, seen = [], set()
        for k in sorted(
            by_count,
            key=lambda k: (Fraction(max(int(k), count), min(int(k), count)), k),
        ):
            groups.append(by_count[k])
            seen.update(days[k])
            if (
                sum(map(len, groups)) >= params["minimum_donor_bundles"]
                and len(seen) >= params["minimum_donor_station_days"]
            ):
                return np.concatenate(groups)
        raise MethodFailure("orders", "insufficient_donor_support")

    if (
        not donors.donor_id.is_unique
        or not windows.index.is_unique
        or not bundles.index.is_unique
    ):
        raise ValueError("Ambiguous donor identity")
    return profile, params, donors, windows, bundles, volumes, parcel_ids, choices


def build_orders(fixture, locations, catalog):
    profile, params, donors, windows, bundles, volumes, parcel_ids, choices = _load(
        catalog
    )
    orders = []
    shift_start, shift_end = read(PROJECT / "config/vehicle.json")["operating_window_s"]
    ready_end = PROFILE_READY_END_S
    for loc in fixture["customer_ids"]:
        units = int(locations.loc[loc, "units_final"])

        def stream(purpose):
            return _stream(
                fixture["root_seed"],
                fixture["task_key"],
                fixture["profile_version"],
                [purpose, loc],
            )

        count_rng, donor_rng, ready_rng = map(
            stream, ["order-count", "order-donor", "parcel-ready"]
        )
        for attempt in range(params["maximum_full_proposals"]):
            active, count = active_location_count(
                units,
                fixture["p"],
                params["mean_parcels_per_active_household"],
                count_rng,
                conditioned_active=True,
            )
            typ = choices(count)
            record = donors.iloc[typ[donor_rng.integers(len(typ))]]
            original = int(record.package_count)
            rows = np.asarray(bundles.at[record.donor_id, "parcel_rows"], int)
            if len(rows) != original:
                raise ValueError("Order and parcel membership disagree")
            if count != original:
                rows = donor_rng.choice(rows, size=count, replace=(count > original))
            volume = volumes[rows].tolist()
            demand = sum(volume)
            if demand <= params["cargo_capacity_cm3"]:
                break
        else:
            raise MethodFailure("orders", "order_proposal_limit", loc)
        g = service_curve(count, profile["service"])
        intercept = profile["service"]["service_a_s"]
        correction = (
            intercept
            + (g - intercept)
            * (
                0.5
                + 0.5
                * (active / max(1, count / params["mean_parcels_per_active_household"]))
                ** 0.5
            )
        ) / g
        service = (
            float(record.service_s)
            * g
            * correction
            / service_curve(original, profile["service"])
        )
        window = (
            windows.loc[record.donor_id].tolist()
            if record.donor_id in windows.index
            else None
        )
        ready = ready_rng.uniform(0, ready_end, size=count).tolist()
        start = max(shift_start, *ready)
        due = shift_end - service
        if window:
            start, due = max(start, window[0]), min(due, window[1])
        orders.append(
            dict(
                location_id=loc,
                tract=locations.at[loc, "tract_id"],
                units=units,
                active_households=active,
                parcels=count,
                donor_id=record.donor_id,
                donor_station_day=record.source_station_day_id,
                donor_parcels=original,
                donor_service_s=float(record.service_s),
                donor_local_tw_s=window,
                source_parcel_ids=parcel_ids[rows].tolist(),
                parcel_volumes_cm3=volume,
                parcel_ready_s=ready,
                parcel_promise_s=shift_end,
                capacity_rejections=attempt,
                demand_cm3=demand,
                service_s=service,
                ready_s=float(start),
                due_s=float(due),
            )
        )
    return orders
