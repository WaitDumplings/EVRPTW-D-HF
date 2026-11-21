"""Assemble a quota-complete public release from certified city instances."""

import argparse
import json
import os
import shutil
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

import pandas as pd

from export_output import city_inventory, verify_published_archive
from instance_file import RULES, file_header
from solver_parquet import source_nodes_and_info
from stage2.common import (
    BUDGET,
    CITIES,
    PROJECT,
    digest,
    lock,
    public_split,
    read,
    sha,
    write,
)
from stage2.plan import QUOTA, city_contract

PROVENANCE = (
    "inventory.json",
    "calibration.json",
    "calibration-ready.json",
    "quality_thresholds.json",
    "metric_scales.csv",
    "public_sources.json",
)
CONFIG = PROJECT / "config"
CAPACITY = {
    "insufficient_reachable_frontier_capacity",
    "insufficient_depot_capacity",
    "empty_ratio_support",
}
META = "_release"
PUBLIC_COLUMNS = (
    "city",
    "split",
    "n",
    "cs_budget",
    "instance_id",
    "path",
    "bytes",
    "sha256",
    "service_date",
    "day_type",
    "depot_id",
    "seed",
    "p",
    "K_seed",
    "K_geo",
    "R",
    "M_target",
    "M",
    "tract_count",
    "content_sha256",
    "solver_input_sha256",
)


def require(ok, message):
    if not ok:
        raise ValueError(message)


def paths_for(city, task):
    return public_split(city, task["split"]), task["n"], task["slot"]


def quotas(city):
    return {
        (s, n): count
        for s, scales in QUOTA.items()
        if (city == "jacksonville") == (s == "test2")
        for n, count in scales.items()
    }


def config_hashes():
    return {p.name: sha(p) for p in CONFIG.iterdir() if p.is_file()}


def describe(archive, portable, receipt, binding, expected_vehicle):
    with zipfile.ZipFile(archive) as z:
        root = zipfile.Path(z)
        nodes, info = source_nodes_and_info(root)
        activation = json.loads(root.joinpath("customer_activation.json").read_text())
        instance = json.loads(root.joinpath("instance.json").read_text())
    header = file_header(portable)
    payload = header["payload"]
    require(
        payload["instance"] == info
        and payload["cle"] == binding
        and payload["rules"] == RULES,
        f"Parquet differs from source: {portable}",
    )
    require(
        info["source_protocol_status"] == receipt["status"] == "protocol_certified"
        and info["instance_id"] == receipt["instance_id"]
        and json.loads(info["vehicle_json"]) == expected_vehicle,
        f"Wrong receipt/vehicle: {portable}",
    )
    task = receipt["task"]
    require(
        all(instance[k] == v for k, v in task.items())
        and receipt["signature"] == digest([instance["plan_sha256"], task]),
        f"Source task/receipt mismatch: {archive}",
    )
    footprint = activation["footprint"]
    row = {
        k: info[k]
        for k in (
            "city",
            "split",
            "instance_id",
            "service_date",
            "day_type",
            "depot_id",
            "seed",
        )
    }
    row.update(
        n=info["customer_count"],
        cs_budget=info["station_count"],
        bytes=portable.stat().st_size,
        content_sha256=header["content_sha256"],
        solver_input_sha256=digest(
            [
                nodes.drop(["instance_id"]).to_pylist(),
                info["matrix_sha256_json"],
                info["vehicle_json"],
                info["path_policy"],
                info["objective"],
            ]
        ),
        **{k: activation[k] for k in ("p", "K_seed", "K_geo", "R", "M_target")},
        M=footprint["M"],
        tract_count=footprint["T"],
    )
    return row, instance


def validate_rows(city, df):
    require(tuple(df.columns) == PUBLIC_COLUMNS, f"Wrong index schema: {city}")
    require(
        df.groupby(["split", "n"]).size().to_dict() == quotas(city),
        f"Wrong quotas: {city}",
    )
    require(
        df.city.eq(city).all() and df.n.map(BUDGET).eq(df.cs_budget).all(),
        f"Wrong city/CS: {city}",
    )
    for k in ("instance_id", "path", "solver_input_sha256"):
        require(not df[k].duplicated().any(), f"Duplicate {k}: {city}")
    for r in df.itertuples(index=False):
        path = (
            Path("instances")
            / city
            / r.split
            / f"Cus{r.n}"
            / f"{r.instance_id}.parquet"
        )
        require(
            len(r.instance_id) == 24
            and all(c in "0123456789abcdef" for c in r.instance_id)
            and path.as_posix() == r.path,
            f"Invalid instance path: {r.path}",
        )


