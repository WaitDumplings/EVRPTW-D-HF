"""Portable solver format and canonical array hashes."""

import csv
import hashlib
import io
import json
import zipfile
import numpy as np

KEYS = (
    "shortest_distance_m",
    "shortest_distance_time_s",
    "fastest_time_distance_m",
    "fastest_time_s",
)
FIELDS = (
    "NODE_IDX",
    "LOCATION_ID",
    "TYPE",
    "LON",
    "LAT",
    "DEMAND_CM3",
    "READY_S",
    "DUE_S",
    "SERVICE_S",
    "CHARGE_KW",
)


def encode(obj):
    return (
        json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode()


def hash_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def array_hashes(arrays, n):
    if set(arrays) != set(KEYS):
        raise ValueError("Invalid road array names")
    result = {}
    for k in KEYS:
        matrix = arrays[k]
        if (
            matrix.shape != (n, n)
            or matrix.dtype.kind != "f"
            or matrix.dtype.itemsize != 8
            or not np.isfinite(matrix).all()
            or (matrix < 0).any()
            or matrix.diagonal().any()
        ):
            raise ValueError("Invalid road array: " + k)
        result[k] = hash_bytes(np.ascontiguousarray(matrix, dtype="<f8").tobytes())
    return result


def node_table(ids, orders, stations, locs, vehicle):
    by_id = {o["location_id"]: o for o in orders}
    rows = []
    for index, loc in enumerate(ids):
        site = locs.loc[loc]
        row = dict(
            zip(
                FIELDS,
                [
                    index,
                    loc,
                    "depot"
                    if index == 0
                    else "customer"
                    if loc in by_id
                    else "charging",
                    float(site.location_lon),
                    float(site.location_lat),
                    0.0,
                    *map(float, vehicle["operating_window_s"]),
                    0.0,
                    0.0,
                ],
            )
        )
        if loc in by_id:
            o = by_id[loc]
            row.update(
                DEMAND_CM3=o["demand_cm3"],
                SERVICE_S=o["service_s"],
                READY_S=o["ready_s"],
                DUE_S=o["due_s"],
            )
        elif loc in stations:
            row["CHARGE_KW"] = float(site.power_kw)
        rows.append(row)
    return rows


def node_text(nodes):
    f = io.StringIO(newline="")
    writer = csv.DictWriter(f, FIELDS, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(nodes)
    return f.getvalue().encode()


def put_zip(path, files):
    with zipfile.ZipFile(
        path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as target:
        for name, raw in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            target.writestr(
                info, raw, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9
            )


def write_all(path, files, arrays):
    path.mkdir(parents=True, exist_ok=True)
    for name, raw in files.items():
        (path / name).write_bytes(raw)
    np.savez_compressed(path / "costs.npz", **{k: arrays[k] for k in KEYS})
