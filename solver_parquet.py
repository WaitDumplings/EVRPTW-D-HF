"""Lossless solver views of one delivery task, with a common Parquet representation."""

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.dataset as ds

MATRICES = {
    "cvrp": ("shortest_distance_m",),
    "vrptw": ("shortest_distance_m", "shortest_distance_time_s"),
    "evrptw": (
        "shortest_distance_m",
        "shortest_distance_time_s",
        "fastest_time_distance_m",
        "fastest_time_s",
    ),
}
PROBLEMS = tuple(MATRICES)
BASIC_FIELDS = ("node_idx", "location_id", "type", "lon", "lat", "demand_cm3")
TIME_FIELDS = (*BASIC_FIELDS, "ready_s", "due_s", "service_s")
NODE_FIELDS = {
    "cvrp": BASIC_FIELDS,
    "vrptw": TIME_FIELDS,
    "evrptw": (*TIME_FIELDS, "charge_kw"),
}
INFO_SCHEMA = pa.schema(
    [
        ("instance_id", pa.string()),
        ("problem_type", pa.string()),
        ("city", pa.string()),
        ("split", pa.string()),
        ("service_date", pa.string()),
        ("day_type", pa.string()),
        ("seed", pa.int64()),
        ("depot_id", pa.string()),
        ("customer_count", pa.int32()),
        ("station_count", pa.int32()),
        ("node_count", pa.int32()),
        ("capacity_cm3", pa.float64()),
        ("fleet_size", pa.int32()),
        ("objective", pa.string()),
        ("path_policy", pa.string()),
        ("method_version", pa.string()),
        ("task_key_json", pa.string()),
        ("source_protocol_status", pa.string()),
        ("source_shared_sha256", pa.string()),
        ("vehicle_json", pa.string()),
        ("matrix_sha256_json", pa.string()),
        ("nodes_sha256", pa.string()),
    ]
)
NODE_SCHEMA = pa.schema(
    [
        ("node_idx", pa.int32()),
        ("location_id", pa.string()),
        ("type", pa.string()),
        *((k, pa.float64()) for k in NODE_FIELDS["evrptw"][3:]),
    ]
)


def json_sha256(value):
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def hashes(arrays):
    return {
        k: hashlib.sha256(np.ascontiguousarray(v, dtype="<f8")).hexdigest()
        for k, v in arrays.items()
    }


def solver_vehicle(vehicle, problem):
    if problem == "evrptw":
        return vehicle
    result = {k: vehicle[k] for k in ("cargo_capacity_cm3", "fleet_size")}
    rules = vehicle["route_rules"]
    result["route_rules"] = {
        k: rules[k]
        for k in (
            "start_at_depot",
            "return_to_same_depot",
            "single_trip_per_vehicle",
            "serve_each_customer_once",
            "split_delivery_allowed",
        )
    }
    if problem == "vrptw":
        result.update({k: vehicle[k] for k in ("operating_window_s", "time_axis")})
        result["route_rules"]["time_window_semantics"] = rules["time_window_semantics"]
    return result


