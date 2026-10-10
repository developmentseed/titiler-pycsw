# titiler-pycsw

Connect a [pycsw](https://pycsw.org) STAC API to
[TiTiler](https://github.com/developmentseed/titiler) for dynamic tiling.

Every tile request issues a bbox item-search against the catalogue and mosaics
the assets that come back. There is no persisted MosaicJSON, so the map always
reflects the catalogue's current contents.

## Quickstart

```bash
pip install -e ".[uvicorn]"

TITILER_PYCSW_STAC_API_URL="https://host/pycsw/stac" \
uvicorn titiler.pycsw.main:app --port 8081
```

`/docs` serves the API reference.

A local pycsw to load your own records into:

```bash
docker compose up --build          # pycsw on :8000, tiler on :8081
```

It starts with an empty repository; `stack/` has a sample collection and item
to POST into the transactional endpoints. The sample item's asset href is a
placeholder — point it at a real COG before expecting pixels.

## Endpoints

```
/mosaic/tiles/{tileMatrixSetId}/{z}/{x}/{y}[@{scale}x][.{format}]
/mosaic/{tileMatrixSetId}/tilejson.json
/mosaic/point/{lon},{lat}
/mosaic/bbox/{minx},{miny},{maxx},{maxy}[/{width}x{height}].{format}
/mosaic/feature[/{width}x{height}][.{format}]        (POST, GeoJSON body)
/healthz
```

Search is controlled by `collections`, `ids`, `datetime`, `sortby`, `filter`,
`limit` and `alt_crs`; rendering by the usual TiTiler parameters (`assets`,
`rescale`, `colormap_name`, `expression`, …).

## Configuration

All settings are environment variables prefixed `TITILER_PYCSW_`.

| Variable | Default | Notes |
|---|---|---|
| `STAC_API_URL` | `http://localhost:8000/stac` | Catalogue to search |
| `DEFAULT_LIMIT` | `100` | Items per mosaic request |
| `DEFAULT_SORTBY` | `-datetime` | Decides which item wins overlapping pixels |
| `MAX_PAGES` | `10` | `rel=next` hops per search; each is a round trip |
| `ASSET_BASE_URL` | catalogue origin | Base for assets published as server paths |
| `ALT_SEARCH_CRS` | unset | CRS for a second bbox search, for items whose `bbox` is projected |
| `REQUEST_TIMEOUT` | `30.0` | Seconds |
| `CACHE_TTL` / `CACHE_MAXSIZE` | `300` / `512` | Search-response cache |
| `RETRY` / `DELAY` | `3` / `0.0` | Retries on transport and 5xx errors |

## Filtering

pycsw filters on **its own catalogue field names, not STAC property names**.
`GET /queryables` lists them but under-reports, so an unknown name is rejected
with the full set that works:

| STAC | pycsw |
|---|---|
| `collection` | `collections` |
| `id` | `identifier` |
| `eo:cloud_cover` | `cloudcover` |
| `instruments` | `instrument` |
| `view:off_nadir` | `off_nadir` |

```
?filter={"op":"<","args":[{"property":"cloudcover"},20]}
```

A filter composes with `bbox`, `collections`, `datetime` and `ids` rather than
replacing them.
