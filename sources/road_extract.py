#!/usr/bin/env python3
"""Freeze complete OSM ways for uniform city + 20km road-access reconstruction."""

from __future__ import annotations

import json

import geopandas as gpd
import osmium
import shapely
from shapely.geometry import LineString, Point

from evrptw_cle.terminal_access import ACCESS_NODE_TAG_CONTRACT, ACCESS_NODE_TAGS
from evrptw_cle.util import sha256_file, write_json


def extract_region(pbf, boundary_path, dest, city, region, url, digest, timestamp):
    cities = {}
    boundary = gpd.read_file(boundary_path)
    local_crs = boundary.estimate_utm_crs()
    # Exact complete geometry; no small boundary component is dropped.
    envelope = boundary.to_crs(local_crs).geometry.union_all().buffer(20000)
    envelope = gpd.GeoSeries([envelope], crs=local_crs).to_crs(4326).iloc[0]
    shapely.prepare(envelope)
    cities[city] = dict(
        bounds=envelope.bounds,
        envelope=envelope,
        rows=[],
        nodes=[],
        referenced_nodes=set(),
        outside_envelope_access_nodes=0,
        boundary_path=boundary_path,
    )
    print("READ", region, list(cities), flush=True)
    processor = (
        osmium.FileProcessor(str(pbf))
        .with_locations()
        .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
        .with_filter(osmium.filter.KeyFilter("highway"))
    )
    for obj in processor:
        tags = dict(obj.tags)
        if "highway" not in tags:
            continue
        coords = [(n.lon, n.lat) for n in obj.nodes if n.location.valid()]
        if len(coords) != len(obj.nodes) or len(coords) < 2:
            continue
        xs, ys = zip(*coords)
        bounds = min(xs), min(ys), max(xs), max(ys)
        near = []
        for city, values in cities.items():
            x0, y0, x1, y1 = values["bounds"]
            if (
                bounds[2] >= x0
                and bounds[0] <= x1
                and bounds[3] >= y0
                and bounds[1] <= y1
            ):
                near.append(city)
        if not near:
            continue
        geometry = LineString(coords)
        record = dict(
            osmid=str(obj.id),
            node_ids_json=json.dumps([n.ref for n in obj.nodes]),
            tags_json=json.dumps(tags, sort_keys=True),
            osm_object_version=obj.version,
            osm_edit_timestamp=obj.timestamp.isoformat(),
            geometry=geometry,
        )
        for city in near:
            if cities[city]["envelope"].intersects(geometry):
                cities[city]["rows"].append(record)
                cities[city]["referenced_nodes"].update(n.ref for n in obj.nodes)
    # Complete ways can curve or terminate outside the selection envelope. A
    # second pass over the same frozen PBF retains every tagged restriction node
    # they reference, rather than silently losing outside-envelope gates.
    print("READ_RESTRICTION_NODES", region, flush=True)
    node_processor = (
        osmium.FileProcessor(str(pbf))
        .with_filter(osmium.filter.EntityFilter(osmium.osm.NODE))
        .with_filter(osmium.filter.KeyFilter(*ACCESS_NODE_TAGS))
    )
    for obj in node_processor:
        if not obj.location.valid():
            raise ValueError(
                "Restriction node has no source coordinates: " + str(obj.id)
            )
        point = Point(obj.location.lon, obj.location.lat)
        tags = dict(obj.tags)
        record = dict(
            osmid=str(obj.id),
            tags_json=json.dumps(tags, sort_keys=True),
            osm_object_version=obj.version,
            osm_edit_timestamp=obj.timestamp.isoformat(),
            geometry=point,
        )
        for values in cities.values():
            inside = values["envelope"].covers(point)
            if inside or obj.id in values["referenced_nodes"]:
                values["nodes"].append(record)
                if not inside:
                    values["outside_envelope_access_nodes"] += 1
    for city, values in cities.items():
        dest.mkdir(parents=True, exist_ok=True)
        ways = gpd.GeoDataFrame(values["rows"], geometry="geometry", crs=4326)
        ways.to_parquet(dest / "osm_ways.parquet", index=False)
        nodes = gpd.GeoDataFrame(
            values["nodes"],
            columns=[
                "osmid",
                "tags_json",
                "osm_object_version",
                "osm_edit_timestamp",
                "geometry",
            ],
            geometry="geometry",
            crs=4326,
        )
        nodes.to_parquet(dest / "osm_access_nodes.parquet", index=False)
        write_json(
            dest / "extraction.json",
            dict(
                schema="evrptw_complete_osm_access_ways_v1",
                access_node_tag_contract=ACCESS_NODE_TAG_CONTRACT,
                city_slug=city,
                region=region,
                pbf_sha256=digest,
                pbf_replication_timestamp_utc=timestamp,
                pbf_source_url=url,
                pbf_path=pbf.name,
                admin_boundary_sha256=sha256_file(values["boundary_path"]),
                max_routing_buffer_km=20,
                way_count=len(ways),
                restricted_or_barrier_node_count=len(nodes),
                outside_envelope_referenced_access_node_count=values[
                    "outside_envelope_access_nodes"
                ],
                access_nodes_complete_for_retained_ways=True,
                restriction_node_scan="second pass over the same frozen PBF; all tagged nodes used by retained complete ways, including outside the envelope",
                outputs={
                    name: sha256_file(dest / name)
                    for name in ["osm_ways.parquet", "osm_access_nodes.parquet"]
                },
            ),
        )
        print("COMPLETE", city, len(ways), len(nodes), flush=True)
