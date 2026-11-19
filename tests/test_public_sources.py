import gzip
import json
import shutil
import subprocess
import zipfile

import geopandas as gpd
import pytest
from shapely.geometry import box, mapping
from stage2.common import sha
from sources.geography import geography, microsoft, nsi
from sources.osm import layers
from sources.download import Store


class PublisherFiles:
    def __init__(self, files):
        self.files = files
        self.records = {}

    def fetch(self, key, url, known=None):
        path = self.files[key]
        value = sha(path)
        assert not known or value == known
        self.records[key] = dict(url=url, sha256=value, bytes=path.stat().st_size)
        return path


def test_raw_geography_and_road_layers(tmp_path):
    """From raw TIGER ZIP, RS stream, gzipped footprints and PBF to city inputs."""
    if not shutil.which("osmium"):
        pytest.skip("osmium-tool required for PBF extraction")
    b = box(-87.66, 41.88, -87.64, 41.90)
    left = box(-87.66, 41.88, -87.65, 41.90)
    right = box(-87.65, 41.88, -87.64, 41.90)
    files = {}

    def tiger(code, kind, geo, fields):
        name = f"tl_2025_{code}_{kind}"
        folder = tmp_path / name
        folder.mkdir()
        gpd.GeoDataFrame(fields, geometry=geo, crs=4326).to_file(
            folder / (name + ".shp")
        )
        z = tmp_path / (name + ".zip")
        with zipfile.ZipFile(z, "w") as archive:
            for p in folder.iterdir():
                archive.write(p, p.name)
        files["census/" + z.name] = z

    tiger("17", "place", [b], dict(GEOID=["1714000"], ALAND=[3000000], AWATER=[100]))
    tiger("us", "county", [b], dict(STATEFP=["17"], GEOID=["17031"]))
    tiger("17031", "areawater", [box(-87.641, 41.899, -87.6405, 41.8995)], dict(ID=[1]))
    tiger("17", "bg", [left, right], dict(GEOID=["170310001001", "170310002001"]))
    dest = tmp_path / "city"
    dest.mkdir()
    store = PublisherFiles(files)
    city = {"place": "1714000", "crs": "EPSG:32616", "region": "illinois"}
    land, counties = geography(store, "chicago", city, dest)
    assert counties == ["17031"]
    assert json.loads((dest / "rook.json").read_text())["neighbors"] == {
        "17031000100": ["17031000200"],
        "17031000200": ["17031000100"],
    }
    shape = box(-87.656, 41.884, -87.655, 41.885)
    feat = {
        "type": "Feature",
        "geometry": mapping(shape),
        "properties": {"height": 6.4},
    }
    ms = tmp_path / "ms.gz"
    with gzip.open(ms, "wb") as stream:
        stream.write((json.dumps(feat) + "\n").encode() * 2)
    files["microsoft/001.geojsonl.gz"] = ms
    microsoft(
        store,
        {"001": {"url": "https://test.invalid/ms", "sha256": sha(ms)}},
        ["001"],
        land,
        city["crs"],
        dest / "ms.parquet",
    )
    assert gpd.read_parquet(dest / "ms.parquet").building_id.tolist() == [
        "global20260813:msft_global_001_000000001"
    ]
    raw = tmp_path / "nsi.seq"
    with raw.open("wb") as stream:
        for ident, units in enumerate([0, 2]):
            f = {
                "type": "Feature",
                "properties": dict(
                    fd_id=ident,
                    bid="A",
                    occtype="RES1",
                    resunits=units,
                    num_story=2,
                    bldheight=10,
                    sqft=200,
                    ftprntsqft=100,
                ),
                "geometry": {"type": "Point", "coordinates": [-87.6555, 41.8845]},
            }
            stream.write(b"\x1e" + json.dumps(f).encode() + b"\n")
    files["nsi/17031.geojsonseq"] = raw
    nsi(store, counties, land, dest / "nsi.parquet")
    assert gpd.read_parquet(dest / "nsi.parquet").resunits.tolist() == [0, 2]
    coordinates = [
        (-87.656, 41.884),
        (-87.655, 41.884),
        (-87.655, 41.885),
        (-87.656, 41.885),
        (-87.654, 41.884),
    ]
    nodes = [
        f'<node id="{i}" lat="{y}" lon="{x}" version="1" timestamp="2026-10-25T15:00:00Z"/>'
        for i, (x, y) in enumerate(coordinates, 1)
    ]

    def way(ident, refs, tags):
        return (
            f'<way id="{ident}" version="1" timestamp="2026-10-25T15:00:00Z">'
            + "".join(f'<nd ref="{r}"/>' for r in refs)
            + tags
            + "</way>"
        )

    ways = [
        way(
            1,
            [1, 2, 3, 4, 1],
            '<tag k="building" v="warehouse"/><tag k="operator" v="UPS"/>',
        ),
        way(2, [1, 2, 5], '<tag k="highway" v="residential"/>'),
    ]
    source = tmp_path / "map.osm"
    source.write_text('<osm version="0.6">' + "".join(nodes + ways) + "</osm>")
    pbf = tmp_path / "map.osm.pbf"
    subprocess.run(
        [
            "osmium",
            "cat",
            str(source),
            "-o",
            str(pbf),
            "--output-header",
            "osmosis_replication_timestamp=2026-10-25T21:00:00Z",
        ],
        check=True,
    )
    files["osm/illinois-261025.osm.pbf"] = pbf
    road = layers(store, "chicago", city, dest, land, "2026-10-25")
    assert road["sha256"] == sha(pbf)
    assert gpd.read_parquet(dest / "osm.parquet").osm_id.tolist() == ["w1"]
    assert gpd.read_parquet(dest / "depot.parquet").source_osm_id.tolist() == ["w1"]
    assert gpd.read_parquet(dest / "roads/osm_ways.parquet").osmid.tolist() == ["2"]


