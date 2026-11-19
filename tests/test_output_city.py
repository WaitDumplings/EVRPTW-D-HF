import shutil

import pytest

from evrptw_scenarios.cle_binding import BUILDING_SCHEMA
from export_output import publish_city, verify_city
from stage2.common import CITIES, read, sha, write


def test_city_copy_binding_and_conflicts(tmp_path):
    work = tmp_path / "work"
    city = "chicago"
    base, shared = work / "stage1/cities" / city, work / "shared" / city
    write(base / "roads/road.json", {"one": 1})
    write(
        base / "cle_manifest.json",
        {
            "city_slug": city,
            "schema": BUILDING_SCHEMA,
            "files": {"roads/road.json": {"sha256": sha(base / "roads/road.json")}},
            "sources": {"inventory": {"sha256": "inventory"}},
        },
    )
    cle = sha(base / "cle_manifest.json")
    write(base / "stage1_validation.json", {"passed": True, "cle_manifest_sha256": cle})
    validation = sha(base / "stage1_validation.json")
    write(
        base / "public_authority.json",
        {
            "schema": "evrptwd_public_city_v1",
            "city": city,
            "calibration_cohort": CITIES,
            "inputs_sha256": "inventory",
            "validation_sha256": validation,
            "cle_manifest_sha256": cle,
            "calibration_sha256": "cal",
        },
    )
    write(shared / "rook.json", {"neighbors": {}})
    write(
        shared / "shared.json",
        {
            "city": city,
            "source": {
                "cle": cle,
                "cohort": sha(base / "public_authority.json"),
                "validation": validation,
            },
            "files": {"rook.json": sha(shared / "rook.json")},
            "vintage": "inventory",
            "calibration": "cal",
        },
    )
    output = tmp_path / "output"
    dest = publish_city(work, output, city)
    assert publish_city(work, output, city) == dest
    new = tmp_path / "other-machine"
    shutil.copytree(dest, new)
    binding = verify_city(new, city)
    with pytest.raises(ValueError, match="Different CLE"):
        verify_city(new, city, {**binding, "shared_sha256": "other"})
    write(new / "shared/rook.json", {"neighbors": {"x": []}})
    with pytest.raises(ValueError, match="Damaged CLE asset"):
        verify_city(new, city)
    write(base / "roads/road.json", {"different": 2})
    other = tmp_path / "failed"
    with pytest.raises(ValueError, match="Damaged CLE asset"):
        publish_city(work, other, city)
    assert not (other / "CLE" / city).exists()
    meta = read(shared / "shared.json")
    meta["source"]["cle"] = "wrong"
    write(shared / "shared.json", meta)
    with pytest.raises(ValueError, match="do not match"):
        publish_city(work, other, city)
