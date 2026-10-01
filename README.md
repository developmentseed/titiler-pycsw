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
| `ALT_SEARCH_CRS` | unset | CRS for a second search; see below |
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

## Catalogue quirks it handles

- **Records per response are capped** below the requested `limit`, so searches
  follow `rel=next` up to `MAX_PAGES`.
- **Assets published as server paths** (`/data/…tif`) are resolved against the
  catalogue origin; see `ASSET_BASE_URL`.
- **Items whose `bbox` is in a projected CRS** are invisible to a lon/lat
  search — pycsw indexes the raw numbers and offers no `bbox-crs`. Set
  `ALT_SEARCH_CRS` (or `alt_crs=` per request) to run a second search in that
  CRS. The rasters carry their own CRS and rio-tiler reprojects them normally;
  only discovery needed the help.
- **Items missing the requested asset** drop out of the mosaic rather than
  failing the tile.

Reading a whole viewport multiplies asset reads: rio-tiler reads a chunk of
`MOSAIC_CONCURRENCY` assets before checking whether the tile is already full,
so the default of 10 reads about ten to use one. Lower it (4 works well) when
the asset host is slow. GDAL's `CPL_VSIL_CURL_CACHE_SIZE` defaults to 16MB,
smaller than a single Sentinel-2 band; raising it keeps chunks alive between
requests.

## Known gaps

- **Non-raster assets are offered as tileable.** `thumbnail`, `wmts` and
  `tiles` are listed alongside real bands and fail if selected.
- **A collection can mix radiometries.** One catalogue publishes both `uint16`
  DN and `float32` reflectance items in a single collection; one `rescale`
  cannot serve both, and the float files declare a `nodata` they do not use, so
  their fill pixels arrive valid and paint over the scenes beneath.
- **Null `datetime` defeats the default sort**, making it arbitrary which item
  wins for overlapping pixels.
- **`/healthz` can report a false positive** — it only checks for a status
  below 500, so any service on the configured host passes.

## Development

Dependencies are locked in `uv.lock`; the `dev` group carries the test and lint
tooling.

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```