@dataclass
class Problem:
    info: dict
    nodes: pa.Table
    matrices: dict

    def view(self, problem):
        source = self.info["problem_type"]
        if source == problem:
            return self
        if PROBLEMS.index(problem) > PROBLEMS.index(source):
            raise ValueError(f"{source} does not contain all fields for {problem}")
        n = (
            self.info["node_count"]
            if problem == "evrptw"
            else 1 + self.info["customer_count"]
        )
        nodes = self.nodes.slice(0, n).select(["instance_id", *NODE_FIELDS[problem]])
        arrays = {k: self.matrices[k][:n, :n] for k in MATRICES[problem]}
        vehicle = solver_vehicle(json.loads(self.info["vehicle_json"]), problem)
        info = dict(
            self.info,
            problem_type=problem,
            node_count=n,
            station_count=n - 1 - self.info["customer_count"],
            vehicle_json=json.dumps(vehicle, sort_keys=True),
            matrix_sha256_json=json.dumps(hashes(arrays), sort_keys=True),
            nodes_sha256=json_sha256(nodes.to_pylist()),
        )
        return Problem(info, nodes, arrays)

    def tables(self):
        n = self.nodes.num_rows
        offsets = pa.array(np.arange(n + 1, dtype="int32") * n)
        columns = {
            "instance_id": self.nodes["instance_id"],
            "from_idx": self.nodes["node_idx"],
        }
        for key, matrix in self.matrices.items():
            values = pa.array(
                np.ascontiguousarray(matrix).reshape(-1), type=pa.float64()
            )
            columns[key] = pa.ListArray.from_arrays(offsets, values)
        return {
            "instances": pa.Table.from_pylist([self.info], schema=INFO_SCHEMA),
            "nodes": self.nodes,
            "matrix_rows": pa.table(columns),
        }


def source_nodes_and_info(folder):
    """Read the common solver inputs from a restored directory or compact ZIP root."""
    from stage2.common import public_split
    from stage2.exchange import FIELDS

    def read(path):
        return json.loads(path.read_text(encoding="utf-8"))

    inst = read(folder / "instance.json")
    if (
        inst["execution_path_policy"] != "shortest_distance"
        or inst["primary_objective"] != "total_distance"
    ):
        raise ValueError("Unsupported route policy or objective")
    vehicle = read(folder / "vehicle.json")
    orders = read(folder / "orders.json")
    customers = read(folder / "customer_activation.json")["customer_ids"]
    stations = read(folder / "station_selection.json")["selected"]
    with (folder / "nodes.txt").open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    ids = [inst.get("depot") or inst["depot_id"], *customers, *stations]
    types = ["depot", *(["customer"] * len(customers)), *(["charging"] * len(stations))]
    if (
        len(rows) != inst["node_count"]
        or len(set(ids)) != len(ids)
        or len(customers) != inst["customer_count"]
        or len(stations) != inst["external_charging_station_count"]
        or [o["location_id"] for o in orders] != customers
        or [int(r["NODE_IDX"]) for r in rows] != list(range(len(ids)))
        or [r["LOCATION_ID"] for r in rows] != ids
        or [r["TYPE"] for r in rows] != types
    ):
        raise ValueError("Instance node order, IDs or counts disagree")
    for row, order in zip(rows[1:], orders):
        if any(
            float(row[k]) != order[k.lower()]
            for k in ("DEMAND_CM3", "READY_S", "DUE_S", "SERVICE_S")
        ):
            raise ValueError("Node attributes disagree with the realized order")
    cols = [
        pa.array([r[k] for r in rows]).cast(field.type)
        for k, field in zip(FIELDS, NODE_SCHEMA)
    ]
    nodes = pa.Table.from_arrays(cols, schema=NODE_SCHEMA)
    nodes = nodes.add_column(
        0, "instance_id", pa.array([inst["instance_id"]] * len(rows))
    )
    info = {
        "instance_id": inst["instance_id"],
        "problem_type": "evrptw",
        "city": inst["city"],
        "split": public_split(inst["city"], inst["split"]),
        "service_date": inst["date"],
        "day_type": inst["day_type"],
        "seed": inst["seed"],
        "depot_id": ids[0],
        "customer_count": len(customers),
        "station_count": len(stations),
        "node_count": len(rows),
        "capacity_cm3": vehicle["cargo_capacity_cm3"],
        "fleet_size": vehicle["fleet_size"],
        "objective": inst["primary_objective"],
        "path_policy": inst["execution_path_policy"],
        "method_version": inst["method_version"],
        "task_key_json": json.dumps(inst["task_key"]),
        "source_protocol_status": inst["protocol_status"],
        "source_shared_sha256": inst.get("shared_sha256"),
        "vehicle_json": json.dumps(vehicle, sort_keys=True),
        "matrix_sha256_json": json.dumps(inst["matrix_hashes"], sort_keys=True),
        "nodes_sha256": json_sha256(nodes.to_pylist()),
    }
    return nodes, info


