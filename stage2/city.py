"""Convert an accepted city CLE to portable assets and bind its static daily router."""

from functools import lru_cache
from pathlib import Path
import os
import shutil
import tempfile

import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.sparse.csgraph import dijkstra

from .common import PROJECT, lock, read, sha, write
from .customers import admitted_location_mask, PROFILE
from .frontier import residential_costs_s
from evrptw_scenarios.routing import canonical_endpoint_frame, FrozenCostRouter
from evrptw_scenarios.cle_binding import cle_authority, BUILDING_SCHEMA


def assign_bg(frame, bg):
    points = frame.to_crs(4326).geometry.representative_point()
    geo = gpd.GeoDataFrame(
        {"location_id": frame.location_id.to_numpy()},
        geometry=points.to_numpy(),
        crs=4326,
    )
    geo["location_lon"], geo["location_lat"] = points.x.to_numpy(), points.y.to_numpy()
    # Boundaries can contain a point on two polygons. Assign the smaller GEOID on ties.
    out = gpd.sjoin(
        geo.to_crs(bg.crs),
        bg[["bg_geoid", "geometry"]],
        how="left",
        predicate="intersects",
    )
    out = out.sort_values(["location_id", "bg_geoid"]).drop_duplicates("location_id")
    return pd.DataFrame(out.drop(columns=["geometry", "index_right"])).set_index(
        "location_id"
    )