def draw(args):
    """Generate only missing certified content for one recorded capacity failure."""
    from export_output import publish_instance
    from stage2.batch import run

    work, output, city = args.work.resolve(), args.output.resolve(), args.city
    plan_path = work / "plans" / (city + ".json")
    panel = read(plan_path)
    city_contract(city, panel)
    key = (args.split, args.n, args.slot)
    task = next((t for t in panel["tasks"] if paths_for(city, t) == key), None)
    require(task is not None, f"Unknown slot: {key}")
    rel = Path(city) / task["split"] / f"Cus{args.n}" / f"instance{args.slot:04d}"
    orig = read(work / "ledger" / rel / "result.json")
    parent_hash = sha(plan_path)
    require(
        orig["signature"] == digest([parent_hash, task])
        and orig["task"] == task
        and orig["status"] == "generation_failed"
        and orig.get("reason") in CAPACITY,
        "Only an identified capacity rejection is eligible",
    )
    require(
        orig["reason"] != "insufficient_depot_capacity"
        and orig["reason"] != "empty_ratio_support",
        "This depot's eligible supply or R support is fixed; report the slot separately",
    )
    home = work / "quota-draws" / rel
    with lock(home / "draw.lock"):
        for attempt in range(1, args.attempts + 1):
            job = home / str(attempt)
            actual = dict(
                task,
                seed=int(
                    digest(["quota-fill-v1", parent_hash, task["task_key"], attempt])[
                        :15
                    ],
                    16,
                ),
            )
            tasks = [actual]
            new_plan = dict(panel, tasks=tasks, tasks_sha256=digest(tasks))
            plan_file = job / "draw.json"
            if plan_file.exists():
                require(
                    read(plan_file) == new_plan, f"Different saved draw: {plan_file}"
                )
            else:
                write(plan_file, new_plan)
            run(
                plan_file,
                work / "shared" / city,
                work / "public/amazon",
                job,
                1,
                None,
                None,
                None,
                "0/1",
            )
            record = job / "ledger" / rel / "result.json"
            result = read(record)
            if (
                result["status"] == "generation_failed"
                and result.get("reason") in CAPACITY
            ):
                continue
            require(
                result["status"] == "protocol_certified",
                f"Inspect {record}: {result['status']}",
            )
            archive = job / result["file"]
            require(
                sha(archive) == result["sha256"],
                f"Changed certified archive: {archive}",
            )
            publish_instance(archive, output)
            choice = {
                "split": args.split,
                "n": args.n,
                "slot": args.slot,
                "receipt": str(record),
                "archive": str(archive),
                "plan": str(plan_file),
            }
            write(home / "choice.json", choice)
            print(home / "choice.json")
            return
        raise ValueError(
            f"No accepted draw in attempts 1..{args.attempts}; records: {home}"
        )


