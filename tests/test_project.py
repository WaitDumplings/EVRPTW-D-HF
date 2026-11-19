"""Exercise the city adapter, independent process scheduling, cold restore and retry."""

import json
import os
from pathlib import Path
import pytest
import subprocess
import sys
from collections import Counter
import zipfile

import geopandas as gpd
import numpy as np
import pandas as pd

from stage2.common import CITIES, PROJECT, read, sha, write
from stage2.plan import plan_tasks


def miniature_cle(root):
    city = "chicago"
    base = root / "cities" / city
    from shapely.geometry import box

    bg = gpd.GeoDataFrame(
        {"bg_geoid": ["170310608001"]},
        geometry=[box(-87.662, 41.89, -87.652, 41.9)],
        crs=4326,
    )
    geo = root.parent / "public/chicago"
    geo.mkdir(parents=True)
    bg.to_parquet(geo / "bg.parquet", index=False)
    write(geo / "rook.json", dict(neighbors={"17031060800": []}))
    ref = bg.geometry.iloc[0].representative_point()
    layers = [
        ("customer", 100, "site_id", "customers/latent_locations.parquet"),
        ("depot", 2, "candidate_id", "depots/depots.parquet"),
        ("charging", 12, "charging_location_id", "charging/chargers.parquet"),
    ]
    canon = []
    ledger = []
    for typ, count, key, table in layers:
        rows = []
        for i in range(count):
            index = len(canon)
            ident = f"{typ}:{i}"
            gateway = str(index % 5)
            row = {
                key: str(i),
                "connector_length_m": 0.0,
                "requires_terminal_paths": True,
                "directed_projection_offsets": "[]",
            }
            cost = {
                "location_id": ident,
                "connector_length_m": 0.0,
                "connector_time_s_per_inverse_kph": 0.0,
            }
            for d in ("inbound", "outbound"):
                row.update(
                    {
                        f"terminal_{d}_path_json": "[]",
                        f"terminal_{d}_gateway": gateway,
                        f"terminal_{d}_length_m": 0.0,
                    }
                )
                cost[f"{d}_terminal_length_m"] = 0.0
                for k in ("weekday", "weekend"):
                    cost[f"{d}_terminal_reference_{k}_time_s"] = 0.0
            if typ == "charging":
                row.update(assigned_power_kw=50.0, reference_charge_mode="dc_fast")
            rows.append(row)
            ledger.append(cost)
            canon.append(
                dict(
                    location_id=ident,
                    location_type=typ,
                    candidate_eligible=True,
                    access_table=table,
                    access_row_id=str(i),
                    residential_units=1.0 if typ == "customer" else None,
                    geometry=ref,
                )
            )
        p = base / table
        p.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(p, index=False)
    gpd.GeoDataFrame(canon, crs=4326).to_parquet(
        base / "locations.parquet", index=False
    )
    pd.DataFrame(ledger).to_parquet(base / "location_access_costs.parquet", index=False)
    roads = []
    for i in range(5):
        for j in range(5):
            if i == j:
                continue
            rows = dict(
                edge_u=str(i),
                edge_v=str(j),
                edge_key="0",
                length_m=1000.0 + i + j,
                in_public_graph=True,
                operating_mode="U",
            )
            for day, speed in [("weekday", 30.0), ("weekend", 40.0)]:
                rows["reference_speed_" + day + "_kph"] = speed
                rows["reference_travel_time_" + day + "_s"] = (
                    rows["length_m"] * 3.6 / speed
                )
            roads.append(rows)
    (base / "speeds").mkdir()
    pd.DataFrame(roads).to_parquet(base / "speeds/directed_speeds.parquet", index=False)
    (base / "boundaries").mkdir()
    bg.to_file(base / "boundaries/service.geojson", driver="GeoJSON")
    write(
        base / "cle_manifest.json",
        dict(
            schema="evrptw_building_first_cle_v1",
            city_slug=city,
            files={
                str(p.relative_to(base)): dict(sha256=sha(p))
                for p in base.rglob("*")
                if p.is_file()
            },
            boundaries=dict(service_boundary="boundaries/service.geojson"),
        ),
    )
    checksum = sha(base / "cle_manifest.json")
    write(
        base / "stage1_validation.json", dict(passed=True, cle_manifest_sha256=checksum)
    )
    write(
        root / "stage1_server_build_receipt.json",
        dict(
            schema="evrptw_stage1_server_build_receipt_v1",
            complete_cle_validation_passed=True,
            freeze_id="stage1-20261102-r1",
            validated_cities=list(CITIES),
            freeze_manifest_sha256="9db2933e7f83ffce3b3917dbad8942111db19683066e9b69d603f455fc34cfde",
            cle_manifests={c: checksum if c == city else "fixture" for c in CITIES},
        ),
    )


