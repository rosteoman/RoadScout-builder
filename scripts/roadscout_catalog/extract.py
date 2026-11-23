"""Extract included highway ways from an OSM PBF using osmium."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import osmium

from .classify import (
    classify_type,
    expand_memberships,
    preferred_name,
)
from .filters import is_included_way

Coord = Tuple[float, float]


@dataclass
class RawWay:
    way_id: int
    tags: Dict[str, str]
    coords: List[Coord]
    road_type: str
    key_kind: str
    key_value: str
    name: Optional[str] = None
    ref: Optional[str] = None
    network: Optional[str] = None
    name_ja: Optional[str] = None
    name_en: Optional[str] = None


class _HighwayHandler(osmium.SimpleHandler):
    def __init__(self) -> None:
        super().__init__()
        self.ways: List[RawWay] = []
        self.skipped = 0
        self.seen = 0
        self.membership_emits = 0

    def way(self, w: osmium.osm.Way) -> None:
        self.seen += 1
        tags = {t.k: t.v for t in w.tags}
        if not is_included_way(tags):
            self.skipped += 1
            return
        if not w.is_closed() and len(w.nodes) < 2:
            self.skipped += 1
            return
        coords: List[Coord] = []
        try:
            for node in w.nodes:
                if not node.location.valid():
                    coords = []
                    break
                coords.append((float(node.location.lon), float(node.location.lat)))
        except osmium.InvalidLocationError:
            self.skipped += 1
            return
        if len(coords) < 2:
            self.skipped += 1
            return

        road_type = classify_type(tags)
        name = preferred_name(tags)
        memberships = expand_memberships(tags, road_type)
        if not memberships:
            self.skipped += 1
            return

        for m_type, key_kind, key_value, ref in memberships:
            self.membership_emits += 1
            self.ways.append(
                RawWay(
                    way_id=int(w.id),
                    tags=tags,
                    coords=coords,
                    road_type=m_type,
                    key_kind=key_kind,
                    key_value=key_value if key_value else f"way:{w.id}",
                    name=name,
                    ref=ref,
                    network=tags.get("network"),
                    name_ja=tags.get("name:ja"),
                    name_en=tags.get("name:en"),
                )
            )


def extract_ways(pbf_path: str) -> Tuple[List[RawWay], Dict[str, int]]:
    handler = _HighwayHandler()
    handler.apply_file(pbf_path, locations=True)
    stats = {
        "ways_seen": handler.seen,
        "ways_included": len({w.way_id for w in handler.ways}),
        "membership_emits": handler.membership_emits,
        "ways_skipped": handler.skipped,
    }
    return handler.ways, stats