def seal(args):
    city, output, work = args.city, args.output.resolve(), args.work.resolve()
    plan_path = output / "reports" / city / "plan.json"
    plan = read(plan_path)
    contract = city_contract(city, plan)
    require(contract["resource_sha256"] == config_hashes(), "Use the production config")
    cle = output / "CLE" / city
    binding, inventory = city_inventory(cle, cle / "shared", city)
    authority = read(cle / "public_authority.json")
    require(
        authority["calibration_cohort"] == [city]
        and binding["shared_sha256"] == plan["shared_sha256"]
        and binding["calibration_sha256"] == plan["calibration_sha256"]
        and authority["inputs_sha256"] == plan["input_vintage"],
        f"Wrong city fit: {city}",
    )
    contract["stage1_engine_sha256"] = authority["engine_sha256"]
    vehicle = read(CONFIG / "vehicle.json")
    overrides = {}
    if args.choices:
        for choice in read(args.choices):
            key = choice["split"], choice["n"], choice["slot"]
            require(key not in overrides, f"Duplicate choice: {key}")
            overrides[key] = choice

    first = pd.read_parquet(plan_path.parent / "slots.parquet").set_index(
        ["split", "n", "slot"], verify_integrity=True
    )
    require(len(first) == len(plan["tasks"]), f"Incomplete original slots: {city}")
    original_plan_sha = sha(plan_path)
    rows, activities = [], Counter()
    for task in plan["tasks"]:
        key = paths_for(city, task)
        initial = first.loc[key]
        require(
            initial.city == city
            and initial.seed == task["seed"]
            and initial.cs_budget == task["cs_budget"],
            f"Original slot differs: {key}",
        )
        choice = overrides.pop(key, None)
        default_choice = (
            work
            / "quota-draws"
            / city
            / task["split"]
            / f"Cus{task['n']}"
            / f"instance{task['slot']:04d}"
            / "choice.json"
        )
        if default_choice.exists():
            saved_choice = read(default_choice)
            require(
                choice is None or choice == saved_choice,
                f"Multiple selections for {key}",
            )
            choice = saved_choice
        home = Path(city) / task["split"] / f"Cus{task['n']}"
        orig_receipt = (
            work / "ledger" / home / f"instance{task['slot']:04d}" / "result.json"
        )
        original = read(orig_receipt)
        require(
            original["signature"] == digest([original_plan_sha, task])
            and original["task"] == task
            and original["status"] == initial.status,
            f"Original receipt mismatch: {key}",
        )
        if choice is None:
            receipt = original
            require(
                receipt["status"] == "protocol_certified",
                f"Provide a certified choice for {city}/{key}",
            )
            archive = work / receipt["file"]
        else:
            require(
                initial.status == "generation_failed"
                and original.get("reason") in CAPACITY,
                f"Unexpected original failure or already certified: {key}",
            )
            receipt = read(Path(choice["receipt"]))
            archive = Path(choice["archive"])
        require(sha(archive) == receipt["sha256"], f"Changed archive: {archive}")
        name = receipt["instance_id"]
        require(
            len(name) == 24 and all(c in "0123456789abcdef" for c in name),
            f"Invalid ID: {name}",
        )
        rel = Path("instances") / city / key[0] / f"Cus{key[1]}" / (name + ".parquet")
        portable = output / rel
        row, instance = describe(archive, portable, receipt, binding, vehicle)
        require(
            (row["city"], row["split"], row["n"], row["cs_budget"], row["day_type"])
            == (city, key[0], key[1], task["cs_budget"], task["day_type"]),
            f"Different quota/day type: {key}",
        )
        require(instance["method_version"] == plan["version"], f"Wrong method: {key}")
        if choice:
            actual_plan_path = Path(choice["plan"])
            actual_plan = read(actual_plan_path)
            require(
                sha(actual_plan_path) == instance["plan_sha256"]
                and actual_plan["tasks_sha256"] == digest(actual_plan["tasks"])
                and all(
                    actual_plan[k] == plan[k]
                    for k in (
                        "city",
                        "version",
                        "resource_sha256",
                        "code_sha256",
                        "shared_sha256",
                        "calibration_sha256",
                        "input_vintage",
                        "runtime",
                    )
                )
                and receipt["task"] in actual_plan["tasks"],
                f"Replacement plan/method differs: {key}",
            )
            new_task = receipt["task"]
            changed = (new_task["depot"], new_task["date"]) != (
                task["depot"],
                task["date"],
            )
            require(
                not changed
                or choice.get("reviewed_existing_depot_date_change") is True,
                f"Review existing depot/date difference: {key}",
            )
            require(
                len(new_task["task_key"]) == 7
                and new_task["task_key"][:3] == [city, task["task_key"][1], task["n"]]
                and new_task["task_key"][4:]
                == [new_task["depot"], new_task["date"], new_task["day_type"]],
                f"Nonstandard task key: {key}",
            )
            verify_published_archive(archive, portable, binding)
            activities[key[0], key[1], original["reason"], changed] += 1
        else:
            require(
                initial.path == rel.as_posix() and initial.instance_id == name,
                f"Wrong first export: {key}",
            )
        row.update(path=rel.as_posix(), sha256=sha(portable))
        if choice is None:
            require(row["sha256"] == initial.sha256, f"Original Parquet changed: {rel}")
        rows.append(row)
    require(not overrides, f"Unexpected choices: {list(overrides)}")
    df = pd.DataFrame(rows, columns=PUBLIC_COLUMNS).sort_values(
        ["split", "n", "instance_id"]
    )
    validate_rows(city, df)
    assets = {f"CLE/{city}/{p.as_posix()}": v[1] for p, v in inventory.items()}
    evidence = plan_path.parent / "provenance"
    declared = read(evidence / "calibration-ready.json")["files"]
    require(
        sha(evidence / "inventory.json") == authority["inputs_sha256"]
        and digest(declared) == binding["calibration_sha256"]
        and read(evidence / "calibration.json")["cities"] == [city]
        and all(
            sha(evidence / f) == declared["units/" + f]
            for f in ("metric_scales.csv", "quality_thresholds.json")
        ),
        f"Different model inputs: {city}",
    )
    for f in PROVENANCE:
        assets[f"reports/{city}/provenance/{f}"] = sha(evidence / f)
    for path, expected in assets.items():
        require(sha(output / path) == expected, f"Changed city asset: {path}")

    meta = Path(META) / city
    parent = output / META
    parent.mkdir(exist_ok=True)
    target = output / meta
    with tempfile.TemporaryDirectory(dir=parent) as tmp:
        stage = Path(tmp) / "metadata"
        stage.mkdir()
        df.to_parquet(stage / "index.parquet", index=False)
        write(
            stage / "city.json",
            {
                "city": city,
                "binding": binding,
                "contract": contract,
                "original_capacity_rejections": [
                    {
                        "split": split,
                        "n": n,
                        "reason": reason,
                        "changed_depot_or_date": changed,
                        "count": count,
                    }
                    for (split, n, reason, changed), count in sorted(activities.items())
                ],
                "files": assets,
                "index_sha256": sha(stage / "index.parquet"),
            },
        )
        files = sorted(
            [*assets, *df.path, f"{meta}/city.json", f"{meta}/index.parquet"]
        )
        (stage / "files.txt").write_text("\n".join(files) + "\n")
        if target.exists():
            require(
                all(
                    sha(stage / f) == sha(target / f)
                    for f in ("index.parquet", "city.json", "files.txt")
                ),
                f"Previously sealed metadata differs: {target}",
            )
        else:
            stage.rename(target)
    print(f"{city}: {len(df)} instances; rsync list: {target / 'files.txt'}")
    for cell, count in sorted(activities.items()):
        print(
            "Original capacity rejections (split, n, reason, depot/date changed):",
            cell,
            count,
        )


