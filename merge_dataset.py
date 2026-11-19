"""Assemble completed city deliveries into one portable solver dataset."""

import argparse
import os
from pathlib import Path
import shutil
import tempfile

import pandas as pd

from export_output import verify_city
from instance_file import file_header
from stage2.common import CITIES, digest, public_split, read, sha, write
from stage2.plan import city_contract, instance_namespace

FINAL = {
    "protocol_certified",
    "generation_failed",
    "protocol_uncertified",
    "proven_infeasible",
}


def link_or_copy(source, target):
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def collect_slots(root, city, target, panel, binding):
    report = root / "reports" / city
    namespace = instance_namespace(panel, sha(report / "plan.json"))
    slots = pd.read_parquet(report / "slots.parquet")
    if len(slots) != len(panel["tasks"]):
        raise ValueError(f"Missing or extra slot records: {city}")
    expected_files = set()
    for row, task in zip(slots.itertuples(index=False), panel["tasks"]):
        split = public_split(city, task["split"])
        if (row.city, row.split, row.n, row.slot, row.seed, row.cs_budget) != (
            city,
            split,
            task["n"],
            task["slot"],
            task["seed"],
            task["cs_budget"],
        ) or row.status not in FINAL:
            raise ValueError(
                f"Unfinished or incompatible slot: {city}/{split}/{task['n']}/{task['slot']}"
            )
        if row.status != "protocol_certified":
            if any(pd.notna(v) for v in (row.path, row.sha256, row.instance_id)):
                raise ValueError(
                    f"Uncertified slot has a published file: {city}/{split}/{row.slot}"
                )
            continue
        name = digest([namespace, task["task_key"], task["seed"]])[:24]
        path = Path("instances") / city / split / f"Cus{row.n}" / (name + ".parquet")
        if (
            row.instance_id != name
            or row.path != path.as_posix()
            or path in expected_files
        ):
            raise ValueError(f"Instance identity disagrees with plan: {path}")
        expected_files.add(path)
        dest = target / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        link_or_copy(root / path, dest)
        if sha(dest) != row.sha256:
            raise ValueError(f"Changed instance file: {path}")
        payload = file_header(dest)["payload"]
        info = payload["instance"]
        if payload["cle"] != binding or (
            info["instance_id"],
            info["split"],
            info["customer_count"],
            info["station_count"],
        ) != (name, split, row.n, row.cs_budget):
            raise ValueError(f"Wrong CLE, split or size in {path}")
    actual = {
        p.relative_to(root) for p in (root / "instances" / city).rglob("*.parquet")
    }
    if actual != expected_files:
        raise ValueError(f"Unexpected instance files: {city}")
    return slots


def merge(roots, dest):
    roots = [Path(r).resolve() for r in roots]
    dest = Path(dest).resolve()
    if dest.exists():
        raise FileExistsError(f"Choose a new dataset output directory: {dest}")
    cities = {}
    for root in roots:
        if not (root / "CLE").is_dir():
            raise ValueError(f"Missing CLE directory: {root}")
        for folder in sorted((root / "CLE").iterdir()):
            if folder.name.startswith("."):
                continue
            if (
                not folder.is_dir()
                or folder.name in cities
                or folder.name not in CITIES
            ):
                raise ValueError(f"Duplicate or unexpected city: {folder.name}")
            cities[folder.name] = root
    if set(cities) != set(CITIES):
        raise ValueError(f"Missing cities: {sorted(set(CITIES) - cities.keys())}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".dataset-") as tmp:
        stage = Path(tmp) / "dataset"
        scope = None
        tables, assets = [], {}
        for city in CITIES:
            root = cities[city]
            source = root / "CLE" / city
            authority = read(source / "public_authority.json")
            if authority["schema"] != "evrptwd_local_city_v1" or authority[
                "calibration_cohort"
            ] != [city]:
                raise ValueError(f"Expected a city-only fit: {city}")
            cle = stage / "CLE" / city
            shutil.copytree(source, cle, copy_function=link_or_copy)
            binding = verify_city(cle, city)
            plan = root / "reports" / city / "plan.json"
            panel = read(plan)
            contract = dict(
                city_contract(city, panel),
                stage1_engine_sha256=authority["engine_sha256"],
            )
            if scope is not None and contract != scope:
                raise ValueError(f"Different release methods: {city}")
            scope = contract
            if (
                panel["shared_sha256"],
                panel["input_vintage"],
                panel["calibration_sha256"],
            ) != (
                binding["shared_sha256"],
                authority["inputs_sha256"],
                binding["calibration_sha256"],
            ):
                raise ValueError(f"Plan differs from CLE: {city}")
            tables.append(collect_slots(root, city, stage, panel, binding))
            report = stage / "reports" / city
            report.mkdir(parents=True)
            evidence = root / "reports" / city / "provenance"
            cal_files = read(evidence / "calibration-ready.json")["files"]
            model = read(evidence / "calibration.json")
            if (
                sha(evidence / "inventory.json") != authority["inputs_sha256"]
                or digest(cal_files) != binding["calibration_sha256"]
                or model["cities"] != [city]
                or any(
                    sha(evidence / f) != cal_files["units/" + f]
                    for f in ("metric_scales.csv", "quality_thresholds.json")
                )
            ):
                raise ValueError(f"Different city source or fit in provenance: {city}")
            shutil.copytree(evidence, report / "provenance", copy_function=link_or_copy)
            shutil.copyfile(plan, report / "plan.json")
            assets[city] = dict(
                **binding,
                inputs_sha256=authority["inputs_sha256"],
                plan_sha256=sha(plan),
            )
        slots = pd.concat(tables, ignore_index=True)
        index = slots.loc[slots.status.eq("protocol_certified")].copy()
        if index.instance_id.duplicated().any():
            raise ValueError("Duplicate certified instance ID across cities")
        slots.to_parquet(stage / "slots.parquet", index=False)
        index.to_parquet(stage / "index.parquet", index=False)
        summary = dict(
            schema="evrptwd_independent_cities_v1",
            planned=len(slots),
            published=len(index),
            counts={str(k): int(v) for k, v in slots.status.value_counts().items()},
            contract=scope,
            cities=assets,
        )
        write(stage / "release.json", summary)
        (stage / "README.md").write_text(
            "---\nconfigs:\n"
            "- config_name: instances\n  default: true\n"
            "  data_files:\n  - split: all\n    path: index.parquet\n"
            "- config_name: slots\n  data_files:\n  - split: all\n    path: slots.parquet\n---\n\n"
            "# EVRPTW-D\n\n"
            f"The index contains {len(index):,} certified instances from {len(cities)} cities. "
            f"The slots table records the outcome of all {len(slots):,} registered tasks.\n\n"
            "Filter the index by city, split, and customer count (n). Each path identifies "
            "one solver-ready Parquet shared by CVRP, VRPTW, and EVRPTW. "
            "City road assets are in CLE/<city>/. "
            "See [generation methods and source credits](https://github.com/WaitDumplings/EVRPTW-D-HF).\n",
            encoding="utf-8",
        )
        stage.rename(dest)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards", nargs="+", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = merge(args.shards, args.out)
    print(
        f"{result['published']} published / {result['planned']} registered slots supplied to {args.out}"
    )
