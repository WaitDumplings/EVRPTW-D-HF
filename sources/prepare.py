"""Download public upstream sources and stage a self-contained city inventory."""

from concurrent.futures import ProcessPoolExecutor
from datetime import date
from functools import lru_cache
import os
from pathlib import Path
import shutil
import tempfile
from stage2.common import CITIES, PROJECT, digest, lock, read, sha, write
from .download import Store

SPEC = PROJECT / "config/public_sources.json"


@lru_cache(maxsize=1)
def source_code_hash():
    # Extraction uses shared Stage 1 classifiers and the Amazon donor builder.
    # Keep their code and versioned rules in the preparation cache identity.
    files = []
    for folder in (
        "sources",
        "evrptw_scenarios",
        "stage1_engine/src",
        "stage1_engine/configs",
        "config",
    ):
        files.extend(
            p
            for p in (PROJECT / folder).rglob("*")
            if p.is_file() and p.suffix in {".py", ".json", ".sql"}
        )
    return digest([(str(p.relative_to(PROJECT)), sha(p)) for p in sorted(files)])


def part(work, name, osm_date, expected):

    work = Path(work)
    root = work / "public"
    target = root / name
    policy = {"component": name, "source_code": source_code_hash(), "spec": sha(SPEC)}
    if name in CITIES:
        policy["osm_date"] = osm_date
    with lock(root / "locks" / name):
        if target.exists():
            rec = read(target / "receipt.json")
            if rec["policy"] != policy or any(
                sha(target / k) != v for k, v in rec["outputs"].items()
            ):
                raise ValueError(
                    f"Different inputs or damaged prepared component: {name}"
                )
            if expected is not None and any(
                expected["files"].get(k) != v for k, v in rec["sources"].items()
            ):
                raise ValueError(
                    f"Component conflicts with the requested source lock: {name}"
                )
            return name
        store = Store(work, expected)
        spec = read(SPEC)
        with tempfile.TemporaryDirectory(dir=root) as tmp:
            dest = Path(tmp) / "build"
            dest.mkdir()
            details = {}
            if name == "amazon":
                from . import orders

                orders.build(store, dest)
            elif name == "afdc":
                from . import charging

                charging.build(store, dest)
                for n in ["station", "units"]:
                    source = work / f"raw/afdc/{n}.csv"
                    shutil.copyfile(source, dest / (n + ".csv"))
            else:
                from . import geography, osm

                s = spec["cities"][name]
                boundary, counties = geography.geography(store, name, s, dest)
                geography.microsoft(
                    store,
                    spec["microsoft"],
                    s["quadkeys"],
                    boundary,
                    s["crs"],
                    dest / "microsoft.parquet",
                )
                geography.nsi(store, counties, boundary, dest / "nsi.parquet")
                details["road"] = osm.layers(store, name, s, dest, boundary, osm_date)
                details["counties"] = counties
            files = {
                str(p.relative_to(dest)): sha(p)
                for p in sorted(dest.rglob("*"))
                if p.is_file()
            }
            write(
                dest / "receipt.json",
                dict(policy=policy, sources=store.records, outputs=files, **details),
            )
            os.rename(dest, target)
    return name


def make_inventory(work):
    root = work / "public"
    cities, files, roads = {}, {}, {}
    spec = read(SPEC)["cities"]

    def entry(component, name, rel):
        key = component + "." + name
        files[key] = {
            "path": component + "/" + rel,
            "sha256": receipts[component]["outputs"][rel],
        }
        return key

    receipts = {n: read(root / n / "receipt.json") for n in [*CITIES, "afdc"]}
    for city in CITIES:
        road = receipts[city]["road"]
        roads[city] = road
        cities[city] = dict(
            area_crs=spec[city]["crs"],
            state_fips=spec[city]["place"][:2],
            boundaries={
                "admin_boundary": entry(city, "admin", "admin.geojson"),
                "service_boundary": entry(city, "land", "land.geojson"),
            },
            depot={
                "raw": entry(city, "depot", "depot.parquet"),
                "extraction": entry(city, "depot_meta", "depot.json"),
                "pbf_sha256": road["sha256"],
            },
        )
    ch = {}
    for n in ["station", "units", "census"]:
        ch[n] = entry("afdc", n, n + ".csv")
        ch[n + "_provenance"] = entry("afdc", n + "_meta", n + ".json")
    pinned = {}
    for rec in receipts.values():
        for k, v in rec["sources"].items():
            if k in pinned and pinned[k] != v:
                raise ValueError(f"Two cities used different copies of {k}")
            pinned[k] = v
    inventory = dict(
        schema="evrptwd_public_stage1_v1",
        cities=cities,
        files=files,
        roads=roads,
        charging=ch,
        source_vintages={
            "tiger": 2025,
            "microsoft_index": "2026-08-13",
            "input_lock": digest(pinned),
        },
        component_sha256={c: digest(v["outputs"]) for c, v in receipts.items()},
    )
    write(root / "inventory.json", inventory)
    return inventory


