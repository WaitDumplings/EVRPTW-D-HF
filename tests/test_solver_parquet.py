import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from export_instance import write_dataset
from solver_parquet import PROBLEMS, from_restored, load_problem
from stage2.common import read, sha, write
from stage2.exchange import FIELDS, KEYS, array_hashes, node_text


def example(folder):
    folder.mkdir()
    ids = ["D", "Z", "A", "S"]
    types = ["depot", "customer", "customer", "charging"]
    nodes = [
        dict(
            zip(
                FIELDS,
                (
                    i,
                    loc,
                    types[i],
                    -87.1,
                    42.0,
                    (0, 1.125, 0.3, 0)[i],
                    100.0,
                    300.0,
                    i * 0.2,
                    11.0 if i == 3 else 0.0,
                ),
            )
        )
        for i, loc in enumerate(ids)
    ]
    (folder / "nodes.txt").write_bytes(node_text(nodes))
    orders = [
        dict(
            location_id=r["LOCATION_ID"],
            **{
                k.lower(): r[k] for k in ("DEMAND_CM3", "READY_S", "DUE_S", "SERVICE_S")
            },
        )
        for r in nodes[1:3]
    ]
    write(folder / "orders.json", orders)
    write(folder / "customer_activation.json", {"customer_ids": ids[1:3]})
    write(folder / "station_selection.json", {"selected": ids[3:]})
    write(
        folder / "vehicle.json",
        read(Path(__file__).resolve().parents[1] / "config/vehicle.json"),
    )
    base = np.arange(16, dtype=float).reshape(4, 4) + 0.25
    np.fill_diagonal(base, 0.0)
    arrays = {k: base * (i + 1) for i, k in enumerate(KEYS)}
    np.savez(folder / "costs.npz", **arrays)
    write(
        folder / "instance.json",
        {
            "instance_id": folder.name,
            "city": "chicago",
            "split": "train",
            "method_version": "r3",
            "seed": 1,
            "task_key": [1],
            "day_type": "weekday",
            "date": "2026-11-03",
            "primary_objective": "total_distance",
            "execution_path_policy": "shortest_distance",
            "depot": "D",
            "customer_count": 2,
            "external_charging_station_count": 1,
            "node_count": 4,
            "protocol_status": "source_status",
            "matrix_hashes": array_hashes(arrays, 4),
        },
    )
    return from_restored(folder), arrays


def test_direct_and_published_views_match_asymmetric_source(tmp_path):
    src, arrays = example(tmp_path / "example")
    for name, count, keys in zip(PROBLEMS, (3, 3, 4), (KEYS[:1], KEYS[:2], KEYS)):
        root = tmp_path / name
        write_dataset([(src.info, lambda: src)], root, name)
        v = load_problem(root, "example", name)
        assert v.info["instance_id"] == "example" and v.info["node_count"] == count
        assert v.nodes["location_id"].to_pylist() == ["D", "Z", "A", "S"][:count]
        assert v.nodes["demand_cm3"].to_pylist() == [0.0, 1.125, 0.3, 0.0][:count]
        assert ("ready_s" in v.nodes.column_names) == (name != "cvrp")
        assert ("charge_kw" in v.nodes.column_names) == (name == "evrptw")
        vehicle = json.loads(v.info["vehicle_json"])
        assert ("battery_capacity_kwh" in vehicle) == (name == "evrptw")
        assert ("operating_window_s" in vehicle) == (name != "cvrp")
        assert set(v.matrices) == set(keys)
        for k in keys:
            assert v.matrices[k].tobytes() == arrays[k][:count, :count].tobytes()
    for name in PROBLEMS:
        derived = load_problem(tmp_path / "evrptw", "example", name)
        direct = load_problem(tmp_path / name, "example", name)
        assert derived.info == direct.info and derived.nodes.equals(direct.nodes)
        for k in direct.matrices:
            assert derived.matrices[k].tobytes() == direct.matrices[k].tobytes()
    with pytest.raises(ValueError, match="Cannot construct"):
        load_problem(tmp_path / "cvrp", "example", "evrptw")


