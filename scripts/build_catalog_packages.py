#!/usr/bin/env python3
"""Build RoadScout region zips for every area in the download catalog.

Each region is read from the Geofabrik extract that contains it. Every
road and path enters the package: numbered roads group by type and number
when the pieces touch, named roads group by name, and an unnamed road is
one continuous path. The same number in two places that do not touch is
two roads.
The zip is published on the public package release.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import osmium
from shapely import make_valid
from shapely.geometry import LineString, Point, Polygon, shape
from shapely.prepared import prep
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from roadscout_catalog.classify import classify_type, normalize_ref, preferred_name
from roadscout_catalog.filters import dashed_catalog_type, is_included_way
from roadscout_catalog.relations_build import classify_relation
from roadscout_catalog.geometry import (
    as_geojson_geometry,
    group_continuous_chains,
    group_straight_chains,
    group_touching_chains,
    multiline_length_m,
)
from roadscout_catalog.overpass_fetch import (
    fetch_relation_polygon,
    fetch_way_geometries,
    fetch_ways_by_ref,
)

GEOFABRIK = "https://download.geofabrik.de"
OSM_FR = "https://download.openstreetmap.fr/extracts"
CATALOG = ROOT / "RoadScout" / "Resources" / "download-areas.json"
RELEASE_REPO = "rosteoman/RoadScout-regions"
RELEASE_TAG = "regiones"
PACKAGE_VERSION = 9
# Primeras versiones: solo España y Japón.
OFFERED_COUNTRY_IDS = {"spain", "japan"}
BUILDER_ID = "catalog-v2-admin-boundary"

# Geofabrik (nov 2026) sirve 404 en varios *-latest de ES; OSM.fr sí tiene extractos.
# clip=False cuando el PBF ya es la región; True si hay que recortar un extracto mayor.
EXTRACT_OVERRIDES: Dict[str, Tuple[str, bool]] = {
    "andalucia": (f"{OSM_FR}/europe/spain/andalucia-latest.osm.pbf", False),
    "aragon": (f"{OSM_FR}/europe/spain/aragon-latest.osm.pbf", False),
    "asturias": (f"{OSM_FR}/europe/spain/asturias-latest.osm.pbf", False),
    "cantabria": (f"{OSM_FR}/europe/spain/cantabria-latest.osm.pbf", False),
    "castilla-y-leon": (f"{OSM_FR}/europe/spain/castilla_y_leon-latest.osm.pbf", False),
    "castilla-la-mancha": (f"{OSM_FR}/europe/spain/castilla_la_mancha-latest.osm.pbf", False),
    "cataluna": (f"{OSM_FR}/europe/spain/catalunya-latest.osm.pbf", False),
    "extremadura": (f"{OSM_FR}/europe/spain/extremadura-latest.osm.pbf", False),
    "galicia": (f"{OSM_FR}/europe/spain/galicia-latest.osm.pbf", False),
    "islas-baleares": (f"{OSM_FR}/europe/spain/illes_balears-latest.osm.pbf", False),
    "la-rioja": (f"{OSM_FR}/europe/spain/la_rioja-latest.osm.pbf", False),
    "madrid": (f"{OSM_FR}/europe/spain/comunidad_de_madrid-latest.osm.pbf", False),
    "murcia": (f"{OSM_FR}/europe/spain/region_de_murcia-latest.osm.pbf", False),
    "navarra": (f"{OSM_FR}/europe/spain/comunidad_foral_de_navarra-latest.osm.pbf", False),
    "pais-vasco": (f"{OSM_FR}/europe/spain/euskadi-latest.osm.pbf", False),
    "valencia": (f"{OSM_FR}/europe/spain/comunitat_valenciana-latest.osm.pbf", False),
    "ceuta": (f"{OSM_FR}/africa/spain/ceuta-latest.osm.pbf", False),
    "melilla": (f"{OSM_FR}/africa/spain/melilla-latest.osm.pbf", False),
    "es-canarias": (f"{OSM_FR}/africa/spain/canarias-latest.osm.pbf", False),
    "chubu": (f"{OSM_FR}/asia/japan/chubu-latest.osm.pbf", False),
    "chugoku": (f"{OSM_FR}/asia/japan/chugoku-latest.osm.pbf", False),
    "hokkaido": (f"{OSM_FR}/asia/japan/hokkaido-latest.osm.pbf", False),
    "kansai": (f"{OSM_FR}/asia/japan/kansai-latest.osm.pbf", False),
    "kanto": (f"{OSM_FR}/asia/japan/kanto-latest.osm.pbf", False),
    "kyushu": (f"{OSM_FR}/asia/japan/kyushu-latest.osm.pbf", False),
    "shikoku": (f"{OSM_FR}/asia/japan/shikoku-latest.osm.pbf", False),
    "tohoku": (f"{OSM_FR}/asia/japan/tohoku-latest.osm.pbf", False),
}

JP_REGION_IDS = {
    "chubu", "chugoku", "hokkaido", "kansai", "kanto", "kyushu", "shikoku", "tohoku",
}
ES_REGION_IDS = {
    "andalucia", "aragon", "asturias", "cantabria", "castilla-y-leon",
    "castilla-la-mancha", "cataluna", "extremadura", "galicia",
    "islas-baleares", "la-rioja", "madrid", "murcia", "navarra",
    "pais-vasco", "valencia", "ceuta", "melilla",
}


def _resolve_data_dir(env_name: str, default: Path) -> Path:
    """Directorio usable: env, o default si es real/creable (no symlink roto)."""
    override = os.environ.get(env_name)
    if override:
        path = Path(override)
        path.mkdir(parents=True, exist_ok=True)
        return path
    if default.is_symlink():
        try:
            if default.resolve().exists():
                return default
        except OSError:
            pass
        default.unlink()
    if not default.exists():
        default.mkdir(parents=True, exist_ok=True)
        return default
    if default.is_dir():
        return default
    raise RuntimeError(f"ruta de datos no usable: {default}")


WORK = _resolve_data_dir("ROADSCOUT_WORK", ROOT / "catalog" / "packages" / "work")
ZIPS = _resolve_data_dir("ROADSCOUT_ZIPS", ROOT / "catalog" / "packages" / "zips")
INDEX_CACHE = Path(os.environ.get("ROADSCOUT_GEOFABRIK_INDEX", str(WORK / "geofabrik-index.json")))
# Sin mínimo de longitud: el documento de verdad no descarta carreteras cortas.
REQUIRED_ROADS = {
    "ceuta": ({"ref": "N-362", "min_length_m": 1_500.0},),
}
COMPLETE_BUFFER_DEG = 0.12
# Por encima de 14 la tesela estirada abre cortes. 16 cubre el zoom de guía.
CATALOG_MIN_ZOOM = 4
CATALOG_MAX_ZOOM = 16
# Píxeles de margen para que el corte del borde no se abra al acercar.
CATALOG_TILE_BUFFER = 64
# Calles de ciudad: se pueden ocultar. Prefectural y superior no entran aquí.
HIDEABLE_STREET_TYPES = {"municipal", "local", "other"}
URBAN_STREET_HIGHWAYS = {"residential", "living_street"}
URBAN_LANDUSE = {"residential", "commercial", "retail", "industrial"}

Coord = Tuple[float, float]


@dataclass
class Piece:
    way_id: int
    coords: List[Coord]
    name: Optional[str]
    name_en: Optional[str]
    ref: Optional[str]
    road_type: str
    in_region: bool = True
    urban: bool = False
    traversable: bool = True


# La relación route=road manda sobre el highway de la vía. Así la 162 no se parte en municipal.
_ROUTE_RANK = {
    "national": 0,
    "motorway": 1,
    "motorroad": 2,
    "prefectural_major": 3,
    "prefectural": 4,
}


@dataclass
class CatalogRoute:
    relation_id: int
    road_type: str
    ref: Optional[str]
    way_ids: List[int]


def best_route_membership(routes: Sequence[CatalogRoute]) -> Dict[int, Tuple[str, Optional[str]]]:
    """Cada vía se queda con la relación legal de mayor rango (nacional antes que municipal)."""
    chosen: Dict[int, Tuple[str, Optional[str]]] = {}
    rank: Dict[int, int] = {}
    for route in routes:
        if route.road_type not in _ROUTE_RANK:
            continue
        level = _ROUTE_RANK[route.road_type]
        for way_id in route.way_ids:
            if way_id not in rank or level < rank[way_id]:
                rank[way_id] = level
                chosen[way_id] = (route.road_type, route.ref)
    return chosen


def apply_route_membership(pieces: Sequence[Piece], membership: Dict[int, Tuple[str, Optional[str]]]) -> None:
    for piece in pieces:
        found = membership.get(piece.way_id)
        if not found or found[0] not in _ROUTE_RANK:
            continue
        piece.road_type = found[0]
        piece.urban = False
        if found[1]:
            piece.ref = found[1]


def promote_outside_pieces(pieces: Sequence[Piece]) -> None:
    """Un trozo fuera de la zona con el mismo número hereda el tipo legal de dentro."""
    legal: Dict[str, str] = {}
    for piece in pieces:
        if piece.in_region and piece.ref and piece.road_type in _ROUTE_RANK:
            legal.setdefault(piece.ref, piece.road_type)
    for piece in pieces:
        if piece.in_region or not piece.ref:
            continue
        inherited = legal.get(piece.ref)
        if inherited and piece.road_type not in _ROUTE_RANK:
            piece.road_type = inherited


@dataclass
class Source:
    url: str
    path: Path
    clip: bool


@dataclass
class Boundary:
    geometry: BaseGeometry
    osm_id: int
    name: Optional[str]


@dataclass
class Built:
    region_id: str
    zip_path: Path
    bytes: int
    roads: int


def _tag(tags: osmium.osm.TagList, key: str) -> Optional[str]:
    value = tags.get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _mapping(tags: osmium.osm.TagList) -> Dict[str, str]:
    return {t.k: t.v for t in tags}


def street_is_urban(road_type: str, highway: str) -> bool:
    """Calle de ciudad. Una prefectural o superior no se oculta aunque cruce la zona."""
    return road_type in HIDEABLE_STREET_TYPES and highway in URBAN_STREET_HIGHWAYS


def mark_urban_streets(pieces: List[Piece], polygons: Sequence[BaseGeometry]) -> None:
    """Marca tramos municipales cuyo centro cae en suelo urbano (landuse)."""
    pending = [
        piece for piece in pieces
        if not piece.urban and piece.road_type in HIDEABLE_STREET_TYPES and len(piece.coords) >= 2
    ]
    if not pending or not polygons:
        return
    tree = STRtree(list(polygons))
    for piece in pending:
        mid = piece.coords[len(piece.coords) // 2]
        point = Point(mid)
        for index in tree.query(point):
            if polygons[int(index)].covers(point):
                piece.urban = True
                break


def classify_scheme(tags: Dict[str, str], scheme: str) -> Optional[str]:
    if not is_included_way(tags):
        return None
    highway = (tags.get("highway") or "").strip()

    names = " ".join(
        part
        for part in (
            tags.get("name"),
            tags.get("name:ja"),
            tags.get("official_name"),
            tags.get("alt_name"),
        )
        if part
    )
    if highway == "track" or "林道" in names or highway in {"path", "bridleway"}:
        return "forest"
    if highway in {"footway", "pedestrian", "steps", "cycleway"}:
        return "municipal"
    if highway == "service" and scheme != "japan":
        return "municipal"

    if scheme == "japan":
        kind = classify_type(tags)
        if kind in {"local", "rindo"}:
            return {"local": "municipal", "rindo": "forest"}[kind]
        return kind

    network = (tags.get("network") or "").lower()
    if highway in {"motorway", "motorway_link"}:
        return "motorway"
    if (tags.get("motorroad") or "").lower() == "yes" or (tags.get("expressway") or "").lower() == "yes":
        return "motorroad"

    if scheme == "unitedStates":
        if "us:i" in network:
            return "motorway"
        if "us:us" in network:
            return "national"
        if highway in {"trunk", "trunk_link"}:
            return "motorroad"
        if highway in {"primary", "primary_link"}:
            return "prefectural_major"
        if highway in {"secondary", "secondary_link"}:
            return "prefectural"
        return "municipal"

    if scheme == "canada":
        if "transcanada" in network:
            return "national"
        if highway in {"trunk", "trunk_link"}:
            return "motorroad"
        if highway in {"primary", "primary_link"}:
            return "prefectural_major"
        if highway in {"secondary", "secondary_link"}:
            return "prefectural"
        return "municipal"

    if scheme == "korea":
        if "expressway" in network or "motorway" in network:
            return "motorway"
        if highway in {"trunk", "trunk_link"}:
            return "motorroad"
        if "national" in network or highway in {"primary", "primary_link"}:
            return "national"
        if "metropolitan" in network:
            return "prefectural_major"
        if highway in {"secondary", "secondary_link"}:
            return "prefectural"
        return "municipal"

    if "e-road" in network or network.endswith(":national") or ":a-road" in network:
        return "national"
    if highway in {"trunk", "trunk_link"}:
        return "motorroad"
    if highway in {"primary", "primary_link"}:
        return "national"
    if highway in {"secondary", "secondary_link"}:
        return "prefectural_major"
    if highway in {"tertiary", "tertiary_link"}:
        return "prefectural"
    return "municipal"


def _boundary_score(bounds: Sequence[float], wanted: Sequence[float]) -> float:
    """Menor es mejor; compara la envolvente administrativa con la esperada."""
    wanted_width = max(wanted[2] - wanted[0], 1e-9)
    wanted_height = max(wanted[3] - wanted[1], 1e-9)
    width = max(bounds[2] - bounds[0], 1e-9)
    height = max(bounds[3] - bounds[1], 1e-9)
    ratio_penalty = abs(width / wanted_width - 1) + abs(height / wanted_height - 1)
    edge_penalty = sum(
        abs(actual - expected) / scale
        for actual, expected, scale in (
            (bounds[0], wanted[0], wanted_width),
            (bounds[2], wanted[2], wanted_width),
            (bounds[1], wanted[1], wanted_height),
            (bounds[3], wanted[3], wanted_height),
        )
    )
    return ratio_penalty + edge_penalty


def resolve_boundary(path: Path, region: dict) -> Boundary:
    """Encuentra el límite administrativo que corresponde al bbox del catálogo.

    El bbox solo sirve para identificar el área. Nunca se utiliza para cortar
    carreteras: el recorte se hace contra el MultiPolygon OSM seleccionado.
    """
    factory = osmium.geom.GeoJSONFactory()
    center_lon, center_lat = _center(region["bbox"])
    wanted = region["bbox"]
    explicit = region.get("boundary_osm_id")
    candidates: List[Tuple[float, Boundary]] = []

    processor = osmium.FileProcessor(
        str(path),
        osmium.osm.NODE | osmium.osm.WAY | osmium.osm.RELATION | osmium.osm.AREA,
    ).with_areas()
    for obj in processor:
        if not isinstance(obj, osmium.osm.Area):
            continue
        area = obj
        tags = _mapping(area.tags)
        if tags.get("boundary") != "administrative":
            continue
        osm_id = int(area.orig_id())
        if explicit is not None and osm_id != int(explicit):
            continue
        try:
            geometry = shape(json.loads(factory.create_multipolygon(area)))
        except (RuntimeError, ValueError, TypeError):
            continue
        if geometry.is_empty or not geometry.is_valid:
            continue
        if not geometry.covers(shape({"type": "Point", "coordinates": [center_lon, center_lat]})):
            continue
        score = 0.0 if explicit is not None else _boundary_score(geometry.bounds, wanted)
        candidates.append(
            (
                score,
                Boundary(
                    geometry=geometry,
                    osm_id=osm_id,
                    name=tags.get("name:es") or tags.get("name:en") or tags.get("name"),
                ),
            )
        )

    if not candidates and explicit is not None:
        cache = WORK / "boundaries" / f"{int(explicit)}.geojson"
        geometry_json = None
        if cache.exists():
            geometry_json = json.loads(cache.read_text(encoding="utf-8"))
            print(f"  límite {explicit} desde caché", flush=True)
        else:
            print(f"  límite {explicit} no está en el extracto; Overpass", flush=True)
            geometry_json = fetch_relation_polygon(int(explicit))
            if geometry_json:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(geometry_json), encoding="utf-8")
        if geometry_json:
            geometry = shape(geometry_json)
            if not geometry.is_empty:
                candidates.append(
                    (
                        0.0,
                        Boundary(geometry=geometry, osm_id=int(explicit), name=region.get("name")),
                    )
                )

    if not candidates:
        raise RuntimeError(f"sin límite administrativo OSM para {region['id']}")
    score, boundary = min(candidates, key=lambda item: item[0])
    if explicit is None and score > 2.5:
        raise RuntimeError(
            f"límite administrativo ambiguo para {region['id']} (score={score:.2f}); "
            "añade boundaryOsmId al catálogo"
        )
    if not boundary.geometry.is_valid:
        boundary = Boundary(
            geometry=make_valid(boundary.geometry),
            osm_id=boundary.osm_id,
            name=boundary.name,
        )
    return boundary


def clipped_lines(coords: Sequence[Coord], boundary: Boundary) -> List[List[Coord]]:
    """La geometría de una ruta no se recorta al polígono administrativo."""
    del boundary
    if len(coords) < 2:
        return []
    return [[(float(lon), float(lat)) for lon, lat in coords]]


def _way_coords(way: osmium.osm.Way) -> List[Coord]:
    coords: List[Coord] = []
    try:
        for node in way.nodes:
            coords.append((node.lon, node.lat))
    except osmium.InvalidLocationError:
        return []
    return coords


class WayCollector(osmium.SimpleHandler):
    def __init__(
        self,
        scheme: str,
        boundary: Optional[Boundary],
        keep_way_ids: Optional[set[int]] = None,
    ) -> None:
        super().__init__()
        self.scheme = scheme
        self.boundary = boundary
        self.keep_way_ids = keep_way_ids or set()
        self._region = prep(boundary.geometry) if boundary is not None else None
        self.pieces: List[Piece] = []
        self.urban_polygons: List[BaseGeometry] = []
        self.excluded_in_region = 0

    def way(self, way: osmium.osm.Way) -> None:
        tags = _mapping(way.tags)
        highway = (tags.get("highway") or "").strip()
        landuse = (tags.get("landuse") or "").strip()
        if landuse in URBAN_LANDUSE and not highway:
            self._keep_urban_polygon(way)
            return
        way_id = int(way.id)
        on_route = way_id in self.keep_way_ids
        road_type = classify_scheme(tags, self.scheme)
        traversable = road_type is not None
        if road_type is None:
            if on_route:
                road_type = "other"
            else:
                road_type = dashed_catalog_type(tags)
            if road_type is None:
                if not highway:
                    return
                coords_probe = _way_coords(way)
                if len(coords_probe) >= 2:
                    in_probe = self._region is None or self._region.intersects(LineString(coords_probe))
                    if in_probe:
                        self.excluded_in_region += 1
                return
        coords = _way_coords(way)
        if len(coords) < 2:
            return
        in_region = self._region is None or self._region.intersects(LineString(coords))
        # Fuera del polígono solo entran las numeradas y las de una relación que cruza el límite.
        if not in_region and not normalize_ref(tags.get("ref")) and not on_route:
            return
        self.pieces.append(
            Piece(
                way_id=way_id,
                coords=coords,
                name=preferred_name(tags),
                name_en=(tags.get("name:en") or "").strip() or None,
                ref=normalize_ref(tags.get("ref")),
                road_type=road_type,
                in_region=in_region,
                urban=street_is_urban(road_type, highway),
                traversable=traversable,
            )
        )

    def _keep_urban_polygon(self, way: osmium.osm.Way) -> None:
        coords: List[Coord] = []
        try:
            for node in way.nodes:
                coords.append((node.lon, node.lat))
        except osmium.InvalidLocationError:
            return
        if len(coords) < 4 or coords[0] != coords[-1]:
            return
        polygon = Polygon(coords)
        if not polygon.is_valid:
            polygon = make_valid(polygon)
        if polygon.is_empty:
            return
        if polygon.geom_type == "Polygon":
            self.urban_polygons.append(polygon)
        elif polygon.geom_type == "MultiPolygon":
            self.urban_polygons.extend(geom for geom in polygon.geoms if not geom.is_empty)


def _slug(text: str) -> str:
    keep = []
    for ch in text.lower():
        if ch.isalnum():
            keep.append(ch)
        elif keep and keep[-1] != "-":
            keep.append("-")
    return "".join(keep).strip("-")[:40] or "road"


def logical_roads(region_id: str, pieces: Iterable[Piece]) -> List[dict]:
    numbered: Dict[Tuple[str, str], List[Piece]] = defaultdict(list)
    named: Dict[str, List[Piece]] = defaultdict(list)
    unnamed: List[Piece] = []
    forest_named: Dict[str, List[Piece]] = defaultdict(list)
    forest_unnamed: List[Piece] = []
    for piece in pieces:
        if piece.road_type == "forest" and not piece.ref:
            if piece.name:
                forest_named[" ".join(piece.name.casefold().split())].append(piece)
            else:
                forest_unnamed.append(piece)
            continue
        if piece.ref:
            numbered[(piece.road_type, piece.ref)].append(piece)
        elif piece.name:
            named[" ".join(piece.name.casefold().split())].append(piece)
        else:
            unnamed.append(piece)

    roads: List[dict] = []
    used_ids: set[str] = set()

    def emit(road_id: str, group: List[Piece], ref: Optional[str]) -> None:
        if not group:
            return
        type_counts: Dict[str, int] = defaultdict(int)
        for piece in group:
            type_counts[piece.road_type] += 1
        road_type = sorted(type_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        name = next((p.name for p in group if p.name), None)
        name_en = next((p.name_en for p in group if p.name_en), None)
        lines: List[List[Coord]] = []
        impassable: List[bool] = []
        for traversable in (True, False):
            part = _stitch_lines(
                [p.coords for p in group if p.traversable == traversable and len(p.coords) >= 2]
            )
            lines.extend(part)
            impassable.extend([not traversable] * len(part))
        if not lines:
            return
        tile_parts = []
        for urban in (False, True):
            for traversable in (True, False):
                part_lines = _stitch_lines(
                    [
                        p.coords
                        for p in group
                        if p.urban == urban and p.traversable == traversable and len(p.coords) >= 2
                    ]
                )
                if part_lines:
                    tile_parts.append(
                        {"urban": urban, "traversable": traversable, "lines": part_lines}
                    )
        length_m = multiline_length_m(lines)
        geometry = as_geojson_geometry(lines)
        way_ids = sorted(p.way_id for p in group)
        first = lines[0][0]
        last = lines[-1][-1]
        label = ref or ((name or "—")[:14])
        base_id = road_id
        suffix = 2
        while road_id in used_ids:
            road_id = f"{base_id}-{suffix}"
            suffix += 1
        used_ids.add(road_id)
        roads.append(
            {
                "id": road_id,
                "name": name,
                "name_en": name_en,
                "ref": ref,
                "type": road_type,
                "length_m": round(length_m, 1),
                "region_id": region_id,
                "continuous": len(lines) <= 1,
                "segment_count": len(lines),
                "osm_way_ids": way_ids,
                "label": label,
                "geometry": geometry,
                "urban": all(p.urban for p in group if len(p.coords) >= 2) and any(p.urban for p in group),
                "impassable": impassable,
                "tile_parts": tile_parts,
                "start": [first[0], first[1]],
                "end": [last[0], last[1]],
            }
        )

    whole_route = {"national", "motorway", "motorroad", "prefectural", "prefectural_major"}
    for (road_type, ref), group in numbered.items():
        if road_type in whole_route:
            if not any(piece.in_region for piece in group):
                continue
            if road_type in {"national", "motorway", "motorroad"}:
                road_id = f"{road_type}:{ref}"
            else:
                road_id = f"{region_id}:{road_type}:{ref}"
            emit(road_id, list(group), ref)
            continue
        for cluster in group_touching_chains(group, lambda piece: piece.coords):
            if not any(piece.in_region for piece in cluster):
                continue
            anchor = min(piece.way_id for piece in cluster)
            emit(f"{region_id}:{road_type}:{ref}:{anchor}", cluster, ref)
    for name_key, group in forest_named.items():
        for cluster in group_touching_chains(group, lambda piece: piece.coords):
            if not any(piece.in_region for piece in cluster):
                continue
            anchor = min(piece.way_id for piece in cluster)
            emit(f"{region_id}:name:{_slug(name_key)}:{anchor}", cluster, None)
    for chain in group_straight_chains(forest_unnamed, lambda piece: piece.coords):
        if not any(piece.in_region for piece in chain):
            continue
        anchor = min(piece.way_id for piece in chain)
        emit(f"{region_id}:way:{anchor}", chain, None)
    for name_key, group in named.items():
        for chain in group_continuous_chains(group, lambda piece: piece.coords):
            if not any(piece.in_region for piece in chain):
                continue
            anchor = min(piece.way_id for piece in chain)
            emit(f"{region_id}:name:{_slug(name_key)}:{anchor}", chain, None)
    for chain in group_continuous_chains(unnamed, lambda piece: piece.coords):
        if not any(piece.in_region for piece in chain):
            continue
        anchor = min(piece.way_id for piece in chain)
        emit(f"{region_id}:way:{anchor}", chain, None)
    return roads


def required_in_region_way_ids(pieces: Iterable[Piece]) -> set[int]:
    """Vías OSM ya admitidas por la especificación y que tocan el polígono.

    No se vuelve a filtrar por longitud ni por nombre: si una vía entró en
    `pieces` y cae dentro de la región, el paquete tiene que contenerla.
    """
    return {piece.way_id for piece in pieces if piece.in_region}


def membership_coverage(
    pieces: Iterable[Piece],
    roads: List[dict],
    extract_extent: Optional[Sequence[float]] = None,
) -> dict:
    required = required_in_region_way_ids(pieces)
    emitted = {int(way_id) for road in roads for way_id in road.get("osm_way_ids") or []}
    missing = sorted(required - emitted)
    truncated = truncated_numbered_refs(pieces, extract_extent)
    return {
        "in_region_eligible": len(required),
        "emitted": len(required & emitted),
        "missing_way_ids": missing,
        "required_way_ids": sorted(required),
        "truncated_refs": truncated,
    }


def truncated_numbered_refs(
    pieces: Iterable[Piece],
    extract_extent: Optional[Sequence[float]],
) -> List[str]:
    """Refs que siguen cortadas en el extracto: no hay geometría más allá del borde."""
    if extract_extent is None:
        return []
    by_ref: Dict[str, List[Piece]] = defaultdict(list)
    for piece in pieces:
        if piece.ref:
            by_ref[piece.ref].append(piece)
    leftover: List[str] = []
    for ref, group in sorted(by_ref.items()):
        if not any(piece.in_region for piece in group):
            continue
        if not any(not piece.in_region for piece in group):
            continue
        lines = _stitch_lines([piece.coords for piece in group if len(piece.coords) >= 2])
        ends = [line[0] for line in lines] + [line[-1] for line in lines]
        on_edge = any(_near_extract_edge(point, extract_extent) for point in ends)
        beyond = any(
            _outside_extract(point, extract_extent)
            for piece in group
            for point in piece.coords
        )
        if on_edge and not beyond:
            leftover.append(ref)
    return leftover


def _same_point(left: Coord, right: Coord) -> bool:
    return abs(left[0] - right[0]) < 1e-5 and abs(left[1] - right[1]) < 1e-5


def _stitch_lines(lines: List[List[Coord]]) -> List[List[Coord]]:
    remaining = [list(line) for line in lines if len(line) >= 2]
    stitched: List[List[Coord]] = []
    while remaining:
        current = remaining.pop()
        changed = True
        while changed:
            changed = False
            for index, other in enumerate(remaining):
                merged: Optional[List[Coord]] = None
                if _same_point(current[-1], other[0]):
                    merged = current + other[1:]
                elif _same_point(current[-1], other[-1]):
                    merged = current + list(reversed(other))[1:]
                elif _same_point(current[0], other[-1]):
                    merged = other + current[1:]
                elif _same_point(current[0], other[0]):
                    merged = list(reversed(other)) + current[1:]
                if merged is None:
                    continue
                current = merged
                remaining.pop(index)
                changed = True
                break
        stitched.append(current)
    return stitched


def _expand_bbox(bbox: Sequence[float], buffer_deg: float) -> List[float]:
    return [
        bbox[0] - buffer_deg,
        bbox[1] - buffer_deg,
        bbox[2] + buffer_deg,
        bbox[3] + buffer_deg,
    ]


def _extract_bbox(pieces: Sequence[Piece]) -> Optional[List[float]]:
    lons: List[float] = []
    lats: List[float] = []
    for piece in pieces:
        for lon, lat in piece.coords:
            lons.append(lon)
            lats.append(lat)
    if not lons:
        return None
    return [min(lons), min(lats), max(lons), max(lats)]


def _near_extract_edge(point: Coord, bbox: Sequence[float], margin: float = 0.002) -> bool:
    lon, lat = point
    return (
        abs(lon - bbox[0]) <= margin
        or abs(lon - bbox[2]) <= margin
        or abs(lat - bbox[1]) <= margin
        or abs(lat - bbox[3]) <= margin
    )


def _outside_extract(point: Coord, bbox: Sequence[float], margin: float = 0.002) -> bool:
    lon, lat = point
    return (
        lon < bbox[0] - margin
        or lon > bbox[2] + margin
        or lat < bbox[1] - margin
        or lat > bbox[3] + margin
    )


def _refs_needing_completion(region: dict, pieces: List[Piece]) -> set[str]:
    refs: set[str] = set()
    extent = _extract_bbox(pieces)
    by_ref: Dict[str, List[Piece]] = defaultdict(list)
    for piece in pieces:
        if piece.ref:
            by_ref[piece.ref].append(piece)
    for ref, group in by_ref.items():
        if not any(piece.in_region for piece in group):
            continue
        if any(not piece.in_region for piece in group):
            refs.add(ref)
            continue
        if extent is None:
            continue
        lines = _stitch_lines([piece.coords for piece in group if len(piece.coords) >= 2])
        ends = [line[0] for line in lines] + [line[-1] for line in lines]
        if any(_near_extract_edge(point, extent) for point in ends):
            refs.add(ref)
    return refs


def complete_truncated_roads(
    region: dict,
    pieces: List[Piece],
    scheme: str,
    boundary: Optional[Boundary],
    parent_path: Optional[Path] = None,
) -> List[Piece]:
    """Completa carreteras numeradas que salen del extracto (padre/vecino o Overpass)."""
    refs = _refs_needing_completion(region, pieces)
    if not refs:
        return pieces

    have_ids = {piece.way_id for piece in pieces}
    added = 0

    if parent_path and parent_path.exists():
        print(f"  completando refs desde extracto padre {parent_path.name}", flush=True)
        extra = WayCollector(scheme, boundary)
        extra.apply_file(str(parent_path), locations=True)
        for piece in extra.pieces:
            if not piece.ref or piece.ref not in refs or piece.way_id in have_ids:
                continue
            pieces.append(piece)
            have_ids.add(piece.way_id)
            added += 1
        print(f"  +{added} vías del extracto padre", flush=True)

    extract_extent = _extract_bbox(pieces)
    query_bbox = _completion_bbox(region, boundary, COMPLETE_BUFFER_DEG)
    overpass_added = _append_overpass_refs(pieces, have_ids, refs, query_bbox, scheme, boundary)
    leftover = set(truncated_numbered_refs(pieces, extract_extent))
    if leftover:
        print(f"  reintentando {len(leftover)} refs truncadas", flush=True)
        wider = _completion_bbox(region, boundary, COMPLETE_BUFFER_DEG * 3)
        overpass_added += _append_overpass_refs(pieces, have_ids, leftover, wider, scheme, boundary)
    if overpass_added:
        print(f"  +{overpass_added} vías desde Overpass", flush=True)
    return pieces


def _completion_bbox(region: dict, boundary: Optional[Boundary], buffer_deg: float) -> List[float]:
    query_bbox = _expand_bbox(region["bbox"], buffer_deg)
    if boundary is not None:
        west, south, east, north = boundary.geometry.bounds
        query_bbox = _expand_bbox((west, south, east, north), buffer_deg)
    return query_bbox


def _append_overpass_refs(
    pieces: List[Piece],
    have_ids: set[int],
    refs: Iterable[str],
    query_bbox: Sequence[float],
    scheme: str,
    boundary: Optional[Boundary],
) -> int:
    added = 0
    for ref in sorted(refs):
        try:
            fetched = fetch_ways_by_ref(
                ref,
                query_bbox,
                on_progress=lambda msg, current=ref: print(f"  Overpass {current}: {msg}", flush=True),
            )
        except RuntimeError as exc:
            print(f"  Overpass {ref}: {exc}", flush=True)
            continue
        for way_id, payload in fetched.items():
            if way_id in have_ids:
                continue
            tags = payload["tags"]
            road_type = classify_scheme(tags, scheme)
            if road_type is None:
                continue
            coords = [(float(lon), float(lat)) for lon, lat in payload["coords"]]
            if len(coords) < 2:
                continue
            in_region = boundary is None or LineString(coords).intersects(boundary.geometry)
            pieces.append(
                Piece(
                    way_id=way_id,
                    coords=coords,
                    name=preferred_name(tags),
                    name_en=(tags.get("name:en") or "").strip() or None,
                    ref=normalize_ref(tags.get("ref")) or ref,
                    road_type=road_type,
                    in_region=in_region,
                )
            )
            have_ids.add(way_id)
            added += 1
    return added


def write_zip(
    region: dict,
    roads: List[dict],
    source_url: str,
    out_zip: Path,
    boundary: Optional[Boundary],
    coverage: Optional[dict] = None,
) -> None:
    import zipfile
    from datetime import datetime, timezone

    folder = out_zip.with_suffix("")
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    total = sum(r["length_m"] for r in roads)
    types = sorted({r["type"] for r in roads})
    manifest = {
        "format_version": "1.0",
        "region_id": region["id"],
        "region_name": region["name"],
        "locale_names": {"es": region["name"]},
        "package_version": PACKAGE_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {
            "provider": "geofabrik",
            "extract_url": source_url,
            "extract_date": None,
            "osm_license": "ODbL",
        },
        "builder": BUILDER_ID,
        "boundary": (
            {
                "kind": "osm-administrative",
                "osm_id": boundary.osm_id,
                "name": boundary.name,
            }
            if boundary
            else {"kind": "geofabrik-extract"}
        ),
        "bbox": region["bbox"],
        "prefectures": [],
        "road_count": len(roads),
        "total_length_m": round(total, 1),
        "road_types": types,
        "scheme": region["scheme"],
        "coverage": (
            {
                "in_region_eligible": coverage["in_region_eligible"],
                "emitted": coverage["emitted"],
                "missing_count": len(coverage.get("missing_way_ids") or []),
                "truncated_count": len(coverage.get("truncated_refs") or []),
                "excluded_by_spec": coverage.get("excluded_by_spec", 0),
            }
            if coverage
            else None
        ),
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    features = []
    index_roads = []
    ndjson_lines = []
    for road in roads:
        props = {k: road[k] for k in (
            "id", "name", "ref", "type", "length_m", "region_id", "continuous",
            "segment_count", "osm_way_ids", "label",
        )}
        props["name_en"] = road["name_en"]
        parts = road.get("tile_parts") or [{"urban": False, "lines": None}]
        wrote_feature = False
        for part in parts:
            feature_props = dict(props)
            if part.get("urban"):
                feature_props["urban"] = True
            if part.get("traversable") is False:
                feature_props["traversable"] = False
            geometry = road["geometry"] if part.get("lines") is None else as_geojson_geometry(part["lines"])
            if not geometry:
                continue
            features.append({"type": "Feature", "properties": feature_props, "geometry": geometry})
            wrote_feature = True
        if not wrote_feature:
            features.append({"type": "Feature", "properties": props, "geometry": road["geometry"]})
        index_roads.append({
            "id": road["id"],
            "name": road["name"],
            "ref": road["ref"],
            "type": road["type"],
            "length_m": road["length_m"],
            "region_id": road["region_id"],
            "continuous": road["continuous"],
            "segment_count": road["segment_count"],
            "start": road["start"],
            "end": road["end"],
            "urban": bool(road.get("urban")),
        })
        coords = road["geometry"].get("coordinates") or []
        if road["geometry"].get("type") == "LineString":
            coords = [coords]
        payload = {"id": road["id"], "coordinates": coords}
        if any(road.get("impassable") or []):
            payload["impassable"] = road["impassable"]
        ndjson_lines.append(json.dumps(payload, ensure_ascii=False))
    (folder / "roads.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False),
        encoding="utf-8",
    )
    (folder / "roads-index.json").write_text(
        json.dumps({"roads": index_roads}, ensure_ascii=False),
        encoding="utf-8",
    )
    (folder / "roads.geometry.ndjson").write_text("\n".join(ndjson_lines) + ("\n" if ndjson_lines else ""), encoding="utf-8")
    (folder / "discontinuities.json").write_text(
        json.dumps([
            {"road_id": r["id"], "segment_count": r["segment_count"]}
            for r in roads if not r["continuous"]
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    if coverage is not None:
        (folder / "coverage.json").write_text(
            json.dumps(coverage, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    mbtiles = folder / "catalog.mbtiles"
    if not features:
        raise RuntimeError(f"{region['id']}: sin features para tippecanoe")
    tippecanoe = shutil.which("tippecanoe")
    if not tippecanoe:
        raise RuntimeError("tippecanoe no está instalado (requerido para catalog.mbtiles)")
    # Sin --no-tile-size-limit: en CCAA grandes eso deja tippecanoe horas en z16.
    # drop-densest mantiene teselas usables y termina en minutos.
    print(
        f"  tippecanoe {region['id']}: {len(features)} features → catalog.mbtiles "
        f"(z{CATALOG_MIN_ZOOM}-{CATALOG_MAX_ZOOM})",
        flush=True,
    )
    result = subprocess.run(
        [
            tippecanoe, "-o", str(mbtiles), "-l", "roads",
            f"--minimum-zoom={CATALOG_MIN_ZOOM}", f"--maximum-zoom={CATALOG_MAX_ZOOM}",
            f"--buffer={CATALOG_TILE_BUFFER}",
            "--drop-densest-as-needed",
            "--extend-zooms-if-still-dropping",
            "--read-parallel",
            "-P",
            "--force", "-y", "id", "-y", "type", "-y", "ref", "-y", "name", "-y", "label", "-y", "urban",
            "-y", "traversable",
            str(folder / "roads.geojson"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not mbtiles.exists() or mbtiles.stat().st_size <= 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"{region['id']}: tippecanoe falló (code={result.returncode})"
            + (f": {detail[:500]}" if detail else "")
        )
    print(f"  tippecanoe OK ({mbtiles.stat().st_size} bytes)", flush=True)
    if out_zip.exists():
        out_zip.unlink()
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=False) as zf:
        for name in (
            "manifest.json",
            "roads.geojson",
            "roads-index.json",
            "roads.geometry.ndjson",
            "discontinuities.json",
            "coverage.json",
            "catalog.mbtiles",
        ):
            path = folder / name
            if path.exists():
                zf.write(path, arcname=name)
    shutil.rmtree(folder, ignore_errors=True)


def validate_package(
    region: dict,
    roads: List[dict],
    out_zip: Path,
    boundary: Optional[Boundary],
    pieces: Optional[Sequence[Piece]] = None,
    extract_extent: Optional[Sequence[float]] = None,
) -> None:
    import zipfile

    ids = [road["id"] for road in roads]
    if len(ids) != len(set(ids)):
        raise RuntimeError(f"{region['id']}: IDs de carretera duplicados")
    if pieces is not None:
        coverage = membership_coverage(pieces, roads, extract_extent)
        if coverage["missing_way_ids"]:
            sample = ", ".join(str(way_id) for way_id in coverage["missing_way_ids"][:8])
            raise RuntimeError(
                f"{region['id']}: faltan {len(coverage['missing_way_ids'])} "
                f"vías del polígono ({sample})"
            )
        if coverage["truncated_refs"]:
            sample = ", ".join(coverage["truncated_refs"][:8])
            raise RuntimeError(
                f"{region['id']}: carreteras aún truncadas ({sample})"
            )

    if boundary:
        allowed = boundary.geometry.buffer(1e-8)
        for road in roads:
            geometry = shape(road["geometry"])
            if geometry.is_empty or not allowed.intersects(geometry):
                raise RuntimeError(f"{region['id']}: carretera sin intersección con el límite ({road['id']})")

    for required_road in REQUIRED_ROADS.get(region["id"], ()):
        matches = [
            road for road in roads
            if (road.get("ref") or "").upper() == required_road["ref"].upper()
        ]
        if not matches:
            raise RuntimeError(f"{region['id']}: falta {required_road['ref']}")
        if max(road["length_m"] for road in matches) < required_road["min_length_m"]:
            raise RuntimeError(
                f"{region['id']}: {required_road['ref']} demasiado corta "
                f"({max(road['length_m'] for road in matches):.0f} m)"
            )

    required = {
        "manifest.json",
        "roads.geojson",
        "roads-index.json",
        "roads.geometry.ndjson",
        "discontinuities.json",
        "coverage.json",
        "catalog.mbtiles",
    }
    with zipfile.ZipFile(out_zip) as archive:
        missing = required.difference(archive.namelist())
    if missing:
        raise RuntimeError(f"{region['id']}: paquete incompleto: {', '.join(sorted(missing))}")


def load_geofabrik() -> Dict[str, dict]:
    INDEX_CACHE.parent.mkdir(parents=True, exist_ok=True)
    url = f"{GEOFABRIK}/index-v1.json"

    def _read() -> dict:
        data = json.loads(INDEX_CACHE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "features" not in data:
            raise json.JSONDecodeError("índice Geofabrik sin features", "", 0)
        return data

    if INDEX_CACHE.exists():
        try:
            data = _read()
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"  índice Geofabrik corrupto ({exc}); redescargo", flush=True)
            INDEX_CACHE.unlink(missing_ok=True)
            urllib.request.urlretrieve(url, INDEX_CACHE)
            data = _read()
    else:
        urllib.request.urlretrieve(url, INDEX_CACHE)
        data = _read()
    out = {}
    for feature in data["features"]:
        props = feature["properties"]
        key = props["id"].replace("/", "-")
        coverage = _feature_coverage(feature["geometry"])
        pbf_url = props.get("urls", {}).get("pbf")
        if pbf_url and coverage:
            out[key] = {"url": pbf_url, **coverage}
    return out


def _lon_coverage(lons: Sequence[float]) -> Tuple[bool, float, float, float]:
    ordered = sorted(lons)
    if ordered[-1] - ordered[0] <= 180:
        return False, ordered[0], ordered[-1], ordered[-1] - ordered[0]
    best = -1.0
    gap_start = ordered[0]
    gap_end = ordered[-1]
    for start, end in zip(ordered, ordered[1:]):
        gap = end - start
        if gap > best:
            best = gap
            gap_start, gap_end = start, end
    if (ordered[0] + 360) - ordered[-1] >= best:
        return False, ordered[0], ordered[-1], ordered[-1] - ordered[0]
    width = (180 - gap_end) + (gap_start + 180)
    return True, gap_end, gap_start, width


def _feature_coverage(geometry: dict) -> Optional[dict]:
    coords = geometry.get("coordinates")
    if not coords:
        return None
    polys = coords if geometry.get("type") == "MultiPolygon" else [coords]
    lons: List[float] = []
    lats: List[float] = []
    for poly in polys:
        ring = poly[0] if poly else []
        for point in ring:
            lons.append(point[0])
            lats.append(point[1])
    if not lons:
        return None
    crosses, west, east, width = _lon_coverage(lons)
    south, north = min(lats), max(lats)
    return {
        "crosses": crosses,
        "west": west,
        "east": east,
        "south": south,
        "north": north,
        "area": max(0.0, width) * max(0.0, north - south),
    }


def _covers(item: dict, lon: float, lat: float) -> bool:
    if not (item["south"] <= lat <= item["north"]):
        return False
    if item["crosses"]:
        return lon >= item["west"] or lon <= item["east"]
    return item["west"] <= lon <= item["east"]


def _area(bbox: Sequence[float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _center(bbox: Sequence[float]) -> Coord:
    return ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)


def _source_from_url(url: str, *, clip: bool) -> Source:
    name = Path(urllib.parse.urlparse(url).path).name or "extract.osm.pbf"
    return Source(url, WORK / name, clip=clip)


def resolve_source_candidates(
    region: dict,
    geofabrik: Dict[str, dict],
) -> List[Source]:
    """Candidatos de extracto, preferidos primero (OSM.fr → Geofabrik por tamaño)."""
    seen: set[str] = set()
    candidates: List[Source] = []

    def add(url: str, clip: bool) -> None:
        if not url or url in seen:
            return
        seen.add(url)
        candidates.append(_source_from_url(url, clip=clip))

    override = EXTRACT_OVERRIDES.get(region["id"])
    if override:
        add(override[0], override[1])

    # Fallback país en OSM.fr si el override regional fallara.
    if region["id"] in {
        "andalucia", "aragon", "asturias", "cantabria", "castilla-y-leon",
        "castilla-la-mancha", "cataluna", "extremadura", "galicia",
        "islas-baleares", "la-rioja", "madrid", "murcia", "navarra",
        "pais-vasco", "valencia", "ceuta", "melilla",
    }:
        add(f"{OSM_FR}/europe/spain.osm.pbf", True)
    if region["id"] == "es-canarias":
        add(f"{OSM_FR}/africa/spain/canarias-latest.osm.pbf", False)
    if region["id"] in {"chubu", "chugoku", "hokkaido", "kansai", "kanto", "kyushu", "shikoku", "tohoku"}:
        add(f"{OSM_FR}/asia/japan.osm.pbf", True)

    direct = geofabrik.get(region["id"])
    if direct:
        add(direct["url"], False)
    lon, lat = _center(region["bbox"])
    matches = sorted(
        (item for item in geofabrik.values() if _covers(item, lon, lat)),
        key=lambda item: item["area"],
    )
    for item in matches:
        add(item["url"], True)
    return candidates


def resolve_source(region: dict, geofabrik: Dict[str, dict]) -> Optional[Source]:
    candidates = resolve_source_candidates(region, geofabrik)
    return candidates[0] if candidates else None


def _http_permanent_failure(exc: BaseException) -> bool:
    """Errores con los que no merece insistir en la misma URL (probar otra fuente)."""
    if isinstance(exc, urllib.error.HTTPError) and exc.code in {404, 410, 403, 502, 503}:
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "http error 404",
            "http error 410",
            "http error 403",
            "http error 502",
            "http error 503",
            "http permanente",
            "descarga agotada",
        )
    )


def resolve_parent_source(
    region: dict,
    geofabrik: Dict[str, dict],
    current: Source,
) -> Optional[Source]:
    """Extracto país (OSM.fr) o el menor Geofabrik que cubra y sea claramente mayor."""
    # Regional OSM.fr → padre país. Evita Overpass masivo en JP (p.ej. Kansai ~11k ways).
    if region["id"] in JP_REGION_IDS and current.path.name != "japan.osm.pbf":
        return _source_from_url(f"{OSM_FR}/asia/japan.osm.pbf", clip=True)
    if region["id"] in ES_REGION_IDS and current.path.name != "spain.osm.pbf":
        return _source_from_url(f"{OSM_FR}/europe/spain.osm.pbf", clip=True)

    lon, lat = _center(region["bbox"])
    matches = [item for item in geofabrik.values() if _covers(item, lon, lat)]
    larger = [
        item for item in matches
        if item["url"] != current.url
    ]
    if not larger:
        return None
    current_area = next((item["area"] for item in matches if item["url"] == current.url), 0.0)
    parents = [item for item in larger if item["area"] > current_area * 1.2]
    if not parents:
        return None
    chosen = min(parents, key=lambda item: item["area"])
    return Source(chosen["url"], WORK / Path(chosen["url"]).name, clip=True)


def _download_total(response, status: int) -> Optional[int]:
    if status == 206:
        content_range = response.headers.get("Content-Range") or ""
        total = content_range.rsplit("/", 1)[-1]
        return int(total) if total.isdigit() else None
    length = response.headers.get("Content-Length")
    return int(length) if length and length.isdigit() else None


def _offline(exc: BaseException) -> bool:
    text = str(exc).lower()
    markers = (
        "nodename nor servname",
        "name or service not known",
        "temporary failure in name resolution",
        "network is unreachable",
        "connection refused",
        "connection reset",
        "timed out",
        "timeout",
        "errno 8",
        "errno 50",
        "errno 51",
        "errno 54",
        "errno 60",
        "errno 61",
    )
    return any(marker in text for marker in markers)


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  ya está {dest.name}", flush=True)
        return
    tmp = dest.with_suffix(dest.suffix + ".partial")
    print(f"  bajando {url}", flush=True)
    last_error: Optional[BaseException] = None
    other = 0
    waits = 0
    while True:
        have = tmp.stat().st_size if tmp.exists() else 0
        headers = {"User-Agent": "RoadScout/catalog"}
        if have:
            headers["Range"] = f"bytes={have}-"
            print(f"  reanudo en {have} bytes", flush=True)
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                status = getattr(response, "status", response.getcode())
                resume = status == 206 and have
                mode = "ab" if resume else "wb"
                expected = _download_total(response, status)
                with tmp.open(mode) as handle:
                    while True:
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
            size = tmp.stat().st_size if tmp.exists() else 0
            if size <= 0:
                raise RuntimeError("descarga vacía")
            if expected is not None and size != expected:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"tamaño {size} distinto de {expected}")
            tmp.replace(dest)
            print(f"  bajado {dest.name} ({dest.stat().st_size} bytes)", flush=True)
            return
        except Exception as exc:
            last_error = exc
            if _http_permanent_failure(exc):
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"HTTP permanente ({exc}) para {url}") from exc
            if _offline(exc):
                waits += 1
                print(f"  sin conexión, espero 1 min ({waits})", flush=True)
                time.sleep(60)
                continue
            other += 1
            print(f"  reintento {other} {exc}", flush=True)
            if other >= 7:
                break
            time.sleep(min(30, 5 * other))
    raise RuntimeError(f"descarga agotada: {last_error}")


def catalog_regions() -> List[dict]:
    payload = json.loads(CATALOG.read_text())
    regions = []
    for continent in payload["continents"]:
        for country in continent["countries"]:
            if country.get("id") not in OFFERED_COUNTRY_IDS:
                continue
            for region in country["regions"]:
                regions.append({
                    "id": region["id"],
                    "name": region["name"],
                    "bbox": region["bbox"],
                    "scheme": country["scheme"],
                    "country": country["name"],
                    "boundary_osm_id": region.get("boundaryOsmId"),
                })
    regions.sort(key=lambda region: _area(region["bbox"]))
    return regions


def _needs_boundary(region: dict, source: Source) -> bool:
    return source.clip or region.get("boundary_osm_id") is not None


class _RouteRelationPass(osmium.SimpleHandler):
    def __init__(self) -> None:
        super().__init__()
        self.routes: List[CatalogRoute] = []

    def relation(self, relation: osmium.osm.Relation) -> None:
        tags = {t.k: t.v for t in relation.tags}
        if tags.get("type") != "route" or tags.get("route") != "road":
            return
        tags["_relation_id"] = str(relation.id)
        classified = classify_relation(tags)
        if classified is None:
            return
        road_type = classified[0]
        name = tags.get("name:ja") or tags.get("name") or ""
        if road_type == "prefectural" and "主要地方道" in name:
            road_type = "prefectural_major"
        if road_type not in _ROUTE_RANK:
            return
        way_ids = [int(member.ref) for member in relation.members if member.type == "w"]
        if not way_ids:
            return
        ref = normalize_ref(tags.get("ref"))
        self.routes.append(
            CatalogRoute(relation_id=int(relation.id), road_type=road_type, ref=ref, way_ids=way_ids)
        )


def read_route_relations(path: Path) -> List[CatalogRoute]:
    scanner = _RouteRelationPass()
    scanner.apply_file(str(path))
    return scanner.routes


class _WantedWayGeomPass(osmium.SimpleHandler):
    """Extrae geometría solo de way ids pedidos (p.ej. desde extracto padre)."""

    def __init__(self, wanted: set[int]) -> None:
        super().__init__()
        self.wanted = wanted
        self.geoms: Dict[int, List[Coord]] = {}

    def way(self, way: osmium.osm.Way) -> None:
        way_id = int(way.id)
        if way_id not in self.wanted:
            return
        coords = _way_coords(way)
        if len(coords) >= 2:
            self.geoms[way_id] = coords


def _append_relation_way_geoms(
    pieces: List[Piece],
    have: set[int],
    fetched: Dict[int, List[Coord]],
    membership: Dict[int, Tuple[str, Optional[str]]],
    boundary: Optional[Boundary],
) -> int:
    added = 0
    for way_id, coords in fetched.items():
        if way_id in have or len(coords) < 2:
            continue
        road_type, ref = membership.get(way_id, ("national", None))
        line = LineString(coords)
        inside = boundary is None or line.intersects(boundary.geometry)
        pieces.append(
            Piece(
                way_id=way_id,
                coords=[(float(lon), float(lat)) for lon, lat in coords],
                name=None,
                name_en=None,
                ref=ref,
                road_type=road_type,
                in_region=inside,
                traversable=True,
            )
        )
        have.add(way_id)
        added += 1
    return added


def complete_relation_gaps(
    pieces: List[Piece],
    routes: Sequence[CatalogRoute],
    membership: Dict[int, Tuple[str, Optional[str]]],
    boundary: Optional[Boundary],
    *,
    parent_path: Optional[Path] = None,
) -> List[Piece]:
    """Completa miembros de relación fuera del extracto: padre primero, Overpass resto."""
    have = {piece.way_id for piece in pieces}
    in_region = {piece.way_id for piece in pieces if piece.in_region}
    missing: List[int] = []
    seen: set[int] = set()
    for route in routes:
        if route.road_type not in _ROUTE_RANK:
            continue
        if not any(way_id in in_region for way_id in route.way_ids):
            continue
        for way_id in route.way_ids:
            if way_id in have or way_id in seen:
                continue
            seen.add(way_id)
            missing.append(way_id)
    if not missing:
        return pieces

    if parent_path and parent_path.exists():
        print(
            f"  relaciones: {len(missing)} vías fuera; buscando en {parent_path.name}",
            flush=True,
        )
        scanner = _WantedWayGeomPass(set(missing))
        scanner.apply_file(str(parent_path), locations=True)
        added = _append_relation_way_geoms(pieces, have, scanner.geoms, membership, boundary)
        if added:
            print(f"  +{added} vías de relación desde extracto padre", flush=True)

    still_missing = [way_id for way_id in missing if way_id not in have]
    if not still_missing:
        return pieces

    print(f"  Overpass relaciones: {len(still_missing)} vías aún fuera", flush=True)
    fetched = fetch_way_geometries(still_missing)
    added = _append_relation_way_geoms(pieces, have, fetched, membership, boundary)
    if added:
        print(f"  +{added} vías de relación desde Overpass", flush=True)
    return pieces


def build_region(
    region: dict,
    source: Source,
    *,
    force: bool = False,
    parent_path: Optional[Path] = None,
) -> Optional[Built]:
    out_zip = ZIPS / f"{region['id']}-v{PACKAGE_VERSION}.zip"
    if not force and out_zip.exists() and out_zip.stat().st_size > 0:
        print(f"existe {out_zip.name}", flush=True)
        return Built(region["id"], out_zip, out_zip.stat().st_size, -1)
    print(f"construyendo {region['id']} ({region['scheme']}) desde {source.path.name} clip={source.clip}", flush=True)
    boundary = resolve_boundary(source.path, region) if _needs_boundary(region, source) else None
    if boundary:
        print(
            f"  límite OSM {boundary.osm_id} {boundary.name or ''} bounds={boundary.geometry.bounds}",
            flush=True,
        )
    routes = read_route_relations(source.path)
    if parent_path and parent_path.exists():
        routes.extend(read_route_relations(parent_path))
    membership = best_route_membership(routes)
    collector = WayCollector(region["scheme"], boundary, keep_way_ids=set(membership))
    collector.apply_file(str(source.path), locations=True)
    extract_extent = _extract_bbox(collector.pieces)
    pieces = collector.pieces
    apply_route_membership(pieces, membership)
    pieces = complete_truncated_roads(
        region,
        pieces,
        region["scheme"],
        boundary,
        parent_path=parent_path,
    )
    pieces = complete_relation_gaps(
        pieces,
        routes,
        membership,
        boundary,
        parent_path=parent_path,
    )
    apply_route_membership(pieces, membership)
    promote_outside_pieces(pieces)
    mark_urban_streets(pieces, collector.urban_polygons)
    roads = logical_roads(region["id"], pieces)
    coverage = membership_coverage(pieces, roads, extract_extent)
    coverage["excluded_by_spec"] = collector.excluded_in_region
    print(
        f"  vías {len(pieces)} carreteras {len(roads)} "
        f"dentro={coverage['in_region_eligible']} emitidas={coverage['emitted']}",
        flush=True,
    )
    if not roads:
        print(f"  sin carreteras: {region['id']}", flush=True)
        return None
    if coverage["missing_way_ids"]:
        sample = ", ".join(str(way_id) for way_id in coverage["missing_way_ids"][:8])
        raise RuntimeError(
            f"{region['id']}: faltan {len(coverage['missing_way_ids'])} "
            f"vías del polígono ({sample})"
        )
    if coverage["truncated_refs"]:
        sample = ", ".join(coverage["truncated_refs"][:8])
        raise RuntimeError(f"{region['id']}: carreteras aún truncadas ({sample})")
    ZIPS.mkdir(parents=True, exist_ok=True)
    try:
        write_zip(region, roads, source.url, out_zip, boundary, coverage)
        validate_package(region, roads, out_zip, boundary, pieces, extract_extent)
        previous = ZIPS / f"{region['id']}-v{PACKAGE_VERSION - 1}.zip"
        if previous.exists():
            previous.unlink()
    except Exception:
        out_zip.unlink(missing_ok=True)
        raise
    return Built(region["id"], out_zip, out_zip.stat().st_size, len(roads))


def _remote_index() -> dict:
    import tempfile

    try:
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    "gh", "release", "download", RELEASE_TAG,
                    "--repo", RELEASE_REPO,
                    "--pattern", "index.json",
                    "--dir", directory,
                    "--clobber",
                ],
                capture_output=True,
                text=True,
            )
            path = Path(directory) / "index.json"
            if result.returncode != 0 or not path.exists():
                return {"packages": []}
            data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"packages": []}
    packages = data.get("packages") if isinstance(data, dict) else None
    return {"packages": packages if isinstance(packages, list) else []}


def _index_from_existing_zips(items: Sequence[Built]) -> dict:
    by_id = {}
    for row in _remote_index().get("packages") or []:
        if isinstance(row, dict) and row.get("id"):
            by_id[row["id"]] = row
    for item in items:
        if not item.zip_path.exists() or item.zip_path.stat().st_size <= 0:
            continue
        by_id[item.region_id] = {
            "id": item.region_id,
            "version": PACKAGE_VERSION,
            "bytes": item.zip_path.stat().st_size,
            "file": item.zip_path.name,
        }
    packages = sorted(by_id.values(), key=lambda row: str(row["id"]))
    return {"packages": packages}


def publish_all(items: Sequence[Built]) -> None:
    from validate_region_package import validate

    present = []
    for item in items:
        if not item.zip_path.exists() or item.zip_path.stat().st_size <= 0:
            continue
        errors = validate(item.zip_path)
        if errors:
            print(f"no publico {item.region_id}: {'; '.join(errors)}", flush=True)
            continue
        present.append(item)
    if not present:
        raise RuntimeError("no hay ZIP validados para publicar")
    index_path = ZIPS / "index.json"
    index = _index_from_existing_zips(present)
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    files = [str(index_path)] + [str(item.zip_path) for item in present]
    existing = subprocess.run(
        ["gh", "release", "view", RELEASE_TAG, "--repo", RELEASE_REPO],
        capture_output=True,
        text=True,
    )
    if existing.returncode != 0:
        subprocess.run(
            [
                "gh", "release", "create", RELEASE_TAG,
                "--repo", RELEASE_REPO,
                "--title", "Regiones",
                "--notes", "Paquetes de carreteras por región. La app descarga el zip de la región elegida.",
                *files,
            ],
            check=True,
        )
    else:
        subprocess.run(
            ["gh", "release", "upload", RELEASE_TAG, "--repo", RELEASE_REPO, "--clobber", *files],
            check=True,
        )
    print(f"publicado {', '.join(item.region_id for item in present)}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--region", action="append", default=[])
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--keep-pbf", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    print(f"work={WORK}", flush=True)
    print(f"zips={ZIPS}", flush=True)

    regions = catalog_regions()
    if args.region:
        wanted = set(args.region)
        regions = [region for region in regions if region["id"] in wanted]
        unknown = wanted - {region["id"] for region in regions}
        if unknown:
            print(f"regiones desconocidas: {' '.join(sorted(unknown))}", flush=True)
            return 1
        if not regions:
            print("ninguna región pedida", flush=True)
            return 1
    if args.limit:
        regions = regions[: args.limit]

    requested_ids = [region["id"] for region in regions]
    failed: List[str] = []

    try:
        geofabrik = load_geofabrik()
    except Exception as exc:
        print(f"aviso: índice Geofabrik no disponible ({exc}); uso solo overrides OSM.fr", flush=True)
        geofabrik = {}

    built: List[Built] = []
    for region in regions:
        out_zip = ZIPS / f"{region['id']}-v{PACKAGE_VERSION}.zip"
        if not args.force and out_zip.exists() and out_zip.stat().st_size > 0:
            item = Built(region["id"], out_zip, out_zip.stat().st_size, -1)
            built.append(item)
            print(f"existe {out_zip.name}", flush=True)
            continue

        candidates = resolve_source_candidates(region, geofabrik)
        if not candidates:
            print(f"sin extracto: {region['id']}", flush=True)
            failed.append(region["id"])
            continue

        region_built = False
        last_exc: Optional[BaseException] = None
        for source in candidates:
            try:
                download(source.url, source.path)
                parent = resolve_parent_source(region, geofabrik, source)
                parent_path = None
                if parent and parent.path != source.path:
                    try:
                        download(parent.url, parent.path)
                        parent_path = parent.path
                    except Exception as parent_exc:
                        if _http_permanent_failure(parent_exc) or "HTTP permanente" in str(parent_exc):
                            print(f"  padre omitido ({parent.url}): {parent_exc}", flush=True)
                        else:
                            raise
                result = build_region(
                    region,
                    source,
                    force=args.force,
                    parent_path=parent_path,
                )
            except Exception as exc:
                last_exc = exc
                # 502/503 del extracto PBF → otra URL. Fallos de Overpass a mitad de
                # build NO deben saltar a japan.osm.pbf (rompe boundaryOsmId/límite).
                msg = str(exc).lower()
                if _http_permanent_failure(exc) and "overpass" not in msg:
                    print(f"  fuente inválida, pruebo otra: {source.url} ({exc})", flush=True)
                    continue
                print(f"fallo {region['id']}: {exc}", flush=True)
                break
            if result:
                built.append(result)
                print(f"OK {region['id']} -> {result.zip_path} via {source.url}", flush=True)
                region_built = True
                if not args.keep_pbf and source.path.exists() and source.path.parent == WORK:
                    # Conserva PBF compartidos (spain.osm.pbf / japan.osm.pbf) entre regiones.
                    shared = source.path.name in {"spain.osm.pbf", "japan.osm.pbf"}
                    if not shared:
                        source.path.unlink(missing_ok=True)
                break
            last_exc = RuntimeError("sin carreteras")
            print(f"  sin carreteras con {source.url}; pruebo otra fuente", flush=True)
        if not region_built:
            failed.append(region["id"])
            print(f"fallo {region['id']}: {last_exc}", flush=True)

    valid = [item for item in built if item.zip_path.exists() and item.zip_path.stat().st_size > 0]
    if args.upload:
        try:
            publish_all(valid)
        except Exception as exc:
            print(f"fallo al publicar: {exc}", flush=True)
            return 1
    built_ids = {item.region_id for item in valid}
    missing_built = [region_id for region_id in requested_ids if region_id not in built_ids]
    failed_unique = sorted(set(failed + missing_built))
    print(f"listos {len(valid)}", flush=True)
    if failed_unique:
        print(f"fallidos {len(failed_unique)}: {' '.join(failed_unique)}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
