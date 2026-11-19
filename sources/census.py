"""Census place selection and county overlaps used by the 2025 land-mask method."""

import geopandas as gpd
from shapely import make_valid
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.ops import unary_union
from pathlib import Path
from typing import Any


def _polygonal_only(geometry: Any):
    geometry = make_valid(geometry)
    if isinstance(geometry, (Polygon, MultiPolygon)):
        return geometry
    if isinstance(geometry, GeometryCollection):
        polygons = [
            part for part in geometry.geoms if isinstance(part, (Polygon, MultiPolygon))
        ]
        return unary_union(polygons) if polygons else Polygon()
    return Polygon()


def _select_place(place_archive: Path, geoid: str) -> gpd.GeoDataFrame:
    places = gpd.read_file(f"zip://{place_archive}")
    selected = places.loc[places["GEOID"].astype(str) == geoid].copy()
    if len(selected) != 1:
        raise RuntimeError(f"Census Place GEOID {geoid} matched {len(selected)} rows")
    return selected.to_crs("EPSG:4326")


def _county_geoids(
    city: gpd.GeoDataFrame, counties: gpd.GeoDataFrame, state_fips: str
) -> list[str]:
    local_crs = city.estimate_utm_crs()
    city_geometry = city.to_crs(local_crs).geometry.iloc[0]
    candidates = counties.loc[
        counties["STATEFP"].astype(str).str.zfill(2) == state_fips
    ].to_crs(local_crs)
    overlaps = candidates.geometry.map(
        lambda geometry: geometry.intersection(city_geometry).area
    )
    selected = candidates.loc[overlaps > 1.0]
    if selected.empty:
        raise RuntimeError("No positive-area county overlap for Census Place")
    return sorted(selected["GEOID"].astype(str).str.zfill(5).tolist())
