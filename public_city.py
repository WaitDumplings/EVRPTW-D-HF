"""Verify the city assets delivered in a two-part public release."""

from pathlib import Path

from stage2.common import read, sha, shared_identity

LAYOUT = "evrptwd_public_two_part_v1"
SHARED_FILES = (
    "access.parquet",
    "boundary.geojson",
    "endpoints.parquet",
    "locations.parquet",
    "rook.json",
    "speeds.parquet",
)
ROAD_FILE = "roads/osm_ways.parquet"
CITY_FILES = tuple(f"shared/{name}" for name in (*SHARED_FILES, "shared.json")) + (
    ROAD_FILE,
)


def verify_public_city(root, city, expected=None):
    """Verify the published files and return their original instance binding."""
    root = Path(root)
    manifest = root / "release.json"
    if not manifest.is_file():
        raise ValueError("Missing public release manifest")
    release = read(manifest)
    if release.get("layout") != LAYOUT:
        raise ValueError("Unknown public release layout")
    if not city or Path(city).name != city or city in (".", ".."):
        raise ValueError(f"Invalid public city: {city}")
    if city not in release["cities"] or city not in release["city_files"]:
        raise ValueError(f"City missing from public release: {city}")

    city_path = Path("cities") / city
    assets = release["city_files"][city]
    if set(assets) != set(CITY_FILES):
        raise ValueError(f"Unexpected public city inventory: {city}")
    if assets[ROAD_FILE].get("path") != (city_path / ROAD_FILE).as_posix():
        raise ValueError(f"Wrong public road path: {city}")
    shared = root / city_path / "shared"
    if {path.name for path in shared.iterdir()} != {*SHARED_FILES, "shared.json"}:
        raise ValueError(f"Unexpected shared directory contents: {city}")
    for name in CITY_FILES:
        path = root / city_path / name
        record = assets[name]
        if (
            not path.is_file()
            or path.stat().st_size != record["bytes"]
            or sha(path) != record["sha256"]
        ):
            raise ValueError(f"Damaged public city asset: {path}")

    meta = read(shared / "shared.json")
    checksums = {name: assets[f"shared/{name}"]["sha256"] for name in SHARED_FILES}
    if (
        meta.get("schema") != "evrptwd_shared_v1"
        or meta.get("city") != city
        or meta.get("files") != checksums
    ):
        raise ValueError(f"Different shared city or file inventory: {city}")
    binding = {
        "city": meta["city"],
        "cle_manifest_sha256": meta["source"]["cle"],
        "shared_sha256": shared_identity(shared),
        "calibration_sha256": meta["calibration"],
    }
    if binding != release["cities"][city] or (
        expected is not None and binding != expected
    ):
        raise ValueError(f"Different public city or shared version: {city}")
    return binding
