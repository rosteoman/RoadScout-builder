"""Region definitions for package builds."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class RegionDef:
    region_id: str
    region_name: str
    locale_names: Dict[str, str]
    prefectures: List[str]
    geofabrik_path: str  # relative to https://download.geofabrik.de/
    # Approximate bbox used for manifest when computed from data fails
    bbox_hint: List[float]  # minLon, minLat, maxLon, maxLat


REGIONS: Dict[str, RegionDef] = {
    "kansai": RegionDef(
        region_id="kansai",
        region_name="Kansai",
        locale_names={
            "ja": "関西",
            "en": "Kansai",
            "es": "Kansai",
        },
        prefectures=[
            "Osaka",
            "Kyoto",
            "Hyogo",
            "Nara",
            "Shiga",
            "Wakayama",
        ],
        geofabrik_path="asia/japan/kansai-latest.osm.pbf",
        bbox_hint=[134.25, 33.40, 136.50, 35.80],
    ),
}


def get_region(region_id: str) -> RegionDef:
    try:
        return REGIONS[region_id]
    except KeyError as exc:
        known = ", ".join(sorted(REGIONS))
        raise SystemExit(f"Región desconocida: {region_id}. Conocidas: {known}") from exc