def test_global_panel():
    loc = pd.DataFrame(
        {"location_type": ["depot"] * 3, "bg_geoid": ["A", "B", "A"]},
        index=["d1", "d2", "d3"],
    )
    all_rows = [
        r for c in CITIES for r in plan_tasks(c, loc, 20261103, "2026-11-02", 28)
    ]
    assert (
        len(all_rows) == 17400
        and len({tuple(r["task_key"]) for r in all_rows}) == 17400
    )
    days = Counter((r["split"], r["n"]) for r in all_rows if r["day_type"] == "weekday")
    assert [days["train", n] for n in [100, 500, 1000]] == [7143, 1429, 714]
    assert days["xs", 2000] == 357 and all(
        days["val", n] == 214 and days["test3", n] == 357 for n in [100, 500, 1000]
    )
    assert all(
        (r["city"] == "jacksonville") == (r["split"] == "test3") for r in all_rows
    )


def test_two_process_restore_and_retry(tmp_path):
    catalog = os.getenv("EVRPTWD_TEST_CATALOG")
    if not catalog:
        pytest.skip("Set EVRPTWD_TEST_CATALOG after public Amazon source preparation")
    miniature_cle(tmp_path / "cle")
    (tmp_path / "public/amazon").symlink_to(
        Path(catalog).resolve(), target_is_directory=True
    )

    def call(*args, ok=True):
        argv = [
            sys.executable,
            str(PROJECT / "pipeline.py"),
            "--work",
            str(tmp_path),
            *map(str, args),
        ]
        r = subprocess.run(argv, capture_output=True, text=True)
        assert (r.returncode == 0) == ok, (r.stdout, r.stderr)
        return r.stdout

    call("prepare-city", "--city", "chicago", "--cle-root", tmp_path / "cle")
    call("plan", "--city", "chicago")
    call("plan", "--city", "chicago", "--custom-n", 300, "--cs", 20, "--count", 3)
    custom = read(tmp_path / "plans/chicago-custom-300.json")["tasks"]
    assert len(custom) == 3 and all(
        row["n"] == 300 and row["cs_budget"] == 20 and row["split"] == "custom"
        for row in custom
    )
    base = ["stage2", "--city", "chicago", "--split", "train", "--n", 100, "--limit", 2]
    err = json.loads(
        call(*base, "--workers", 2, "--catalog", tmp_path / "absent", ok=False)
    )
    assert err == {"execution_error": 2}
    call(*base, "--workers", 2)
    assert '"skipped": 2' in call(*base, "--workers", 2)
    call(*base, "--workers", 1, "--output", tmp_path / "serial")
    for shard in ("0/2", "1/2"):
        call(*base, "--workers", 1, "--output", tmp_path / "shards", "--shard", shard)
    audited = json.loads(
        call("audit", "--city", "chicago", "--outputs", tmp_path / "shards")
    )
    assert (
        not audited["issues"]
        and sum(r.get("protocol_certified", 0) for r in audited["rows"]) == 2
    )
    collision = json.loads(
        call(
            "audit",
            "--city",
            "chicago",
            "--outputs",
            tmp_path,
            tmp_path / "shards",
            ok=False,
        )
    )
    assert [x["issue"] for x in collision["issues"]] == ["duplicate", "duplicate"]
    custom_run = [
        "stage2",
        "--city",
        "chicago",
        "--split",
        "custom",
        "--n",
        300,
        "--limit",
        1,
    ]
    assert json.loads(call(*custom_run)) == {"generation_failed": 1}
    assert json.loads(call(*custom_run)) == {"skipped_method_failure": 1}
    assert json.loads(call(*custom_run, "--retry-method-failures")) == {
        "generation_failed": 1
    }
    files = sorted((tmp_path / "instances").rglob("*.zip"))
    assert len(files) == 2
    for f in files:
        assert (
            f.read_bytes()
            == (tmp_path / "shards" / f.relative_to(tmp_path)).read_bytes()
        )
        assert (
            f.read_bytes()
            == (tmp_path / "serial" / f.relative_to(tmp_path)).read_bytes()
        )
        assert (
            len(
                list(
                    (
                        tmp_path
                        / "ledger"
                        / f.relative_to(tmp_path / "instances").parent
                        / f.name.split(".")[0]
                    ).glob("attempt-*")
                )
            )
            == 2
        )
    call("restore", files[0], "--out", tmp_path / "restored")
    with np.load(tmp_path / "restored/costs.npz") as a:
        assert len(a.files) == 4 and all(a[k].shape == (111, 111) for k in a)
    with zipfile.ZipFile(files[0]) as z:
        assert "costs.npz" not in z.namelist()