def from_restored(folder):
    from stage2.exchange import array_hashes

    folder = Path(folder)
    nodes, info = source_nodes_and_info(folder)
    with np.load(folder / "costs.npz", allow_pickle=False) as costs:
        arrays = {k: costs[k] for k in costs.files}
    if array_hashes(arrays, len(nodes)) != json.loads(info["matrix_sha256_json"]):
        raise ValueError("Source matrix hashes disagree")
    return Problem(info, nodes, arrays)


def partition(info):
    split, city = info["split"] or "example", info["city"]
    if not all(re.fullmatch(r"[A-Za-z0-9_-]+", label) for label in (split, city)):
        raise ValueError("Invalid split or city partition label")
    return Path(split) / city / f"Cus{int(info['customer_count'])}"


def load_problem(root, instance_id, problem="evrptw"):
    """Read one solver view of a local HF-format directory without the original CLE."""
    root = Path(root)
    match = ds.field("instance_id") == instance_id
    found = (
        ds.dataset(root / "instances", format="parquet")
        .to_table(filter=match)
        .to_pylist()
    )
    if len(found) != 1:
        raise ValueError(f"Expected one instance {instance_id!r}, found {len(found)}")
    info = found[0]
    if PROBLEMS.index(problem) > PROBLEMS.index(info["problem_type"]):
        raise ValueError(f"Cannot construct {problem} from {info['problem_type']}")
    folder = partition(info)
    nodes = (
        ds.dataset(root / "nodes" / folder, format="parquet")
        .to_table(filter=match)
        .sort_by("node_idx")
    )
    keys = MATRICES[problem]
    rows = (
        ds.dataset(root / "matrix_rows" / folder, format="parquet")
        .to_table(columns=["from_idx", *keys], filter=match)
        .sort_by("from_idx")
    )
    return decode_problem(info, nodes, rows, problem)


def validate_nodes(info, nodes):
    n = info["node_count"]
    if (
        nodes["node_idx"].to_pylist() != list(range(n))
        or nodes["type"].to_pylist()
        != ["depot"]
        + ["customer"] * info["customer_count"]
        + ["charging"] * info["station_count"]
        or set(nodes["instance_id"].to_pylist()) != {info["instance_id"]}
        or len(set(nodes["location_id"].to_pylist())) != n
        or nodes["location_id"][0].as_py() != info["depot_id"]
        or json_sha256(nodes.to_pylist()) != info["nodes_sha256"]
    ):
        raise ValueError("Invalid Parquet node indices, IDs or roles")


def decode_problem(info, nodes, rows, problem):
    n = info["node_count"]
    keys = MATRICES[problem]
    validate_nodes(info, nodes)
    if rows["from_idx"].to_pylist() != list(range(n)):
        raise ValueError("Invalid matrix origin indices")
    arrays = {}
    expected = json.loads(info["matrix_sha256_json"])
    for k in keys:
        col = rows[k].combine_chunks()
        if (
            col.type != pa.list_(pa.float64())
            or col.null_count
            or col.values.null_count
            or not np.all(np.diff(col.offsets.to_numpy()) == n)
        ):
            raise ValueError(f"Invalid road matrix row lengths for {k}")
        a = col.flatten().to_numpy().reshape(n, n)
        if not np.isfinite(a).all() or (a < 0).any() or a.diagonal().any():
            raise ValueError(f"Invalid road costs for {k}")
        if hashes({k: a})[k] != expected[k]:
            raise ValueError(f"Parquet matrix hash mismatch for {k}")
        arrays[k] = a
    return Problem(info, nodes, arrays).view(problem)
