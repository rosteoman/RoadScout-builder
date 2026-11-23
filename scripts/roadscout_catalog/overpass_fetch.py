"""Fetch OSM way geometries outside the regional PBF via Overpass API.

Used to complete national/motorway route=road relations truncated by Geofabrik
regional extracts (e.g. 国道162号 Kyoto→Fukui when building Kansai).
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from http.client import IncompleteRead
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

Coord = Tuple[float, float]

DEFAULT_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
)

DEFAULT_BATCH_SIZE = 80
DEFAULT_TIMEOUT_S = 180
DEFAULT_RETRIES = 10


def fetch_ways_for_relations(
    relation_ids: Sequence[int],
    *,
    endpoints: Sequence[str] = DEFAULT_ENDPOINTS,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    retries: int = DEFAULT_RETRIES,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Dict[int, List[Coord]]:
    """Fetch all way members of the given relations with full geometry."""
    unique = sorted({int(r) for r in relation_ids if int(r) > 0})
    if not unique:
        return {}
    ids = ",".join(str(i) for i in unique)
    query = f"[out:json][timeout:{timeout_s}];\nrelation(id:{ids});\nway(r);\nout geom;\n"
    if on_progress:
        on_progress(f"relations {ids} ({len(unique)})")
    data = _post_with_retries(
        query,
        endpoints=endpoints,
        timeout_s=timeout_s,
        retries=retries,
    )
    return _elements_to_ways(data)


def fetch_relation_polygon(
    relation_id: int,
    *,
    endpoints: Sequence[str] = DEFAULT_ENDPOINTS,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    retries: int = DEFAULT_RETRIES,
) -> Optional[dict]:
    """Return GeoJSON MultiPolygon geometry for an OSM relation, or None."""
    query = (
        f"[out:json][timeout:{timeout_s}];\n"
        f"relation({int(relation_id)});\n"
        "out geom;\n"
    )
    data = _post_with_retries(
        query,
        endpoints=endpoints,
        timeout_s=timeout_s,
        retries=retries,
    )
    relation = next(
        (element for element in data.get("elements") or [] if element.get("type") == "relation"),
        None,
    )
    if not relation:
        return None
    outers: List[List[Coord]] = []
    inners: List[List[Coord]] = []
    for member in relation.get("members") or []:
        if member.get("type") != "way":
            continue
        coords = _coords_from_element({"geometry": member.get("geometry") or []})
        if len(coords) < 4:
            continue
        role = (member.get("role") or "outer").strip() or "outer"
        (inners if role == "inner" else outers).append(coords)
    if not outers:
        return None
    rings = _stitch_rings(outers)
    hole_rings = _stitch_rings(inners)
    if not rings:
        return None
    polygons = []
    for ring in rings:
        holes = [hole for hole in hole_rings if _point_in_ring(hole[0], ring)]
        polygons.append([ring, *holes])
    return {"type": "MultiPolygon", "coordinates": polygons}


def _stitch_rings(lines: List[List[Coord]]) -> List[List[Coord]]:
    remaining = [list(line) for line in lines if len(line) >= 2]
    rings: List[List[Coord]] = []
    while remaining:
        current = remaining.pop()
        changed = True
        while changed:
            changed = False
            for index, other in enumerate(remaining):
                merged = None
                if _same(current[-1], other[0]):
                    merged = current + other[1:]
                elif _same(current[-1], other[-1]):
                    merged = current + list(reversed(other))[1:]
                elif _same(current[0], other[-1]):
                    merged = other + current[1:]
                elif _same(current[0], other[0]):
                    merged = list(reversed(other)) + current[1:]
                if merged is None:
                    continue
                current = merged
                remaining.pop(index)
                changed = True
                break
        if len(current) >= 4 and _same(current[0], current[-1]):
            rings.append(current)
        elif len(current) >= 3:
            current.append(current[0])
            rings.append(current)
    return rings


def _same(left: Coord, right: Coord) -> bool:
    return abs(left[0] - right[0]) < 1e-6 and abs(left[1] - right[1]) < 1e-6


def _point_in_ring(point: Coord, ring: List[Coord]) -> bool:
    lon, lat = point
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1 + 1e-18) + x1:
            inside = not inside
    return inside


def _ref_overpass_pattern(ref: str) -> str:
    """Match OSM refs that differ only by spaces (VA123 vs VA 123)."""
    parts = re.split(r"(\d+)", ref)
    pattern = []
    for index, part in enumerate(parts):
        if not part:
            continue
        pattern.append(re.escape(part))
        if not part.isdigit() and index + 1 < len(parts) and parts[index + 1].isdigit():
            pattern.append(r"\s*")
    return "".join(pattern) or re.escape(ref)


def fetch_ways_by_ref(
    ref: str,
    bbox: Sequence[float],
    *,
    endpoints: Sequence[str] = DEFAULT_ENDPOINTS,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    retries: int = DEFAULT_RETRIES,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Dict[int, dict]:
    """Return {way_id: {"coords": [...], "tags": {...}}} for a highway ref in bbox.

    bbox is [west, south, east, north]. Overpass uses (south, west, north, east).
    """
    ref = (ref or "").strip()
    if not ref or len(bbox) < 4:
        return {}
    south, west, north, east = bbox[1], bbox[0], bbox[3], bbox[2]
    pattern = _ref_overpass_pattern(ref)
    query = (
        f"[out:json][timeout:{timeout_s}];\n"
        f'way["highway"]["ref"~"{pattern}"]({south},{west},{north},{east});\n'
        "out geom tags;\n"
    )
    if on_progress:
        on_progress(f"ref {ref}")
    data = _post_with_retries(
        query,
        endpoints=endpoints,
        timeout_s=timeout_s,
        retries=retries,
    )
    return _elements_to_ways_with_tags(data)


def fetch_way_geometries(
    way_ids: Sequence[int],
    *,
    endpoints: Sequence[str] = DEFAULT_ENDPOINTS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    retries: int = DEFAULT_RETRIES,
    sleep_between_batches_s: float = 1.5,
    on_progress: Optional[Callable[[int, int, int, int], None]] = None,
) -> Dict[int, List[Coord]]:
    """Return {way_id: [(lon, lat), ...]} for ways Overpass could resolve.

    Si un lote falla (504/red), lo parte en dos y reintenta. Un way suelto
    que siga fallando se omite en lugar de tumbar todo el fetch.
    """
    unique = sorted({int(w) for w in way_ids if int(w) > 0})
    if not unique:
        return {}

    # Listas enormes (p.ej. Kansai ~11k) saturan Overpass con lotes de 80.
    effective_batch = batch_size
    if batch_size == DEFAULT_BATCH_SIZE and len(unique) > 2000:
        effective_batch = 40

    out: Dict[int, List[Coord]] = {}
    skipped: List[int] = []
    total_batches = (len(unique) + effective_batch - 1) // effective_batch

    def fetch_chunk(chunk: List[int]) -> None:
        if not chunk:
            return
        try:
            query = _query_ways(chunk, timeout_s=timeout_s)
            data = _post_with_retries(
                query,
                endpoints=endpoints,
                timeout_s=timeout_s,
                retries=retries,
            )
            out.update(_elements_to_ways(data))
        except RuntimeError as exc:
            if len(chunk) > 1:
                mid = max(1, len(chunk) // 2)
                print(
                    f"  Overpass lote de {len(chunk)} falló ({exc}); "
                    f"partiendo en {mid}+{len(chunk) - mid}",
                    flush=True,
                )
                time.sleep(5.0)
                fetch_chunk(chunk[:mid])
                if sleep_between_batches_s > 0:
                    time.sleep(sleep_between_batches_s)
                fetch_chunk(chunk[mid:])
            else:
                print(f"  Overpass omitido way/{chunk[0]}: {exc}", flush=True)
                skipped.append(chunk[0])

    for batch_index, start in enumerate(range(0, len(unique), effective_batch)):
        batch = unique[start : start + effective_batch]
        if on_progress:
            on_progress(batch_index + 1, total_batches, len(batch), len(unique))
        else:
            print(
                f"  Overpass vías lote {batch_index + 1}/{total_batches} ({len(batch)} ids)",
                flush=True,
            )
        fetch_chunk(batch)
        if start + effective_batch < len(unique) and sleep_between_batches_s > 0:
            time.sleep(sleep_between_batches_s)

    if skipped:
        print(
            f"  Overpass omitió {len(skipped)} vías tras agotar reintentos",
            flush=True,
        )
    return out


def _query_ways(way_ids: Sequence[int], *, timeout_s: int) -> str:
    ids = ",".join(str(i) for i in way_ids)
    return f"[out:json][timeout:{timeout_s}];\nway(id:{ids});\nout geom;\n"


def _coords_from_element(element: dict) -> List[Coord]:
    coords: List[Coord] = []
    for pt in element.get("geometry") or []:
        lon = pt.get("lon")
        lat = pt.get("lat")
        if lon is None or lat is None:
            continue
        coords.append((float(lon), float(lat)))
    return coords


def _elements_to_ways(data: dict) -> Dict[int, List[Coord]]:
    out: Dict[int, List[Coord]] = {}
    for element in data.get("elements") or []:
        if element.get("type") != "way":
            continue
        coords = _coords_from_element(element)
        if len(coords) >= 2:
            out[int(element["id"])] = coords
    return out


def _elements_to_ways_with_tags(data: dict) -> Dict[int, dict]:
    out: Dict[int, dict] = {}
    for element in data.get("elements") or []:
        if element.get("type") != "way":
            continue
        coords = _coords_from_element(element)
        if len(coords) < 2:
            continue
        tags = element.get("tags") or {}
        out[int(element["id"])] = {
            "coords": coords,
            "tags": {str(k): str(v) for k, v in tags.items()},
        }
    return out


def _post_via_curl(endpoint: str, query: str, *, timeout_s: int) -> dict:
    """macOS system Python (LibreSSL 2.8) often fails TLS to Overpass; curl works."""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".overpass", delete=False) as tmp:
        tmp.write(query)
        query_path = Path(tmp.name)
    try:
        result = subprocess.run(
            [
                "curl",
                "-sS",
                "-f",
                "--max-time",
                str(timeout_s + 60),
                "-A",
                "RoadScoutCatalogBuilder/1.0 (region package complete)",
                "-H",
                "Accept: application/json",
                "--data-urlencode",
                f"data@{query_path}",
                endpoint,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or f"curl exit {result.returncode}").strip()
            raise OSError(err)
        return json.loads(result.stdout)
    finally:
        query_path.unlink(missing_ok=True)


def _transient_overpass_error(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    if code in (429, 502, 503, 504):
        return True
    message = str(exc).lower()
    needles = (
        "ssl",
        "handshake",
        "504",
        "502",
        "503",
        "429",
        "timed out",
        "timeout",
        "unreachable",
        "connection reset",
        "temporarily",
        "gateway",
        "nodename",
        "name or service",
        "network is unreachable",
    )
    return any(needle in message for needle in needles)


def _post_with_retries(
    query: str,
    *,
    endpoints: Sequence[str],
    timeout_s: int,
    retries: int,
) -> dict:
    last_error: Optional[BaseException] = None
    form = urllib.parse.urlencode({"data": query}).encode("utf-8")
    for attempt in range(retries):
        endpoint = endpoints[attempt % len(endpoints)]
        req = urllib.request.Request(
            endpoint,
            data=form,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "RoadScoutCatalogBuilder/1.0 (region package complete)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s + 60) as resp:
                raw = resp.read().decode("utf-8")
            return json.loads(raw)
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            socket.timeout,
            json.JSONDecodeError,
            IncompleteRead,
            ConnectionError,
            OSError,
        ) as exc:
            last_error = exc
            message = str(exc).lower()
            # curl: TLS viejo en macOS, y también 5xx/red inestable en GHA.
            if _transient_overpass_error(exc):
                try:
                    return _post_via_curl(endpoint, query, timeout_s=timeout_s)
                except (
                    OSError,
                    json.JSONDecodeError,
                    subprocess.SubprocessError,
                ) as curl_exc:
                    last_error = curl_exc
                    message = str(curl_exc).lower()
            # DNS / gateway: esperar más antes de cambiar de espejo.
            if _transient_overpass_error(last_error or exc) or "nodename" in message:
                time.sleep(min(90.0, 8.0 * (attempt + 1)))
            else:
                time.sleep(min(45.0, 3.0 * (attempt + 1)))
    raise RuntimeError(f"Overpass falló tras {retries} intentos: {last_error}") from last_error


def missing_ids(needed: Iterable[int], have: Set[int]) -> List[int]:
    return sorted({int(i) for i in needed} - have)
