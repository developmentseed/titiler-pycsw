"""titiler.pycsw dependencies."""

import json
from dataclasses import dataclass, field
from typing import Annotated, Any, Dict, List, Optional

from fastapi import HTTPException, Query
from starlette.requests import Request

from titiler.core.dependencies import DefaultDependency
from titiler.pycsw.client import DEFAULT_FILTER_LANG, PyCSWSTACClient
from titiler.pycsw.settings import PyCSWSettings

pycsw_settings = PyCSWSettings()


def split_csv(value: Optional[str]) -> List[str]:
    """A comma-separated query value as a list, without blanks."""
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def parse_cql2(value: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode a CQL2-JSON filter, or reject it with a 400."""
    if not value:
        return None

    try:
        return json.loads(value)
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=400,
            detail=f"`filter` must be valid CQL2-JSON: {e}",
        ) from e


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
    """Assemble the non-spatial pycsw STAC search parameters.

    Every `search()` parameter defaults to None and the serialisers skip falsy
    values, so an absent parameter is simply left out of the dict.
    """
    query = {
        "collections": split_csv(collections),
        "ids": split_csv(ids),
        "datetime": datetime,
        "sortby": sortby,
        "filter": parse_cql2(filter),
        "filter_lang": filter_lang if filter else None,
    }

    return {key: value for key, value in query.items() if value}


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
