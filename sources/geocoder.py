#!/usr/bin/env python3
"""Geocode AFDC street addresses with the public U.S. Census batch geocoder.

The output is address-level QA evidence.  It is not treated as an exact charger
parking-space coordinate by the downstream resolver.
"""

from __future__ import annotations

import csv
import io

import pandas as pd


ENDPOINT = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
EXPECTED_AFDC_COLUMNS = {"ID", "Street Address", "City", "State", "ZIP"}


def _input_csv(frame: pd.DataFrame) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    for row in frame.itertuples(index=False):
        writer.writerow(row)
    return stream.getvalue().encode("utf-8")


def _parse_response(payload: str) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for values in csv.reader(io.StringIO(payload)):
        if not values:
            continue
        padded = values + [""] * (8 - len(values))
        (
            afdc_id,
            input_address,
            match,
            match_type,
            matched_address,
            coordinates,
            tigerline,
            side,
        ) = padded[:8]
        longitude = latitude = None
        if coordinates:
            pieces = coordinates.split(",", maxsplit=1)
            if len(pieces) == 2:
                try:
                    longitude, latitude = float(pieces[0]), float(pieces[1])
                except ValueError:
                    longitude = latitude = None
        rows.append(
            {
                "afdc_id": int(float(afdc_id)),
                "census_input_address": input_address,
                "census_match_status": match.strip().lower() or "unknown",
                "census_match_type": match_type,
                "census_matched_address": matched_address,
                "census_longitude": longitude,
                "census_latitude": latitude,
                "census_tigerline_id": tigerline,
                "census_side": side,
            }
        )
    return pd.DataFrame(rows)
