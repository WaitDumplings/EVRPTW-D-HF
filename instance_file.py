"""Read, write and unpack a single solver-ready Parquet file without a city work directory."""

import csv
import hashlib
import json
import os
import tempfile
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from solver_parquet import MATRICES, NODE_FIELDS, decode_problem, json_sha256

FORMAT = "evrptwd_solver_instance_v1"
FOOTER_KEY = b"evrptwd.instance"
RULES = {
    "objective": "sum directed shortest_distance_m over all route legs, using unrounded float64 metres",
    "distance_unit": "m",
    "time_unit": "s from local midnight",
    "demand_unit": "cm3",
    "energy_unit": "kWh",
    "power_unit": "kW",
    "consumption_unit": "kWh/km",
    "matrix_index": "row node_idx is origin; list offset is destination node_idx (zero-based)",
    "matrix_domain": "finite nonnegative directed costs, zero diagonal",
    "travel_time": "shortest_distance_time_s follows the shortest_distance_m path",
    "window": "hard, inclusive service start; early arrival may wait; depot return by shift end",
    "charging": "start at initial SOC, reserve on arrival; linear full charging at effective station kW; depot is start/end only",
}


def file_header(path):
    return _header(pq.read_schema(path))


def _header(schema):
    meta = schema.metadata
    if not meta or FOOTER_KEY not in meta:
        raise ValueError("Missing portable EVRPTW-D instance footer")
    obj = json.loads(meta[FOOTER_KEY])
    if obj.get("schema") != FORMAT or json_sha256(obj["payload"]) != obj.get(
        "content_sha256"
    ):
        raise ValueError("Unsupported or damaged portable instance header")
    task = obj["payload"]["instance"]
    cle = obj["payload"]["cle"]
    if (task["city"], task["source_shared_sha256"]) != (
        cle["city"],
        cle["shared_sha256"],
    ):
        raise ValueError("Inconsistent city binding in portable instance")
    return obj


def read_instance(path, problem="evrptw"):
    return read_portable(path, problem)[1]


def read_portable(path, problem="evrptw"):
    """Read the footer and requested solver inputs from the same open Parquet."""
    names = ["instance_id", *NODE_FIELDS["evrptw"]]
    with pq.ParquetFile(path) as file:
        header = _header(file.schema_arrow)
        info = header["payload"]["instance"]
        if info["problem_type"] != "evrptw":
            raise ValueError("Portable source must contain the complete EV task")
        table = file.read(columns=[*names, *MATRICES[problem]])
    # Preserve logical order even after a physical Parquet rewrite.
    if table["node_idx"].to_pylist() != list(range(info["node_count"])):
        table = table.sort_by("node_idx")
    nodes = table.select(names)
    rows = table.select(["node_idx", *MATRICES[problem]]).rename_columns(
        ["from_idx", *MATRICES[problem]]
    )
    return header, decode_problem(info, nodes, rows, problem)


def write_instance(path, task, city_binding):
    if task.info["problem_type"] != "evrptw":
        raise ValueError(
            "Publish the complete EV task; select the solver view at read time"
        )
    if (task.info["city"], task.info["source_shared_sha256"]) != (
        city_binding["city"],
        city_binding["shared_sha256"],
    ):
        raise ValueError("Instance belongs to another city or shared CLE version")
    payload = {"instance": task.info, "cle": city_binding, "rules": RULES}
    header = {
        "schema": FORMAT,
        "payload": payload,
        "content_sha256": json_sha256(payload),
    }
    path.parent.mkdir(parents=True, exist_ok=True)

    def reuse():
        existing, _ = read_portable(path)
        if existing != header:
            raise ValueError(f"Different instance or CLE already published at {path}")

    if path.exists():
        reuse()
        return path
    rows = task.tables()["matrix_rows"]
    table = task.nodes
    for key in MATRICES["evrptw"]:
        table = table.append_column(key, rows[key])
    table = table.replace_schema_metadata(
        {FOOTER_KEY: json.dumps(header, sort_keys=True, allow_nan=False).encode()}
    )
    # An abrupt exit may leave this file behind. Its suffix cannot enter a Parquet file list.
    with tempfile.TemporaryDirectory(dir=path.parent, prefix=f".{path.stem}-") as tmp:
        pending = Path(tmp) / "instance.partial"
        pq.write_table(table, pending, compression="zstd")
        with pending.open("rb") as saved:
            os.fsync(saved.fileno())
        try:
            os.link(pending, path)
        except FileExistsError:
            reuse()
    return path


def unpack(path, out, problem):
    source, task = read_portable(path, problem)
    if out.exists():
        raise FileExistsError(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent, prefix=".unpack-") as tmp:
        stage = Path(tmp) / "solver"
        stage.mkdir()
        with (stage / "nodes.tsv").open("w", newline="") as stream:
            writer = csv.DictWriter(
                stream, task.nodes.column_names, delimiter="\t", lineterminator="\n"
            )
            writer.writeheader()
            writer.writerows(task.nodes.to_pylist())
        info = dict(task.info)
        (stage / "vehicle.json").write_text(info.pop("vehicle_json") + "\n")
        info["vehicle_file"] = "vehicle.json"
        info["cle"] = source["payload"]["cle"]
        info["source_content_sha256"] = source["content_sha256"]
        info["files_sha256"] = {
            name: hashlib.sha256((stage / name).read_bytes()).hexdigest()
            for name in ("nodes.tsv", "vehicle.json")
        }
        info["rules"] = {
            k: v
            for k, v in RULES.items()
            if k not in ("charging", "energy_unit", "consumption_unit", "power_unit")
            or problem == "evrptw"
        }
        if problem == "cvrp":
            for key in ("window", "travel_time", "time_unit"):
                info["rules"].pop(key)
        checksums = json.loads(info.pop("matrix_sha256_json"))
        info["matrices"] = {}
        for k, matrix in task.matrices.items():
            name = k + ".f64le"
            np.ascontiguousarray(matrix, dtype="<f8").tofile(stage / name)
            info["matrices"][k] = {
                "file": name,
                "shape": list(matrix.shape),
                "dtype": "<f8",
                "order": "C",
                "sha256": checksums[k],
            }
        (stage / "instance.json").write_text(
            json.dumps(info, sort_keys=True, indent=2, allow_nan=False) + "\n"
        )
        stage.rename(out)
