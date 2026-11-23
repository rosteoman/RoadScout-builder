"""Inclusion / exclusion rules for catalog ways (§4 Documento de verdad)."""

from __future__ import annotations

from typing import Mapping, Optional

# Asphalt / general motor roads
# Calles: no son una ruta elegible. Solo entran si pertenecen a una relación de carretera.
STREET_HIGHWAYS = {
    "residential",
    "living_street",
    "unclassified",
    "service",
}

INCLUDE_HIGHWAYS = {
    "motorway",
    "motorway_link",
    "trunk",
    "trunk_link",
    "primary",
    "primary_link",
    "secondary",
    "secondary_link",
    "tertiary",
    "tertiary_link",
    "road",
}

# Potential rindo / trail — only if motorcycle-circulable
CANDIDATE_TRAIL_HIGHWAYS = {
    "track",
    "path",
    "service",
}

EXCLUDE_HIGHWAYS = {
    "cycleway",
    "footway",
    "pedestrian",
    "steps",
    "bridleway",
    "corridor",
    "elevator",
    "platform",
    "bus_guideway",
    "raceway",
}

EXCLUDE_LIFECYCLE = {
    "proposed",
    "construction",
    "abandoned",
    "disused",
    "razed",
    "dismantled",
}


def _tag(tags: Mapping[str, str], key: str) -> Optional[str]:
    value = tags.get(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _truthy_motor_access(tags: Mapping[str, str]) -> bool:
    for key in ("motorcycle", "motor_vehicle", "vehicle", "access"):
        val = _tag(tags, key)
        if val is None:
            continue
        if val in {"no", "private", "agricultural", "forestry", "delivery", "customer"}:
            return False
        if val in {"yes", "designated", "permissive", "destination", "permit"}:
            return True
    return False


def _explicit_motor_denied(tags: Mapping[str, str]) -> bool:
    for key in ("motorcycle", "motor_vehicle", "vehicle", "access"):
        val = _tag(tags, key)
        if val in {"no", "private"}:
            # access=private alone is not enough to drop public roads; only hard no
            if key == "access" and val == "private":
                continue
            if val == "no":
                return True
    return False


def _looks_like_rindo(tags: Mapping[str, str]) -> bool:
    name_blob = " ".join(
        filter(
            None,
            [
                _tag(tags, "name"),
                _tag(tags, "name:ja"),
                _tag(tags, "official_name"),
                _tag(tags, "alt_name"),
            ],
        )
    )
    if "林道" in name_blob:
        return True
    tracktype = _tag(tags, "tracktype")
    if tracktype in {"grade1", "grade2", "grade3"}:
        return True
    return False


# Calzada que un coche o una moto pueden recorrer por defecto.
MOTOR_HIGHWAYS = INCLUDE_HIGHWAYS | {
    "track",
    "turning_circle",
    "turning_loop",
}

# Servicio que no es una vía de viaje: acceso a una finca, pasillo de parking, emergencia.
SERVICE_NOT_A_ROAD = {
    "driveway",
    "parking_aisle",
    "parking",
    "emergency_access",
    "drive-through",
}

# Ni coche ni moto. Una excepción explícita de acceso puede rescatar senda o carril bici.
NON_MOTOR_HIGHWAYS = {
    "footway",
    "pedestrian",
    "steps",
    "path",
    "cycleway",
    "bridleway",
}

# Instalaciones, obras y vías que no se recorren en coche ni en moto.
NOT_A_ROAD = EXCLUDE_LIFECYCLE | {
    "platform",
    "elevator",
    "corridor",
    "via_ferrata",
    "bus_guideway",
    "busway",
    "planned",
    "escape",
    "raceway",
    "rest_area",
    "services",
    "crossing",
    "bus_stop",
    "traffic_signals",
}

_GRANT = {
    "yes",
    "designated",
    "permissive",
    "destination",
    "customers",
    "delivery",
    "permit",
    "agricultural",
    "forestry",
    "public",
    "official",
    "private",
}
_DENY = {"no"}


def _value(tags: Mapping[str, str], key: str) -> Optional[str]:
    value = _tag(tags, key)
    return value.lower() if value else None


def _decided(tags: Mapping[str, str], keys: tuple[str, ...]) -> Optional[bool]:
    """Primera etiqueta de acceso que concede o niega. None si ninguna habla."""
    for key in keys:
        value = _value(tags, key)
        if value in _DENY:
            return False
        if value in _GRANT:
            return True
    return None


def _car_allowed(tags: Mapping[str, str]) -> Optional[bool]:
    return _decided(tags, ("motorcar", "motor_vehicle", "vehicle", "access"))


def _moto_allowed(tags: Mapping[str, str]) -> Optional[bool]:
    """Moto desde 49 cc: moped y motorcycle. moped=yes rescata un motorcycle=no."""
    if _value(tags, "moped") in _GRANT:
        return True
    motorcycle = _value(tags, "motorcycle")
    if motorcycle in _GRANT:
        return True
    if motorcycle in _DENY:
        return False
    return _decided(tags, ("motor_vehicle", "vehicle", "access"))


def _blocked(tags: Mapping[str, str], highway: str) -> bool:
    if highway in NOT_A_ROAD:
        return True
    if _value(tags, "area") == "yes":
        return True
    if _tag(tags, "aeroway"):
        return True
    if _value(tags, "indoor") == "yes":
        return True
    if _value(tags, "railway") == "platform" or _value(tags, "public_transport") == "platform":
        return True
    for key in ("abandoned", "disused", "construction", "razed", "demolished"):
        if _value(tags, key) == "yes":
            return True
    return False


def is_included_way(tags: Mapping[str, str]) -> bool:
    """Vía que se puede recorrer en coche o en moto de 49 cc en adelante.

    Entran la calzada, la pista forestal, el rindo y el offroad.
    Quedan fuera aceras, sendas a pie, carriles solo de bici, andenes, aeropuertos,
    obras y lo cerrado a coche y a moto. La longitud no decide.
    """
    highway = _tag(tags, "highway")
    if highway is None or _blocked(tags, highway):
        return False

    if highway in STREET_HIGHWAYS:
        return False

    if highway == "service" and _value(tags, "service") in SERVICE_NOT_A_ROAD:
        return False

    # Pista, rindo o tracktype: se recorre en vehículo aunque OSM la marque como senda.
    if (
        highway == "track"
        or _looks_like_rindo(tags)
        or _tag(tags, "tracktype")
        or _value(tags, "4wd_only") == "yes"
    ):
        car = _car_allowed(tags)
        moto = _moto_allowed(tags)
        return not (car is False and moto is False)

    if highway in NON_MOTOR_HIGHWAYS:
        # Por defecto no entra un coche ni una moto. Solo un permiso explícito.
        return _decided(tags, ("motorcar", "motorcycle", "moped", "motor_vehicle")) is True

    if highway in MOTOR_HIGHWAYS or highway == "service":
        car = _car_allowed(tags)
        moto = _moto_allowed(tags)
        if car is False and moto is False:
            return False
        return True

    return False


def is_motor_road_surface(tags: Mapping[str, str]) -> bool:
    """Misma regla que el paquete: se recorre en coche o en moto."""
    return is_included_way(tags)


def dashed_catalog_type(tags: Mapping[str, str]) -> Optional[str]:
    """Tipo de un tramo que no se puede recorrer y aun así hay que dibujar.

    Una pista o una calle cerrada a moto y coche se pinta a trazos. Una acera,
    un sendero de pie o un acceso a finca no entra.
    """
    if is_included_way(tags):
        return None
    highway = _tag(tags, "highway") or ""
    if _blocked(tags, highway):
        return None
    if highway == "service" and _value(tags, "service") in SERVICE_NOT_A_ROAD:
        return None
    if _value(tags, "footway") in {"sidewalk", "crossing"}:
        return None
    names = " ".join(
        filter(
            None,
            [
                _tag(tags, "name"),
                _tag(tags, "name:ja"),
                _tag(tags, "official_name"),
                _tag(tags, "alt_name"),
            ],
        )
    )
    if highway == "track" or "林道" in names or _tag(tags, "tracktype"):
        return "forest"
    if highway in STREET_HIGHWAYS:
        return None
    return None
