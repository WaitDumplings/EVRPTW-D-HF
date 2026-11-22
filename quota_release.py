"""Assemble a quota-complete public release from certified city instances."""

import argparse
import errno
import itertools
import json
import os
import shutil
import tempfile
import zipfile
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pandas as pd

from export_output import city_inventory, verify_published_archive
from instance_file import (
    FORMAT,
    HF_FORMAT,
    RULES,
    file_header,
    read_portable,
    write_instance,
)
from solver_parquet import HF_INFO_FIELDS, source_nodes_and_info
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
from stage2.plan import QUOTA, city_contract, depot_order

PROVENANCE = (
    "inventory.json",
    "calibration.json",
    "calibration-ready.json",
    "quality_thresholds.json",
    "metric_scales.csv",
    "public_sources.json",
)
CONFIG = PROJECT / "config"
FIXED_DEPOT_CAPACITY = {"insufficient_depot_capacity", "empty_ratio_support"}
CAPACITY = FIXED_DEPOT_CAPACITY | {
    "insufficient_reachable_frontier_capacity",
    "insufficient_tracts_for_seed_count",
}
META = "_release"
SEALED_COLUMNS = (
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
HF_COLUMNS = (
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
HF_CONFIG = (
    "amazon_solver_fields.json",
    "candidate_ratio.csv",
    "customer_profile.json",
    "manifest.json",
    "order_profile.json",
    "public_sources.json",
    "stage1_method.json",
    "vehicle.json",
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


def solver_input_hash(nodes, info):
    return digest(
        [
            nodes.drop(["instance_id"]).to_pylist(),
            info["matrix_sha256_json"],
            info["vehicle_json"],
            info["path_policy"],
            info["objective"],
        ]
    )


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
        solver_input_sha256=solver_input_hash(nodes, info),
        **{k: activation[k] for k in ("p", "K_seed", "K_geo", "R", "M_target")},
        M=footprint["M"],
        tract_count=footprint["T"],
    )
    return row, instance


def validate_rows(city, df, columns=SEALED_COLUMNS):
    require(tuple(df.columns) == columns, f"Wrong index schema: {city}")
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
    draw_capacity_slot(
        work, output, panel, parent_hash, task, orig["reason"], args.attempts
    )


def draw_capacity_slot(work, output, panel, parent_hash, task, reason, attempts=None):
    """Advance this logical slot's deterministic draw stream; keep completed attempts on resume."""
    from export_output import publish_instance
    from stage2.batch import run

    city = task["city"]
    rel = Path(city) / task["split"] / f"Cus{task['n']}" / f"instance{task['slot']:04d}"
    home = work / "quota-draws" / rel
    depot = task["depot"]
    alternatives = None
    with lock(home / "draw.lock"):
        for attempt in itertools.count(1):
            if attempts is not None and attempt > attempts:
                raise ValueError(
                    f"No accepted draw in attempts 1..{attempts}; records: {home}"
                )
            if reason in FIXED_DEPOT_CAPACITY:
                if alternatives is None:
                    loc = pd.read_parquet(
                        work / "shared" / city / "locations.parquet",
                        columns=["location_type", "bg_geoid"],
                    )
                    ordered = depot_order(loc, panel["root_seed"], city)
                    start = ordered.index(depot)
                    alternatives = iter(ordered[start + 1 :] + ordered[:start])
                depot = next(alternatives, None)
                require(
                    depot is not None,
                    f"No depot supports {city}/{task['day_type']}/Cus{task['n']}; slot {task['slot']}",
                )
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
            if depot != task["depot"]:
                actual.update(
                    depot=depot,
                    depot_bg=str(loc.at[depot, "bg_geoid"]),
                    task_key=task["task_key"].copy(),
                )
                actual["task_key"][4] = depot
            tasks = [actual]
            new_plan = dict(panel, tasks=tasks, tasks_sha256=digest(tasks))
            plan_file = job / "draw.json"
            if plan_file.exists():
                require(
                    read(plan_file) == new_plan, f"Different saved draw: {plan_file}"
                )
            else:
                write(plan_file, new_plan)
            record = job / "ledger" / rel / "result.json"
            result = read(record) if record.exists() else None
            if result is not None:
                require(
                    result["signature"] == digest([sha(plan_file), actual])
                    and result["task"] == actual,
                    f"Previous draw refers to a different task: {record}",
                )
            if result is None or result["status"] in {
                "execution_error",
                "protocol_certified",
            }:
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
                result = read(record)
            if (
                result["status"] == "generation_failed"
                and result.get("reason") in CAPACITY
            ):
                reason = result["reason"]
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
                "split": public_split(city, task["split"]),
                "n": task["n"],
                "slot": task["slot"],
                "receipt": str(record),
                "archive": str(archive),
                "plan": str(plan_file),
            }
            if depot != task["depot"]:
                choice["capacity_fallback_depot"] = depot
            write(home / "choice.json", choice)
            print(home / "choice.json")
            return choice


def seal(args, *, fill_capacity=False):
    city, output, work = args.city, args.output.resolve(), args.work.resolve()
    with lock(output / ".locks" / f"{city}-quota.lock"):
        seal_city(city, output, work, args.choices, fill_capacity)


def read_city_index(folder, city):
    meta = read(folder / "city.json")
    index = folder / "index.parquet"
    require(
        meta["city"] == meta["binding"]["city"] == city
        and sha(index) == meta["index_sha256"],
        f"Changed manifest: {city}",
    )
    df = pd.read_parquet(index)
    schema = meta.get("instance_schema", FORMAT)
    require(schema in (FORMAT, HF_FORMAT), f"Unknown shard format: {schema}")
    validate_rows(city, df, HF_COLUMNS if schema == HF_FORMAT else SEALED_COLUMNS)
    return meta, df


def verified_assets(output, city, inventory, authority, binding):
    assets = {f"CLE/{city}/{p.as_posix()}": v[1] for p, v in inventory.items()}
    evidence = output / "reports" / city / "provenance"
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

    return assets


def seal_city(city, output, work, choices, fill_capacity):
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
    assets = verified_assets(output, city, inventory, authority, binding)
    target = output / META / city
    if fill_capacity and target.exists():
        # An accepted legacy city can include externally generated supplements.
        # Its final index is authoritative for quota accounting.
        meta, df = read_city_index(target, city)
        require(
            meta["contract"] == contract
            and meta["binding"] == binding
            and meta["files"] == assets,
            f"Different sealed city or method: {city}",
        )
        for row in df.itertuples(index=False):
            require(
                sha(output / row.path) == row.sha256,
                f"Changed sealed instance: {row.path}",
            )
        listed = (target / "files.txt").read_text().splitlines()
        expected = sorted(
            [
                *meta["files"],
                *df.path,
                f"{META}/{city}/city.json",
                f"{META}/{city}/index.parquet",
            ]
        )
        require(sorted(listed) == expected, f"Different sealed transfer list: {city}")
        print(f"{city}: reusing {len(df)} sealed instances")
        return
    vehicle = read(CONFIG / "vehicle.json")
    overrides = {}
    city_choices = work / "quota-draws" / city / "choices.json"
    if choices is None and city_choices.is_file():
        choices = city_choices
    if choices:
        for choice in read(choices):
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
        if (
            fill_capacity
            and choice is None
            and original["status"] == "generation_failed"
            and original.get("reason") in CAPACITY
        ):
            choice = draw_capacity_slot(
                work, output, plan, original_plan_sha, task, original["reason"]
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
        if fill_capacity and not portable.exists():
            from export_output import publish_instance

            require(
                publish_instance(archive, output) == portable,
                f"Certified archive did not export to the expected path: {rel}",
            )
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
                or choice.get("reviewed_existing_depot_date_change") is True
                or (
                    default_choice.is_file()
                    and choice.get("capacity_fallback_depot") == new_task["depot"]
                    and new_task["date"] == task["date"]
                ),
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
    df = pd.DataFrame(rows, columns=SEALED_COLUMNS).sort_values(
        ["split", "n", "instance_id"]
    )
    validate_rows(city, df)

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


def install_asset(source, target, expected):
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        try:
            os.link(source, target)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
                copy = Path(tmp) / "asset"
                shutil.copyfile(source, copy)
                require(sha(copy) == expected, f"Checksum mismatch: {source}")
                os.link(copy, target)
            return
    require(sha(target) == expected, f"Checksum mismatch: {target}")


def project_instance(source, target, row, binding):
    require(sha(source) == row.sha256, f"Checksum mismatch: {source}")
    header, task = read_portable(source)
    require(
        header["schema"] == FORMAT
        and header["content_sha256"] == row.content_sha256
        and header["payload"]["cle"] == binding
        and header["payload"]["rules"] == RULES
        and task.info["instance_id"] == row.instance_id,
        f"Sealed instance mismatch: {source}",
    )
    info = {key: task.info[key] for key in HF_INFO_FIELDS}
    require(
        solver_input_hash(task.nodes, info) == row.solver_input_sha256,
        f"Solver input mismatch: {source}",
    )
    write_instance(target, replace(task, info=info), binding, schema=HF_FORMAT)
    # The reader recomputes the ordered node digest and all four matrix digests.
    published, _ = read_portable(target)
    require(
        published["schema"] == HF_FORMAT
        and published["payload"] == {"instance": info, "cle": binding, "rules": RULES},
        f"Published instance differs: {target}",
    )
    return target.stat().st_size, sha(target), published["content_sha256"]


def city_assets(source, city, meta):
    base = source / "CLE" / city
    binding, inventory = city_inventory(base, base / "shared", city)
    files = {f"CLE/{city}/{p}": value[1] for p, value in inventory.items()}
    for name in PROVENANCE:
        path = f"reports/{city}/provenance/{name}"
        files[path] = meta["files"][path]
    require(
        binding == meta["binding"] and files == meta["files"],
        f"Different city assets: {city}",
    )
    return files


def write_hf_city(source, dest, city, meta, df, *, final):
    for path, expected in city_assets(source, city, meta).items():
        rel = Path(path)
        if final and rel.parts[0] == "reports":
            rel = Path("parameters/cities") / city / rel.name
        install_asset(source / path, dest / rel, expected)

    result = df.loc[:, HF_COLUMNS].copy()
    projected = meta.get("instance_schema") == HF_FORMAT
    for row in df.itertuples():
        src, target = source / row.path, dest / row.path
        if projected:
            install_asset(src, target, row.sha256)
            head, task = read_portable(target)
            info = task.info
            require(
                head["schema"] == HF_FORMAT
                and head["content_sha256"] == row.content_sha256
                and head["payload"]["cle"] == meta["binding"]
                and all(
                    info[k] == getattr(row, k)
                    for k in (
                        "instance_id",
                        "city",
                        "split",
                        "day_type",
                        "service_date",
                        "depot_id",
                    )
                )
                and info["customer_count"] == row.n
                and info["station_count"] == row.cs_budget
                and solver_input_hash(task.nodes, info) == row.solver_input_sha256
                and target.stat().st_size == row.bytes,
                f"Wrong public instance: {target}",
            )
        else:
            result.loc[row.Index, ["bytes", "sha256", "content_sha256"]] = (
                project_instance(src, target, row, meta["binding"])
            )
    return result.sort_values(["split", "n", "instance_id"]).reset_index(drop=True)


def project(args):
    source, dest, city = args.output.resolve(), args.out.resolve(), args.city
    require(
        source != dest
        and not dest.is_relative_to(source)
        and not source.is_relative_to(dest),
        "Choose a separate public shard root",
    )
    folder = source / META / city
    meta, df = read_city_index(folder, city)
    require(
        meta.get("instance_schema", FORMAT) == FORMAT, "Expected a sealed source city"
    )
    require(
        meta["contract"]["resource_sha256"] == config_hashes(),
        "Different generation config",
    )
    with lock(dest / ".locks" / f"{city}.lock"):
        target = dest / META / city
        if target.exists():
            previous, _ = read_city_index(target, city)
            require(
                previous.get("sealed_index_sha256") == meta["index_sha256"],
                f"Different sealed source for existing shard: {city}",
            )
        frame = write_hf_city(source, dest, city, meta, df, final=False)
        validate_rows(city, frame, HF_COLUMNS)
        parent = target.parent
        parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as tmp:
            ready = Path(tmp) / city
            ready.mkdir()
            frame.to_parquet(ready / "index.parquet", index=False)
            transfer = {k: meta[k] for k in ("city", "files", "binding", "contract")}
            transfer.update(
                instance_schema=HF_FORMAT,
                sealed_index_sha256=meta["index_sha256"],
                index_sha256=sha(ready / "index.parquet"),
            )
            write(ready / "city.json", transfer)
            paths = sorted(
                [
                    *meta["files"],
                    *frame.path,
                    f"{META}/{city}/index.parquet",
                    f"{META}/{city}/city.json",
                ]
            )
            (ready / "files.txt").write_text("\n".join(paths) + "\n")
            if target.exists():
                require(
                    previous == transfer
                    and (target / "files.txt").read_bytes()
                    == (ready / "files.txt").read_bytes(),
                    f"Changed public shard: {city}",
                )
            else:
                ready.rename(target)
    print(
        f"{city}: {len(frame)} public instances; transfer list {target / 'files.txt'}"
    )


def city_catalogs(shards):
    catalogs = {}
    for root in shards:
        for p in (root / META).glob("*/city.json"):
            city = p.parent.name
            require(
                city in CITIES and city not in catalogs,
                f"Unexpected/duplicate city: {city}",
            )
            meta, df = read_city_index(p.parent, city)
            catalogs[city] = root, meta, df
    require(
        set(catalogs) == set(CITIES), f"Missing cities: {set(CITIES) - catalogs.keys()}"
    )
    return catalogs


def merge(args):
    catalogs = city_catalogs(args.shards)
    accepted_roots = getattr(args, "accepted_shards", None)
    if not args.check_only and any(
        m.get("instance_schema") == HF_FORMAT for _, m, _ in catalogs.values()
    ):
        require(accepted_roots, "Supply --accepted-shards for the final public merge")
    if accepted_roots:
        accepted = city_catalogs(accepted_roots)
        stable = [
            k for k in HF_COLUMNS if k not in ("bytes", "sha256", "content_sha256")
        ]
        for city, (_, meta, df) in catalogs.items():
            _, original, old = accepted[city]
            require(
                meta.get("instance_schema") == HF_FORMAT
                and original.get("instance_schema", FORMAT) == FORMAT
                and meta["sealed_index_sha256"] == original["index_sha256"]
                and all(
                    meta[k] == original[k] for k in ("contract", "binding", "files")
                )
                and df[stable]
                .set_index("instance_id")
                .sort_index()
                .equals(old[stable].set_index("instance_id").sort_index()),
                f"Public shard differs from accepted panel: {city}",
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
        frames = [
            write_hf_city(root, stage, city, meta, df, final=True)
            for city, (root, meta, df) in catalogs.items()
        ]
        data = pd.concat(frames, ignore_index=True).sort_values(
            ["city", "split", "n", "instance_id"], ignore_index=True
        )
        (stage / "config").mkdir()
        for name in HF_CONFIG:
            shutil.copyfile(CONFIG / name, stage / "config" / name)
        for name in ("LICENSE.md", "NOTICE.md"):
            shutil.copyfile(PROJECT / name, stage / name)
        (stage / "docs").mkdir()
        shutil.copyfile(
            PROJECT / "docs/SOURCES.zh-CN.md", stage / "docs/SOURCES.zh-CN.md"
        )
        data.to_parquet(stage / "index.parquet", index=False)
        write(
            stage / "release.json",
            {
                "instances": len(data),
                "instance_schema": HF_FORMAT,
                "contract": {
                    k: v
                    for k, v in next(iter(catalogs.values()))[1]["contract"].items()
                    if k
                    in (
                        "version",
                        "code_sha256",
                        "stage1_engine_sha256",
                        "calendar_start",
                        "calendar_days",
                    )
                },
                "config_sha256": {
                    name: sha(stage / "config" / name) for name in HF_CONFIG
                },
                "cities": {c: m["binding"] for c, (_, m, _) in catalogs.items()},
                "index_sha256": sha(stage / "index.parquet"),
            },
        )
        (stage / "README.md").write_text(
            "---\nconfigs:\n- config_name: instances\n  data_files:\n  - split: catalog\n    path: index.parquet\n"
            "license: other\n"
            "license_name: evrptw-d-mixed\n"
            "license_link: LICENSE.md\n"
            "pretty_name: EVRPTW-D\n"
            "language: [en]\n"
            "size_categories: [10K<n<100K]\n"
            "tags: [vehicle-routing, evrptw, vrptw, cvrp, combinatorial-optimization, road-network]\n---\n"
            f"# EVRPTW-D\n\n{len(data):,} accepted tasks in {len(catalogs)} cities. Filter index.parquet by city, split and n. "
            "Each instance file provides the same depot, customers and realized demands for the CVRP, VRPTW and EVRPTW views. "
            "Download the file in the index row's `path`. It contains ordered nodes, vehicle settings and four directed road matrices. "
            "Instances are sampled subject to customer availability and a common construction protocol. "
            "R is the target candidate-to-customer ratio. M is the realized candidate count. "
            "Vehicle and generator profiles are in config/, and city calibrations in parameters/cities/. "
            "See the [generator and data credits](https://github.com/WaitDumplings/EVRPTW-D-HF).\n"
            "\n## License\n\n"
            "Mixed license. OpenStreetMap-derived road networks, travel matrices and location coordinates are under ODbL 1.0. "
            "Order attributes, instance composition, the catalog and documentation are under CC BY-NC 4.0. "
            "A complete instance may be used for non-commercial purposes only. See [LICENSE.md](LICENSE.md).\n"
        )
        stage.rename(dest)
    print(dest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for command in ("seal", "fill"):
        p = subs.add_parser(command)
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
    p = subs.add_parser("project", help="Prepare one accepted city for Hugging Face")
    p.add_argument(
        "--output", type=Path, required=True, help="Existing sealed production root"
    )
    p.add_argument("--out", type=Path, required=True, help="Separate public shard root")
    p.add_argument("--city", choices=CITIES, required=True)
    p = subs.add_parser("merge")
    p.add_argument("--shards", nargs="+", type=Path, required=True)
    p.add_argument(
        "--accepted-shards",
        nargs="+",
        type=Path,
        help="Cross-check public shards against accepted v1 manifest roots",
    )
    p.add_argument("--out", type=Path)
    p.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    if args.command in {"fill", "seal"}:
        seal(args, fill_capacity=args.command == "fill")
    else:
        {"merge": merge, "draw": draw, "project": project}[args.command](args)
