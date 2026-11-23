#!/usr/bin/env python3
"""Valida un paquete antes de publicarlo y explica cada fallo."""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
import zipfile
from pathlib import Path

BUILDER_ID = "catalog-v2-admin-boundary"
REQUIRED_ROADS = {
    "ceuta": ({"ref": "N-362", "min_length_m": 1_500.0},),
}
REQUIRED = {
    "manifest.json",
    "roads.geojson",
    "roads-index.json",
    "roads.geometry.ndjson",
    "discontinuities.json",
    "coverage.json",
    "catalog.mbtiles",
}


def validate(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"ZIP ilegible: {exc}"]

    with archive:
        names = set(archive.namelist())
        missing = REQUIRED.difference(names)
        if missing:
            errors.append(f"faltan archivos: {', '.join(sorted(missing))}")
            return errors

        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("package_version", 0) < 2:
            errors.append("package_version anterior a 2")
        if manifest.get("builder") != BUILDER_ID:
            errors.append(f"constructor no admitido: {manifest.get('builder')!r}")
        if not manifest.get("boundary"):
            errors.append("el manifiesto no identifica el límite utilizado")

        features = json.loads(archive.read("roads.geojson")).get("features", [])
        # Una carretera lógica puede salir en varias features (urban / traversable).
        feature_ids = [feature.get("properties", {}).get("id") for feature in features]
        if any(not road_id for road_id in feature_ids):
            errors.append("hay carreteras sin ID estable")
        logical_feature_ids = set(feature_ids)
        coverage = json.loads(archive.read("coverage.json"))
        missing_ways = coverage.get("missing_way_ids") or []
        if missing_ways:
            sample = ", ".join(str(way_id) for way_id in missing_ways[:8])
            errors.append(f"faltan {len(missing_ways)} vías del polígono ({sample})")
        required_ways = {int(way_id) for way_id in coverage.get("required_way_ids") or []}
        emitted_ways = {
            int(way_id)
            for feature in features
            for way_id in feature.get("properties", {}).get("osm_way_ids") or []
        }
        leftover = sorted(required_ways - emitted_ways)
        if leftover:
            sample = ", ".join(str(way_id) for way_id in leftover[:8])
            errors.append(f"coverage.json no coincide con el GeoJSON ({sample})")
        truncated = coverage.get("truncated_refs") or []
        if truncated:
            errors.append(f"carreteras aún truncadas: {', '.join(truncated[:8])}")
        if coverage.get("in_region_eligible", 0) <= 0:
            errors.append("coverage.json no enumera vías del polígono")

        region_id = manifest.get("region_id")
        for required_road in REQUIRED_ROADS.get(region_id, ()):
            matches = [
                feature
                for feature in features
                if (feature.get("properties", {}).get("ref") or "").upper()
                == required_road["ref"].upper()
            ]
            if not matches:
                errors.append(f"falta la carretera {required_road['ref']}")
            elif max(float(f.get("properties", {}).get("length_m") or 0) for f in matches) < required_road["min_length_m"]:
                errors.append(f"{required_road['ref']} es demasiado corta")
        index_rows = json.loads(archive.read("roads-index.json")).get("roads", [])
        index_ids = [row.get("id") for row in index_rows]
        if any(not road_id for road_id in index_ids):
            errors.append("roads-index.json tiene entradas sin ID")
        if len(index_ids) != len(set(index_ids)):
            errors.append("roads-index.json tiene IDs de carretera duplicados")
        index_id_set = set(index_ids)
        if logical_feature_ids != index_id_set:
            errors.append("roads-index.json no coincide con roads.geojson")
        ndjson_ids: list[object] = []
        for line in archive.read("roads.geometry.ndjson").decode("utf-8").splitlines():
            if not line.strip():
                continue
            ndjson_ids.append(json.loads(line).get("id"))
        ndjson_id_set = set(ndjson_ids)
        if logical_feature_ids != ndjson_id_set:
            errors.append("roads.geometry.ndjson no coincide con roads.geojson")
        if len(ndjson_ids) != len(ndjson_id_set):
            errors.append("roads.geometry.ndjson tiene IDs de carretera duplicados")
        types = {feature.get("properties", {}).get("type") for feature in features}
        if not types.intersection({"national", "municipal", "motorway", "motorroad", "prefectural", "prefectural_major", "forest"}):
            errors.append("el catálogo no tiene tipos de carretera reconocibles")
        if not any(feature.get("properties", {}).get("continuous") for feature in features):
            errors.append("ninguna carretera queda continua")

        with tempfile.TemporaryDirectory() as directory:
            mbtiles = Path(directory) / "catalog.mbtiles"
            mbtiles.write_bytes(archive.read("catalog.mbtiles"))
            connection = sqlite3.connect(f"file:{mbtiles}?mode=ro", uri=True)
            try:
                row = connection.execute(
                    "SELECT value FROM metadata WHERE name = 'json'"
                ).fetchone()
            finally:
                connection.close()
            metadata = json.loads(row[0]) if row else {}
            layers = metadata.get("vector_layers", [])
            roads = next((layer for layer in layers if layer.get("id") == "roads"), None)
            fields = set((roads or {}).get("fields", {}))
            expected = {"id", "type", "ref", "name", "label"}
            if roads is None or not expected.issubset(fields):
                errors.append("MBTiles no expone la capa roads con todos sus campos")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("packages", type=Path, nargs="+")
    args = parser.parse_args()
    failures = 0
    for package in args.packages:
        errors = validate(package)
        if errors:
            failures += 1
            print(f"FALLO {package}")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"OK {package}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
