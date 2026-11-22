"""Exercise first-draw and replacement routing through a miniature city release."""

import os
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import pandas as pd
import pytest
import yaml
from shapely.geometry import LineString
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
    roads = cle / "private_sources/osm_ways.parquet"
    roads.parent.mkdir()
    gpd.GeoDataFrame(
        {
            "osmid": ["42"],
            "node_ids_json": ["[1, 2]"],
            "tags_json": ['{"highway":"service","name":"Oak 212-555-0123"}'],
        },
        geometry=[LineString([(-87.6, 41.8), (-87.61, 41.81)])],
        crs="EPSG:4326",
    ).to_parquet(roads, index=False)
    manifest = cle / "cle_manifest.json"
    write(
        manifest,
        {
            "city_slug": city,
            "schema": "evrptw_building_first_cle_v1",
            "files": {
                "roads.json": {"sha256": sha(cle / "roads.json")},
                "private_sources/osm_ways.parquet": {"sha256": sha(roads)},
            },
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
    shared.mkdir()
    for name in (
        "access.parquet",
        "boundary.geojson",
        "endpoints.parquet",
        "locations.parquet",
        "rook.json",
        "speeds.parquet",
    ):
        (shared / name).write_bytes(name.encode())
    write(
        shared / "shared.json",
        {
            "city": city,
            "calibration": cal,
            "vintage": vintage,
            "schema": "evrptwd_shared_v1",
            "files": {p.name: sha(p) for p in shared.iterdir()},
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
        qr,
        "city_contract",
        lambda c, p: {
            "version": p["version"],
            "code_sha256": p["code_sha256"],
            "calendar_start": "2026-11-02",
            "calendar_days": 28,
            "resource_sha256": p["resource_sha256"],
        },
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
    with pytest.raises(ValueError, match="Expected v2 public shards"):
        qr.merge(
            SimpleNamespace(shards=[output], accepted_shards=[output], check_only=True)
        )
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
    accepted = tmp_path / "accepted-manifests"
    for name in ("city.json", "index.parquet"):
        dst = accepted / "_release" / city / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(output / "_release" / city / name, dst)
    assembled = tmp_path / "from-public"
    with pytest.raises(ValueError, match="Supply --accepted-shards"):
        qr.merge(SimpleNamespace(shards=[shard], check_only=False, out=assembled))
    ref = tmp_path / "reference" / "index.parquet"
    ref.parent.mkdir()
    shrows.to_parquet(ref, compression="gzip", index=False)
    merge = SimpleNamespace(
        shards=[shard],
        accepted_shards=[accepted],
        check_only=True,
        reference_index=ref,
        out=assembled,
    )
    qr.merge(merge)
    assert not assembled.exists()
    merge.check_only = False
    shrows.iloc[::-1].to_parquet(ref, compression="gzip", index=False)
    with pytest.raises(ValueError, match="Reference index differs"):
        qr.merge(merge)
    assert not assembled.exists()
    shrows.to_parquet(ref, compression="gzip", index=False)
    write(ref.parent / "release.json", {"index_sha256": "0" * 64})
    with pytest.raises(
        ValueError, match="Reference index differs from its release manifest"
    ):
        qr.merge(merge)
    assert not assembled.exists()
    write(ref.parent / "release.json", {"index_sha256": sha(ref)})
    qr.merge(merge)
    (log,) = (assembled.parent / "delivery").glob(f"{assembled.name}-road-names-*.tsv")
    assert log.read_text().splitlines() == ["city\tname_rows_cleared", f"{city}\t1"]
    df = pd.read_parquet(assembled / "index.parquet")
    pd.testing.assert_frame_equal(df, shrows)
    assert set(df.instance_id) == {rows[0]["instance_id"], supplemental_id}
    assert len(df) == 2 and "seed" not in df
    assert not any("slot" in c or "retry" in c for c in df)
    release = read(assembled / "release.json")
    assert set(release) == {
        "layout",
        "instances",
        "instance_schema",
        "contract",
        "cities",
        "city_files",
        "index_sha256",
    }
    assert release["layout"] == "evrptwd_public_two_part_v1"
    assert set(release["contract"]) == {
        "version",
        "code_sha256",
        "stage1_engine_sha256",
        "calendar_start",
        "calendar_days",
    }
    assert release["index_sha256"] == sha(ref) == sha(assembled / "index.parquet")
    assert release["cities"][city] == binding
    city_paths = {f"shared/{p.name}" for p in shared.iterdir()} | {
        "roads/osm_ways.parquet"
    }
    assert set(release["city_files"][city]) == city_paths
    expected = (
        set(df.path)
        | {f"cities/{city}/{p}" for p in city_paths}
        | {
            "release.json",
            "index.parquet",
            "README.md",
            "LICENSE.md",
            "NOTICE.md",
        }
    )
    assert {
        p.relative_to(assembled).as_posix() for p in assembled.rglob("*") if p.is_file()
    } == expected
    for name, record in release["city_files"][city].items():
        asset = assembled / "cities" / city / name
        assert (asset.stat().st_size, sha(asset)) == (record["bytes"], record["sha256"])
    extra = assembled / "unexpected.txt"
    extra.write_text("extra")
    with pytest.raises(ValueError, match="Release file set differs"):
        qr.verify_release_paths(assembled, [city], df.path)
    extra.unlink()
    hidden = assembled / "README.md"
    hidden.rename(tmp_path / "saved-readme")
    with pytest.raises(ValueError, match="Release file set differs"):
        qr.verify_release_paths(assembled, [city], df.path)
    (tmp_path / "saved-readme").rename(hidden)
    card = (assembled / "README.md").read_text()
    assert card.startswith("---\n")
    header, _ = card.removeprefix("---\n").split("\n---\n", 1)
    metadata = yaml.safe_load(header)
    expected = {
        "configs": [
            {
                "config_name": "instances",
                "data_files": [{"split": "catalog", "path": "index.parquet"}],
            }
        ],
        "license": "other",
        "license_name": "evrptw-d-mixed",
        "license_link": "LICENSE.md",
        "pretty_name": "EVRPTW-D",
        "language": ["en"],
        "size_categories": ["10K<n<100K"],
        "tags": [
            "vehicle-routing",
            "evrptw",
            "vrptw",
            "cvrp",
            "combinatorial-optimization",
            "road-network",
        ],
    }
    assert {key: metadata[key] for key in expected} == expected
    for name in ("README.md", "LICENSE.md", "NOTICE.md"):
        content = (assembled / name).read_text()
        assert (qr.PROJECT / "dataset_card" / name).read_bytes() == (
            assembled / name
        ).read_bytes()
        for forbidden in (
            "config/",
            "parameters/",
            "docs/",
            "private_sources",
            "CLE/",
            "Stage 1",
            "Stage 2",
            "github.com/WaitDumplings",
        ):
            assert forbidden not in content, name
    for phrase in ("paper", "accompanying", "as described with the code"):
        assert phrase not in card.casefold()
    column_table = card.split("### Index columns\n", 1)[1].split("\n## ", 1)[0]
    entries = [
        row.strip("|").split("|")
        for row in column_table.splitlines()
        if row.startswith("| `")
    ]
    assert [col.strip().strip("`") for col, _ in entries] == list(qr.HF_COLUMNS)
    assert all(0 < len(meaning.split()) <= 15 for _, meaning in entries)
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
        public = file_header(assembled / path)
        assert public["schema"] == HF_FORMAT
        assert set(public["payload"]["instance"]) == set(HF_INFO_FIELDS)
        assert row.sha256 == sha(assembled / path) == sha(shard / path)
        assert row.content_sha256 == public["content_sha256"]
        assert os.stat(shard / path).st_ino == os.stat(assembled / path).st_ino
        for view in PROBLEMS:
            _, before = read_portable(output / path, view)
            _, after = read_portable(assembled / path, view)
            assert before.nodes.equals(after.nodes)
            assert after.info == {k: before.info[k] for k in HF_INFO_FIELDS}
            assert set(after.tables()["instances"].column_names) == set(HF_INFO_FIELDS)
            for key in before.matrices:
                assert before.matrices[key].tobytes() == after.matrices[key].tobytes()
    # Omitted provenance must still be accepted before making a new release.
    merge.out = tmp_path / "bad-release"
    omitted = shard / "CLE" / city / "roads.json"
    saved_bytes = omitted.read_bytes()
    omitted.write_bytes(b"broken")
    with pytest.raises(ValueError, match="Checksum mismatch"):
        qr.merge(merge)
    assert not merge.out.exists()
    omitted.write_bytes(saved_bytes)
    instance = shard / df.path.iloc[0]
    saved_bytes = instance.read_bytes()
    instance.write_bytes(b"broken")
    with pytest.raises(ValueError, match="Checksum mismatch"):
        qr.merge(merge)
    assert not merge.out.exists()
    instance.write_bytes(saved_bytes)
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


def test_repository_documents_preserve_f8fffc4():
    # Fixed digests also work when testing an exported source tree without .git.
    assert (
        sha(qr.PROJECT / "README.md")
        == "3bca7ca5a872b7ff6042a66c85052a765d0d63044b8df040d0aeade924591575"
    )
    assert (
        sha(qr.PROJECT / "NOTICE.md")
        == "eeaca04361529bf7c65896fbf4e8b9549b465989fe361bf14588a465ec0708d9"
    )
    assert not (qr.PROJECT / "LICENSE.md").exists()


@pytest.mark.parametrize("key", qr.CONTRACT_FIELDS)
@pytest.mark.parametrize("problem", ["missing", "different"])
@pytest.mark.parametrize("check_only", [True, False])
def test_merge_rejects_invalid_release_contract_before_install(
    tmp_path, monkeypatch, key, problem, check_only
):
    contract = dict(
        version="v1",
        code_sha256="a" * 64,
        stage1_engine_sha256="b" * 64,
        calendar_start="2026-11-02",
        calendar_days=28,
    )
    changed = contract.copy()
    if problem == "missing":
        del changed[key]
    else:
        changed[key] = 29 if key == "calendar_days" else "changed"
    catalogs = {
        city: (None, {"contract": values, "instance_schema": HF_FORMAT}, None)
        for city, values in [("chicago", contract), ("houston", changed)]
    }
    monkeypatch.setattr(qr, "city_catalogs", lambda roots: catalogs)
    monkeypatch.setattr(
        qr,
        "install_city_environment",
        lambda *args: pytest.fail("unexpected file installation"),
    )
    target = tmp_path / "release"
    with pytest.raises(
        ValueError, match=f"{problem.title()} release contract field {key}: houston"
    ):
        qr.merge(
            SimpleNamespace(
                shards=["shard"],
                accepted_shards=["accepted"],
                check_only=check_only,
                out=target,
            )
        )
    assert not target.exists()
