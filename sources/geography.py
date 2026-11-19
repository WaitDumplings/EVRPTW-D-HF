"""Census city land masks, tract adjacency and original building inventories."""

import gzip
import json

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from evrptw_cle.microsoft_composite import _source, exact_duplicates
from stage2.common import sha, write
from . import census

TIGER = "https://www2.census.gov/geo/tiger/TIGER2025"


def geography(store, city, spec, directory):
    def tiger(kind, code):
        name = f"tl_2025_{code}_{kind.lower()}.zip"
        return store.fetch("census/" + name, f"{TIGER}/{kind}/{name}")

    state = spec["place"][:2]
    place = census._select_place(tiger("PLACE", state), spec["place"])
    counties = census._county_geoids(place, gpd.read_file(tiger("COUNTY", "us")), state)
    crs = spec["crs"]
    admin = census._polygonal_only(place.to_crs(crs).geometry.iloc[0])
    water = []
    for county in counties:
        areas = gpd.read_file(tiger("AREAWATER", county), mask=place).to_crs(crs)
        water.extend(areas.geometry.intersection(admin))
    land = census._polygonal_only(admin.difference(shapely.union_all(water)))
    if land.is_empty or not land.is_valid:
        raise ValueError(f"Invalid Census land boundary: {city}")
    place.to_file(directory / "admin.geojson", driver="GeoJSON")
    service = gpd.GeoDataFrame(geometry=[land], crs=crs).to_crs(4326)
    service.to_file(directory / "land.geojson", driver="GeoJSON")
    bgpath = tiger("BG", state)
    bg = gpd.read_file(bgpath, columns=["GEOID"])
    bg["tract"] = bg.GEOID.str[:11]
    mask = service.to_crs(bg.crs).geometry.iloc[0]
    tract_ids = bg.loc[bg.intersects(mask), "tract"].unique()
    # Full native geometries establish rook adjacency even when city clipping cuts a shared edge.
    bg = bg[bg.tract.isin(tract_ids)].sort_values("GEOID")
    bg.rename(columns={"GEOID": "bg_geoid"})[["bg_geoid", "geometry"]].to_parquet(
        directory / "bg.parquet", index=False
    )
    tracts = bg.dissolve(by="tract").sort_index().geometry
    geom = tracts.to_numpy()
    i, j = shapely.STRtree(geom).query(geom, predicate="touches")
    ok = (i < j) & shapely.relate_pattern(geom[i], geom[j], "F***1****")
    adj = {t: [] for t in tracts.index}
    for a, b in zip(tracts.index[i[ok]], tracts.index[j[ok]]):
        adj[a].append(b)
        adj[b].append(a)
    write(
        directory / "rook.json",
        dict(
            source=bgpath.name,
            sha256=sha(bgpath),
            neighbors={k: sorted(v) for k, v in adj.items()},
        ),
    )
    return service.geometry.iloc[0], counties


def microsoft(store, catalog, keys, boundary, crs, output):
    """Select original source points, then validate and deduplicate complete Global polygons."""
    shapely.prepare(boundary)
    rows = []
    for key in sorted(keys):
        rec = catalog[key]
        path = store.fetch(f"microsoft/{key}.geojsonl.gz", rec["url"], rec["sha256"])
        with gzip.open(path, "rb") as stream:
            from itertools import islice

            start = 1
            while lines := list(islice(stream, 8192)):
                shapes = shapely.from_geojson(np.array(lines, dtype=object))
                keep = shapely.covers(
                    boundary, shapely.point_on_surface(shapes)
                ) & shapely.is_valid(shapes)
                for i in np.flatnonzero(keep):
                    attrs = json.loads(lines[i]).get("properties") or {}
                    rows.append(
                        (
                            f"msft_global_{key}_{start + i:09d}",
                            "global",
                            attrs.get("height"),
                            shapes[i],
                        )
                    )
                start += len(lines)
    frame = gpd.GeoDataFrame(
        rows,
        columns=["building_id", "source_kind", "height", "geometry"],
        crs=4326,
        geometry="geometry",
    )
    if frame.empty:
        raise ValueError("Microsoft returned no in-scope buildings")
    frame = _source(frame, "global")
    frame = frame[frame.geom_type.isin(["Polygon", "MultiPolygon"])]
    metric = frame.to_crs(crs).geometry
    frame = frame[metric.is_valid & metric.area.gt(0)].sort_values("building_id")
    frame = frame[~exact_duplicates(frame.geometry.array)]
    frame.to_parquet(output, index=False)


def nsi(store, counties, boundary, output):
    """Stream county responses (GeoJSON Text Sequences). Retain zero-unit and nonresidential evidence."""
    records = []
    seen = {}
    for county in counties:
        url = f"https://nsi.sec.usace.army.mil/nsiapi/structures?fips={county}&fmt=fs"
        path = store.fetch(f"nsi/{county}.geojsonseq", url)
        tail = b""

        def features():
            nonlocal tail
            with path.open("rb") as stream:
                while chunk := stream.read(1 << 20):
                    parts = (tail + chunk).split(b"\x1e")
                    tail = parts.pop()
                    yield from (json.loads(p) for p in parts if p.strip())
                if tail.strip():
                    yield json.loads(tail)

        for feature in features():
            attrs = feature["properties"]
            geom = feature["geometry"]
            if geom["type"] != "Point":
                raise ValueError("Expected NSI point")
            x, y = geom["coordinates"][:2]
            if not shapely.intersects_xy(boundary, x, y):
                continue
            fid = int(attrs["fd_id"])
            signature = (x, y, json.dumps(attrs, sort_keys=True))
            if fid in seen:
                if seen[fid] != signature:
                    raise ValueError(f"Conflicting NSI fd_id {fid}")
                continue
            seen[fid] = signature
            records.append({**attrs, "fd_id": fid, "geometry": shapely.Point(x, y)})
    if not records:
        raise ValueError("USACE returned no in-scope structures")
    frame = gpd.GeoDataFrame(records, crs=4326).sort_values("fd_id")
    for col in ("resunits", "bldheight", "num_story", "sqft", "ftprntsqft"):
        if col not in frame:
            raise ValueError(f"USACE response lacks {col}")
        frame[col] = pd.to_numeric(frame[col], errors="raise")
    frame.to_parquet(output, index=False)
