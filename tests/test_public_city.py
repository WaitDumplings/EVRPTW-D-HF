import ast
import sys
from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from test_solver_parquet import example

import export_output
from instance_file import HF_FORMAT, write_instance
from public_city import LAYOUT, ROAD_FILE, SHARED_FILES, verify_public_city
from solver_parquet import HF_INFO_FIELDS
from stage2.common import read, sha, shared_identity, write


def file_record(path):
    return {"bytes": path.stat().st_size, "sha256": sha(path)}


@pytest.fixture
def release(tmp_path):
    root = tmp_path / "release"
    manifest = {"layout": LAYOUT, "cities": {}, "city_files": {}}
    for city in ("chicago", "dallas"):
        base = root / "cities" / city
        shared = base / "shared"
        shared.mkdir(parents=True)
        for name in SHARED_FILES:
            if not name.endswith(".parquet"):
                continue
            pq.write_table(pa.table({"city": [city]}), shared / name)
        write(shared / "rook.json", {"neighbors": {}})
        write(
            shared / "boundary.geojson",
            {
                "type": "Polygon",
                "coordinates": [
                    [[-88, 41], [-87, 41], [-87, 43], [-88, 43], [-88, 41]]
                ],
            },
        )
        meta = {
            "schema": "evrptwd_shared_v1",
            "city": city,
            "files": {name: sha(shared / name) for name in SHARED_FILES},
            "source": {"cle": "a" * 64},
            "calibration": "b" * 64,
            "vintage": "c" * 64,
        }
        write(shared / "shared.json", meta)
        road = base / ROAD_FILE
        road.parent.mkdir()
        pq.write_table(pa.table({"osmid": ["123"]}), road)
        files = {
            f"shared/{name}": file_record(shared / name)
            for name in (*SHARED_FILES, "shared.json")
        }
        files[ROAD_FILE] = {
            **file_record(road),
            "path": road.relative_to(root).as_posix(),
        }
        manifest["city_files"][city] = files
        manifest["cities"][city] = {
            "city": city,
            "shared_sha256": shared_identity(shared),
            "cle_manifest_sha256": meta["source"]["cle"],
            "calibration_sha256": meta["calibration"],
        }
    write(root / "release.json", manifest)
    return root


@pytest.mark.parametrize(
    "name",
    [*(f"shared/{name}" for name in SHARED_FILES), "shared/shared.json", ROAD_FILE],
)
def test_corrupt_public_city_asset(release, name):
    path = release / "cities/chicago" / name
    data = path.read_bytes()
    path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    with pytest.raises(ValueError, match="Damaged public city asset"):
        verify_public_city(release, "chicago")


@pytest.mark.parametrize("change", ["schema", "city", "extra", "missing", "digest"])
def test_shared_identity_and_fixed_file_list(release, change):
    path = release / "cities/chicago/shared/shared.json"
    meta = read(path)
    if change == "schema":
        meta["schema"] = "another_schema"
    elif change == "city":
        meta["city"] = "dallas"
    elif change == "extra":
        meta["files"]["../unlisted"] = "0" * 64
    elif change == "missing":
        del meta["files"]["access.parquet"]
    else:
        meta["files"]["access.parquet"] = "0" * 64
    write(path, meta)
    manifest = read(release / "release.json")
    manifest["city_files"]["chicago"]["shared/shared.json"] = file_record(path)
    write(release / "release.json", manifest)
    with pytest.raises(ValueError, match="Different shared city or file inventory"):
        verify_public_city(release, "chicago")


@pytest.mark.parametrize("extra", [True, False])
def test_unexpected_shared_contents(release, extra):
    shared = release / "cities/chicago/shared"
    if extra:
        (shared / "extra.txt").touch()
    else:
        (shared / "speeds.parquet").unlink()
    with pytest.raises(ValueError, match="Unexpected shared directory contents"):
        verify_public_city(release, "chicago")


def test_public_city_rejects_foreign_bindings_and_paths(release):
    chicago = verify_public_city(release, "chicago")
    with pytest.raises(ValueError, match="Different public city or shared version"):
        verify_public_city(release, "dallas", expected=chicago)
    with pytest.raises(ValueError, match="Different public city or shared version"):
        verify_public_city(
            release, "chicago", expected={**chicago, "shared_sha256": "0" * 64}
        )
    with pytest.raises(ValueError, match="Invalid public city"):
        verify_public_city(release, "../chicago")
    manifest = read(release / "release.json")
    manifest["city_files"]["chicago"][ROAD_FILE]["path"] = "../elsewhere.parquet"
    write(release / "release.json", manifest)
    with pytest.raises(ValueError, match="Wrong public road path"):
        verify_public_city(release, "chicago")


@pytest.mark.parametrize("change", ["remove_manifest", "remove_layout", "wrong_layout"])
def test_two_part_tree_cannot_fall_back_to_internal_verification(
    release, monkeypatch, change
):
    path = release / "release.json"
    if change == "remove_manifest":
        path.unlink()
    else:
        manifest = read(path)
        if change == "remove_layout":
            del manifest["layout"]
        else:
            manifest["layout"] = "unknown-layout"
        write(path, manifest)
    monkeypatch.setattr(
        export_output, "verify_city", lambda *a, **kw: pytest.fail("legacy verifier")
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["export_output.py", "--out", str(release), "verify-cle", "--city", "chicago"],
    )
    with pytest.raises(
        ValueError,
        match="Missing public release manifest|Unknown public release layout",
    ):
        export_output.main()


def test_cached_snapshot_from_another_directory(release, tmp_path, monkeypatch):
    # Hub snapshots hold symlinks to content-addressed blobs outside the snapshot.
    snapshot = tmp_path / "snapshots" / "revision"
    for path in release.rglob("*"):
        target = snapshot / path.relative_to(release)
        if path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(path)
    monkeypatch.chdir(tmp_path)
    assert (
        verify_public_city(Path("snapshots/revision"), "chicago")
        == read(release / "release.json")["cities"]["chicago"]
    )


def test_public_verify_cli_and_plot(release, tmp_path, monkeypatch, capsys):
    binding = read(release / "release.json")["cities"]["chicago"]
    task, _ = example(tmp_path / "instance")
    info = {k: task.info[k] for k in HF_INFO_FIELDS}
    info["source_shared_sha256"] = binding["shared_sha256"]
    instance = tmp_path / "example.parquet"
    write_instance(instance, replace(task, info=info), binding, schema=HF_FORMAT)

    args = [
        "export_output.py",
        "--out",
        str(release),
        "verify-cle",
        "--city",
        "chicago",
    ]
    for options in ([], ["--instance", str(instance)]):
        monkeypatch.setattr(sys, "argv", args + options)
        export_output.main()
        assert ast.literal_eval(capsys.readouterr().out.strip()) == binding

    png = tmp_path / "map.png"
    assert export_output.plot_instance(instance, release, png) == png
    assert png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