def merge(args):
    catalogs = {}
    for root in args.shards:
        for p in (root / META).glob("*/city.json"):
            city = p.parent.name
            require(
                city in CITIES and city not in catalogs,
                f"Unexpected/duplicate city: {city}",
            )
            meta = read(p)
            index = p.parent / "index.parquet"
            require(
                meta["city"] == city
                and meta["binding"]["city"] == city
                and sha(index) == meta["index_sha256"],
                f"Changed manifest: {city}",
            )
            df = pd.read_parquet(index)
            validate_rows(city, df)
            catalogs[city] = root, meta, df
    require(
        set(catalogs) == set(CITIES), f"Missing cities: {set(CITIES) - catalogs.keys()}"
    )
    data = pd.concat([catalogs[city][2] for city in CITIES], ignore_index=True)
    require(
        len(data) == sum(sum(quotas(city).values()) for city in CITIES)
        and data.instance_id.is_unique
        and data.solver_input_sha256.is_unique,
        "Incomplete or duplicate global panel",
    )
    contracts = {digest(m["contract"]) for _, m, _ in catalogs.values()}
    require(
        len(contracts) == 1
        and all(
            m["contract"]["resource_sha256"] == config_hashes()
            for _, m, _ in catalogs.values()
        ),
        "Different generation configurations",
    )
    if args.check_only:
        print(
            f"{len(catalogs)} cities, {len(data)} accepted, {data.bytes.sum():,} instance bytes"
        )
        return
    require(
        args.out is not None and not args.out.exists(),
        "Choose an unused release directory",
    )
    dest = args.out.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".quota-") as tmp:
        stage = Path(tmp) / "data"
        stage.mkdir()
        for city, (root, meta, df) in catalogs.items():
            files = dict(meta["files"])
            files.update(zip(df.path, df.sha256))
            for path, expected in files.items():
                rel = Path(path)
                require(
                    not rel.is_absolute()
                    and ".." not in rel.parts
                    and (
                        rel.parts[:2] in {("CLE", city), ("instances", city)}
                        or (
                            rel.parts[:3] == ("reports", city, "provenance")
                            and rel.name in PROVENANCE
                            and len(rel.parts) == 4
                        )
                    ),
                    f"Unexpected asset: {path}",
                )
                output_rel = (
                    Path("parameters/cities") / city / rel.name
                    if rel.parts[0] == "reports"
                    else rel
                )
                target = stage / output_rel
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(root / rel, target)
                require(sha(target) == expected, f"Checksum mismatch: {path}")
            binding, inventory = city_inventory(
                stage / "CLE" / city, stage / "CLE" / city / "shared", city
            )
            required = {
                f"CLE/{city}/{p.as_posix()}": v[1] for p, v in inventory.items()
            }
            require(
                binding == meta["binding"]
                and all(meta["files"].get(k) == v for k, v in required.items())
                and all(
                    f"reports/{city}/provenance/{f}" in meta["files"]
                    for f in PROVENANCE
                ),
                f"Missing city assets: {city}",
            )
        shutil.copytree(CONFIG, stage / "config")
        shutil.copyfile(PROJECT / "NOTICE.md", stage / "NOTICE.md")
        (stage / "docs").mkdir()
        shutil.copyfile(
            PROJECT / "docs/SOURCES.zh-CN.md", stage / "docs/SOURCES.zh-CN.md"
        )
        data.to_parquet(stage / "index.parquet", index=False)
        write(
            stage / "release.json",
            {
                "instances": len(data),
                "original_capacity_rejections": {
                    c: m["original_capacity_rejections"]
                    for c, (_, m, _) in catalogs.items()
                },
                "contract": next(iter(catalogs.values()))[1]["contract"],
                "cities": {c: m["binding"] for c, (_, m, _) in catalogs.items()},
                "index_sha256": sha(stage / "index.parquet"),
            },
        )
        (stage / "README.md").write_text(
            "---\nconfigs:\n- config_name: instances\n  data_files:\n  - split: all\n    path: index.parquet\n---\n"
            f"# EVRPTW-D\n\n{len(data):,} accepted tasks in {len(catalogs)} cities. Filter index.parquet by city, split and n. "
            "Each instance file supports CVRP, VRPTW and EVRPTW. Construction samples must pass the same "
            "generation and certification criteria. Capacity rejections are resampled within the quota cell. "
            "An insufficient reachable candidate count is a construction rejection. Falling short of R times n alone is allowed when the footprint supports n customers. See release.json for first-draw rejection counts. "
            "R is the drawn candidate-to-customer ratio; M is the realized candidate count. "
            "Vehicle and generator profiles are in config/, and city calibrations in parameters/cities/. "
            "See the [generator and data credits](https://github.com/WaitDumplings/EVRPTW-D-HF).\n"
        )
        stage.rename(dest)
    print(dest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    p = subs.add_parser("seal")
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--city", choices=CITIES, required=True)
    p.add_argument("--choices", type=Path)
    p = subs.add_parser("draw")
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--city", choices=CITIES, required=True)
    p.add_argument("--split", choices=tuple(QUOTA), required=True)
    p.add_argument("--n", choices=tuple(BUDGET), type=int, required=True)
    p.add_argument("--slot", type=int, required=True)
    p.add_argument(
        "--attempts",
        type=int,
        required=True,
        help="Last attempt number in deterministic order",
    )
    p = subs.add_parser("merge")
    p.add_argument("--shards", nargs="+", type=Path, required=True)
    p.add_argument("--out", type=Path)
    p.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    {"seal": seal, "merge": merge, "draw": draw}[args.command](args)
