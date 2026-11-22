"""Recompute the four persisted matrices using only the public city and instance."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd
from test_solver_parquet import example

from instance_file import HF_FORMAT, read_portable, write_instance
from solver_parquet import HF_INFO_FIELDS, MATRICES, Problem, hashes
from stage2.city import CityDay
from stage2.common import sha, shared_identity, write
from stage2.stations import route_arrays


def test_reroute_from_shared_and_portable_instance(tmp_path):
    shared = tmp_path / "cities/chicago/shared"
    shared.mkdir(parents=True)
    with TemporaryDirectory(dir=tmp_path) as scratch:
        original, _ = example(Path(scratch) / "routing-fixture")

    ids = original.nodes["location_id"].to_pylist()
    locations = original.nodes.select(["location_id", "type"]).to_pandas()
    locations = locations.rename(columns={"type": "location_type"}).set_index(
        "location_id"
    )
    locations.to_parquet(shared / "locations.parquet")
    # Each location sits at its own road vertex. As in the city adapter fixture,
    # zero-length terminal paths connect it to that vertex in both directions.
    endpoints = pd.DataFrame(
        {
            "location_id": ids,
            "requires_terminal_paths": True,
            "connector_length_m": 0.0,
            "directed_projection_offsets": "[]",
        }
    )
    access = pd.DataFrame(
        {
            "location_id": ids,
            "connector_length_m": 0.0,
            "connector_time_s_per_inverse_kph": 0.0,
        }
    )
    for direction in ("inbound", "outbound"):
        endpoints[f"terminal_{direction}_gateway"] = ids
        endpoints[f"terminal_{direction}_path_json"] = "[]"
        endpoints[f"terminal_{direction}_length_m"] = 0.0
        access[f"{direction}_terminal_length_m"] = 0.0
        for day in ("weekday", "weekend"):
            access[f"{direction}_terminal_reference_{day}_time_s"] = 0.0
    endpoints.to_parquet(shared / "endpoints.parquet", index=False)
    access.to_parquet(shared / "access.parquet", index=False)

    # A directed ring with two parallel roads on every leg. The shorter road
    # takes longer, so the two path policies have different exact answers.
    roads = []
    for u, v in zip(ids, ids[1:] + ids[:1]):
        for key, metres, seconds in [("short", 3.0, 7.0), ("fast", 11.0, 1.0)]:
            roads.append(
                {
                    "edge_u": u,
                    "edge_v": v,
                    "edge_key": key,
                    "length_m": metres,
                    "in_public_graph": True,
                    "operating_mode": "U",
                    "reference_speed_weekday_kph": 3.6 * metres / seconds,
                    "reference_travel_time_weekday_s": seconds,
                    "reference_speed_weekend_kph": 1.8 * metres / seconds,
                    "reference_travel_time_weekend_s": seconds * 2,
                }
            )
    pd.DataFrame(roads).to_parquet(shared / "speeds.parquet", index=False)
    write(shared / "rook.json", {"neighbors": {}})
    write(shared / "boundary.geojson", {"type": "FeatureCollection", "features": []})
    source_cle, calibration = "a" * 64, "b" * 64
    write(
        shared / "shared.json",
        {
            "schema": "evrptwd_shared_v1",
            "city": "chicago",
            "source": {"cle": source_cle},
            "calibration": calibration,
            "vintage": "c" * 64,
            "files": {p.name: sha(p) for p in shared.iterdir()},
        },
    )
    binding = {
        "city": "chicago",
        "shared_sha256": shared_identity(shared),
        "cle_manifest_sha256": source_cle,
        "calibration_sha256": calibration,
    }
    positions = np.arange(len(ids))
    legs = (positions[None, :] - positions[:, None]) % len(ids)
    expected = {
        "shortest_distance_m": 3.0 * legs,
        "shortest_distance_time_s": 7.0 * legs,
        "fastest_time_distance_m": 11.0 * legs,
        "fastest_time_s": 1.0 * legs,
    }
    info = {k: original.info[k] for k in HF_INFO_FIELDS}
    info.update(
        source_shared_sha256=binding["shared_sha256"],
        matrix_sha256_json=json.dumps(hashes(expected), sort_keys=True),
    )
    path = tmp_path / "instances/chicago/train/Cus2/routing-fixture.parquet"
    write_instance(
        path, Problem(info, original.nodes, expected), binding, schema=HF_FORMAT
    )

    header, task = read_portable(path)
    assert header["payload"]["cle"]["shared_sha256"] == shared_identity(shared)
    node_ids = task.nodes["location_id"].to_pylist()
    city = CityDay(shared, task.info["day_type"], ids=node_ids)
    matrices, _ = route_arrays(
        city.router, city.bound, node_ids, json.loads(task.info["vehicle_json"])
    )
    assert set(matrices) == set(MATRICES["evrptw"])
    for name, matrix in matrices.items():
        assert matrix.dtype == task.matrices[name].dtype
        assert matrix.shape == task.matrices[name].shape
        assert matrix.tobytes() == task.matrices[name].tobytes()
