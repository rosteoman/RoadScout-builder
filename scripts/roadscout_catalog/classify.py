"""Classify OSM ways into RoadScout catalog types and logical keys."""

from __future__ import annotations

import re
import unicodedata
from typing import List, Mapping, Optional, Tuple

RoadType = str  # motorway | national | prefectural | local | rindo | other


def _tag(tags: Mapping[str, str], key: str) -> Optional[str]:
    value = tags.get(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def name_blob(tags: Mapping[str, str]) -> str:
    parts = []
    for key in (
        "name:ja",
        "name",
        "official_name",
        "official_name:ja",
        "alt_name",
        "name:en",
        "short_name",
    ):
        value = _tag(tags, key)
        if value:
            parts.append(value)
    return " ".join(parts)


def preferred_name(tags: Mapping[str, str]) -> Optional[str]:
    for key in ("name:ja", "name", "official_name", "alt_name", "name:en"):
        value = _tag(tags, key)
        if value:
            return value
    return None


def normalize_ref_token(raw: Optional[str]) -> Optional[str]:
    """Normalize a single ref token (no semicolon splitting)."""
    if not raw:
        return None
    part = raw.strip()
    if not part:
        return None
    part = re.sub(r"^(国道|府道|県道|都道|一般府道|一般県道|主要地方道)\s*", "", part)
    part = part.replace("号", "")
    part = part.strip()
    cleaned = re.sub(r"[^0-9A-Za-z\-_/]", "", part)
    return cleaned or None


def normalize_ref(raw: Optional[str]) -> Optional[str]:
    """Primary ref (first token). Prefer refs_from_tags for shared routes."""
    if not raw:
        return None
    first = raw.split(";")[0].strip()
    return normalize_ref_token(first)


def refs_from_tags(tags: Mapping[str, str]) -> List[str]:
    """All refs on a way, including concurrent `168;425` style tags."""
    out: List[str] = []
    raw = _tag(tags, "ref")
    if raw:
        for part in raw.split(";"):
            token = normalize_ref_token(part)
            if token and token not in out:
                out.append(token)
    from_name = ref_from_name(name_blob(tags))
    if from_name and from_name not in out:
        out.append(from_name)
    return out


def ref_from_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    m = re.search(
        r"(?:国道|府道|県道|都道|一般府道|一般県道|主要地方道)\s*([0-9A-Za-z\-]+)\s*号?",
        name,
    )
    if m:
        return normalize_ref(m.group(1))
    return None


def classify_type(tags: Mapping[str, str]) -> RoadType:
    highway = _tag(tags, "highway") or ""
    network = (_tag(tags, "network") or "").lower()
    names = name_blob(tags)
    ref = _tag(tags, "ref")

    if highway in {"motorway", "motorway_link"} or network.startswith("jp:e") or "expressway" in network:
        return "motorway"

    motorroad = (_tag(tags, "motorroad") or "").lower()
    expressway = (_tag(tags, "expressway") or "").lower()
    if motorroad == "yes" or expressway == "yes":
        return "motorroad"

    if "林道" in names or highway == "track":
        return "forest"
    if highway == "path" and (
        "林道" in names or _tag(tags, "motorcycle") in {"yes", "designated"}
    ):
        return "forest"

    if "jp:national" in network or network == "national" or "国道" in names:
        return "national"

    if (
        "jp:prefectural" in network
        or "prefectural" in network
        or "府道" in names
        or "県道" in names
        or "都道" in names
        or "主要地方道" in names
    ):
        if "主要地方道" in names:
            return "prefectural_major"
        return "prefectural"

    # Heuristic: trunk without local name is often national in JP
    if highway in {"trunk", "trunk_link", "primary", "primary_link"}:
        nref = normalize_ref(ref) or ref_from_name(names)
        if nref and re.match(r"^\d{1,3}$", nref):
            if "国道" in names or (not preferred_name(tags) and highway.startswith("trunk")):
                return "national"

    if highway in {
        "residential",
        "living_street",
        "unclassified",
        "tertiary",
        "tertiary_link",
        "secondary",
        "secondary_link",
        "primary",
        "primary_link",
        "trunk",
        "trunk_link",
        "road",
        "service",
    }:
        return "municipal"

    return "other"


def logical_key(tags: Mapping[str, str], road_type: RoadType) -> Tuple[str, str]:
    keys = logical_keys(tags, road_type)
    return keys[0]


def logical_keys(tags: Mapping[str, str], road_type: RoadType) -> List[Tuple[str, str]]:
    """
    One or more grouping keys. Concurrent OSM refs (e.g. 168;425) produce
    multiple keys so shared geometry belongs to each logical road.
    """
    name = preferred_name(tags)
    names = name_blob(tags)
    refs = refs_from_tags(tags)
    keys: List[Tuple[str, str]] = []

    if road_type in {"motorway", "motorroad"}:
        for ref in refs:
            keys.append(("ref", f"{road_type}:{ref}"))
        if not keys and name:
            keys.append(("name", f"{road_type}:{_slug(name)}"))
    elif road_type == "national":
        for ref in refs:
            keys.append(("ref", f"national:{ref}"))
        if not keys and name:
            keys.append(("name", f"national:{_slug(name)}"))
    elif road_type in {"prefectural", "prefectural_major"}:
        for ref in refs:
            if name:
                keys.append(("ref", f"{road_type}:{ref}:{_slug(name)}"))
            else:
                keys.append(("ref", f"{road_type}:{ref}"))
        if not keys and name:
            keys.append(("name", f"{road_type}:{_slug(name)}"))
    elif road_type in {"rindo", "forest"}:
        if name:
            keys.append(("name", f"{road_type}:{_slug(name)}"))
    elif road_type in {"local", "municipal", "other"}:
        # Shared national refs on local-named ways still feed national roads via
        # duplicate emit when classify is national; here keep local identity.
        for ref in refs:
            keys.append(("ref", f"{road_type}:{ref}"))
        if name and not keys:
            keys.append(("name", f"{road_type}:{_slug(name)}"))
        elif name and keys:
            # Prefer name-based local identity when the only refs are shared
            # numeric national numbers — avoid bogus local:425 clutter.
            if all(re.match(r"^\d{1,3}$", r) for r in refs) and "国道" not in names:
                keys = [("name", f"{road_type}:{_slug(name)}")]

    if not keys and name:
        keys.append(("name", f"{road_type}:{_slug(name)}"))
    if not keys:
        keys.append(("way", ""))
    # unique preserve order
    seen = set()
    uniq: List[Tuple[str, str]] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq


def expand_memberships(tags: Mapping[str, str], road_type: RoadType) -> List[Tuple[RoadType, str, str, Optional[str]]]:
    """
    Return list of (type, key_kind, key_value, ref_for_entry) for catalog buckets.
    A way with ref=168;425 and national classification joins national:168 and national:425.
    Also: if way is classified national/local but carries extra numeric refs that look
    like concurrent nationals on trunk/primary, attach those national keys too.
    """
    memberships: List[Tuple[RoadType, str, str, Optional[str]]] = []
    base_keys = logical_keys(tags, road_type)
    refs = refs_from_tags(tags)
    primary_ref = refs[0] if refs else None

    for kind, value in base_keys:
        ref_for = None
        if kind == "ref" and ":" in value:
            # national:425 or prefectural:12:slug
            parts = value.split(":")
            if len(parts) >= 2 and parts[0] in {"national", "motorway", "local", "other"}:
                ref_for = parts[1]
            elif parts[0] == "prefectural":
                ref_for = parts[1]
        memberships.append((road_type, kind, value if value else f"way", ref_for or primary_ref))

    highway = _tag(tags, "highway") or ""
    names = name_blob(tags)
    # Concurrent national numbers on the same asphalt
    if highway in {
        "trunk", "trunk_link", "primary", "primary_link",
        "secondary", "secondary_link", "tertiary", "tertiary_link",
    }:
        for ref in refs:
            if not re.match(r"^\d{1,3}$", ref):
                continue
            key = ("ref", f"national:{ref}")
            # Always attach numeric refs on major roads to national:ref when
            # the way is national OR shares with an explicit 国道 / multi-ref.
            if road_type == "national" or "国道" in names or len(refs) > 1:
                tup = ("national", key[0], key[1], ref)
                if tup not in memberships and (key[0], key[1]) not in {(m[1], m[2]) for m in memberships}:
                    memberships.append(tup)

    # Deduplicate by (type, key_value)
    seen = set()
    out: List[Tuple[RoadType, str, str, Optional[str]]] = []
    for m in memberships:
        sig = (m[0], m[2])
        if sig in seen:
            continue
        seen.add(sig)
        out.append(m)
    return out


def has_catalog_identity(tags: Mapping[str, str], road_type: RoadType) -> bool:
    """Nombre y número son opcionales. No se usa para dejar vías fuera."""
    del tags, road_type
    return True


def make_road_id(region_id: str, key_kind: str, key_value: str, way_ids: list) -> str:
    if key_kind in {"ref", "name"} and key_value:
        slug = key_value
    else:
        sample = "-".join(str(w) for w in sorted(way_ids)[:3])
        slug = f"osm-{sample}"
    slug = _slug(slug)
    return f"{region_id}:{slug}"


def _slug(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).strip().lower()
    text = text.replace(" ", "-")
    text = re.sub(r"[^0-9a-zA-Zぁ-んァ-ン一-龥\-_.:]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text[:120] or "unnamed"
