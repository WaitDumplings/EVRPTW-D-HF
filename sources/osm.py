"""Extract the three OSM layers from one publisher PBF."""

import json
import subprocess
import tempfile
from pathlib import Path

import geopandas as gpd
import osmium
import shapely

from evrptw_cle.depot_candidates import _is_matched_object
from stage2.common import write
from sources.road_extract import extract_region


def buildings(pbf, boundary, output):
    factory = osmium.geom.WKBFactory()
    memberways, rows = set(), []
    xmin, ymin, xmax, ymax = boundary.bounds
    objects = (
        osmium.FileProcessor(str(pbf))
        .with_areas(osmium.filter.KeyFilter("building"))
        .with_filter(osmium.filter.EntityFilter(osmium.osm.AREA | osmium.osm.RELATION))
        .with_filter(osmium.filter.KeyFilter("building"))
    )
    for obj in objects:
        if obj.is_relation():
            if obj.tags.get("building") and obj.tags.get("type") == "multipolygon":
                memberways.update(
                    m.ref
                    for m in obj.members
                    if m.type == "w" and m.role in ("outer", "")
                )
            continue
        if not obj.is_area() or obj.tags.get("building") in (None, "no"):
            continue
        near = any(
            n.location.valid() and xmin <= n.lon <= xmax and ymin <= n.lat <= ymax
            for ring in obj.outer_rings()
            for n in ring
        )
        if not near:
            continue
        try:
            geom = shapely.from_wkb(factory.create_multipolygon(obj))
        except (ValueError, RuntimeError):
            continue
        if not geom.is_valid or geom.is_empty or not boundary.intersects(geom):
            continue
        tags = dict(obj.tags)
        rows.append(
            dict(
                osm_id=("w" if obj.from_way() else "r") + str(obj.orig_id()),
                orig_id=obj.orig_id(),
                from_way=obj.from_way(),
                building=tags["building"],
                building_part=tags.get("building:part", "no") != "no",
                tags=json.dumps(tags, sort_keys=True),
                geometry=geom,
            )
        )
    if not rows:
        raise ValueError("No OSM building polygons in this city")
    frame = gpd.GeoDataFrame(rows, crs=4326)
    frame = frame.loc[~(frame.from_way & frame.orig_id.isin(memberways))].copy()
    frame["geom_key"] = shapely.to_wkb(shapely.normalize(frame.geometry.array))
    frame.sort_values(["from_way", "osm_id"]).drop_duplicates("geom_key").drop(
        columns="geom_key"
    ).to_parquet(output, index=False)


def depots(pbf, boundary_path, output):
    filters = [
        "building=warehouse,industrial",
        "industrial=warehouse,logistics,distribution",
        "office=logistics",
        "amenity=post_depot",
        "landuse=industrial,depot,warehouse,logistics",
        "depot",
        "logistics",
    ]
    with tempfile.TemporaryDirectory(dir=output.parent) as temp:
        folder = Path(temp)
        city, candidates, exported = (
            folder / n for n in ["city.osm.pbf", "candidates.osm.pbf", "depots.geojson"]
        )
        subprocess.run(
            [
                "osmium",
                "extract",
                "--polygon",
                str(boundary_path),
                "--strategy",
                "complete_ways",
                "-o",
                str(city),
                str(pbf),
            ],
            check=True,
        )
        subprocess.run(
            [
                "osmium",
                "tags-filter",
                "-o",
                str(candidates),
                str(city),
                *["nwr/" + s for s in filters],
            ],
            check=True,
        )
        subprocess.run(
            [
                "osmium",
                "export",
                "--add-unique-id",
                "type_id",
                "-o",
                str(exported),
                str(candidates),
            ],
            check=True,
        )
        frame = gpd.read_file(exported)
    if frame.empty:
        raise ValueError("No OSM depot candidates")
    frame = frame.loc[frame.apply(_is_matched_object, axis=1)].copy()

    def identity(key):
        kind = key[0]
        n = int(key[1:])
        if kind == "a":
            return ("r" if n % 2 else "w") + str(n // 2)
        return key

    frame["source_osm_id"] = frame.id.map(identity)
    frame["source_osm_url"] = frame.source_osm_id.map(
        lambda k: (
            "https://www.openstreetmap.org/"
            + {"w": "way", "r": "relation", "n": "node"}[k[0]]
            + "/"
            + k[1:]
        )
    )
    frame["geometry_priority"] = frame.geom_type.map(
        {
            "Polygon": 3,
            "MultiPolygon": 3,
            "LineString": 2,
            "MultiLineString": 2,
            "Point": 1,
        }
    ).fillna(0)
    frame.sort_values(
        ["geometry_priority", "id"], ascending=[False, True]
    ).drop_duplicates("source_osm_id").to_parquet(output, index=False)


def layers(store, city, spec, directory, boundary, date):
    region = spec["region"]
    tag = date.replace("-", "")[2:]
    url = f"https://download.geofabrik.de/north-america/us/{region}-{tag}.osm.pbf"
    key = f"osm/{region}-{tag}.osm.pbf"
    pbf = store.fetch(key, url)
    with osmium.io.Reader(str(pbf)) as reader:
        stamp = reader.header().get("osmosis_replication_timestamp")
    if not stamp:
        raise ValueError("Geofabrik source has no replication timestamp")
    checksum = store.records[key]["sha256"]
    extract_region(
        pbf,
        directory / "admin.geojson",
        directory / "roads",
        city,
        region,
        url,
        checksum,
        stamp,
    )
    buildings(pbf, boundary, directory / "osm.parquet")
    depots(pbf, directory / "land.geojson", directory / "depot.parquet")
    write(
        directory / "depot.json",
        dict(
            city=city, pbf_sha256=checksum, replication_timestamp=stamp, source_url=url
        ),
    )
    return {"sha256": checksum, "timestamp": stamp}
