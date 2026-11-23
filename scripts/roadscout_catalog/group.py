"""Group raw OSM ways into logical catalog roads."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .classify import make_road_id
from .extract import RawWay
from .geometry import (
    as_geojson_geometry,
    bbox_of_components,
    connect_segments,
    multiline_length_m,
)

Coord = Tuple[float, float]


@dataclass
class LogicalRoad:
    id: str
    name: Optional[str]
    ref: Optional[str]
    type: str
    length_m: float
    region_id: str
    continuous: bool
    segment_count: int
    osm_way_ids: List[int]
    geometry: dict
    network: Optional[str] = None
    name_ja: Optional[str] = None
    name_en: Optional[str] = None
    gap_count: int = 0
    toll_lines: List[List[Coord]] = field(default_factory=list)


@dataclass
class Discontinuity:
    road_id: str
    component_count: int
    gap_count: int
    note: str = "OSM ways no conectan por nodos/extremos cercanos"


def group_ways(ways: List[RawWay], region_id: str) -> Tuple[List[LogicalRoad], List[Discontinuity]]:
    buckets: Dict[str, List[RawWay]] = defaultdict(list)
    for way in ways:
        # Unnamed one-off ways: keep each as its own road (still searchable by id)
        if way.key_kind == "way":
            buckets[f"way:{way.way_id}"].append(way)
        else:
            buckets[way.key_value].append(way)

    roads: List[LogicalRoad] = []
    discontinuities: List[Discontinuity] = []

    for key_value, group in buckets.items():
        way_ids = sorted(w.way_id for w in group)
        # Majority type
        type_counts: Dict[str, int] = defaultdict(int)
        for w in group:
            type_counts[w.road_type] += 1
        road_type = sorted(type_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

        name = _pick_name(group)
        ref = _pick_ref(group)
        network = next((w.network for w in group if w.network), None)
        name_ja = next((w.name_ja for w in group if w.name_ja), None)
        name_en = next((w.name_en for w in group if w.name_en), None)

        key_kind = group[0].key_kind if group[0].key_kind != "way" else "way"
        road_id = make_road_id(region_id, key_kind, key_value, way_ids)

        components, gap_count = connect_segments([w.coords for w in group])
        length_m = multiline_length_m(components)
        continuous = len(components) <= 1
        geometry = as_geojson_geometry(components) if components else {
            "type": "LineString",
            "coordinates": [],
        }

        roads.append(
            LogicalRoad(
                id=road_id,
                name=name,
                ref=ref,
                type=road_type,
                length_m=round(length_m, 1),
                region_id=region_id,
                continuous=continuous,
                segment_count=len(components),
                osm_way_ids=way_ids,
                geometry=geometry,
                network=network,
                name_ja=name_ja,
                name_en=name_en,
                gap_count=gap_count,
            )
        )
        if not continuous:
            discontinuities.append(
                Discontinuity(
                    road_id=road_id,
                    component_count=len(components),
                    gap_count=gap_count,
                )
            )

    roads.sort(key=lambda r: (r.type, r.ref or "", r.name or "", r.id))
    return roads, discontinuities


def _pick_name(group: List[RawWay]) -> Optional[str]:
    for w in group:
        if w.name:
            return w.name
    return None


def _pick_ref(group: List[RawWay]) -> Optional[str]:
    for w in group:
        if w.ref:
            return w.ref
    return None


def union_bbox(roads: List[LogicalRoad], fallback: List[float]) -> List[float]:
    if not roads:
        return list(fallback)
    min_lon = min_lat = float("inf")
    max_lon = max_lat = float("-inf")
    for road in roads:
        comps: List[List[Coord]]
        geom = road.geometry
        if geom["type"] == "LineString":
            comps = [geom["coordinates"]]
        else:
            comps = geom["coordinates"]
        bb = bbox_of_components(comps)
        min_lon = min(min_lon, bb[0])
        min_lat = min(min_lat, bb[1])
        max_lon = max(max_lon, bb[2])
        max_lat = max(max_lat, bb[3])
    return [min_lon, min_lat, max_lon, max_lat]
