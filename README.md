# RoadScout-builder

Repo **público** solo para generar paquetes de región con GitHub Actions (minutos gratis).

La app iOS sigue en el repo privado `rosteoman/RoadScout`.

## Uso

Actions → **Build region packages** → Run workflow

- `country`: smoke | spain | japan | all
- `regions`: opcional, CSV (`kansai,chubu,kanto`)
- `force`: reconstruir
- `publish`: subir al release público `rosteoman/RoadScout-regions` (tag `regiones`)

Secret necesario para publish: `REGIONS_RELEASE_TOKEN` (PAT con escritura en `RoadScout-regions`).

## Qué falta en Japón

Tras el cupo de Actions privado: construir `kansai`, `chubu`, `kanto` y publicar.
