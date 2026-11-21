"""Exercise first-draw and replacement routing through a miniature city release."""

import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from test_solver_parquet import example

import export_output
import quota_release as qr
import stage2.batch
from instance_file import write_instance
from solver_parquet import from_restored
from stage2.common import digest, read, sha, shared_identity, write


def test_certified_release_and_capacity_draw(tmp_path, monkeypatch):
    city = "chicago"
    work, output = tmp_path / "work", tmp_path / "output"
    report = output / "reports" / city
    ev = report / "provenance"
    for f in qr.PROVENANCE:
        write(ev / f, {"cities": [city]})
    declared = {
        "units/" + f: sha(ev / f)
        for f in ("metric_scales.csv", "quality_thresholds.json")
    }
    write(ev / "calibration-ready.json", {"files": declared})
    cal, vintage = digest(declared), sha(ev / "inventory.json")
    cle = output / "CLE" / city
    write(cle / "roads.json", [1])
    manifest = cle / "cle_manifest.json"
    write(
        manifest,
        {
            "city_slug": city,
            "schema": "evrptw_building_first_cle_v1",
            "files": {"roads.json": {"sha256": sha(cle / "roads.json")}},
            "sources": {"inventory": {"sha256": vintage}},
        },
    )
    write(
        cle / "stage1_validation.json",
        {"passed": True, "cle_manifest_sha256": sha(manifest)},
    )
    authority = {
        "city": city,
        "schema": "evrptwd_local_city_v1",
        "calibration_cohort": [city],
        "validation_sha256": sha(cle / "stage1_validation.json"),
        "inputs_sha256": vintage,
        "calibration_sha256": cal,
        "cle_manifest_sha256": sha(manifest),
        "engine_sha256": "engine",
    }
    write(cle / "public_authority.json", authority)
    shared = cle / "shared"
    write(
        shared / "shared.json",
        {
            "city": city,
            "calibration": cal,
            "vintage": vintage,
            "files": {},
            "source": {
                "cle": sha(manifest),
                "cohort": sha(cle / "public_authority.json"),
                "validation": authority["validation_sha256"],
            },
        },
    )
    binding = qr.city_inventory(cle, shared, city)[0]
    monkeypatch.setattr(qr, "CITIES", [city])
    monkeypatch.setattr(qr, "QUOTA", {"train": {2: 2}})
    monkeypatch.setattr(qr, "BUDGET", {2: 1})
    monkeypatch.setattr(
        qr, "city_contract", lambda c, p: {"resource_sha256": p["resource_sha256"]}
    )
    tasks = [
        {
            "city": city,
            "split": "train",
            "n": 2,
            "cs_budget": 1,
            "slot": i,
            "serial": i - 1,
            "seed": 1,
            "depot": "D",
            "date": "2026-11-03",
            "day_type": "weekday",
            "task_key": [city, "train", 2, i, "D", "2026-11-03", "weekday"],
        }
        for i in (1, 2)
    ]
    panel = {
        "city": city,
        "version": "r3",
        "code_sha256": "abc",
        "resource_sha256": qr.config_hashes(),
        "shared_sha256": shared_identity(shared),
        "calibration_sha256": cal,
        "input_vintage": vintage,
        "runtime": {},
        "tasks": tasks,
        "tasks_sha256": digest(tasks),
    }
    plan = work / "plans/chicago.json"
    write(plan, panel)
    write(report / "plan.json", panel)
    example_dirs = {}

    def make(task, plan_path, home, status):
        key = Path("chicago/train/Cus2") / f"instance{task['slot']:04d}"
        rec = home / "ledger" / key / "result.json"
        result = {
            "status": status,
            "task": task,
            "signature": digest([sha(plan_path), task]),
        }
        if status == "generation_failed":
            result["reason"] = "insufficient_reachable_frontier_capacity"
        else:
            name = digest([sha(plan_path), task["task_key"], task["seed"]])[:24]
            source = tmp_path / name
            example(source)
            write(
                source / "instance.json",
                dict(
                    read(source / "instance.json"),
                    **dict(
                        task,
                        instance_id=name,
                        plan_sha256=sha(plan_path),
                        shared_sha256=binding["shared_sha256"],
                        protocol_status=status,
                    ),
                ),
            )
            a = read(source / "customer_activation.json")
            write(
                source / "customer_activation.json",
                dict(
                    a,
                    K_seed=1,
                    K_geo=1,
                    p=0.03,
                    R=2,
                    M_target=4,
                    footprint={"T": 1, "M": 4},
                ),
            )
            if task["slot"] == 2:
                # A different directed matrix makes this a distinct solver task.
                import numpy as np

                from stage2.exchange import array_hashes

                with np.load(source / "costs.npz") as f:
                    arr = {k: 2 * f[k] for k in f.files}
                np.savez(source / "costs.npz", **arr)
                write(
                    source / "instance.json",
                    dict(
                        read(source / "instance.json"),
                        matrix_hashes=array_hashes(arr, 4),
                    ),
                )
            ar = home / "instances" / key.with_suffix(".evrp.zip")
            ar.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(ar, "w") as z:
                for f in source.iterdir():
                    if f.suffix != ".npz":
                        z.write(f, f.name)
            result.update(
                sha256=sha(ar), instance_id=name, file=str(ar.relative_to(home))
            )
            example_dirs[ar] = source
        write(rec, result)
        return result

    def publish(ar, dest):
        task = from_restored(example_dirs[ar])
        path = (
            dest
            / "instances/chicago/train/Cus2"
            / (task.info["instance_id"] + ".parquet")
        )
        write_instance(path, task, binding)
        return path

    rows = []
    for i, t in enumerate(tasks):
        result = make(t, plan, work, "generation_failed" if i else "protocol_certified")
        path = publish(work / result["file"], output) if i == 0 else None
        rows.append(
            {
                "city": city,
                "split": "train",
                "n": 2,
                "slot": t["slot"],
                "seed": 1,
                "cs_budget": 1,
                "status": result["status"],
                "instance_id": result.get("instance_id"),
                "path": path.relative_to(output).as_posix() if path else None,
                "sha256": sha(path) if path else None,
            }
        )
    pd.DataFrame(rows).to_parquet(report / "slots.parquet", index=False)
    monkeypatch.setattr(export_output, "publish_instance", publish)
    calls = []

    def run(plan, shared, catalog, job, *args):
        if (job / "ledger/chicago/train/Cus2/instance0002/result.json").exists():
            return
        t = read(plan)["tasks"][0]
        calls.append(t)
        make(
            t,
            plan,
            job,
            "generation_failed" if len(calls) == 1 else "protocol_certified",
        )

    monkeypatch.setattr(stage2.batch, "run", run)
    args = SimpleNamespace(
        city=city,
        output=output,
        work=work,
        choices=None,
        split="train",
        n=2,
        slot=2,
        attempts=2,
    )
    with pytest.raises(ValueError, match="Provide a certified choice"):
        qr.seal(args)
    qr.draw(args)
    assert len({t["seed"] for t in calls}) == 2 and all(
        t["task_key"] == tasks[1]["task_key"] for t in calls
    )
    qr.seal(args)
    qr.seal(args)
    dest = tmp_path / "release"
    merge = SimpleNamespace(shards=[output], out=dest, check_only=True)
    qr.merge(merge)
    merge.check_only = False
    qr.merge(merge)
    df = pd.read_parquet(dest / "index.parquet")
    assert len(df) == 2 and all(not ("slot" in c or "retry" in c) for c in df.columns)
    assert (
        read(dest / "release.json")["original_capacity_rejections"]["chicago"][0][
            "count"
        ]
        == 1
    )
    assert (dest / "config/vehicle.json").is_file() and not (
        dest / "slots.parquet"
    ).exists()
    for path in df.path:
        assert os.stat(output / path).st_ino == os.stat(dest / path).st_ino
    # A changed transfer must not leave a seemingly complete release.
    (output / df.path.iloc[0]).write_bytes(b"broken")
    merge.out = tmp_path / "bad-release"
    with pytest.raises(ValueError, match="Checksum mismatch"):
        qr.merge(merge)
    assert not merge.out.exists()
