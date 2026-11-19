#!/usr/bin/env python3
"""Export existing delivery tasks as CVRP, VRPTW or EVRPTW Parquet datasets."""

import argparse
import json
import tempfile
import zipfile
from contextlib import ExitStack
from functools import partial
from itertools import groupby
from pathlib import Path

import pyarrow.parquet as pq

from solver_parquet import PROBLEMS, from_restored, load_problem, partition
from stage2.common import read


def write_card(root, splits, problem):
    lines = ["---", "configs:"]
    for table in ("instances", "nodes", "matrix_rows"):
        lines.append(f"- config_name: {table}")
        if table == "instances":
            lines.append("  default: true")
        lines.append("  data_files:")
        for split in sorted(splits):
            lines.extend(
                [f"  - split: {split}", f'    path: "{table}/{split}/*/*/*.parquet"']
            )
    lines += [
        "---",
        f"# EVRPTW-D · {problem.upper()}",
        "",
        "Join tables by `instance_id`. In `nodes`, `node_idx` gives the solver order.",
        "Each `matrix_rows` row starts at `from_idx`; list index is the destination node.",
        "Road distances are metres and times are seconds. Loads are cubic centimetres.",
        "The default path minimizes directed road distance. A null fleet size means unlimited vehicles.",
        "A null source split is shown under `example`. The source protocol status describes the EV witness.",
        "",
        "Roads use © OpenStreetMap contributors. Footprints are from Microsoft, structure attributes from USACE NSI,",
        "boundaries from the U.S. Census Bureau and charging facilities from AFDC.",
        "Order attributes use the Amazon Last Mile Routing Research Challenge data (CC BY-NC 4.0).",
        (
            "[OSM attribution](https://www.openstreetmap.org/copyright) · "
            "[Amazon source](https://registry.opendata.aws/amazon-last-mile-challenges/)."
        ),
    ]
    (root / "README.md").write_text("\n".join(lines) + "\n")


def source_info(path):
    if path.is_dir():
        info = read(path / "instance.json")
    elif path.suffix == ".parquet":
        from instance_file import file_header

        info = file_header(path)["payload"]["instance"]
    else:
        with zipfile.ZipFile(path) as z:
            info = json.loads(z.read("instance.json"))
    return {k: info[k] for k in ("instance_id", "split", "city", "customer_count")}


def source_problem(path, work, info):
    if path.is_dir():
        return from_restored(path)
    if path.suffix == ".parquet":
        from instance_file import read_instance

        return read_instance(path)
    from stage2.generate import restore

    with tempfile.TemporaryDirectory() as tmp:
        expanded = Path(tmp) / "restored"
        restore(path, work / "shared" / info["city"], expanded)
        return from_restored(expanded)


class TableShards:
    def __init__(self, folder, target_bytes):
        self.folder = folder
        self.folder.mkdir(parents=True)
        self.target_bytes = target_bytes
        self.writer = None
        self.size = 0
        self.part = 0

    def close(self):
        if self.writer is not None:
            self.writer.close()
            self.writer = None

    def write(self, table):
        if self.writer is None or self.size + table.nbytes > self.target_bytes:
            self.close()
            path = self.folder / f"part-{self.part:05d}.parquet"
            self.writer = pq.ParquetWriter(path, table.schema, compression="zstd")
            self.size = 0
            self.part += 1
        self.writer.write_table(table)
        self.size += table.nbytes


def write_dataset(records, out, problem, shard_bytes=256 << 20):
    """records contains metadata and lazy readers. Publish the directory only after success."""
    if out.exists():
        raise FileExistsError(f"Output already exists: {out}")
    records = sorted(
        records, key=lambda item: (partition(item[0]), item[0]["instance_id"])
    )
    if not records:
        raise ValueError("Provide at least one instance")
    ids = set()
    splits = set()
    for info, _ in records:
        split = info["split"] or "example"
        if info["instance_id"] in ids:
            raise ValueError(f"Duplicate instance ID: {info['instance_id']}")
        ids.add(info["instance_id"])
        splits.add(split)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent) as tmp:
        root = Path(tmp) / "dataset"
        root.mkdir()
        for folder, group in groupby(records, key=lambda item: partition(item[0])):
            with ExitStack() as stack:
                writers = {}
                for name in ("instances", "nodes", "matrix_rows"):
                    writers[name] = TableShards(root / name / folder, shard_bytes)
                    stack.callback(writers[name].close)
                for info, load in group:
                    view = load().view(problem)
                    if (
                        view.info["instance_id"] != info["instance_id"]
                        or partition(view.info) != folder
                    ):
                        raise ValueError(
                            "Source instance identity changed during export"
                        )
                    for table, data in view.tables().items():
                        writers[table].write(data)
        write_card(root, splits, problem)
        root.rename(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        help="Portable Parquet files, restored directories or compact ZIPs",
    )
    parser.add_argument(
        "--input-list",
        type=Path,
        help="UTF-8 file, one source path per line relative to the list",
    )
    parser.add_argument(
        "--instance-id", help="Read a single instance from an existing Parquet dataset"
    )
    parser.add_argument("--problem", choices=PROBLEMS, default="evrptw")
    parser.add_argument(
        "--out", type=Path, required=True, help="New output dataset directory"
    )
    parser.add_argument(
        "--work",
        type=Path,
        default=Path("work"),
        help="Generator work root for ZIP restoration",
    )
    args = parser.parse_args()
    if args.instance_id:
        if len(args.inputs) != 1 or args.input_list:
            parser.error("--instance-id requires exactly one Parquet dataset directory")
        view = load_problem(args.inputs[0], args.instance_id, args.problem)
        records = [(view.info, lambda: view)]
    else:
        paths = list(args.inputs)
        if args.input_list:
            paths.extend(
                args.input_list.parent / line
                for line in args.input_list.read_text().splitlines()
                if line.strip()
            )
        records = []
        for path in paths:
            info = source_info(path)
            records.append(
                (
                    info,
                    partial(source_problem, path, args.work, info),
                )
            )
    write_dataset(records, args.out, args.problem)
    print(f"{args.problem.upper()}: {len(records)} instance(s) -> {args.out}")


if __name__ == "__main__":
    main()