def test_stage1_parallel_city_dispatch(tmp_path, monkeypatch):
    """Exercise the r1 coordinator with tiny child programs instead of eleven road builds."""
    from stage2 import stage_one

    roots = tmp_path / "stage1"
    child = tmp_path / "worker.py"
    child.write_text("""
import json, os, sys, time
from pathlib import Path
dest, city=Path(sys.argv[1]),sys.argv[2]
(dest/city).write_text(str(os.getpid()))
deadline=time.monotonic()+10
while len(list(dest.glob('*'))) < 2:
    if time.monotonic()>deadline: raise RuntimeError('two subprocesses did not overlap')
    time.sleep(.02)
""")
    marker = tmp_path / "processes"
    marker.mkdir()

    def launch(command, log, env):
        if "components" in command:
            city = command[command.index("--cities") + 1]
            subprocess.run([sys.executable, child, marker, city], check=True, env=env)
        if "verify" in command:
            city = command[command.index("--cities") + 1]
            city_root = roots / "cities" / city
            write(city_root / "cle_manifest.json", {"city": city, "files": {}})
            write(
                city_root / "stage1_validation.json",
                {
                    "passed": True,
                    "cle_manifest_sha256": sha(city_root / "cle_manifest.json"),
                },
            )

    monkeypatch.setattr(stage_one, "launch", launch)
    from stage2.common import digest

    staged = tmp_path / "inputs"
    for n in [*CITIES, "afdc"]:
        write(staged / n / "receipt.json", {"outputs": {}})
    write(
        staged / "inventory.json",
        {
            "schema": "evrptwd_public_stage1_v1",
            "cities": dict.fromkeys(CITIES, {}),
            "source_vintages": {},
            "component_sha256": dict.fromkeys([*CITIES, "afdc"], digest({})),
        },
    )
    phase = lambda name, cities: stage_one.run(
        tmp_path / "inputs", roots, cities, name, 2
    )
    phase("shared", CITIES)
    assert list(read(roots / "stage1.local.json")["cities"]) == list(CITIES)
    phase("cities", ["chicago", "dallas"])
    assert len({p.read_text() for p in marker.iterdir()}) == 2
    for city in set(CITIES) - {"chicago", "dallas"}:
        launch(["verify", "--cities", city], None, None)
    phase("seal", CITIES)
    stamp = sha(roots / "stage1_server_build_receipt.json")
    phase("cities", ["chicago", "dallas"])
    phase("seal", CITIES)
    assert sha(roots / "stage1_server_build_receipt.json") == stamp
    receipt = read(roots / "stage1_server_build_receipt.json")
    assert (
        set(receipt["cle_manifests"]) == set(CITIES)
        and receipt["complete_cle_validation_passed"]
    )


def test_r1_local_access_context_with_policy(tmp_path):
    """The original r1 test fixture omits the policy field emitted by the r1 context writer."""
    import importlib.util
    from shapely.geometry import box
    from stage2.common import FROZEN

    sys.path.insert(0, str(FROZEN / "src"))
    try:
        from evrptw_cle.local_road_access import LocalAccessContext, POLICY

        spec = importlib.util.spec_from_file_location(
            "frozen_road_test", FROZEN / "tests/test_local_road_access.py"
        )
        original = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(original)
        ctx = original.context()
        ctx.water = box(1000, 1000, 1010, 1010)
        ctx.save(tmp_path)
        ctx.physical.to_parquet(tmp_path / "physical_roads.parquet", index=False)
        files = {p.name: sha(p) for p in tmp_path.iterdir() if p.is_file()}
        write(
            tmp_path / "access_context_manifest.json",
            dict(area_crs=ctx.area_crs, policy=POLICY, outputs=files),
        )
        restored = LocalAccessContext.load(tmp_path)
        assert set(restored.core) == set(ctx.core) and set(restored.access) == set(
            ctx.access
        )
        assert restored.reference == ctx.reference and restored._path(
            "c", restored.outbound
        ) == ctx._path("c", ctx.outbound)
    finally:
        sys.path.remove(str(FROZEN / "src"))


def test_release_index(tmp_path):
    from stage2.plan import save_plan, seal_plans
    import pytest

    loc = pd.DataFrame({"location_type": ["depot"], "bg_geoid": ["A"]}, index=["d1"])
    folder = tmp_path / "plans"
    for city in CITIES:
        base = tmp_path / "shared" / city
        write(base / "shared.json", {"city": city})
        loc.to_parquet(base / "locations.parquet")
        save_plan(base, folder / (city + ".json"), 42, "2026-11-02", 28)
    index = seal_plans(folder)
    assert index["task_count"] == 17400 and len(index["plans_sha256"]) == 11
    record = read(folder / "chicago.json")
    record["calendar_days"] = 35
    write(folder / "chicago.json", record)
    with pytest.raises(ValueError, match="Incompatible release plan"):
        seal_plans(folder)
