"""Bind public-source builds to the PBF and equipment file actually acquired."""

from functools import lru_cache
import json
import os
from pathlib import Path


@lru_cache(maxsize=2)
def _read(path):
    return json.loads(Path(path).read_text())


def inventory():
    path = os.environ.get("EVRPTWD_PUBLIC_INVENTORY")
    return _read(path) if path else None


def road_matches(source):
    manifest = inventory()
    if manifest is None:
        return source.get("pbf_replication_timestamp_utc") == "2026-10-25T21:21:21Z"
    record = manifest["roads"].get(source.get("city_slug"))
    return bool(
        record
        and record["sha256"] == source.get("pbf_sha256")
        and record["timestamp"] == source.get("pbf_replication_timestamp_utc")
    )


def equipment_hash(contract):
    current = inventory()
    return (
        current["files"][current["charging"]["units"]]["sha256"]
        if current
        else contract["unit_source"]["sha256"]
    )