def test_shards_and_rejected_exports(tmp_path):
    a, _ = example(tmp_path / "a")
    b, _ = example(tmp_path / "b")
    records = [(b.info, lambda: b), (a.info, lambda: a)]
    root = tmp_path / "release"
    write_dataset(records, root, "evrptw", shard_bytes=1)
    for name in ("instances", "nodes", "matrix_rows"):
        assert len(list((root / name).rglob("*.parquet"))) == 2
    assert load_problem(root, "b", "cvrp").info["instance_id"] == "b"
    with pytest.raises(FileExistsError):
        write_dataset(records, root, "vrptw")
    bad = tmp_path / "bad"
    with pytest.raises(ValueError, match="Duplicate"):
        write_dataset(records * 2, bad, "cvrp")
    with pytest.raises(ValueError, match="identity changed"):
        write_dataset([(a.info, lambda: a), (b.info, lambda: a)], bad, "cvrp")
    assert not bad.exists()


def test_read_detects_changed_values(tmp_path):
    v, _ = example(tmp_path / "a")
    root = tmp_path / "dataset"
    write_dataset([(v.info, lambda: v)], root, "evrptw")
    file = next((root / "matrix_rows").rglob("*.parquet"))
    table = pq.read_table(file)
    idx = table.schema.get_field_index(KEYS[0])
    values = table[KEYS[0]].to_pylist()
    values[0][1] += 1
    pq.write_table(
        table.set_column(idx, KEYS[0], pa.array(values, type=table.field(idx).type)),
        file,
    )
    with pytest.raises(ValueError, match="matrix hash"):
        load_problem(root, "a", "cvrp")
    file = next((root / "nodes").rglob("*.parquet"))
    table = pq.read_table(file)
    field = table.schema.get_field_index("demand_cm3")
    pq.write_table(
        table.set_column(field, "demand_cm3", pa.array([0.0, 1.0, 1.0, 0.0])), file
    )
    with pytest.raises(ValueError, match="Parquet node"):
        load_problem(root, "a", "cvrp")


def test_portable_solver_file_relocation_reencoding_and_raw_output(tmp_path):
    from export_instance import source_info, source_problem
    from export_output import verify_instance
    from instance_file import file_header, read_instance, unpack, write_instance

    task, matrices = example(tmp_path / "source")
    task.info["source_shared_sha256"] = "a" * 64
    meta = tmp_path / "source/instance.json"
    write(meta, {**read(meta), "shared_sha256": "a" * 64})
    binding = {
        "city": "chicago",
        "shared_sha256": "a" * 64,
        "cle_manifest_sha256": "b" * 64,
        "calibration_sha256": "c" * 64,
    }
    path = tmp_path / "isolated" / "renamed.parquet"
    write_instance(path, task, binding)
    header = file_header(path)
    table = pq.read_table(path).take(pa.array([3, 1, 0, 2]))
    pq.write_table(table, path, compression="snappy")
    before = path.read_bytes()
    write_instance(path, task, binding)
    assert path.read_bytes() == before and file_header(path) == header
    assert verify_instance(path, tmp_path / "source")["customers"] == 2
    assert source_info(path)["instance_id"] == task.info["instance_id"]
    assert source_problem(path, tmp_path / "no-work", source_info(path)).nodes.equals(
        task.nodes
    )
    for mode in PROBLEMS:
        solver = read_instance(path, mode)
        n = solver.info["node_count"]
        assert solver.nodes.equals(task.view(mode).nodes)
        target = tmp_path / mode
        unpack(path, target, mode)
        raw = read(target / "instance.json")
        assert (
            raw["cle"] == binding
            and raw["source_content_sha256"] == header["content_sha256"]
        )

        assert all(
            sha(target / name) == value for name, value in raw["files_sha256"].items()
        )
        for key, array in solver.matrices.items():
            descriptor = raw["matrices"][key]
            assert array.tobytes() == matrices[key][:n, :n].tobytes()
            assert (target / descriptor["file"]).read_bytes() == array.astype(
                "<f8"
            ).tobytes()
    with pytest.raises(ValueError, match="another city"):
        write_instance(path, task, {**binding, "city": "dallas"})
    with pytest.raises(ValueError, match="already published"):
        write_instance(path, task, {**binding, "cle_manifest_sha256": "f" * 64})
    assert path.read_bytes() == before

    # Both files are internally valid; only the EV fastest-time matrix differs.
    matrices["fastest_time_s"][0, 1] += 1
    np.savez(meta.parent / "costs.npz", **matrices)
    write(meta, {**read(meta), "matrix_hashes": array_hashes(matrices, 4)})
    with pytest.raises(ValueError, match="Restored and portable"):
        verify_instance(path, meta.parent)
