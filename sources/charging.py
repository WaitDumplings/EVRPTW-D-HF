"""AFDC station and EVSE exports, with Census evidence for address-level stations."""

import os
import pandas as pd
import requests
from .geocoder import ENDPOINT, _input_csv, _parse_response
from stage2.common import sha, write

BASE = "https://developer.nlr.gov/api/alt-fuel-stations/v1"
QUERY = "?fuel_type=ELEC&access=public&status=E&country=US&limit=all"


def build(store, output):
    files = {}
    for name, endpoint in [
        ("station", BASE + ".csv"),
        ("units", BASE + "/ev-charging-units.csv"),
    ]:
        url = endpoint + QUERY
        path = store.fetch(
            f"afdc/{name}.csv",
            url,
            params={"api_key": os.environ.get("AFDC_API_KEY", "DEMO_KEY")},
        )
        table = pd.read_csv(path, low_memory=False, dtype={"ZIP": str})
        from evrptw_cle.charging_component import _source_scope

        _source_scope(table, name)
        if table.empty:
            raise ValueError("AFDC returned an empty export")
        if name == "station":
            stations = table
        files[name] = path
        write(output / (name + ".json"), store.records[f"afdc/{name}.csv"])
    # AFDC GPS coordinates are already used directly. The Census request applies to address geocodes.
    from evrptw_cle.charger_resolution import normalize_text

    addresses = stations.loc[
        stations["Geocode Status"].map(normalize_text).eq("200 8")
    ].sort_values("ID")
    from evrptw_cle.public_locations import excluded_location_reason

    addresses = addresses.loc[
        [
            not excluded_location_reason(row, purpose="charging")
            for _, row in addresses.iterrows()
        ]
    ]
    rows = []
    for start in range(0, len(addresses), 5000):
        part = addresses.iloc[start : start + 5000][
            ["ID", "Street Address", "City", "State", "ZIP"]
        ].fillna("")
        payload = _input_csv(part)
        import hashlib

        key = "afdc/census-" + hashlib.sha256(payload).hexdigest() + ".csv"
        path = store.fetch(
            key,
            ENDPOINT,
            post=lambda: requests.post(
                ENDPOINT,
                data={"benchmark": "Public_AR_Current"},
                files={"addressFile": ("addresses.csv", payload, "text/csv")},
                stream=True,
                timeout=(30, 300),
            ),
        )
        results = _parse_response(path.read_text())
        if sorted(results.afdc_id.tolist()) != sorted(part.ID.astype(int).tolist()):
            raise ValueError("Incomplete Census address geocodes")
        rows.append(results)
    census = (
        pd.concat(rows).sort_values("afdc_id")
        if rows
        else pd.DataFrame(
            columns=[
                "afdc_id",
                "census_match_status",
                "census_input_address",
                "census_longitude",
                "census_latitude",
            ]
        )
    )
    census.to_csv(output / "census.csv", index=False)
    write(
        output / "census.json",
        dict(
            benchmark="Public_AR_Current",
            station_sha256=sha(files["station"]),
            rows=len(census),
            sources=sorted(k for k in store.records if k.startswith("afdc/census-")),
        ),
    )
