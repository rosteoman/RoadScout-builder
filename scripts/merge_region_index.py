#!/usr/bin/env python3
"""Fusiona ZIPs nuevos con el index.json remoto del release de regiones.

Uso:
  python3 scripts/merge_region_index.py \\
    --zips-dir path/to/zips \\
    --out path/to/index.json \\
    [--remote-index path/to/existing-index.json] \\
    [--version 9]

Sin --remote-index, intenta descargar index.json del release
rosteoman/RoadScout-regions tag regiones vía gh.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Optional

RELEASE_REPO = "rosteoman/RoadScout-regions"
RELEASE_TAG = "regiones"
ZIP_NAME = re.compile(r"^(?P<id>.+)-v(?P<version>\d+)\.zip$")


def _load_index(path: Optional[Path]) -> Dict[str, dict]:
    if path is None or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    packages = data.get("packages") if isinstance(data, dict) else None
    if not isinstance(packages, list):
        return {}
    by_id: Dict[str, dict] = {}
    for row in packages:
        if isinstance(row, dict) and row.get("id"):
            by_id[str(row["id"])] = row
    return by_id


def _download_remote_index() -> Dict[str, dict]:
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
                return {}
            return _load_index(path)
    except OSError:
        return {}


def _entries_from_zips(zips_dir: Path, default_version: int) -> Dict[str, dict]:
    by_id: Dict[str, dict] = {}
    for path in sorted(zips_dir.glob("*-v*.zip")):
        match = ZIP_NAME.match(path.name)
        if not match or path.stat().st_size <= 0:
            continue
        region_id = match.group("id")
        version = int(match.group("version"))
        by_id[region_id] = {
            "id": region_id,
            "version": version if version > 0 else default_version,
            "bytes": path.stat().st_size,
            "file": path.name,
        }
    return by_id


def merge(
    zips_dir: Path,
    *,
    remote_index: Optional[Path],
    default_version: int,
    fetch_remote: bool,
) -> dict:
    by_id: Dict[str, dict] = {}
    if remote_index is not None:
        by_id = _load_index(remote_index)
    elif fetch_remote:
        by_id = _download_remote_index()
    by_id.update(_entries_from_zips(zips_dir, default_version))
    packages = sorted(by_id.values(), key=lambda row: str(row["id"]))
    return {"packages": packages}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zips-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--remote-index", type=Path, default=None)
    parser.add_argument("--version", type=int, default=9)
    parser.add_argument(
        "--no-fetch-remote",
        action="store_true",
        help="No descargar index.json del release si falta --remote-index",
    )
    args = parser.parse_args()

    if not args.zips_dir.is_dir():
        print(f"no existe directorio de zips: {args.zips_dir}", flush=True)
        return 1

    index = merge(
        args.zips_dir,
        remote_index=args.remote_index,
        default_version=args.version,
        fetch_remote=not args.no_fetch_remote and args.remote_index is None,
    )

    if not index["packages"]:
        print("índice vacío: no hay paquetes", flush=True)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"escrito {args.out} ({len(index['packages'])} paquetes)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
