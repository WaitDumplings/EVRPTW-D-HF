"""Run the frozen r1 cohort calibration once, then dispatch independent city subprocesses."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import os
import subprocess
import sys
import tempfile

from .common import CITIES, CONFIG, FROZEN, digest, lock, read, sha, write


def local_config(inputs, output):
    obj = read(CONFIG)
    if not (inputs / "inventory.json").is_file():
        missing = [
            n for n in [*CITIES, "afdc"] if not (inputs / n / "receipt.json").is_file()
        ]
        raise ValueError(
            f"Run sources --only stage1 before shared calibration. Missing components: {missing}"
        )
    inv = read(inputs / "inventory.json")
    if inv["schema"] != "evrptwd_public_stage1_v1" or set(inv["cities"]) != set(CITIES):
        raise ValueError("Run sources --only stage1 for the full calibration cohort")
    for city, spec in obj["cities"].items():
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
        inventory=str(inputs / "inventory.json"),
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


def calibration_files(output):
    paths = [
        "units/quality_thresholds.json",
        "units/metric_scales.csv",
        "units/calibration_predictions.parquet",
    ]
    for city in CITIES:
        paths.extend(
            f"cities/{city}/customer_inputs/{name}.parquet"
            for name in ("unit_reference_roster", "final_buildings")
        )
    return {name: sha(output / name) for name in paths}


def run(inputs, output, cities, phase, workers):
    env = dict(
        os.environ,
        PYTHONPATH=str(FROZEN / "src"),
        OPENBLAS_NUM_THREADS="1",
        OMP_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
        VECLIB_MAXIMUM_THREADS="1",
        PYTHONHASHSEED="0",
    )
    output = output.resolve()
    logs = output / "logs"
    with lock(output / ".config.lock"):
        cfg = local_config(inputs.resolve(), output)
    shared_marker = output / "calibration-ready.json"
    inventory_path = inputs.resolve() / "inventory.json"
    env["EVRPTWD_PUBLIC_INVENTORY"] = str(inventory_path)
    public_sha = sha(inventory_path)
    from sources.prepare import verify_inputs

    verify_inputs(inputs.resolve(), cities if phase == "cities" else CITIES)
    engine_hash = digest(
        [
            (str(p.relative_to(FROZEN)), sha(p))
            for top in ("src", "configs")
            for p in sorted((FROZEN / top).rglob("*"))
            if p.is_file() and p.suffix in {".py", ".json", ".sql"}
        ]
    )
    signature = digest([read(cfg), public_sha, engine_hash])
    commands = ["-m", "evrptw_cle.stage1"]
    if phase in ("all", "shared"):
        with lock(output / ".stage1.lock"):
            if not shared_marker.exists():
                pending = output / ".building-calibration"
                if pending.exists():
                    archive_root = output / "interrupted"
                    archive_root.mkdir(exist_ok=True)
                    archive = Path(
                        tempfile.mkdtemp(prefix="calibration-", dir=archive_root)
                    )
                    for name in ("units", "cities"):
                        part = output / name
                        if part.exists():
                            part.rename(archive / name)
                pending.touch()
                for step in (
                    "prepare",
                    "calibrate-units",
                    "estimate-units",
                    "finalize",
                ):
                    launch([*commands, step, cfg], logs / (step + ".log"), env)
                write(
                    shared_marker,
                    {"signature": signature, "files": calibration_files(output)},
                )
                pending.unlink(missing_ok=True)
    if not shared_marker.is_file() or read(shared_marker)["signature"] != signature:
        raise ValueError("Run stage1 --phase shared once on the eleven-city cohort")
    if calibration_files(output) != read(shared_marker)["files"]:
        raise ValueError("Joint housing calibration files have changed")
    calibration_sha = digest(read(shared_marker)["files"])
    if phase in ("all", "cities"):

        def one(city):
            with (
                lock(output / ".stage1.lock", shared=True),
                lock(output / "locks" / city),
            ):
                base = output / "cities" / city
                certificate = base / "stage1_validation.json"
                manifest = base / "cle_manifest.json"
                if certificate.is_file() and manifest.is_file():
                    prior = read(certificate)
                    if prior.get("passed") is True and prior.get(
                        "cle_manifest_sha256"
                    ) == sha(manifest):
                        files = read(manifest)["files"]
                        if all(
                            (base / name).is_file()
                            and sha(base / name) == item["sha256"]
                            for name, item in files.items()
                        ):
                            print("Stage 1 already verified", city, flush=True)
                            return
                # The reference component driver handles component reuse and its provenance checks.
                launch(
                    [
                        *commands,
                        "components",
                        cfg,
                        "--cities",
                        city,
                        "--component",
                        "all",
                    ],
                    logs / (city + ".log"),
                    env,
                )
                # The reference verify CLI writes a cohort summary: serialize this short reporting stage.
                with lock(output / ".verify.lock"):
                    launch(
                        [*commands, "verify", cfg, "--cities", city],
                        logs / (city + ".log"),
                        env,
                    )
                cert = read(output / "cities" / city / "stage1_validation.json")
                if not cert["passed"]:
                    raise ValueError("Stage 1 validation failed for " + city)
                print("Stage 1 verified", city, flush=True)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(one, cities))
        for city in cities:
            base = output / "cities" / city
            write(
                base / "public_authority.json",
                dict(
                    schema="evrptwd_public_city_v1",
                    city=city,
                    calibration_cohort=list(CITIES),
                    inputs_sha256=public_sha,
                    engine_sha256=engine_hash,
                    calibration_sha256=calibration_sha,
                    cle_manifest_sha256=sha(base / "cle_manifest.json"),
                    validation_sha256=sha(base / "stage1_validation.json"),
                ),
            )
    if phase == "seal" or (phase == "all" and set(cities) == set(CITIES)):
        with lock(output / ".stage1.lock"):
            reports = []
            manifests = {}
            for city in CITIES:
                base = output / "cities" / city
                cert = read(base / "stage1_validation.json")
                manifests[city] = sha(base / "cle_manifest.json")
                if not cert["passed"] or cert["cle_manifest_sha256"] != manifests[city]:
                    raise ValueError("Invalid city receipt: " + city)
                reports.append({**cert, "city": city})
            receipt_path = output / "stage1_server_build_receipt.json"
            if receipt_path.exists():
                old = read(receipt_path)
                if (
                    old.get("schema") == "evrptw_stage1_server_build_receipt_v1"
                    and old.get("complete_cle_validation_passed") is True
                    and old.get("cle_manifests") == manifests
                    and old.get("config_sha256") == sha(cfg)
                    and old.get("inputs_sha256") == public_sha
                    and old.get("engine_sha256") == engine_hash
                    and old.get("calibration_sha256") == calibration_sha
                    and (output / "validation_summary.json").exists()
                    and read(output / "validation_summary.json") == reports
                ):
                    return
            write(output / "validation_summary.json", reports)
            write(
                output / "stage1_server_build_receipt.json",
                dict(
                    schema="evrptw_stage1_server_build_receipt_v1",
                    freeze_id="stage1-public-v1",
                    inputs_sha256=public_sha,
                    engine_sha256=engine_hash,
                    calibration_sha256=calibration_sha,
                    complete_cle_validation_passed=True,
                    stage2_included=False,
                    completed_utc=datetime.now(timezone.utc).isoformat(),
                    config_sha256=sha(cfg),
                    validated_cities=list(CITIES),
                    cle_manifests=manifests,
                ),
            )
