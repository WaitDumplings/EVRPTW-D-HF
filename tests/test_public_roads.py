import hashlib
import json
import re
from collections import defaultdict
from itertools import pairwise

import geopandas as gpd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from evrptw_cle.common_speed_binding import common_source_edge_union
from evrptw_cle.delivery_access import build_delivery_access_context
from evrptw_cle.local_road_access import build_access_context
from shapely.geometry import LineString

from public_roads import export_public_roads
from stage1_engine.tests.test_road_direction_barrier import _inputs
from stage2.common import sha


@pytest.fixture
def road_inputs(tmp_path):
    options = _inputs(tmp_path)
    source = options["source_dir"] / "osm_ways.parquet"
    labels = [
        {"highway": "residential", "name": "Elm", "contact:email": "test@example.org"},
        {"highway": "secondary", "name": "", "phone": "+1 (202) 555-0123"},
        {"highway": "tertiary", "website": "https://example.org"},
    ]
    # Parallel curved ways share their outer nodes. The third way is split at B.
    ways = gpd.GeoDataFrame(
        {
            "osmid": ["21", "5", "31"],
            "node_ids_json": ['["A","X","B"]', '["A","Y","B"]', '["C","B","D"]'],
            "tags_json": [json.dumps(tags) for tags in labels],
            "osm_object_version": [2, 1, 3],
            "osm_edit_timestamp": ["2020-01-03T04:05:06Z"] * 3,
        },
        geometry=[
            LineString([(0, 0), (50, 10), (100, 0)]),
            LineString([(0, 0), (50, 20), (100, 0)]),
            LineString([(100, -25), (100, 0), (100, 100)]),
        ],
        crs=3857,
    ).to_crs(4326)
    ways.to_parquet(source, index=False, row_group_size=1)
    receipt = source.parent / "extraction.json"
    manifest = json.loads(receipt.read_bytes())
    manifest["outputs"][source.name] = sha(source)
    receipt.write_text(json.dumps(manifest))
    return source, options


def test_geometry_export(road_inputs, tmp_path):
    source, _ = road_inputs
    target = tmp_path / "roads" / source.name
    digest = sha(source)
    assert export_public_roads(source, target, digest) == 0
    second = tmp_path / "repeat.parquet"
    export_public_roads(source, second, digest)
    assert target.read_bytes() == second.read_bytes()

    output = pq.read_table(target)
    original = pq.read_table(source)
    assert output.column_names == [
        "osmid",
        "node_ids_json",
        "geometry",
        "highway",
        "name",
    ]
    for column in ["osmid", "node_ids_json", "geometry"]:
        assert output[column].equals(original[column])
    decoded = gpd.read_parquet(target)
    assert decoded.crs == gpd.read_parquet(source).crs
    assert output["highway"].to_pylist() == ["residential", "secondary", "tertiary"]
    assert output["name"].to_pylist() == ["Elm", "", None]

    meta = pq.read_metadata(target).metadata
    assert set(meta) == {b"geo"}
    geo = json.loads(meta[b"geo"])
    assert set(geo) == {"version", "primary_column", "columns"}
    assert set(geo["columns"]["geometry"]) == {"encoding", "geometry_types", "crs"}
    contact = re.compile(
        r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\b(?:https?://|www\.)\S+|"
        r"(?<!\d)(?:\+1[ .-]?)?(?:\(\d{3}\)[ .-]?|\d{3}[ .-])\d{3}[ .-]\d{4}(?!\d)",
        re.IGNORECASE,
    )
    for field in output.schema:
        if pa.types.is_string(field.type) or pa.types.is_large_string(field.type):
            for value in output[field.name].to_pylist():
                assert value is None or not contact.search(value)

    with pytest.raises(ValueError, match="Checksum mismatch"):
        export_public_roads(source, tmp_path / "wrong.parquet", "0" * 64)
    assert not (tmp_path / "wrong.parquet").exists()
    with pytest.raises(FileExistsError):
        export_public_roads(source, target, digest)
    assert target.read_bytes() == second.read_bytes()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("highway", "service 312.555.0123"),
        ("highway", "inquiries@example.org"),
        ("highway", "alternate https://example.org"),
        ("osmid", "x@example.org"),
        ("node_ids_json", '["312-555-0123"]'),
    ],
)
def test_contact_in_preserved_field_fails_export(road_inputs, tmp_path, field, value):
    source, _ = road_inputs
    frame = gpd.read_parquet(source)
    if field == "highway":
        tags = json.loads(frame.loc[1, "tags_json"])
        tags[field] = value
        frame.loc[1, "tags_json"] = json.dumps(tags)
    else:
        frame.loc[1, field] = value
    frame.to_parquet(source, index=False)
    target = tmp_path / "roads" / source.name
    with pytest.raises(ValueError) as failure:
        export_public_roads(source, target, sha(source))
    assert (
        str(failure.value) == f"Road contact-format matches: rows=1, columns: {field}=1"
    )
    assert not target.exists()