def prepare(cle_root, city, target, geography):
    base = cle_root / "cities" / city
    manifest = read(base / "cle_manifest.json")
    if manifest["schema"] != BUILDING_SCHEMA or manifest["city_slug"] != city:
        raise ValueError("Expected r1 building CLE for " + city)
    authority, certificate = cle_authority(base, manifest)
    bgfile = geography / "bg.parquet"
    rook = geography / "rook.json"
    source = {
        "cle": sha(base / "cle_manifest.json"),
        "validation": sha(certificate),
        "cohort": sha(authority),
        "block_groups": sha(bgfile),
        "rook": sha(rook),
        "adapter": sha(Path(__file__)),
        "router": sha(PROJECT / "evrptw_scenarios/routing.py"),
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    with lock(target.parent / (target.name + ".prepare.lock")):
        if target.exists():
            if read(target / "shared.json")["source"] != source:
                raise ValueError(
                    "Existing shared city has different sources. Use a separate output root."
                )
            verify_shared(target)
            return
        boundary = manifest["boundaries"]["service_boundary"]
        for name in [
            "locations.parquet",
            "location_access_costs.parquet",
            "customers/latent_locations.parquet",
            "depots/depots.parquet",
            "charging/chargers.parquet",
            "speeds/directed_speeds.parquet",
            boundary,
        ]:
            if sha(base / name) != manifest["files"][name]["sha256"]:
                raise ValueError("CLE input changed: " + name)
        with tempfile.TemporaryDirectory(dir=target.parent) as temp:
            stage = Path(temp) / "city"
            stage.mkdir()
            frame = gpd.read_parquet(
                base / "locations.parquet",
                columns=[
                    "location_id",
                    "location_type",
                    "candidate_eligible",
                    "residential_units",
                    "access_row_id",
                    "geometry",
                ],
            )
            frame = frame[frame.candidate_eligible].sort_values("location_id")
            loc = assign_bg(frame, gpd.read_parquet(bgfile)).join(
                frame.set_index("location_id")[
                    ["location_type", "residential_units", "access_row_id"]
                ]
            )
            loc = loc.rename(columns={"residential_units": "units_final"})
            missing = loc.bg_geoid.isna() & loc.location_type.isin(
                ["depot", "customer"]
            )
            if missing.any():
                raise ValueError(
                    f"{int(missing.sum())} eligible depot/customer points outside the TIGER BG input"
                )
            loc["tract_id"] = loc.bg_geoid.str[:11]
            active_tracts = set(loc.loc[loc.location_type == "customer", "tract_id"])
            unmatched = active_tracts - set(read(rook)["neighbors"])
            if unmatched:
                raise ValueError(
                    "Missing TIGER rook rows for residential tracts: "
                    + str(sorted(unmatched))
                )
            cus = loc[loc.location_type == "customer"].units_final
            if not (cus.notna() & (cus >= 1) & (cus == np.floor(cus))).all():
                raise ValueError("CLE customer units must be positive integers")
            ch = pd.read_parquet(
                base / "charging/chargers.parquet",
                columns=[
                    "charging_location_id",
                    "assigned_power_kw",
                    "reference_charge_mode",
                ],
            ).set_index("charging_location_id")
            is_cs = loc.location_type == "charging"
            attrs = ch.loc[loc.loc[is_cs, "access_row_id"]]
            modes = attrs.reference_charge_mode
            if not modes.isin(["dc_fast", "ac_level2", "ac_level1"]).all():
                raise ValueError("Unknown charging mode")
            veh = read(PROJECT / "config/vehicle.json")
            if PROFILE["kwh_per_km"] != veh["specific_energy_kwh_per_km"] or PROFILE[
                "one_way_budget_kwh"
            ] != veh["battery_capacity_kwh"] * (
                veh["initial_soc"] - veh["reserve_soc"]
            ):
                raise ValueError(
                    "Customer admission and vehicle energy profiles disagree"
                )
            loc.loc[is_cs, "power_kw"] = (
                0.9
                * np.minimum(
                    attrs.assigned_power_kw,
                    np.where(modes == "dc_fast", veh["dc_cap_kw"], veh["ac_cap_kw"]),
                ).to_numpy()
            )
            if not loc.loc[is_cs, "power_kw"].gt(0).all():
                raise ValueError("Missing effective power")
            loc.loc[is_cs, "reference_charge_mode"] = modes.to_numpy()
            loc.drop(columns="access_row_id").to_parquet(stage / "locations.parquet")
            endpoints, ledger = canonical_endpoint_frame(base, loc.index)
            endpoints.to_parquet(stage / "endpoints.parquet", index=False)
            ledger[ledger.location_id.isin(loc.index)].to_parquet(
                stage / "access.parquet", index=False
            )
            cols = [
                "edge_u",
                "edge_v",
                "edge_key",
                "length_m",
                "in_public_graph",
                "operating_mode",
                "reference_speed_weekday_kph",
                "reference_speed_weekend_kph",
                "reference_travel_time_weekday_s",
                "reference_travel_time_weekend_s",
            ]
            pd.read_parquet(
                base / "speeds/directed_speeds.parquet", columns=cols
            ).to_parquet(stage / "speeds.parquet", index=False, compression="zstd")
            shutil.copyfile(rook, stage / "rook.json")
            shutil.copyfile(base / boundary, stage / "boundary.geojson")
            write(
                stage / "shared.json",
                dict(
                    schema="evrptwd_shared_v1",
                    city=city,
                    source=source,
                    vintage=read(authority).get(
                        "inputs_sha256", read(authority).get("freeze_manifest_sha256")
                    ),
                    files={f.name: sha(f) for f in sorted(stage.iterdir())},
                    locations=loc.location_type.value_counts().to_dict(),
                ),
            )
            os.rename(stage, target)


def verify_shared(root):
    meta = read(root / "shared.json")
    for name, value in meta["files"].items():
        if sha(root / name) != value:
            raise ValueError("Shared asset mismatch: " + name)
    return meta


class CityDay:
    def __init__(self, root, day, ids=None):
        self.loc = pd.read_parquet(root / "locations.parquet")
        if ids is not None:
            self.loc = self.loc.loc[ids]
        filters = None if ids is None else [("location_id", "in", list(ids))]
        frame = pd.read_parquet(root / "endpoints.parquet", filters=filters)
        self.router = FrozenCostRouter(
            pd.read_parquet(root / "speeds.parquet"), day_type=day
        )
        self.bound = {
            e.location_id: e
            for e in self.router.bind_endpoints(
                frame, pd.read_parquet(root / "access.parquet", filters=filters)
            )
        }
        self.candidates = self.loc[self.loc.location_type == "customer"]
        self.neighbors = read(root / "rook.json")["neighbors"]
        self.reverse = self.router.graphs["fastest_time"].T.tocsr()
        self.stock = lru_cache(maxsize=2)(self._stock)
        self._medoids = lru_cache(maxsize=32)(self._root_medoid)
        self._forward_centers = {}
        self._backward_centers = {}

    def center_times(self, center, locations):
        """Use one outward tree and one inward tree per fixed center across frontier tracts."""
        return self._forward(center, locations), self._back(
            center, locations, cache=True
        )

    def clear_centers(self):
        self._forward_centers.clear()
        self._backward_centers.clear()

    def medoid(self, ids, weights):
        return self._medoids(tuple(ids), tuple(weights))

    def _root_medoid(self, ids, weights):
        """Evaluate every candidate on its directed roads, sharing one augmented graph."""
        if len(ids) == 1:
            return 0.0, ids[0]
        ends = [self.bound[k] for k in ids]
        graph = self.router._augmented(ends, "fastest_time")
        root = len(self.router.node_ids)
        count = len(ids)
        best = (float("inf"), ids[0])
        # One forward sweep supplies outbound and transposed inbound rows. Large
        # tract matrices live in a temporary file, never in the instance archive.
        with tempfile.TemporaryFile() as scratch:
            matrix = (
                np.empty((count, count))
                if count <= 1024
                else np.memmap(
                    scratch, mode="w+", dtype="float64", shape=(count, count)
                )
            )
            for first in range(0, count, 8):
                indices = range(first, min(count, first + 8))
                ds, ps = dijkstra(
                    graph, indices=[root + i for i in indices], return_predecessors=True
                )
                for j, i in enumerate(indices):
                    matrix[i] = self.router._source_row(
                        i, ends, ds[j], ps[j], root + i, 1.0, policy="fastest_time"
                    )[1]
            for first in range(0, count, 64):
                chunk = slice(first, min(first + 64, count))
                costs = residential_costs_s(matrix[chunk], matrix[:, chunk].T, weights)
                best = min(best, min(zip(costs, ids[chunk])))
            del matrix
        return best

    def _forward(self, origin, destinations):
        if origin not in self._forward_centers:
            graph = self.router._augmented([self.bound[origin]], "fastest_time")
            self._forward_centers[origin] = dijkstra(
                graph, indices=len(self.router.node_ids), return_predecessors=True
            )
        dist, parents = self._forward_centers[origin]
        ends = [self.bound[origin], *(self.bound[k] for k in destinations)]
        row = self.router._source_row(
            0,
            ends,
            dist,
            parents,
            len(self.router.node_ids),
            1.0,
            policy="fastest_time",
        )[1][1:]
        for i, k in enumerate(destinations):
            if k == origin:
                row[i] = 0.0
        return row

    def _back(self, dest, sources, *, cache=False):
        target = self.bound[dest]
        opt = target.inbound
        seconds = self._backward_centers.get(dest)
        if seconds is None:
            rows = dijkstra(self.reverse, indices=[o.node for o in opt])
            seconds = (rows + np.array([o.time_s for o in opt])[:, None]).min(axis=0)
            if cache:
                self._backward_centers[dest] = seconds
        refs = dict(target.public_refs)
        result = []
        for k in sources:
            src = self.bound[k]
            best = min(seconds[o.node] + o.time_s for o in src.outbound)
            for edge, a in src.public_refs:
                b = refs.get(edge)
                if b is not None and b >= a - 1e-10:
                    best = min(
                        best,
                        src.connector.time_s
                        + self.router._native(edge, a, max(a, b)).time_s
                        + target.connector.time_s,
                    )
            result.append(0.0 if k == dest else best)
        return np.asarray(result)

    def _stock(self, depot):
        ids = self.candidates.index
        dist, t = self.router.source_costs(
            self.bound[depot], [self.bound[k] for k in ids], policy="fastest_time"
        )
        mask = admitted_location_mask(
            self.candidates.tract_id, dist, t, self._back(depot, ids)
        )
        stock = self.candidates[mask]
        return {
            t: {k: int(u) for k, u in sub.units_final.items()}
            for t, sub in stock.groupby("tract_id")
        }

    def travel(self, origins, dests):
        if len(origins) == 1:
            return self._forward(origins[0], dests)[None, :]
        if len(dests) == 1:
            return self._back(dests[0], origins, cache=True)[:, None]
        if len(dests) < len(origins):
            return np.stack([self._back(k, origins) for k in dests], axis=1)
        # source_costs uses a separate origin row; explicitly zero a co-located ID on its own diagonal.
        ends = [self.bound[k] for k in dests]
        pos = {k: j for j, k in enumerate(dests)}
        result = []
        for k in origins:
            t = self.router.source_costs(self.bound[k], ends, policy="fastest_time")[1]
            if k in pos:
                t[pos[k]] = 0.0
            result.append(t)
        return np.stack(result)