def test_download_and_replay_lock(tmp_path, monkeypatch):
    class Response:
        ok = True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def iter_content(self, size):
            yield b"raw"

    monkeypatch.setattr("sources.download.requests.get", lambda *args, **kw: Response())
    url = "https://test.invalid/independent"
    first = Store(tmp_path / "host1")
    first.fetch("source", url)
    second = Store(tmp_path / "host2", {"files": first.records})
    assert second.fetch("source", url).read_bytes() == b"raw"
    second.fetch("source", url).write_bytes(b"bad")
    with pytest.raises(ValueError, match="Cached source"):
        second.fetch("source", url)


def test_order_reference_uses_values_and_row_order(tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from sources.orders import logical_hashes

    tables = {
        "service": dict(
            donor_id=["A", "B"],
            source_station_day_id=["S", "S"],
            package_count=[1, 1],
            service_s=[8.0, 9.0],
        ),
        "windows": dict(donor_id=["B"], a=[0.0], b=[120.0]),
        "bundles": dict(donor_id=["A", "B"], parcel_rows=[[0], [1]]),
        "parcels": dict(volume_cm3=[100.0, 180.0], source_parcel_ref=["p0", "p1"]),
    }
    for k, v in tables.items():
        pq.write_table(pa.table(v), tmp_path / (k + ".parquet"))
    expected = logical_hashes(tmp_path)
    for k, v in tables.items():
        pq.write_table(
            pa.table(v),
            tmp_path / (k + ".parquet"),
            compression="gzip",
            row_group_size=1,
        )
    assert logical_hashes(tmp_path) == expected
    p = tmp_path / "parcels.parquet"
    pq.write_table(pa.table(tables["parcels"]).take([1, 0]), p)
    assert logical_hashes(tmp_path)[p.name] != expected[p.name]


def test_interrupted_stage1_component_rebuilds_in_place(tmp_path, monkeypatch):
    from evrptw_cle import stage1_components as comp
    from stage2.common import write

    inventory = tmp_path / "inventory.json"
    write(inventory, {})
    project = {
        "output": str(tmp_path),
        "components": {
            "inventory": str(inventory),
            "road_sources": str(tmp_path),
            "source_root": str(tmp_path),
        },
    }
    base = tmp_path / "cities/chicago"
    calls = []

    def build(kind, **kwargs):
        calls.append(kind)
        (base / "roads").mkdir()
        (base / "roads/attempt").write_text(str(len(calls)))
        if len(calls) == 1:
            raise OSError("interrupted")
        return {"complete": True}

    monkeypatch.setattr(comp, "build_stage1_noncustomer_component", build)
    opts = dict(project=project, city="chicago", component="roads")
    with pytest.raises(OSError, match="interrupted"):
        comp.build_component(**opts)
    assert comp.build_component(**opts) == {"complete": True}
    assert (base / "roads/attempt").read_text() == "2"
    assert next((tmp_path / "interrupted").rglob("attempt")).read_text() == "1"
    assert not (base / ".building-roads").exists()


def test_partial_request_refuses_mixed_city_dates(tmp_path):
    from sources.prepare import prepare, source_code_hash, SPEC
    from stage2.common import write

    receipt = dict(
        policy=dict(
            source_code=source_code_hash(), spec=sha(SPEC), osm_date="2026-11-01"
        ),
        sources={},
        outputs={},
    )
    write(tmp_path / "public/chicago/receipt.json", receipt)
    with pytest.raises(ValueError, match="different OSM date"):
        prepare(tmp_path, "cities", [], 1, "2026-11-02", None)
    assert not (tmp_path / "public/sources.lock.json").exists()


def test_partial_fetch_preserves_its_input_lock(tmp_path):
    from sources.prepare import prepare
    from stage2.common import read, write

    ref = tmp_path / "public/sources.lock.json"
    content = {
        "schema": "evrptwd_public_source_lock_v1",
        "files": {
            "still-needed": {
                "url": "https://example.org/file",
                "sha256": "a" * 64,
                "bytes": 1,
            }
        },
    }
    write(ref, content)
    prepare(tmp_path, "cities", [], 1, "latest", ref)
    assert read(ref) == content


def test_source_lock_supplies_snapshot_date(tmp_path):
    from sources.prepare import prepare, source_code_hash, SPEC
    from stage2.common import read, write

    ref = tmp_path / "reference.json"
    write(
        ref,
        {
            "schema": "evrptwd_public_source_lock_v1",
            "files": {},
            "osm_date": "2026-11-01",
        },
    )
    write(
        tmp_path / "public/chicago/receipt.json",
        dict(
            policy=dict(
                source_code=source_code_hash(), spec=sha(SPEC), osm_date="2026-11-01"
            ),
            sources={},
        ),
    )
    prepare(tmp_path, "cities", [], 1, None, ref)
    assert read(tmp_path / "public/sources.lock.json")["osm_date"] == "2026-11-01"
    with pytest.raises(ValueError, match="conflicts"):
        prepare(tmp_path, "cities", [], 1, "2026-11-02", ref)
