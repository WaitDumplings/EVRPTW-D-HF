"""Build from one city at a time, including all housing support thresholds."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import os
import subprocess
import sys
import tempfile

from .common import CONFIG, FROZEN, digest, lock, read, sha, write


def local_config(inputs, output, city, inventory_path):
    obj = read(CONFIG)
    inv = read(inventory_path)
    spec = obj["cities"][city]
    obj["cities"] = {city: spec}
    for field, file in {
        "microsoft": "microsoft.parquet",
        "osm": "osm.parquet",
        "nsi": "nsi.parquet",
        "service_boundary": "land.geojson",
        "admin_boundary": "admin.geojson",
    }.items():
        spec[field] = str(inputs / city / file)
    obj["components"] = dict(
        source_root=str(inputs),
        road_sources=str(inputs),
        road_subdir="roads",
        inventory=str(inventory_path),
    )
    obj["source_snapshots"] = inv["source_vintages"]
    obj["output"] = str(output)
    cfg = output / "stage1.local.json"
    if cfg.exists() and read(cfg) != obj:
        raise ValueError("Stage 1 output is already bound to another input root/config")
    write(cfg, obj, sort_keys=False)
    return cfg


def launch(command, log, env):
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as stream:
        print(datetime.now(timezone.utc).isoformat(), command, file=stream, flush=True)
        subprocess.run(
            [sys.executable, "-u", *map(str, command)],
            stdout=stream,
            stderr=subprocess.STDOUT,
            env=env,
            check=True,
        )


def calibration_files(output, city):
    names = [
        "units/quality_thresholds.json",
        "units/metric_scales.csv",
        "units/calibration_predictions.parquet",
    ]
    names.extend(
        f"cities/{city}/customer_inputs/{name}.parquet"
        for name in ("unit_reference_roster", "final_buildings")
    )
    return {name: sha(output / name) for name in names}


def city_root(work, city):
    local = work / "stage1" / city
    return local if local.is_dir() else work / "stage1"


def run(inputs, output, cities, workers):
    inputs = inputs.resolve()
    output = output.resolve()
    engine_hash = digest(
        [
            (str(p.relative_to(FROZEN)), sha(p))
            for top in ("src", "configs")
            for p in sorted((FROZEN / top).rglob("*"))
            if p.is_file() and p.suffix in {".py", ".json", ".sql"}
        ]
    )

    def build(city):
        root = output / city
        with lock(root / ".stage1.lock"):
            build_city(inputs, root, city, engine_hash)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(build, dict.fromkeys(cities)))


def build_city(inputs, output, city, engine_hash):
    from sources.prepare import verify_inputs, write_inventory

    inventory = output / "inventory.json"
    write_inventory(inputs, [city], inventory)
    verify_inputs(inputs, [city], inventory)
    cfg = local_config(inputs, output, city, inventory)
    public_sha = sha(inventory)
    signature = digest([read(cfg), public_sha, engine_hash])
    env = dict(
        os.environ,
        PYTHONPATH=str(FROZEN / "src"),
        EVRPTWD_PUBLIC_INVENTORY=str(inventory),
        OPENBLAS_NUM_THREADS="1",
        OMP_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
        VECLIB_MAXIMUM_THREADS="1",
        PYTHONHASHSEED="0",
    )

    def step(name, *args):
        launch(
            ["-m", "evrptw_cle.stage1", name, cfg, "--cities", city, *args],
            output / "logs" / (name + ".log"),
            env,
        )

    marker = output / "calibration-ready.json"
    if not marker.exists():
        pending = output / ".building-calibration"
        if pending.exists():
            archive_root = output / "interrupted"
            archive_root.mkdir(exist_ok=True)
            archive = Path(tempfile.mkdtemp(prefix="housing-", dir=archive_root))
            for name in ("units", "cities"):
                if (output / name).exists():
                    (output / name).rename(archive / name)
        pending.touch()
        for name in ("prepare", "calibrate-units", "estimate-units", "finalize"):
            step(name)
        files = calibration_files(output, city)
        write(marker, {"signature": signature, "files": files})
        pending.unlink(missing_ok=True)
    else:
        recorded = read(marker)
        if recorded["signature"] != signature:
            raise ValueError(
                f"City inputs or method differ from the recorded fit: {city}"
            )
        files = calibration_files(output, city)
        if files != recorded["files"]:
            raise ValueError(f"Housing calibration files have changed: {city}")

    base = output / "cities" / city
    certificate, manifest = base / "stage1_validation.json", base / "cle_manifest.json"
    valid = False
    if certificate.is_file() and manifest.is_file():
        prior = read(certificate)
        valid = (
            prior.get("passed") is True
            and prior.get("cle_manifest_sha256") == sha(manifest)
            and all(
                (base / name).is_file() and sha(base / name) == item["sha256"]
                for name, item in read(manifest)["files"].items()
            )
        )
    if not valid:
        step("components", "--component", "all")
        step("verify")
        cert = read(certificate)
        if not cert["passed"] or cert["cle_manifest_sha256"] != sha(manifest):
            raise ValueError(f"Stage 1 validation failed for {city}")
    write(
        base / "public_authority.json",
        dict(
            schema="evrptwd_local_city_v1",
            city=city,
            calibration_cohort=[city],
            inputs_sha256=public_sha,
            engine_sha256=engine_hash,
            calibration_sha256=digest(files),
            cle_manifest_sha256=sha(manifest),
            validation_sha256=sha(certificate),
        ),
    )
    print("Stage 1 verified", city, flush=True)