def prepare(work, only, cities, workers, osm_date, lockfile):
    expected = read(lockfile) if lockfile else None
    if expected and expected.get("schema") != "evrptwd_public_source_lock_v1":
        raise ValueError("Unknown source lock")
    reference_date = (expected or {}).get("osm_date")
    osm_date = osm_date or reference_date or "latest"
    if osm_date != "latest":
        date.fromisoformat(osm_date)
    if reference_date and osm_date != reference_date:
        raise ValueError("Requested OSM date conflicts with the supplied source lock")
    names = list(cities) if only in ("all", "stage1", "cities") else []
    if only in ("stage1", "all", "afdc"):
        names.append("afdc")
    if only in ("stage2", "all"):
        names.append("amazon")
    with ProcessPoolExecutor(max_workers=workers) as pool:
        jobs = [pool.submit(part, work, n, osm_date, expected) for n in names]
        for job in jobs:
            print("Prepared", job.result(), flush=True)
    root = work / "public"
    with lock(root / "locks" / "index"):
        records = {}
        city_dates = set()
        for n in [*CITIES, "afdc", "amazon"]:
            if (root / n / "receipt.json").exists():
                receipt = read(root / n / "receipt.json")
                policy = receipt["policy"]
                if policy["source_code"] != source_code_hash() or policy["spec"] != sha(
                    SPEC
                ):
                    raise ValueError(
                        f"Prepared component uses a different processing version: {n}"
                    )
                if (
                    n in CITIES
                    and only in ("all", "stage1", "cities")
                    and policy["osm_date"] != osm_date
                ):
                    raise ValueError(f"Prepared city uses a different OSM date: {n}")
                if n in CITIES:
                    city_dates.add(policy["osm_date"])
                sources = receipt["sources"]
                if any(k in records and records[k] != v for k, v in sources.items()):
                    raise ValueError("Conflicting source digests between components")
                if expected is not None and any(
                    expected["files"].get(k) != v for k, v in sources.items()
                ):
                    raise ValueError(
                        f"Prepared component conflicts with source lock: {n}"
                    )
                records.update(sources)
        if len(city_dates) > 1:
            raise ValueError("Prepared cities use different OSM dates")
        if all((root / n / "receipt.json").is_file() for n in [*CITIES, "afdc"]):
            make_inventory(work)
        lock_target = root / "sources.lock.json"
        # A host may put its reference lock at the normal output path and fetch
        # only one component first. Its outstanding entries are still needed.
        reusing_reference = (
            lockfile is not None and Path(lockfile).resolve() == lock_target.resolve()
        )
        if not reusing_reference:
            write(
                lock_target,
                dict(
                    schema="evrptwd_public_source_lock_v1",
                    osm_date=next(iter(city_dates), None),
                    files=records,
                ),
            )
    print("Source lock:", root / "sources.lock.json")


def verify_inputs(root, cities):
    inv = read(root / "inventory.json")
    for n in [*cities, "afdc"]:
        rec = read(root / n / "receipt.json")
        if digest(rec["outputs"]) != inv["component_sha256"][n] or any(
            sha(root / n / k) != v for k, v in rec["outputs"].items()
        ):
            raise ValueError("Changed public-source input: " + n)
