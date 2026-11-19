"""Publish accepted Stage 1 cities and self-contained solver files into output/."""

import argparse
import json
import re
import shutil
import tempfile
from pathlib import Path

from evrptw_scenarios.cle_binding import cle_authority
from export_instance import source_info
from instance_file import (
    file_header,
    read_instance,
    read_portable,
    unpack,
    write_instance,
)
from solver_parquet import PROBLEMS, from_restored, partition
from stage2.common import CITIES, lock, read, sha, shared_identity, write


def city_inventory(base, shared, city):
    manifest = read(base / "cle_manifest.json")
    if manifest["city_slug"] != city or not (base / "public_authority.json").is_file():
        raise ValueError(f"Expected an accepted public-source Stage 1 CLE for {city}")
    authority, _ = cle_authority(base, manifest)
    receipt = read(authority)
    authority_hash = sha(authority)
    meta = read(shared / "shared.json")
    if (
        meta["city"] != city
        or meta["source"]["cle"] != receipt["cle_manifest_sha256"]
        or meta["source"]["cohort"] != authority_hash
        or meta["source"]["validation"] != receipt["validation_sha256"]
        or meta["calibration"] != receipt["calibration_sha256"]
        or meta["vintage"] != receipt["inputs_sha256"]
    ):
        raise ValueError(
            f"Stage 1 receipt and solver shared assets do not match for {city}"
        )
    files = {name: item["sha256"] for name, item in manifest["files"].items()}
    files.update(
        {
            "cle_manifest.json": receipt["cle_manifest_sha256"],
            "stage1_validation.json": receipt["validation_sha256"],
            "public_authority.json": authority_hash,
        }
    )
    shared_files = {**meta["files"], "shared.json": sha(shared / "shared.json")}
    inventory = {}
    for root, prefix, entries in (
        (base, Path(), files),
        (shared, Path("shared"), shared_files),
    ):
        for name, checksum in entries.items():
            path = Path(name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError(f"Invalid asset path {name}")
            target = prefix / path
            if target in inventory:
                raise ValueError(f"Duplicate asset path {target}")
            inventory[target] = (root / path, checksum)
    binding = {
        "city": city,
        "cle_manifest_sha256": files["cle_manifest.json"],
        "shared_sha256": shared_identity(shared),
        "calibration_sha256": meta["calibration"],
    }
    return binding, inventory


def verify_city(base, city, expected=None, full=True):
    binding, files = city_inventory(base, base / "shared", city)
    if expected is not None and binding != expected:
        raise ValueError("Different CLE or shared version")
    if full:
        for path, checksum in files.values():
            if sha(path) != checksum:
                raise ValueError(f"Damaged CLE asset: {path}")
    return binding


def publish_city(work, output, city):
    base, shared = work / "stage1/cities" / city, work / "shared" / city
    binding, files = city_inventory(base, shared, city)
    dest = output / "CLE" / city
    dest.parent.mkdir(parents=True, exist_ok=True)
    with lock(output / ".locks" / (city + ".lock")):
        if dest.exists():
            verify_city(dest, city, expected=binding)
        else:
            with tempfile.TemporaryDirectory(
                dir=dest.parent, prefix=f".{city}-pending-"
            ) as tmp:
                stage = Path(tmp) / city
                for name, (source, _) in files.items():
                    target = stage / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                verify_city(stage, city, expected=binding)
                stage.rename(dest)
    return dest


def publish_instance(source, output):
    expected = None
    if source.is_dir():
        task = from_restored(source)
    elif source.suffix == ".parquet":
        header, task = read_portable(source)
        expected = header["payload"]["cle"]
    else:
        from stage2.generate import restore

        city = source_info(source)["city"]
        if city not in CITIES:
            raise ValueError("Unknown archive city")
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "restored"
            restore(source, output / "CLE" / city / "shared", folder)
            task = from_restored(folder)
    split, city, scale = partition(task.info).parts
    name = task.info["instance_id"]
    if city not in CITIES or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("Invalid city or instance identifier")
    binding = verify_city(output / "CLE" / city, city, expected=expected, full=False)
    target = output / "instances" / city / split / scale / (name + ".parquet")
    return write_instance(target, task, binding)


def plot_instance(file, output, dest):
    import pyarrow.csv as arrow_csv
    import pyarrow.parquet as pq

    from solver_parquet import NODE_FIELDS, validate_nodes
    from stage2.plot import draw

    payload = file_header(file)["payload"]
    info = payload["instance"]
    base = output / "CLE" / info["city"]
    verify_city(base, info["city"], expected=payload["cle"], full=False)
    shared = base / "shared"
    boundary = shared / "boundary.geojson"
    if sha(boundary) != read(shared / "shared.json")["files"]["boundary.geojson"]:
        raise ValueError("Different CLE map boundary")
    nodes = (
        pq.ParquetFile(file)
        .read(columns=["instance_id", *NODE_FIELDS["evrptw"]])
        .sort_by("node_idx")
    )
    validate_nodes(info, nodes)
    # The common map renderer takes the existing uppercase, tab-separated node table.
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        write(folder / "instance.json", info)
        coords = nodes.select(["type", "lon", "lat"]).rename_columns(
            ["TYPE", "LON", "LAT"]
        )
        arrow_csv.write_csv(
            coords,
            str(folder / "nodes.txt"),
            write_options=arrow_csv.WriteOptions(delimiter="\t"),
        )
        draw(folder, dest, boundary)
    return dest


def verify_instance(path, restored):
    source = from_restored(restored)
    portable = read_instance(path)
    # Both readers verify the four matrix digests and the ordered node table.
    if source.info != portable.info or not source.nodes.equals(portable.nodes):
        raise ValueError("Restored and portable instance inputs differ")
    result = {}
    for problem in PROBLEMS:
        delivered = portable.view(problem)
        result[problem] = {
            "nodes": delivered.info["node_count"],
            "stations": delivered.info["station_count"],
            "matrix_sha256": json.loads(delivered.info["matrix_sha256_json"]),
        }
    return {
        "instance_id": source.info["instance_id"],
        "customers": source.info["customer_count"],
        "views": result,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("output"),
        help="Delivery root (default: output/)",
    )
    subs = parser.add_subparsers(dest="command", required=True)
    cle = subs.add_parser("cle", help="Publish each selected city once")
    cle.add_argument("--work", type=Path, required=True)
    cle.add_argument("--city", nargs="+", choices=CITIES, required=True)
    inst = subs.add_parser(
        "instance",
        help="Publish one existing ZIP, restored directory or portable Parquet",
    )
    inst.add_argument("source", type=Path)
    verify = subs.add_parser(
        "verify-cle",
        help="Hash all delivered assets and optionally check an instance binding",
    )
    verify.add_argument("--city", choices=CITIES, required=True)
    verify.add_argument("--instance", type=Path)
    plot = subs.add_parser(
        "plot", help="Plot a portable instance with its published city boundary"
    )
    plot.add_argument("file", type=Path)
    plot.add_argument("--dest", type=Path, required=True)
    check = subs.add_parser(
        "verify-instance",
        help="Compare the three portable solver views with a restored source",
    )
    check.add_argument("file", type=Path)
    check.add_argument("--restored", type=Path, required=True)
    raw = subs.add_parser(
        "unpack",
        help="Export a solver view as TSV, JSON and raw row-major binary matrices",
    )
    raw.add_argument("file", type=Path)
    raw.add_argument("--problem", choices=PROBLEMS, default="evrptw")
    raw.add_argument("--dest", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "cle":
        for city in args.city:
            print(publish_city(args.work, args.out, city))
    elif args.command == "instance":
        print(publish_instance(args.source, args.out))
    elif args.command == "verify-cle":
        expected = (
            file_header(args.instance)["payload"]["cle"] if args.instance else None
        )
        print(verify_city(args.out / "CLE" / args.city, args.city, expected))
    elif args.command == "plot":
        print(plot_instance(args.file, args.out, args.dest))
    elif args.command == "verify-instance":
        print(json.dumps(verify_instance(args.file, args.restored), indent=2))
    elif args.command == "unpack":
        unpack(args.file, args.dest, args.problem)
        print(args.dest)


if __name__ == "__main__":
    main()
