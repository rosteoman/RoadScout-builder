"""Export region package files and zip."""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .group import Discontinuity, LogicalRoad
from .regions import RegionDef

# MapLibre geojson-vt recorta LineStrings largas en bordes de tile.
# Trozos cortos + overlap de 1 punto evitan huecos al zoom bajo.
_MAX_POINTS_PER_CHUNK = 100


def _chunk_line(coords: List[Any], max_points: int = _MAX_POINTS_PER_CHUNK) -> List[List[Any]]:
    if len(coords) < 2:
        return []
    if len(coords) <= max_points:
        return [coords]
    chunks: List[List[Any]] = []
    start = 0
    while start < len(coords) - 1:
        end = min(start + max_points, len(coords))
        chunk = coords[start:end]
        if len(chunk) >= 2:
            chunks.append(chunk)
        if end >= len(coords):
            break
        start = end - 1
    return chunks


def _same_point(a: List[Any], b: List[Any]) -> bool:
    return abs(float(a[0]) - float(b[0])) <= 1e-9 and abs(float(a[1]) - float(b[1])) <= 1e-9


def _append_lines(
    features: List[dict],
    road_id: str,
    props: Dict[str, Any],
    lines: List[List[Any]],
    *,
    toll: bool,
) -> None:
    chunks: List[List[Any]] = []
    for line in lines:
        chunks.extend(_chunk_line(line))
    if not chunks:
        return
    tagged = dict(props)
    tagged["toll"] = toll
    if len(chunks) == 1 and not toll:
        features.append(
            {
                "type": "Feature",
                "id": road_id,
                "properties": tagged,
                "geometry": {"type": "LineString", "coordinates": chunks[0]},
            }
        )
        return
    for index, chunk in enumerate(chunks):
        suffix = f"toll-{index}" if toll else str(index)
        features.append(
            {
                "type": "Feature",
                "id": f"{road_id}#{suffix}",
                "properties": tagged,
                "geometry": {"type": "LineString", "coordinates": chunk},
            }
        )


def _append_yen(
    features: List[dict],
    road_id: str,
    props: Dict[str, Any],
    lines: List[List[Any]],
) -> None:
    for index, line in enumerate(lines):
        if len(line) < 2:
            continue
        ends = [line[0]]
        if not _same_point(line[0], line[-1]):
            ends.append(line[-1])
        for end_index, coord in enumerate(ends):
            tagged = dict(props)
            tagged["toll"] = True
            tagged["marker"] = "yen"
            features.append(
                {
                    "type": "Feature",
                    "id": f"{road_id}#yen-{index}-{end_index}",
                    "properties": tagged,
                    "geometry": {"type": "Point", "coordinates": coord},
                }
            )


def _lines_from_geometry(geom: dict) -> List[List[Any]]:
    if geom.get("type") == "MultiLineString":
        return [ln for ln in (geom.get("coordinates") or []) if len(ln) >= 2]
    if geom.get("type") == "LineString":
        coords = geom.get("coordinates") or []
        return [coords] if len(coords) >= 2 else []
    return []


def write_package(
    out_dir: Path,
    region: RegionDef,
    roads: List[LogicalRoad],
    discontinuities: List[Discontinuity],
    *,
    package_version: int,
    source_url: str,
    extract_date: Optional[str],
    build_stats: Dict[str, Any],
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)

    features = []
    index_roads = []
    ndjson_lines = []
    for road in roads:
        props = {
            "id": road.id,
            "name": road.name,
            "ref": road.ref,
            "type": road.type,
            "length_m": road.length_m,
            "region_id": road.region_id,
            "continuous": road.continuous,
            "segment_count": road.segment_count,
            "osm_way_ids": road.osm_way_ids,
            "name_ja": road.name_ja,
            "name_en": road.name_en,
            "network": road.network,
            "label": road.ref or ((road.name or "—")[:14]),
        }
        lines = _lines_from_geometry(road.geometry or {})
        _append_lines(features, road.id, props, lines, toll=False)
        toll_lines = [line for line in (road.toll_lines or []) if len(line) >= 2]
        if toll_lines:
            _append_lines(features, road.id, props, toll_lines, toll=True)
            _append_yen(features, road.id, props, toll_lines)
        if lines:
            start = lines[0][0]
            end = lines[-1][-1]
            index_roads.append(
                {
                    "id": road.id,
                    "name": road.name,
                    "ref": road.ref,
                    "type": road.type,
                    "length_m": road.length_m,
                    "region_id": road.region_id,
                    "continuous": road.continuous,
                    "segment_count": road.segment_count,
                    "label": road.ref or road.name or "—",
                    "start": start,
                    "end": end,
                }
            )
            payload: Dict[str, Any] = {"id": road.id, "coordinates": lines}
            if toll_lines:
                payload["toll"] = toll_lines
            ndjson_lines.append(json.dumps(payload, ensure_ascii=False))

    roads_path = out_dir / "roads.geojson"
    roads_path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False),
        encoding="utf-8",
    )
    (out_dir / "roads-index.json").write_text(
        json.dumps({"roads": index_roads}, ensure_ascii=False),
        encoding="utf-8",
    )
    (out_dir / "roads.geometry.ndjson").write_text(
        ("\n".join(ndjson_lines) + ("\n" if ndjson_lines else "")),
        encoding="utf-8",
    )

    disc_path = out_dir / "discontinuities.json"
    disc_path.write_text(
        json.dumps(
            {
                "format_version": "1.0",
                "items": [
                    {
                        "road_id": d.road_id,
                        "component_count": d.component_count,
                        "gap_count": d.gap_count,
                        "note": d.note,
                    }
                    for d in discontinuities
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    total_length = round(sum(r.length_m for r in roads), 1)
    types = sorted({r.type for r in roads})
    bbox = build_stats.get("bbox") or region.bbox_hint

    manifest = {
        "format_version": "1.0",
        "region_id": region.region_id,
        "region_name": region.region_name,
        "locale_names": region.locale_names,
        "package_version": package_version,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {
            "provider": "geofabrik",
            "extract_url": source_url,
            "extract_date": extract_date,
            "osm_license": "ODbL",
        },
        "bbox": bbox,
        "prefectures": region.prefectures,
        "road_count": len(roads),
        "total_length_m": total_length,
        "road_types": types,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    report = {
        "format_version": "1.0",
        "region_id": region.region_id,
        "package_version": package_version,
        "stats": build_stats,
        "discontinuity_count": len(discontinuities),
        "road_count": len(roads),
        "total_length_m": total_length,
        "by_type": _count_by_type(roads),
    }
    (out_dir / "build-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    zip_path = out_dir.parent / f"{region.region_id}-v{package_version}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in (
            "manifest.json",
            "roads.geojson",
            "roads-index.json",
            "roads.geometry.ndjson",
            "discontinuities.json",
            "build-report.json",
        ):
            zf.write(out_dir / name, arcname=f"{region.region_id}/{name}")
    return zip_path


def _count_by_type(roads: List[LogicalRoad]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for road in roads:
        counts[road.type] = counts.get(road.type, 0) + 1
    return dict(sorted(counts.items()))
