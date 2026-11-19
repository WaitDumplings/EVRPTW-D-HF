"""Bind a Stage 2 input to its verified Stage 1 data authority."""

import json
from pathlib import Path

from stage2.common import sha as sha256_file


BUILDING_SCHEMA = "evrptw_building_first_cle_v1"
R1_FREEZE_ID = "stage1-20261102-r1"
R1_FREEZE_SHA256 = "9db2933e7f83ffce3b3917dbad8942111db19683066e9b69d603f455fc34cfde"


def cle_authority(city_dir, manifest):
    """Return the accepted authority and independent city validation paths."""
    root = Path(city_dir)
    digest = sha256_file(root / "cle_manifest.json")
    city = manifest["city_slug"]
    if (
        manifest["schema"] == BUILDING_SCHEMA
        and (root / "public_authority.json").is_file()
    ):
        from stage2.common import CITIES

        authority = root / "public_authority.json"
        validation = root / "stage1_validation.json"
        receipt = json.loads(authority.read_text())
        if (
            receipt.get("schema")
            not in {"evrptwd_public_city_v1", "evrptwd_local_city_v1"}
            or receipt.get("city") != city
            or set(receipt.get("calibration_cohort", []))
            != ({city} if receipt["schema"] == "evrptwd_local_city_v1" else set(CITIES))
            or receipt.get("inputs_sha256")
            != manifest.get("sources", {}).get("inventory", {}).get("sha256")
            or receipt.get("validation_sha256") != sha256_file(validation)
            or receipt.get("cle_manifest_sha256") != digest
        ):
            raise ValueError(
                "Public source city receipt does not match CLE and validation"
            )
    elif manifest["schema"] == BUILDING_SCHEMA:
        if root.parent.name != "cities" or root.name != city:
            raise ValueError(
                "Building CLE must be in its verified Stage 1 cohort: cities/<city>"
            )
        authority = root.parent.parent / "stage1_server_build_receipt.json"
        validation = root / "stage1_validation.json"
        receipt = json.loads(authority.read_text())
        cohort = receipt.get("cle_manifests", {})
        if (
            receipt.get("schema") != "evrptw_stage1_server_build_receipt_v1"
            or receipt.get("freeze_id") != R1_FREEZE_ID
            or receipt.get("freeze_manifest_sha256") != R1_FREEZE_SHA256
            or receipt.get("complete_cle_validation_passed") is not True
            or set(cohort) != set(receipt.get("validated_cities", []))
            or len(cohort) != 11
            or cohort.get(city) != digest
        ):
            raise ValueError(
                "Stage 1 cohort receipt does not authorize this building CLE"
            )
    elif manifest["schema"] == "evrptw_city_logistics_environment_v1":
        authority = root / manifest["freeze_authority"]
        validation = root / "cle_verification.json"
        receipt = json.loads(authority.read_text())
        entry = receipt.get("cities", {}).get(city, {})
        if (
            receipt.get("frozen") is not True
            or entry.get("cle_manifest_sha256") != digest
            or entry.get("verification_sha256") != sha256_file(validation)
        ):
            raise ValueError("Historical freeze does not authorize this CLE")
    else:
        raise ValueError("Unsupported input CLE schema")
    certificate = json.loads(validation.read_text())
    if (
        certificate.get("passed") is not True
        or certificate.get("cle_manifest_sha256") != digest
    ):
        raise ValueError("Stage 1 validation is missing or bound to another CLE")
    return authority, validation
