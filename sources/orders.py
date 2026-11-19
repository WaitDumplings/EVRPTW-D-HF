"""Derive the address-level fields actually consumed by the release generator."""

import hashlib
import json

import pandas as pd
import pyarrow.parquet as pq

from evrptw_scenarios.marks import build_donor_artifact
from stage2.common import PROJECT, read, write

BASE = "https://amazon-last-mile-challenges.s3.us-west-2.amazonaws.com/almrrc2021/almrrc2021-data-training/model_build_inputs/"


# Only fields read by Stage 2 enter this fingerprint. Row order binds donor indices.
SOLVER_COLUMNS = {
    "service.parquet": [
        "donor_id",
        "source_station_day_id",
        "package_count",
        "service_s",
    ],
    "windows.parquet": ["donor_id", "a", "b"],
    "bundles.parquet": ["donor_id", "parcel_rows"],
    "parcels.parquet": ["volume_cm3", "source_parcel_ref"],
}


def logical_hashes(catalog):
    hashes = {}
    encode = json.JSONEncoder(
        ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).encode
    for filename, columns in SOLVER_COLUMNS.items():
        h = hashlib.sha256()
        h.update((encode(columns) + "\n").encode("ascii"))
        parquet = pq.ParquetFile(catalog / filename)
        for batch in parquet.iter_batches(batch_size=8192, columns=columns):
            rows = zip(*(batch.column(i).to_pylist() for i in range(len(columns))))
            h.update("".join(encode(row) + "\n" for row in rows).encode("ascii"))
        hashes[filename] = h.hexdigest()
    return hashes


def order_tables(catalog):
    bundles = pd.read_parquet(
        catalog / "bundles.parquet",
        columns=[
            "donor_id",
            "source_station_day_id",
            "package_count",
            "source_kind",
            "individual_marks_valid",
        ],
    )
    donors = bundles.loc[
        bundles.source_kind.eq("delivered") & bundles.individual_marks_valid,
        ["donor_id", "source_station_day_id", "package_count"],
    ].copy()
    parcels = pd.read_parquet(
        catalog / "parcels.parquet",
        columns=[
            "donor_id",
            "source_planned_service_s",
            "source_tw_status",
            "source_tw_start_offset_s",
            "source_tw_end_offset_s",
        ],
    )
    parcels = parcels[parcels.donor_id.isin(donors.donor_id)]
    donors = donors.join(
        parcels.groupby("donor_id").source_planned_service_s.sum().rename("service_s"),
        on="donor_id",
        validate="one_to_one",
    )
    windows = (
        parcels[parcels.source_tw_status.eq("specified")]
        .groupby("donor_id", sort=True)
        .agg(a=("source_tw_start_offset_s", "max"), b=("source_tw_end_offset_s", "min"))
        .reset_index()
    )
    if (
        len(donors) != 893402
        or len(windows) != 57673
        or donors.service_s.isna().any()
        or (windows.a > windows.b).any()
    ):
        raise ValueError(
            "Rebuilt address orders differ from the registered Amazon training population"
        )
    return donors.sort_values("donor_id").reset_index(drop=True), windows


def build(store, output):
    ref = read(PROJECT / "config/manifest.json")
    raw = {
        key: store.fetch(f"amazon/{key}.json", BASE + key + ".json", h)
        for key, h in ref["canonical_source_sha256"].items()
    }
    build_donor_artifact(
        package_path=raw["package_data"],
        route_path=raw["route_data"],
        output_dir=output,
    )
    donors, windows = order_tables(output)
    donors.to_parquet(output / "service.parquet", index=False)
    windows.to_parquet(output / "windows.parquet", index=False)
    fingerprints = logical_hashes(output)
    if fingerprints != read(PROJECT / "config/amazon_solver_fields.json"):
        raise ValueError(
            "Rebuilt Amazon order values differ from the registered reference"
        )
    write(output / "solver_fields.json", fingerprints)
