"""titiler.pycsw dependencies."""

import json
from dataclasses import dataclass, field
from typing import Annotated, Any, Dict, Optional

from fastapi import HTTPException, Query
from starlette.requests import Request

from titiler.core.dependencies import DefaultDependency
from titiler.pycsw.client import DEFAULT_FILTER_LANG, PyCSWSTACClient
from titiler.pycsw.settings import PyCSWSettings

pycsw_settings = PyCSWSettings()


def PyCSWQueryParams(
    collections: Annotated[
        Optional[str],
        Query(description="Comma-separated list of STAC collection ids to search."),
    ] = None,
    ids: Annotated[
        Optional[str],
        Query(description="Comma-separated list of STAC item ids to restrict to."),
    ] = None,
    datetime: Annotated[
        Optional[str],
        Query(
            description=(
                "RFC3339 datetime or interval. Open intervals use `..`, e.g. "
                "`2020-01-01T00:00:00Z/..`."
            ),
        ),
    ] = None,
    filter: Annotated[
        Optional[str],
        Query(
            description=(
                "CQL2-JSON filter, JSON-encoded. Composes with `collections`, "
                "`datetime`, `ids` and the bbox. Properties are pycsw field names, "
                "not STAC ones — `cloudcover`, not `eo:cloud_cover`; an unknown "
                "name is rejected with the list that works."
            ),
        ),
    ] = None,
    filter_lang: Annotated[
        str,
        Query(
            alias="filter-lang",
            description="Filter language for `filter` (currently `cql2-json`).",
        ),
    ] = DEFAULT_FILTER_LANG,
    sortby: Annotated[
        Optional[str],
        Query(
            description=(
                "Sort key, e.g. `-datetime` for newest first. Decides which item "
                "wins for overlapping pixels. Empty lets pycsw choose."
            ),
        ),
    ] = pycsw_settings.default_sortby,
) -> Dict[str, Any]:
    """Assemble the non-spatial pycsw STAC search parameters."""
    query: Dict[str, Any] = {}

    if collections:
        query["collections"] = [c.strip() for c in collections.split(",")]
    if ids:
        query["ids"] = [i.strip() for i in ids.split(",")]
    if datetime:
        query["datetime"] = datetime
    if sortby:
        query["sortby"] = sortby
    if filter:
        try:
            query["filter"] = json.loads(filter)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=400,
                detail=f"`filter` must be valid CQL2-JSON: {e}",
            ) from e
        query["filter_lang"] = filter_lang

    return query


@dataclass(init=False)
class BackendParams(DefaultDependency):
    """Inject the shared pycsw STAC client (hidden from the OpenAPI schema)."""

    client: PyCSWSTACClient = field(init=False)

    def __init__(self, request: Request):
        """Pull the shared client off app state."""
        self.client = request.app.state.pycsw_client


@dataclass
class PyCSWSearchOptions(DefaultDependency):
    """Control how catalogue items feed a single mosaic image."""

    limit: Annotated[
        Optional[int],
        Query(
            ge=1,
            le=10_000,
            description=(
                "Max number of items to mosaic for this request. pycsw caps "
                "records per response, so reaching it may take several `rel=next` "
                "hops, bounded by `TITILER_PYCSW_MAX_PAGES`."
            ),
        ),
    ] = pycsw_settings.default_limit

    alt_crs: Annotated[
        Optional[str],
        Query(
            description=(
                "CRS to run a second item-search in, e.g. `EPSG:2100`, reaching "
                "items whose bbox is not lon/lat. Costs one extra search per "
                "request. Defaults to `TITILER_PYCSW_ALT_SEARCH_CRS`."
            ),
        ),
    ] = pycsw_settings.alt_search_crs

    reverse: Annotated[
        bool,
        Query(
            description=(
                "Mosaic in reverse order, so the last item wins for overlapping "
                "pixels instead of the first."
            ),
        ),
    ] = False
