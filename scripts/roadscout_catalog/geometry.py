"""Geometry helpers: length and connecting way segments."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple, TypeVar

from pyproj import Geod

Coord = Tuple[float, float]  # lon, lat
GEOD = Geod(ellps="WGS84")


def line_length_m(coords: Sequence[Coord]) -> float:
    if len(coords) < 2:
        return 0.0
    total = 0.0
    for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
        _, _, dist = GEOD.inv(lon1, lat1, lon2, lat2)
        total += abs(dist)
    return float(total)


def multiline_length_m(parts: Sequence[Sequence[Coord]]) -> float:
    return float(sum(line_length_m(part) for part in parts))


def reverse_coords(coords: Sequence[Coord]) -> List[Coord]:
    return list(reversed(coords))


def connect_segments(
    segments: Iterable[Sequence[Coord]],
    snap_m: float = 80.0,
) -> Tuple[List[List[Coord]], int]:
    """
    Merge segments that share endpoints (within snap_m), then retry with
    progressively larger snaps so concurrent-route gaps close when possible.
    """
    remaining: List[List[Coord]] = [list(seg) for seg in segments if len(seg) >= 2]
    if not remaining:
        return [], 0

    components = _greedy_merge(remaining, snap_m)
    for wider in (150.0, 300.0):
        if len(components) <= 1:
            break
        components = _greedy_merge(components, wider)

    components = prune_orphan_components(components)
    gap_count = max(0, len(components) - 1)
    return components, gap_count


def _greedy_merge(segments: List[List[Coord]], snap_m: float) -> List[List[Coord]]:
    remaining: List[List[Coord]] = [list(seg) for seg in segments if len(seg) >= 2]
    components: List[List[Coord]] = []
    while remaining:
        current = remaining.pop()
        changed = True
        while changed:
            changed = False
            i = 0
            while i < len(remaining):
                other = remaining[i]
                merged = _try_merge(current, other, snap_m)
                if merged is not None:
                    current = merged
                    remaining.pop(i)
                    changed = True
                    continue
                i += 1
        components.append(current)
    return components


def prune_orphan_components(
    components: List[List[Coord]],
    *,
    min_keep_m: float = 120.0,
    far_m: float = 2500.0,
) -> List[List[Coord]]:
    """
    Drop tiny crumbs far from the main network of a logical road.
    Keeps real long discontinuities; removes noise stubs.
    """
    if len(components) <= 1:
        return components

    lengths = [line_length_m(c) for c in components]
    main_idx = max(range(len(components)), key=lambda i: lengths[i])
    main = components[main_idx]
    main_ends = [main[0], main[-1]]

    kept: List[List[Coord]] = []
    for comp, length in zip(components, lengths):
        if length >= min_keep_m:
            kept.append(comp)
            continue
        # tiny: keep only if near any already-kept / main endpoint
        near = False
        for p in (comp[0], comp[-1]):
            for q in main_ends:
                if _endpoint_dist_m(p, q) <= far_m:
                    near = True
                    break
            if near:
                break
            for other in kept:
                if _endpoint_dist_m(p, other[0]) <= far_m or _endpoint_dist_m(p, other[-1]) <= far_m:
                    near = True
                    break
            if near:
                break
        if near:
            kept.append(comp)
    return kept if kept else [main]


def _endpoint_dist_m(a: Coord, b: Coord) -> float:
    _, _, dist = GEOD.inv(a[0], a[1], b[0], b[1])
    return abs(dist)


def _try_merge(a: List[Coord], b: List[Coord], snap_m: float) -> Optional[List[Coord]]:
    candidates = [
        (a, b),
        (a, reverse_coords(b)),
        (reverse_coords(a), b),
        (reverse_coords(a), reverse_coords(b)),
    ]
    best = None
    best_dist = snap_m + 1
    for left, right in candidates:
        d = _endpoint_dist_m(left[-1], right[0])
        if d <= snap_m and d < best_dist:
            best = (left, right, d)
            best_dist = d
        # also allow right then left
        d2 = _endpoint_dist_m(right[-1], left[0])
        if d2 <= snap_m and d2 < best_dist:
            best = (right, left, d2)
            best_dist = d2
    if best is None:
        return None
    left, right, _ = best
    if left[-1] == right[0]:
        return left + right[1:]
    return left + right


def as_geojson_geometry(components: List[List[Coord]]) -> dict:
    if len(components) == 1:
        return {"type": "LineString", "coordinates": components[0]}
    return {"type": "MultiLineString", "coordinates": components}


T = TypeVar("T")


def group_continuous_chains(
    items: Sequence[T],
    coords_of: Callable[[T], Sequence[Coord]],
) -> List[List[T]]:
    """Each chain is one continuous path.

    Two pieces join only when they meet at an endpoint that belongs to
    exactly those two. A junction stops the chain, so each arm stays separate.
    """
    indexed: List[Tuple[T, List[Coord]]] = []
    for item in items:
        coords = [c for c in coords_of(item)]
        if len(coords) >= 2:
            indexed.append((item, coords))
    if not indexed:
        return []

    def point_key(coord: Coord) -> Tuple[float, float]:
        return (round(coord[0], 6), round(coord[1], 6))

    degree: Dict[Tuple[float, float], int] = defaultdict(int)
    at: Dict[Tuple[float, float], List[int]] = defaultdict(list)
    for index, (_, coords) in enumerate(indexed):
        start = point_key(coords[0])
        end = point_key(coords[-1])
        degree[start] += 1
        at[start].append(index)
        if end != start:
            degree[end] += 1
            at[end].append(index)

    parent = list(range(len(indexed)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for point, indexes in at.items():
        if degree[point] == 2 and len(indexes) == 2:
            union(indexes[0], indexes[1])

    groups: Dict[int, List[T]] = defaultdict(list)
    for index, (item, _) in enumerate(indexed):
        groups[find(index)].append(item)
    return list(groups.values())


def group_touching_chains(
    items: Sequence[T],
    coords_of: Callable[[T], Sequence[Coord]],
    *,
    snap_deg: float = 0.00025,
) -> List[List[T]]:
    """Une piezas que comparten un extremo, también en un cruce.

    Dos tramos con el mismo nombre siguen siendo carreteras distintas si no
    se tocan. Un cruce no las parte.
    """
    indexed = _indexed_lines(items, coords_of)
    if not indexed:
        return []
    parent = list(range(len(indexed)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    at: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for index, (_, coords) in enumerate(indexed):
        at[_snap_key(coords[0], snap_deg)].append(index)
        at[_snap_key(coords[-1], snap_deg)].append(index)
    for indexes in at.values():
        first = indexes[0]
        for other in indexes[1:]:
            union(first, other)

    groups: Dict[int, List[T]] = defaultdict(list)
    for index, (item, _) in enumerate(indexed):
        groups[find(index)].append(item)
    return list(groups.values())


def group_straight_chains(
    items: Sequence[T],
    coords_of: Callable[[T], Sequence[Coord]],
    *,
    snap_deg: float = 0.00025,
) -> List[List[T]]:
    """Sigue la pista por el camino más recto en cada cruce.

    Un ramal que sale de lado se queda aparte. Dos tramos alineados, el mismo.
    """
    indexed = _indexed_lines(items, coords_of)
    if not indexed:
        return []
    parent = list(range(len(indexed)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    ends: Dict[Tuple[int, int], List[Tuple[int, float]]] = defaultdict(list)
    for index, (_, coords) in enumerate(indexed):
        start = _snap_key(coords[0], snap_deg)
        end = _snap_key(coords[-1], snap_deg)
        if start == end:
            continue
        ends[start].append((index, _bearing_away(coords, from_start=True)))
        ends[end].append((index, _bearing_away(coords, from_start=False)))

    for incidents in ends.values():
        pairs: List[Tuple[float, int, int]] = []
        for left in range(len(incidents)):
            for right in range(left + 1, len(incidents)):
                if incidents[left][0] == incidents[right][0]:
                    continue
                score = _turn_from_straight(incidents[left][1], incidents[right][1])
                pairs.append((score, incidents[left][0], incidents[right][0]))
        pairs.sort()
        used: set[int] = set()
        for score, left, right in pairs:
            if score > 35:
                break
            if left in used or right in used:
                continue
            used.add(left)
            used.add(right)
            union(left, right)

    groups: Dict[int, List[T]] = defaultdict(list)
    for index, (item, _) in enumerate(indexed):
        groups[find(index)].append(item)
    return list(groups.values())


def _indexed_lines(
    items: Sequence[T],
    coords_of: Callable[[T], Sequence[Coord]],
) -> List[Tuple[T, List[Coord]]]:
    indexed: List[Tuple[T, List[Coord]]] = []
    for item in items:
        coords = [coord for coord in coords_of(item)]
        if len(coords) >= 2:
            indexed.append((item, coords))
    return indexed


def _snap_key(coord: Coord, snap_deg: float) -> Tuple[int, int]:
    return (round(coord[0] / snap_deg), round(coord[1] / snap_deg))


def _bearing_away(coords: Sequence[Coord], *, from_start: bool) -> float:
    if from_start:
        origin = coords[0]
        target = next((point for point in coords[1:] if point != origin), coords[-1])
    else:
        origin = coords[-1]
        target = next((point for point in reversed(coords[:-1]) if point != origin), coords[0])
    lat = math.radians(origin[1])
    dx = (target[0] - origin[0]) * math.cos(lat)
    dy = target[1] - origin[1]
    return math.degrees(math.atan2(dx, dy)) % 360


def _turn_from_straight(left: float, right: float) -> float:
    diff = abs(left - right) % 360
    diff = min(diff, 360 - diff)
    return abs(diff - 180)


def bbox_of_components(components: Sequence[Sequence[Coord]]) -> List[float]:
    min_lon = min_lat = float("inf")
    max_lon = max_lat = float("-inf")
    for part in components:
        for lon, lat in part:
            min_lon = min(min_lon, lon)
            min_lat = min(min_lat, lat)
            max_lon = max(max_lon, lon)
            max_lat = max(max_lat, lat)
    if min_lon == float("inf"):
        return [0.0, 0.0, 0.0, 0.0]
    return [min_lon, min_lat, max_lon, max_lat]
