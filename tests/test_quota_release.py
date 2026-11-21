"""Exercise first-draw and replacement routing through a miniature city release."""

import os
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from test_solver_parquet import example

import export_output
import quota_release as qr
import stage2.batch
from instance_file import HF_FORMAT, file_header, read_portable, write_instance
from solver_parquet import HF_INFO_FIELDS, PROBLEMS, from_restored
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
    success_at = 10

    def run(plan, shared, catalog, job, *args):
        if (job / "ledger/chicago/train/Cus2/instance0002/result.json").exists():
            return
        t = read(plan)["tasks"][0]
        calls.append(t)
        make(
            t,
            plan,
            job,
            "generation_failed" if len(calls) < success_at else "protocol_certified",
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
        attempts=1,
    )
    with pytest.raises(ValueError, match="Provide a certified choice"):
        qr.seal(args)
    # A finite manual run may leave a failed draw. City fill continues at the next
    # distinct seed, without rebuilding any certified slot or replaying the failure.
    with pytest.raises(ValueError, match="No accepted draw"):
        qr.draw(args)
    qr.seal(args, fill_capacity=True)
    assert len({t["seed"] for t in calls}) == success_at and all(
        t["task_key"] == tasks[1]["task_key"] for t in calls
    )
    qr.seal(args)
    directory = work / "quota-draws/chicago"
    saved = directory / "train/Cus2/instance0002/choice.json"
    # An external choice can fill the same slot even if only its ZIP was delivered.
    write(directory / "choices.json", [read(saved)])
    saved.unlink()
    (output / "_release/chicago").rename(tmp_path / "first-seal")
    supplemental_id = read(read(directory / "choices.json")[0]["receipt"])[
        "instance_id"
    ]
    (output / "instances/chicago/train/Cus2" / (supplemental_id + ".parquet")).unlink()
    qr.seal(args, fill_capacity=True)
    (directory / "choices.json").unlink()
    # A previously sealed city (including a legacy B shard) needs no private map.
    qr.seal(args, fill_capacity=True)
    assert len(calls) == success_at
    dest = tmp_path / "release"
    merge = SimpleNamespace(shards=[output], out=dest, check_only=True)
    qr.merge(merge)
    merge.check_only = False
    qr.merge(merge)
    df = pd.read_parquet(dest / "index.parquet")
    assert set(df.instance_id) == {rows[0]["instance_id"], supplemental_id}
    assert len(df) == 2 and all(not ("slot" in c or "retry" in c) for c in df.columns)
    assert "seed" not in df and "original_capacity_rejections" not in read(
        dest / "release.json"
    )
    assert (dest / "config/vehicle.json").is_file() and not (
        dest / "config/release_panel.json"
    ).exists()
    assert not (dest / "_release").exists() and not (dest / "slots.parquet").exists()
    assert "rejection" not in (dest / "README.md").read_text().lower()
    private, old = qr.read_city_index(output / "_release" / city, city)
    assert (
        private["original_capacity_rejections"][0]["count"] == 1
        and old.seed.nunique() == 2
    )
    shard = tmp_path / "public-shard"
    projection = SimpleNamespace(city=city, output=output, out=shard)
    qr.project(projection)
    qr.project(projection)  # same verified files and index on resume
    shmeta, shrows = qr.read_city_index(shard / "_release" / city, city)
    assert shmeta["sealed_index_sha256"] == private["index_sha256"]
    pd.testing.assert_frame_equal(df, shrows)
    accepted = tmp_path / "accepted-manifests"
    for name in ("city.json", "index.parquet"):
        dst = accepted / "_release" / city / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(output / "_release" / city / name, dst)
    assembled = tmp_path / "from-public"
    with pytest.raises(ValueError, match="Supply --accepted-shards"):
        qr.merge(SimpleNamespace(shards=[shard], check_only=False, out=assembled))
    qr.merge(
        SimpleNamespace(
            shards=[shard], accepted_shards=[accepted], check_only=False, out=assembled
        )
    )
    # City and method artifacts in the public root contain no Stage 2 draw records.
    private_fields = (
        "original_capacity_rejections",
        "root_seed",
        "task_key_json",
        "source_protocol_status",
        "changed_depot_or_date",
        "quota-draws",
        "supplements/",
        "plan_sha256",
        "release_panel.json",
    )
    for public_file in assembled.rglob("*"):
        if public_file.suffix in {".json", ".md", ".csv"}:
            public_text = public_file.read_text()
            assert not any(k in public_text for k in private_fields), public_file
    with pytest.raises(ValueError, match="Invalid instance path"):
        qr.validate_rows(
            city,
            shrows.assign(path=["../unexpected", shrows.path.iloc[1]]),
            qr.HF_COLUMNS,
        )
    pd.testing.assert_frame_equal(df, pd.read_parquet(assembled / "index.parquet"))
    for row in df.itertuples():
        path = row.path
        public = file_header(dest / path)
        assert public["schema"] == HF_FORMAT
        assert set(public["payload"]["instance"]) == set(HF_INFO_FIELDS)
        assert row.sha256 == sha(dest / path) == sha(shard / path)
        assert row.content_sha256 == public["content_sha256"]
        assert os.stat(shard / path).st_ino == os.stat(assembled / path).st_ino
        for view in PROBLEMS:
            _, before = read_portable(output / path, view)
            _, after = read_portable(dest / path, view)
            assert before.nodes.equals(after.nodes)
            assert after.info == {k: before.info[k] for k in HF_INFO_FIELDS}
            assert set(after.tables()["instances"].column_names) == set(HF_INFO_FIELDS)
            for key in before.matrices:
                assert before.matrices[key].tobytes() == after.matrices[key].tobytes()
    # Linking public shards back to the accepted panel also checks every stable field.
    changed = shrows.copy()
    changed.loc[0, "R"] += 1
    folder = shard / "_release" / city
    changed.to_parquet(folder / "index.parquet", index=False)
    write(
        folder / "city.json", dict(shmeta, index_sha256=sha(folder / "index.parquet"))
    )
    with pytest.raises(ValueError, match="differs from accepted panel"):
        qr.merge(
            SimpleNamespace(shards=[shard], accepted_shards=[output], check_only=True)
        )
    # A changed transfer must not leave a seemingly complete release.
    (output / df.path.iloc[0]).write_bytes(b"broken")
    merge.out = tmp_path / "bad-release"
    with pytest.raises(ValueError, match="Checksum mismatch"):
        qr.merge(merge)
    assert not merge.out.exists()


