#!/usr/bin/env python3
"""EVRPTW-D city pipeline. Run any subcommand with --help."""

import argparse
import json
import os
from pathlib import Path
import sys

from stage2.common import CITIES, FROZEN, PROJECT, read

for thread_limit in (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
):
    os.environ[thread_limit] = "1"
sys.path.insert(0, str(FROZEN / "src"))


def positive(v):
    n = int(v)
    if n < 1:
        raise argparse.ArgumentTypeError("Expected a positive integer")
    return n


def cli(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--work",
        type=Path,
        default=Path("work"),
        help="Working directory for inputs, city assets, plans and instances",
    )
    subs = p.add_subparsers(dest="cmd", required=True)
    fetch = subs.add_parser(
        "sources", help="Download publisher sources and construct normalized inputs"
    )
    fetch.add_argument(
        "--only",
        choices=["all", "live", "stage1", "stage2", "cities", "afdc"],
        default="all",
    )
    fetch.add_argument("--city", nargs="+", choices=CITIES, default=list(CITIES))
    fetch.add_argument("--workers", type=positive, default=1)
    fetch.add_argument(
        "--osm-date",
        help="Geofabrik snapshot YYYY-MM-DD (default: source lock, otherwise 2026-11-01)",
    )
    fetch.add_argument(
        "--source-lock",
        type=Path,
        help="Verify raw responses against another host's small sources.lock.json",
    )
    s = subs.add_parser(
        "stage1",
        help="Build a CLE with housing estimates fitted independently within each city",
    )
    s.add_argument("--city", nargs="+", choices=CITIES, default=list(CITIES))
    s.add_argument("--workers", type=positive, default=1)
    s.add_argument("--inputs", type=Path)
    c = subs.add_parser(
        "prepare-city",
        help="Build reusable road and location inputs from an accepted city CLE",
    )
    c.add_argument("--city", required=True, choices=CITIES)
    c.add_argument(
        "--cle-root",
        type=Path,
        help="Directory containing cities/ and stage1_server_build_receipt.json",
    )
    a = subs.add_parser(
        "plan", help="Write city task slots with fixed seeds, dates and depot IDs"
    )
    a.add_argument("--city", required=True, choices=CITIES)
    release = read(PROJECT / "config/release_panel.json")
    a.add_argument("--seed", type=int, default=release["root_seed"])
    a.add_argument("--start-date", default=release["calendar_start"])
    a.add_argument("--calendar-days", type=positive, default=release["calendar_days"])
    a.add_argument(
        "--custom-n", type=int, help="Independent experimental panel (100 through 2000)"
    )
    a.add_argument(
        "--cs", type=positive, help="External station budget for a custom panel"
    )
    a.add_argument("--count", type=positive, help="Number of tasks in the custom panel")
    subs.add_parser(
        "seal-plans", help="Bind the 11 official city plans in one release index"
    )
    b = subs.add_parser("stage2", help="Run, resume or retry a city plan")
    b.add_argument("--city", required=True, choices=CITIES)
    b.add_argument("--workers", type=positive, default=1)
    b.add_argument(
        "--split",
        choices=["train", "val", "test1", "test2", "test3", "xs", "custom"],
        metavar="{train,val,test1,test2,xs,custom}",
    )
    b.add_argument("--n", type=int)
    b.add_argument("--limit", type=positive)
    b.add_argument(
        "--retry-method-failures",
        action="store_true",
        help="Repeat unsupported tasks with their original seed",
    )
    b.add_argument("--shard", default="0/1", help="Zero-based shard index/count")
    b.add_argument("--catalog", type=Path)
    b.add_argument(
        "--output", type=Path, help="Optional task output root for this host"
    )
    audit = subs.add_parser(
        "audit",
        help="Aggregate the city panel and verify output from one or more shards",
    )
    audit.add_argument("--city", required=True, choices=CITIES)
    audit.add_argument(
        "--outputs",
        nargs="+",
        type=Path,
        help="Host output roots, each containing ledger/",
    )
    audit.add_argument(
        "--custom-n",
        type=positive,
        help="Audit an experimental panel instead of release slots",
    )
    r = subs.add_parser(
        "restore", help="Recompute four matrices and check the published witness"
    )
    r.add_argument("instance", type=Path)
    r.add_argument("--out", required=True, type=Path)
    v = subs.add_parser("plot", help="Draw an instance ZIP or expanded directory")
    v.add_argument("instance", type=Path)
    v.add_argument("--out", type=Path)
    v.add_argument("--boundary", type=Path)
    args = p.parse_args(argv)
    w = args.work.resolve()
    if hasattr(args, "city") and isinstance(args.city, str):
        shared = w / "shared" / args.city
        plan = w / "plans" / (args.city + ".json")
    custom = None
    if args.cmd == "plan" and any(
        v is not None for v in (args.custom_n, args.cs, args.count)
    ):
        if (
            None in (args.custom_n, args.cs, args.count)
            or not 100 <= args.custom_n <= 2000
        ):
            p.error(
                "Custom plans require --custom-n (100..2000), --cs and --count together"
            )
        custom = (args.custom_n, args.cs, args.count)
        plan = plan.with_name(f"{args.city}-custom-{args.custom_n}.json")
    if args.cmd == "stage2" and args.split == "custom":
        if args.n is None or not 100 <= args.n <= 2000:
            p.error("Custom execution requires --n (100..2000)")
        plan = plan.with_name(f"{args.city}-custom-{args.n}.json")
    if args.cmd == "sources":
        from sources.prepare import prepare

        prepare(w, args.only, args.city, args.workers, args.osm_date, args.source_lock)
    elif args.cmd == "stage1":
        from stage2.stage_one import run

        run(
            args.inputs or w / "public",
            w / "stage1",
            args.city,
            args.workers,
        )
    elif args.cmd == "prepare-city":
        from stage2.city import prepare
        from stage2.stage_one import city_root

        prepare(
            args.cle_root or city_root(w, args.city),
            args.city,
            shared,
            w / "public" / args.city,
        )
        print(shared)
    elif args.cmd == "plan":
        from stage2.plan import save_plan

        print(
            save_plan(
                shared, plan, args.seed, args.start_date, args.calendar_days, custom
            ),
            "slots:",
            plan,
        )
    elif args.cmd == "seal-plans":
        from stage2.plan import seal_plans

        result = seal_plans(w / "plans")
        print(result["task_count"], "slots:", w / "plans/release.json")
    elif args.cmd == "stage2":
        from stage2.batch import run

        result = run(
            plan,
            shared,
            (args.catalog or w / "public/amazon").resolve(),
            (args.output or w).resolve(),
            args.workers,
            args.split,
            args.n,
            args.limit,
            args.shard,
            args.retry_method_failures,
        )
        print(json.dumps(result, sort_keys=True))
        if result.get("execution_error"):
            return 1
    elif args.cmd == "audit":
        from stage2.audit import audit

        if args.custom_n is not None:
            plan = plan.with_name(f"{args.city}-custom-{args.custom_n}.json")
        result = audit(plan, args.outputs or [w])
        print(json.dumps(result, indent=2))
        return int(bool(result["issues"]))
    elif args.cmd == "restore":
        import zipfile
        from stage2.generate import restore

        with zipfile.ZipFile(args.instance) as z:
            city = json.loads(z.read("instance.json"))["city"]
        print(restore(args.instance, w / "shared" / city, args.out))
    elif args.cmd == "plot":
        from stage2.plot import draw

        target = args.out or args.instance.with_suffix(".png")
        draw(args.instance, target, args.boundary, w / "shared")
        print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