@pytest.mark.parametrize(
    "phone_or_url",
    [
        "Visitor road +13125550123",
        "Frontage (312) 555-0123",
        "North entrance HTTP://example.org/route",
        "Junction WWW.example.org/directions",
        "Dispatch 555-0198",
        "Office +44 20 7946 0958",
    ],
)
def test_only_contact_names_are_set_to_null(road_inputs, tmp_path, phone_or_url):
    source, _ = road_inputs
    original = pq.read_table(source)
    tags = [json.loads(raw) for raw in original["tags_json"].to_pylist()]
    tags[0]["name"] = phone_or_url
    tags[2]["name"] = "dispatch@example.org"
    updated = original.set_column(
        original.schema.get_field_index("tags_json"),
        "tags_json",
        pa.array([json.dumps(t) for t in tags]),
    )
    pq.write_table(updated, source)
    target = tmp_path / "roads.parquet"
    repeat = tmp_path / "repeat.parquet"
    for path in (target, repeat):
        assert export_public_roads(source, path, sha(source)) == 2
    assert target.read_bytes() == repeat.read_bytes()
    public = pq.read_table(target)
    for field in ["geometry", "osmid", "node_ids_json"]:
        assert public[field].equals(original[field])
    assert public["highway"].to_pylist() == [t["highway"] for t in tags]
    assert public["name"].to_pylist() == [None, "", None]


def test_ordinary_contact_and_website_street_names_are_preserved(road_inputs, tmp_path):
    source, _ = road_inputs
    frame = gpd.read_parquet(source)
    names = ["Contact Road", "Old Website Drive", "Contact Junction"]
    for index, name in enumerate(names):
        tags = json.loads(frame.loc[index, "tags_json"])
        frame.loc[index, "tags_json"] = json.dumps({**tags, "name": name})
    frame.to_parquet(source, index=False)
    target = tmp_path / "roads" / source.name
    export_public_roads(source, target, sha(source))
    assert pq.read_table(target, columns=["name"])["name"].to_pylist() == names


def test_contact_report_counts_rows_across_columns_and_batches(road_inputs, tmp_path):
    source, _ = road_inputs
    size = 32_769  # The last input row is in the second export batch.
    table = pq.read_table(source).take(pa.array([0] * size))
    tags = ['{"highway": "residential"}'] * size
    tags[1] = (
        '{"highway": "http://example.com", "name": "WWW.example.org (212) 555-0147"}'
    )
    tags[-1] = '{"highway": "service", "name": "secret@example.org"}'
    osmids = ["21"] * size
    osmids[1] = osmids[-1] = "secret@example.org"
    table = table.set_column(
        table.schema.get_field_index("osmid"), "osmid", pa.array(osmids)
    )
    table = table.set_column(
        table.schema.get_field_index("tags_json"), "tags_json", pa.array(tags)
    )
    pq.write_table(table, source)

    target = tmp_path / "roads" / source.name
    with pytest.raises(ValueError) as failure:
        export_public_roads(source, target, sha(source))
    assert (
        str(failure.value)
        == "Road contact-format matches: rows=2, columns: osmid=2, highway=1"
    )
    assert not target.exists()


def test_numeric_identifiers_preserved(road_inputs, tmp_path):
    source, _ = road_inputs
    frame = gpd.read_parquet(source)
    frame.loc[0, "osmid"] = "3125550123"
    frame.loc[0, "node_ids_json"] = "[13125550123, 3125550123, 31255501234]"
    frame.to_parquet(source, index=False)
    target = tmp_path / "roads" / source.name
    export_public_roads(source, target, sha(source))
    assert pq.read_table(target)["osmid"][0].as_py() == frame.loc[0, "osmid"]
    assert (
        pq.read_table(target)["node_ids_json"][0].as_py()
        == frame.loc[0, "node_ids_json"]
    )


def test_every_speed_edge_matches_one_ordered_road_slice(road_inputs, tmp_path):
    source, options = road_inputs
    build_access_context(**options, output_dir=tmp_path / "seed")
    ctx, _ = build_delivery_access_context(
        **options, seed_dir=tmp_path / "seed", output_dir=tmp_path / "delivery"
    )
    _, inventory = common_source_edge_union(ctx)
    speeds = tmp_path / "speeds.parquet"
    inventory[["edge_u", "edge_v", "edge_key"]].to_parquet(speeds, index=False)
    edges = pq.read_table(speeds).to_pylist()
    assert len(edges) == 8  # Two bidirectional parallel roads and two split sections.
    junctions = {edge[k] for edge in edges for k in ("edge_u", "edge_v")}
    target = tmp_path / "roads" / source.name
    source_table = pq.read_table(source)
    tags = source_table["tags_json"].to_pylist()
    tags[0] = json.dumps({**json.loads(tags[0]), "name": "Road (312) 555-0123"})
    source_table = source_table.set_column(
        source_table.schema.get_field_index("tags_json"), "tags_json", pa.array(tags)
    )
    pq.write_table(source_table, source)
    assert export_public_roads(source, target, sha(source)) == 1

    sections = defaultdict(list)
    streets = gpd.read_parquet(target)
    for way in streets.itertuples():
        ids = json.loads(way.node_ids_json)
        coords = list(way.geometry.coords)
        assert len(coords) == len(ids)
        cuts = [i for i, node in enumerate(ids) if node in junctions]
        for start, end in pairwise(cuts):
            u, v = ids[start], ids[end]
            prefix = hashlib.sha1(f"{way.osmid}|{u}|{v}|{start}".encode()).hexdigest()[
                :20
            ]
            part = coords[start : end + 1]
            sections[u, v, prefix + "f"].append(part)
            sections[v, u, prefix + "r"].append(part[::-1])
    for edge in edges:
        identity = (edge["edge_u"], edge["edge_v"], edge["edge_key"])
        (coords,) = sections[identity]
        native = ctx.core.edges[identity]["geometry"]
        assert native.equals_exact(LineString(coords), 1e-10)