@pytest.mark.parametrize(
    "last_status", ["protocol_certified", "generation_failed", "execution_error"]
)
def test_fixed_depot_rejection_tries_next_depot_on_same_day(
    tmp_path, monkeypatch, last_status
):
    city = "chicago"
    loc = pd.DataFrame(
        {"location_type": ["depot", "depot"], "bg_geoid": ["B1", "B2"]},
        index=["D1", "D2"],
    )
    shared = tmp_path / "shared" / city
    shared.mkdir(parents=True)
    loc.to_parquet(shared / "locations.parquet")
    task = {
        "city": city,
        "split": "train",
        "n": 100,
        "cs_budget": 10,
        "slot": 1,
        "serial": 0,
        "seed": 7,
        "depot": "D1",
        "depot_bg": "B1",
        "date": "2026-11-03",
        "day_type": "weekday",
        "task_key": [city, "train", 100, 1, "D1", "2026-11-03", "weekday"],
    }
    panel = {"root_seed": 7}
    actual = []

    def run(plan, shared, catalog, job, *args):
        t = read(plan)["tasks"][0]
        actual.append(t)
        r = {"status": last_status, "signature": digest([sha(plan), t]), "task": t}
        if last_status == "protocol_certified":
            zip_path = job / "example.zip"
            zip_path.write_bytes(b"fixture")
            r.update(file="example.zip", sha256=sha(zip_path))
        if last_status == "generation_failed":
            r["reason"] = "empty_ratio_support"
        write(job / "ledger/chicago/train/Cus100/instance0001/result.json", r)

    monkeypatch.setattr(stage2.batch, "run", run)
    monkeypatch.setattr(export_output, "publish_instance", lambda *a: None)
    args = (
        tmp_path,
        tmp_path / "out",
        panel,
        "original",
        task,
        "insufficient_depot_capacity",
    )
    if last_status == "protocol_certified":
        choice = qr.draw_capacity_slot(*args)
        assert choice["capacity_fallback_depot"] == "D2"
    else:
        with pytest.raises(
            ValueError,
            match="No depot supports"
            if last_status == "generation_failed"
            else "execution_error",
        ):
            qr.draw_capacity_slot(*args)
    assert len(actual) == 1
    t = actual[0]
    assert (
        t["depot"],
        t["depot_bg"],
        t["task_key"][4],
        t["date"],
        t["split"],
        t["n"],
    ) == ("D2", "B2", "D2", task["date"], "train", 100)
