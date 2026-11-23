"""Build logical roads primarily from OSM route=road relations."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple
import time

import osmium

from .classify import (
    _slug,
    normalize_ref_token,
    preferred_name,
    refs_from_tags,
)
from .filters import is_included_way, is_motor_road_surface
from .geometry import (
    as_geojson_geometry,
    connect_segments,
    line_length_m,
    multiline_length_m,
)
from .group import Discontinuity, LogicalRoad
from .overpass_fetch import fetch_way_geometries, fetch_ways_for_relations, missing_ids

Coord = Tuple[float, float]

# Sin corte por longitud: una forestal transitable y con nombre entra aunque sea corta.
MIN_RINDO_LENGTH_M = 0.0

# Completar cualquier carretera legal cuyo trazado se salga del extracto regional.
_COMPLETE_TYPES = frozenset({
    "national",
    "motorway",
    "motorroad",
    "prefectural",
    "prefectural_major",
})

# Prefer filling missing geometries from neighboring extracts before Overpass.
# Paths relative to download.geofabrik.de/
DEFAULT_NEIGHBOR_EXTRACTS = (
    "asia/japan/chubu-latest.osm.pbf",
    "asia/japan/chugoku-latest.osm.pbf",
)


@dataclass
class RouteRelation:
    relation_id: int
    tags: Dict[str, str]
    way_ids: List[int]  # member order preserved


@dataclass
class WayGeom:
    way_id: int
    tags: Dict[str, str]
    coords: List[Coord]


def _is_motorroad_relation(tags: Dict[str, str], name: str, net_l: str) -> bool:
    """Vía reservada a vehículos de motor que no es una autopista JP:E."""
    if "自動車専用道路" in name:
        return True
    if (tags.get("motorroad") or "").lower() == "yes":
        return True
    return "motorroad" in net_l


def way_is_toll(tags: Dict[str, str]) -> bool:
    if (tags.get("toll") or "").lower() == "yes":
        return True
    for key in ("toll:motorcycle", "toll:motorcar", "toll:motor_vehicle"):
        if (tags.get(key) or "").lower() == "yes":
            return True
    return False


def _is_motorroad_way(tags: Dict[str, str]) -> bool:
    blob = " ".join(
        filter(
            None,
            [tags.get("name:ja"), tags.get("name"), tags.get("official_name"), tags.get("alt_name")],
        )
    )
    if any(token in blob for token in ("国道", "府道", "県道", "都道")):
        return False
    if "自動車専用道路" in blob:
        return True
    return (tags.get("motorroad") or "").lower() == "yes"


def classify_relation(tags: Dict[str, str]) -> Optional[Tuple[str, str, str]]:
    """
    Returns (road_type, key_value, key_kind) or None if not a catalog route.
    Nationals with the same ref merge across relations (bypasses included).
    Prefecturals stay per-relation to avoid cross-prefecture collisions.
    """
    if tags.get("type") != "route" or tags.get("route") != "road":
        return None

    network = (tags.get("network") or "").strip()
    net_l = network.lower()
    name = tags.get("name:ja") or tags.get("name") or tags.get("official_name") or ""
    ref = normalize_ref_token(tags.get("ref") or "")

    if net_l.startswith("jp:e") or net_l in {"jp:expressway", "jp:motorway"}:
        key = f"motorway:{ref}" if ref else f"motorway:rel-{(tags.get('name') or 'x')}"
        return ("motorway", key, "ref" if ref else "name")

    if "jp:national" in net_l or net_l == "national" or "国道" in name:
        if not ref and not name:
            return None
        key = f"national:{ref}" if ref else f"national:{_slug(name)}"
        return ("national", key, "ref" if ref else "name")

    if (
        "prefectural" in net_l
        or "府道" in name
        or "県道" in name
        or "都道" in name
        or "主要地方道" in name
    ):
        # Per-relation identity (same ref exists in many prefectures).
        rid = tags.get("_relation_id")  # filled by caller
        base = ref or _slug(name) or "unknown"
        key = f"prefectural:{base}:rel-{rid}"
        return ("prefectural", key, "ref")

    if _is_motorroad_relation(tags, name, net_l):
        base = ref or _slug(name) or "unknown"
        rid = tags.get("_relation_id")
        key = f"motorroad:{base}:rel-{rid}"
        return ("motorroad", key, "ref" if ref else "name")

    # Named municipal / other formal routes with ref
    if ref or name:
        key = f"other:{ref}" if ref else f"other:{_slug(name)}"
        return ("other", key, "ref" if ref else "name")

    return None


class _RelationPass(osmium.SimpleHandler):
    def __init__(self) -> None:
        super().__init__()
        self.relations: List[RouteRelation] = []

    def relation(self, r: osmium.osm.Relation) -> None:
        tags = {t.k: t.v for t in r.tags}
        if tags.get("type") != "route" or tags.get("route") != "road":
            return
        way_ids = [int(m.ref) for m in r.members if m.type == "w"]
        if not way_ids:
            return
        tags = dict(tags)
        tags["_relation_id"] = str(r.id)
        if classify_relation(tags) is None:
            return
        self.relations.append(RouteRelation(relation_id=int(r.id), tags=tags, way_ids=way_ids))


class _WayPass(osmium.SimpleHandler):
    def __init__(self, needed_ids: Set[int], collect_supplemental: bool) -> None:
        super().__init__()
        self.needed_ids = needed_ids
        self.collect_supplemental = collect_supplemental
        self.ways: Dict[int, WayGeom] = {}
        self.supplemental: List[WayGeom] = []
        self.seen = 0

    def way(self, w: osmium.osm.Way) -> None:
        self.seen += 1
        wid = int(w.id)
        tags = {t.k: t.v for t in w.tags}
        want = wid in self.needed_ids
        supp = False
        if not want and self.collect_supplemental:
            supp = _is_supplemental_candidate(tags)
            if not supp:
                return
        elif not want:
            return

        if want and not is_motor_road_surface(tags) and tags.get("highway") not in {
            "track", "path", "service", "unclassified", "residential",
            "tertiary", "secondary", "primary", "trunk", "motorway",
            "motorway_link", "trunk_link", "primary_link", "secondary_link",
            "tertiary_link", "living_street", "road",
        }:
            # Still keep relation members even if exotic highway — relation is authority
            pass

        coords: List[Coord] = []
        try:
            for node in w.nodes:
                if not node.location.valid():
                    coords = []
                    break
                coords.append((float(node.location.lon), float(node.location.lat)))
        except osmium.InvalidLocationError:
            return
        if len(coords) < 2:
            return

        geom = WayGeom(way_id=wid, tags=tags, coords=coords)
        if want:
            self.ways[wid] = geom
        if supp:
            self.supplemental.append(geom)


def _is_supplemental_candidate(tags: Dict[str, str]) -> bool:
    """Vías locales y rindos que no salen de una relación route=road."""
    if not is_included_way(tags):
        return False
    highway = tags.get("highway") or ""
    blob = " ".join(
        filter(
            None,
            [tags.get("name:ja"), tags.get("name"), tags.get("official_name"), tags.get("alt_name")],
        )
    )
    if "国道" in blob or "府道" in blob or "県道" in blob or "都道" in blob:
        return False
    if _is_motorroad_way(tags):
        return True
    if "林道" in blob or highway == "track":
        return True
    if highway == "path" and tags.get("motorcycle") in {"yes", "designated"}:
        return True
    if highway in {
        "primary", "secondary", "tertiary", "trunk", "unclassified", "residential", "road",
        "living_street", "tertiary_link", "secondary_link", "primary_link", "trunk_link",
    }:
        return True
    return False


def _is_rindo(tags: Dict[str, str]) -> bool:
    blob = " ".join(
        filter(
            None,
            [tags.get("name:ja"), tags.get("name"), tags.get("official_name"), tags.get("alt_name")],
        )
    )
    highway = tags.get("highway") or ""
    return "林道" in blob or highway == "track" or (
        highway == "path" and tags.get("motorcycle") in {"yes", "designated"}
    )


def chain_toll_geometries(
    way_ids: Sequence[int],
    ways: Dict[int, WayGeom],
) -> List[List[Coord]]:
    """Tramos contiguos con peaje. Un tramo gratis corta la cadena."""
    pieces: List[List[Coord]] = []
    current: List[Coord] = []

    def flush() -> None:
        nonlocal current
        if len(current) >= 2:
            pieces.append(current)
        current = []

    for wid in way_ids:
        geom = ways.get(wid)
        if geom is None or len(geom.coords) < 2 or not way_is_toll(geom.tags):
            flush()
            continue
        coords = list(geom.coords)
        rev = list(reversed(coords))
        if not current:
            current = coords
            continue
        if _same_point(current[-1], coords[0]):
            current.extend(coords[1:])
        elif _same_point(current[-1], rev[0]):
            current.extend(rev[1:])
        elif _same_point(current[0], coords[-1]):
            current = coords[:-1] + current
        elif _same_point(current[0], rev[-1]):
            current = rev[:-1] + current
        else:
            flush()
            current = coords
    flush()
    return pieces


def chain_member_geometries(
    way_ids: Sequence[int],
    ways: Dict[int, WayGeom],
) -> Tuple[List[List[Coord]], List[int]]:
    """
    Walk relation members in order; join when endpoints share coordinates.
    Returns components + list of way ids that had geometry.
    """
    used: List[int] = []
    components: List[List[Coord]] = []
    current: List[Coord] = []

    def flush() -> None:
        nonlocal current
        if len(current) >= 2:
            components.append(current)
        current = []

    for wid in way_ids:
        geom = ways.get(wid)
        if geom is None or len(geom.coords) < 2:
            continue
        used.append(wid)
        coords = list(geom.coords)
        rev = list(reversed(coords))
        if not current:
            current = coords
            continue
        # Prefer forward continuity along the relation
        if _same_point(current[-1], coords[0]):
            current.extend(coords[1:])
        elif _same_point(current[-1], rev[0]):
            current.extend(rev[1:])
        elif _same_point(current[0], coords[-1]):
            current = coords[:-1] + current
        elif _same_point(current[0], rev[-1]):
            current = rev[:-1] + current
        else:
            flush()
            current = coords
    flush()

    # Close remaining micro-gaps between components
    components, _ = connect_segments(components, snap_m=50.0)
    return components, used


def _same_point(a: Coord, b: Coord, eps: float = 1e-9) -> bool:
    return abs(a[0] - b[0]) <= eps and abs(a[1] - b[1]) <= eps


def _join_exact(lines: List[List[Coord]]) -> List[List[Coord]]:
    """Une tramos que comparten extremo. No salta un hueco."""
    remaining = [list(line) for line in lines if len(line) >= 2]
    changed = True
    while changed and len(remaining) > 1:
        changed = False
        used = [False] * len(remaining)
        nxt: List[List[Coord]] = []
        for i, line in enumerate(remaining):
            if used[i]:
                continue
            used[i] = True
            grew = True
            while grew:
                grew = False
                for j, other in enumerate(remaining):
                    if used[j]:
                        continue
                    merged = _merge_exact(line, other)
                    if merged is None:
                        continue
                    line = merged
                    used[j] = True
                    grew = True
                    changed = True
            nxt.append(line)
        remaining = nxt
    return remaining


def _merge_exact(left: List[Coord], right: List[Coord]) -> Optional[List[Coord]]:
    rev = list(reversed(right))
    if _same_point(left[-1], right[0]):
        return left + right[1:]
    if _same_point(left[-1], rev[0]):
        return left + rev[1:]
    if _same_point(left[0], right[-1]):
        return right + left[1:]
    if _same_point(left[0], rev[-1]):
        return rev + left[1:]
    return None


def build_catalog_from_pbf(
    pbf_path: str,
    region_id: str,
    *,
    complete_cross_region: bool = True,
    neighbor_pbf_paths: Optional[Sequence[str]] = None,
    complete_refs: Optional[Sequence[str]] = None,
) -> Tuple[List[LogicalRoad], List[Discontinuity], Dict]:
    # Pass 1: relations
    rel_pass = _RelationPass()
    rel_pass.apply_file(pbf_path, locations=False)

    needed: Set[int] = set()
    for rel in rel_pass.relations:
        needed.update(rel.way_ids)

    # Pass 2: geometries
    way_pass = _WayPass(needed_ids=needed, collect_supplemental=True)
    way_pass.apply_file(pbf_path, locations=True)

    # Group relations into logical roads
    buckets: Dict[str, dict] = {}
    for rel in rel_pass.relations:
        classified = classify_relation(rel.tags)
        if classified is None:
            continue
        road_type, key_value, key_kind = classified
        bucket = buckets.setdefault(
            key_value,
            {
                "type": road_type,
                "key_kind": key_kind,
                "key_value": key_value,
                "relation_ids": [],
                "way_ids_ordered": [],
                "tags_list": [],
            },
        )
        bucket["relation_ids"].append(rel.relation_id)
        bucket["tags_list"].append(rel.tags)
        bucket["way_ids_ordered"].extend(rel.way_ids)

    for bucket in buckets.values():
        seen_w: Set[int] = set()
        ordered: List[int] = []
        for wid in bucket["way_ids_ordered"]:
            if wid not in seen_w:
                seen_w.add(wid)
                ordered.append(wid)
        bucket["way_ids_ordered"] = ordered

    complete_stats = _complete_truncated_route_geometries(
        buckets,
        way_pass.ways,
        enabled=complete_cross_region,
        neighbor_pbf_paths=neighbor_pbf_paths,
        complete_refs=set(complete_refs) if complete_refs else None,
    )

    roads: List[LogicalRoad] = []
    discontinuities: List[Discontinuity] = []
    relation_way_ids: Set[int] = set()

    for key_value, bucket in buckets.items():
        components, used_ids = chain_member_geometries(bucket["way_ids_ordered"], way_pass.ways)
        # Deduplicate way ids preserving order
        seen = set()
        uniq_ways = []
        for wid in used_ids:
            if wid not in seen:
                seen.add(wid)
                uniq_ways.append(wid)
        relation_way_ids.update(uniq_ways)

        if not components:
            continue

        tags0 = bucket["tags_list"][0]
        name = tags0.get("name:ja") or tags0.get("name") or tags0.get("official_name")
        # Prefer the longest named relation's name if merging nationals
        for t in bucket["tags_list"]:
            n = t.get("name:ja") or t.get("name")
            if n and ("国道" in n or name is None):
                name = n
                break
        ref = normalize_ref_token(tags0.get("ref") or "")
        for t in bucket["tags_list"]:
            r = normalize_ref_token(t.get("ref") or "")
            if r:
                ref = r
                break

        length_m = multiline_length_m(components)
        continuous = len(components) <= 1
        road_id = f"{region_id}:{_slug(key_value)}"
        toll_lines = chain_toll_geometries(bucket["way_ids_ordered"], way_pass.ways)
        road = LogicalRoad(
            id=road_id,
            name=name,
            ref=ref,
            type=bucket["type"],
            length_m=round(length_m, 1),
            region_id=region_id,
            continuous=continuous,
            segment_count=len(components),
            osm_way_ids=uniq_ways,
            geometry=as_geojson_geometry(components),
            network=tags0.get("network"),
            name_ja=tags0.get("name:ja"),
            name_en=tags0.get("name:en"),
            gap_count=max(0, len(components) - 1),
            toll_lines=toll_lines,
        )
        roads.append(road)
        if not continuous:
            discontinuities.append(
                Discontinuity(
                    road_id=road_id,
                    component_count=len(components),
                    gap_count=road.gap_count,
                    note="Huecos entre miembros de relación OSM route=road (o entre bypasses fusionados)",
                )
            )

    # Supplemental: rindos >= 2 km, named locals not already in a relation road
    supp_roads, supp_disc, supp_stats = _build_supplemental(
        way_pass.supplemental,
        region_id=region_id,
        skip_way_ids=relation_way_ids,
    )
    roads.extend(supp_roads)
    discontinuities.extend(supp_disc)

    roads.sort(key=lambda r: (r.type, r.ref or "", r.name or "", r.id))
    stats = {
        "relations_route_road": len(rel_pass.relations),
        "logical_from_relations": len(buckets),
        "ways_needed": len(needed),
        "ways_resolved": len(way_pass.ways),
        "ways_seen": way_pass.seen,
        "supplemental_candidates": len(way_pass.supplemental),
        "supplemental_roads": len(supp_roads),
        "rindo_min_length_m": MIN_RINDO_LENGTH_M,
        **supp_stats,
        **complete_stats,
    }
    return roads, discontinuities, stats


def _complete_truncated_route_geometries(
    buckets: Dict[str, dict],
    ways: Dict[int, WayGeom],
    *,
    enabled: bool,
    neighbor_pbf_paths: Optional[Sequence[str]] = None,
    complete_refs: Optional[Set[str]] = None,
) -> Dict:
    """
    For national/motorway buckets, fetch missing member ways so the logical
    road spans the full OSM relation (not just the regional PBF).

    Order: neighbor PBFs (fast) → Overpass by relation.
    If complete_refs is set, only those refs are completed (e.g. {"162"}).
    """
    stats: Dict = {
        "cross_region_complete_enabled": enabled,
        "cross_region_missing_requested": 0,
        "cross_region_ways_from_neighbors": 0,
        "cross_region_ways_from_overpass": 0,
        "cross_region_buckets": 0,
        "cross_region_still_missing": 0,
        "cross_region_overpass_rounds": 0,
        "cross_region_way_rounds": 0,
        "cross_region_neighbor_pbfs": list(neighbor_pbf_paths or []),
        "cross_region_complete_refs": sorted(complete_refs) if complete_refs else None,
    }
    if not enabled:
        return stats

    have = set(ways.keys())
    truncated: List[dict] = []
    to_fetch: Set[int] = set()
    for bucket in buckets.values():
        if bucket["type"] not in _COMPLETE_TYPES:
            continue
        if complete_refs is not None:
            ref = None
            for t in bucket["tags_list"]:
                ref = normalize_ref_token(t.get("ref") or "") or ref
            if ref not in complete_refs:
                continue
        missing = missing_ids(bucket["way_ids_ordered"], have)
        if not missing:
            continue
        truncated.append(bucket)
        to_fetch.update(missing)

    stats["cross_region_buckets"] = len(truncated)
    stats["cross_region_missing_requested"] = len(to_fetch)
    if not to_fetch:
        return stats

    print(
        f"  Completando {len(to_fetch)} ways fuera del extracto "
        f"({len(truncated)} nacionales/autovías truncadas)…"
    )

    # 1) Neighbor extracts
    for path in neighbor_pbf_paths or []:
        remaining = to_fetch - set(ways.keys())
        if not remaining:
            break
        print(f"    Escaneando vecino: {path} ({len(remaining)} pendientes)…")
        before = len(ways)
        _ingest_ways_from_pbf(path, remaining, ways)
        gained = len(ways) - before
        stats["cross_region_ways_from_neighbors"] += gained
        print(f"      +{gained} geometrías")

    # 2) Overpass por relación. Una consulta fallida no cierra la carretera:
    # se vuelve a pedir hasta obtener respuesta.
    answered: Set[int] = set()
    overpass_round = 0
    while True:
        still_ids = to_fetch - set(ways.keys())
        if not still_ids:
            break
        pending = _pending_relations(truncated, ways, answered)
        if not pending:
            break
        overpass_round += 1
        stats["cross_region_overpass_rounds"] = overpass_round
        if overpass_round == 1:
            print(f"    Overpass por relación ({len(still_ids)} ways aún faltan)…")
        else:
            labels = ", ".join(sorted({label for label, _rid in pending}))
            print(
                f"    Reintento {overpass_round}: {len(pending)} relaciones sin respuesta "
                f"({labels}). El paquete no se guarda todavía."
            )
            time.sleep(min(120.0, 15.0 * overpass_round))
        failed: List[Tuple[str, int]] = []
        for label, rid in pending:
            bucket = next(b for b in truncated if b["key_value"] == label)
            need = set(missing_ids(bucket["way_ids_ordered"], set(ways.keys())))
            if rid in answered or not need:
                continue
            try:
                fetched = fetch_ways_for_relations(
                    [rid],
                    on_progress=lambda msg, label=label, rid=rid: print(
                        f"      {label} rel/{rid}: {msg}"
                    ),
                )
            except RuntimeError as exc:
                print(f"      {label} rel/{rid}: Overpass falló ({exc})")
                failed.append((label, rid))
                time.sleep(5.0)
                continue
            answered.add(rid)
            added = 0
            for wid, coords in fetched.items():
                if wid in need and wid not in ways:
                    ways[wid] = WayGeom(way_id=wid, tags={}, coords=coords)
                    added += 1
            stats["cross_region_ways_from_overpass"] += added
            remain = len(missing_ids(bucket["way_ids_ordered"], set(ways.keys())))
            print(f"      {label} rel/{rid}: +{added} ways (quedan {remain})")
            time.sleep(1.5)
        if failed:
            continue

    # 3) Fallback por way id: la relación respondió pero faltan tramos, o
    # vecinos/Overpass por relación no bastaron.
    still_ids = sorted(to_fetch - set(ways.keys()))
    way_round = 0
    while still_ids:
        way_round += 1
        stats["cross_region_way_rounds"] = way_round
        print(
            f"    Overpass por way id: {len(still_ids)} pendientes "
            f"(pasada {way_round}). El paquete no se guarda todavía."
        )
        if way_round > 1:
            time.sleep(min(120.0, 20.0 * way_round))
        before = len(ways)
        try:
            fetched = fetch_way_geometries(
                still_ids,
                on_progress=lambda done, total, batch, _n: print(
                    f"      lote {done}/{total} ({batch} ways)"
                ),
            )
        except RuntimeError as exc:
            print(f"      Overpass por way id falló ({exc})")
            time.sleep(30.0)
            still_ids = sorted(to_fetch - set(ways.keys()))
            continue
        for wid, coords in fetched.items():
            if wid not in ways and len(coords) >= 2:
                ways[wid] = WayGeom(way_id=wid, tags={}, coords=coords)
        gained = len(ways) - before
        stats["cross_region_ways_from_overpass"] += gained
        print(f"      +{gained} geometrías por way id")
        still_ids = sorted(to_fetch - set(ways.keys()))
        if gained == 0 and still_ids:
            # Respondió sin esos ids: no hay más que pedir en Overpass.
            break

    still = sorted(to_fetch - set(ways.keys()))
    stats["cross_region_still_missing"] = len(still)
    if still:
        print(
            f"  Sin geometría para {len(still)} ways tras vecinos y Overpass. "
            "El paquete no se escribe."
        )
    else:
        print("  Completado cross-region: sin ways pendientes")
    return stats


def _pending_relations(
    truncated: Sequence[dict],
    ways: Dict[int, WayGeom],
    answered: Set[int],
) -> List[Tuple[str, int]]:
    """Relaciones cuya carretera sigue incompleta y que aún no han respondido."""
    have = set(ways.keys())
    pending: List[Tuple[str, int]] = []
    ordered = sorted(
        truncated,
        key=lambda b: len(missing_ids(b["way_ids_ordered"], have)),
    )
    for bucket in ordered:
        if not missing_ids(bucket["way_ids_ordered"], have):
            continue
        label = bucket["key_value"]
        for rid in sorted(set(bucket["relation_ids"])):
            if rid not in answered:
                pending.append((label, int(rid)))
    return pending


def _ingest_ways_from_pbf(
    pbf_path: str,
    needed_ids: Set[int],
    ways: Dict[int, WayGeom],
) -> None:
    if not needed_ids:
        return
    handler = _WayPass(needed_ids=set(needed_ids), collect_supplemental=False)
    handler.apply_file(pbf_path, locations=True)
    for wid, geom in handler.ways.items():
        if wid not in ways:
            ways[wid] = geom


def _build_supplemental(
    candidates: List[WayGeom],
    *,
    region_id: str,
    skip_way_ids: Set[int],
) -> Tuple[List[LogicalRoad], List[Discontinuity], Dict]:
    # Group rindos by name; locals by name
    rindo_groups: Dict[str, List[WayGeom]] = defaultdict(list)
    local_groups: Dict[str, List[WayGeom]] = defaultdict(list)
    motorroad_groups: Dict[str, List[WayGeom]] = defaultdict(list)
    dropped_short_rindo = 0

    for geom in candidates:
        if geom.way_id in skip_way_ids:
            continue
        if _is_rindo(geom.tags):
            name = preferred_name(geom.tags) or f"rindo-{geom.way_id}"
            rindo_groups[_slug(name)].append(geom)
        elif _is_motorroad_way(geom.tags):
            name = preferred_name(geom.tags) or f"motorroad-{geom.way_id}"
            motorroad_groups[_slug(name)].append(geom)
        else:
            name = preferred_name(geom.tags)
            key = _slug(name) if name else f"way-{geom.way_id}"
            local_groups[key].append(geom)

    roads: List[LogicalRoad] = []
    discs: List[Discontinuity] = []

    for slug, group in rindo_groups.items():
        components, _ = connect_segments([g.coords for g in group], snap_m=80.0)
        length_m = multiline_length_m(components)
        if length_m < MIN_RINDO_LENGTH_M:
            dropped_short_rindo += 1
            continue
        name = preferred_name(group[0].tags)
        road_id = f"{region_id}:rindo:{slug}"
        continuous = len(components) <= 1
        toll_lines = _join_exact([list(g.coords) for g in group if way_is_toll(g.tags)])
        roads.append(
            LogicalRoad(
                id=road_id,
                name=name,
                ref=None,
                type="forest",
                length_m=round(length_m, 1),
                region_id=region_id,
                continuous=continuous,
                segment_count=len(components),
                osm_way_ids=sorted(g.way_id for g in group),
                geometry=as_geojson_geometry(components),
                gap_count=max(0, len(components) - 1),
                toll_lines=toll_lines,
            )
        )
        if not continuous:
            discs.append(
                Discontinuity(
                    road_id=road_id,
                    component_count=len(components),
                    gap_count=max(0, len(components) - 1),
                    note="Rindo discontinuo",
                )
            )

    for slug, group in motorroad_groups.items():
        components, _ = connect_segments([g.coords for g in group], snap_m=50.0)
        length_m = multiline_length_m(components)
        if length_m <= 0:
            continue
        name = preferred_name(group[0].tags)
        road_id = f"{region_id}:motorroad:{slug}"
        continuous = len(components) <= 1
        toll_lines = _join_exact([list(g.coords) for g in group if way_is_toll(g.tags)])
        roads.append(
            LogicalRoad(
                id=road_id,
                name=name,
                ref=normalize_ref_token(group[0].tags.get("ref") or ""),
                type="motorroad",
                length_m=round(length_m, 1),
                region_id=region_id,
                continuous=continuous,
                segment_count=len(components),
                osm_way_ids=sorted(g.way_id for g in group),
                geometry=as_geojson_geometry(components),
                gap_count=max(0, len(components) - 1),
                toll_lines=toll_lines,
            )
        )
        if not continuous:
            discs.append(
                Discontinuity(
                    road_id=road_id,
                    component_count=len(components),
                    gap_count=max(0, len(components) - 1),
                    note="Autovía discontinua",
                )
            )

    for slug, group in local_groups.items():
        components, _ = connect_segments([g.coords for g in group], snap_m=50.0)
        length_m = multiline_length_m(components)
        name = preferred_name(group[0].tags)
        road_id = f"{region_id}:local:{slug}"
        continuous = len(components) <= 1
        toll_lines = _join_exact([list(g.coords) for g in group if way_is_toll(g.tags)])
        roads.append(
            LogicalRoad(
                id=road_id,
                name=name,
                ref=normalize_ref_token(group[0].tags.get("ref") or ""),
                type="municipal",
                length_m=round(length_m, 1),
                region_id=region_id,
                continuous=continuous,
                segment_count=len(components),
                osm_way_ids=sorted(g.way_id for g in group),
                geometry=as_geojson_geometry(components),
                gap_count=max(0, len(components) - 1),
                toll_lines=toll_lines,
            )
        )
        if not continuous:
            discs.append(
                Discontinuity(
                    road_id=road_id,
                    component_count=len(components),
                    gap_count=max(0, len(components) - 1),
                )
            )

    return roads, discs, {"rindo_dropped_short": dropped_short_rindo}
