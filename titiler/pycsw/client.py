"""titiler.pycsw STAC API client."""

import logging
from typing import Any, Dict, List, NamedTuple, Optional, Set

import httpx
from cachetools import TTLCache, cached
from cachetools.keys import hashkey
from rio_tiler.types import BBox

from titiler.pycsw.settings import CacheSettings, PyCSWSettings

logger = logging.getLogger(__name__)

cache_config = CacheSettings()
pycsw_config = PyCSWSettings()

# how much of an error response body to keep in the raised exception
ERROR_BODY_CHARS = 500

# decimal places a bbox is pinned to on the wire and in cache keys, ~1mm
BBOX_PRECISION = 8

# the only filter language pycsw accepts
DEFAULT_FILTER_LANG = "cql2-json"

# pycsw's query_mappings, which `GET /queryables` under-reports
PYCSW_CORE_QUERYABLES = frozenset(
    {
        "anytext",
        "bbox",
        "cloudcover",
        "collections",
        "date",
        "date_creation",
        "date_modified",
        "datetime",
        "description",
        "distancevalue",
        "edition",
        "geometry",
        "identifier",
        "instrument",
        "keywords",
        "off_nadir",
        "otherconstraints",
        "parentidentifier",
        "platform",
        "sensortype",
        "time_begin",
        "time_end",
        "title",
        "type",
        "typename",
        "updated",
    }
)

# the STAC spellings people reach for first, and what pycsw actually calls them
STAC_TO_PYCSW_HINTS = {
    "collection": "collections",
    "id": "identifier",
    "ids": "identifier",
    "eo:cloud_cover": "cloudcover",
    "instruments": "instrument",
    "view:off_nadir": "off_nadir",
    "start_datetime": "time_begin",
    "end_datetime": "time_end",
    "created": "date_creation",
    "updated_at": "updated",
}


class PageRequest(NamedTuple):
    """One HTTP request in a paginated search."""

    method: str
    url: str
    body: Optional[Dict[str, Any]] = None
    params: Optional[Dict[str, Any]] = None


class PyCSWSTACError(Exception):
    """pycsw STAC API returned an error response."""


class PyCSWSTACServerError(PyCSWSTACError):
    """pycsw STAC API returned a 5xx response (retryable)."""


class PyCSWInvalidFilterError(PyCSWSTACError):
    """`filter` referenced a property the catalogue cannot filter on."""


PYCSW_STATUS_CODES = {
    PyCSWSTACError: 400,
    PyCSWSTACServerError: 502,
    PyCSWInvalidFilterError: 400,
}


def cql2_property_names(node: Any) -> Set[str]:
    """Collect every `{"property": ...}` name in a CQL2-JSON expression."""
    names: Set[str] = set()

    if isinstance(node, dict):
        prop = node.get("property")
        if isinstance(prop, str):
            names.add(prop)

        for value in node.values():
            names |= cql2_property_names(value)

    elif isinstance(node, list):
        for value in node:
            names |= cql2_property_names(value)

    return names


class PyCSWSTACClient:
    """Synchronous STAC item-search client for pycsw."""

    def __init__(
        self,
        url: str,
        client: Optional[httpx.Client] = None,
        timeout: float = pycsw_config.request_timeout,
        max_pages: int = pycsw_config.max_pages,
    ) -> None:
        """Initialize the client.

        Args:
            url: base URL of the pycsw STAC API, e.g. `https://host/stac`.
            client: pre-configured `httpx.Client` to share a connection pool with.
            timeout: request timeout in seconds, used when `client` is not given.
            max_pages: most `rel=next` hops to follow before giving up on `limit`.

        """
        self.url = url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self.max_pages = max_pages

    def close(self) -> None:
        """Close the underlying HTTP client if we own it."""
        if self._owns_client:
            self._client.close()

    @cached(  # type: ignore
        TTLCache(maxsize=cache_config.maxsize, ttl=cache_config.ttl),
        key=lambda self: hashkey(self.url),
    )
    def queryables(self) -> Set[str]:
        """Property names `GET /queryables` advertises as filterable."""
        resp = self._client.get(f"{self.url}/queryables")

        if resp.status_code >= 400:
            raise PyCSWSTACError(
                f"pycsw queryables request failed ({resp.status_code}): "
                f"{resp.text[:ERROR_BODY_CHARS]}"
            )

        return set(resp.json().get("properties", {}))

    def known_queryables(self) -> Set[str]:
        """Advertised property names unioned with `PYCSW_CORE_QUERYABLES`."""
        try:
            advertised = self.queryables()
        except (PyCSWSTACError, httpx.HTTPError, ValueError):
            advertised = set()

        return set(PYCSW_CORE_QUERYABLES) | advertised

    def validate_filter(self, filter: Dict[str, Any]) -> None:
        """Reject `filter` properties the catalogue cannot filter on."""
        requested = cql2_property_names(filter)
        if not requested:
            return

        known = self.known_queryables()
        unknown = sorted(requested - known)
        if not unknown:
            return

        hints = [
            f"`{name}` -> `{STAC_TO_PYCSW_HINTS[name]}`"
            for name in unknown
            if name in STAC_TO_PYCSW_HINTS
        ]
        message = (
            f"Unknown queryable(s) in `filter`: {', '.join(unknown)}. "
            "pycsw filters on catalogue field names, not STAC property names."
        )
        if hints:
            message += f" Did you mean {', '.join(hints)}?"
        message += f" Available: {', '.join(sorted(known))}."

        raise PyCSWInvalidFilterError(message)

    def search(
        self,
        bbox: Optional[BBox] = None,
        datetime: Optional[str] = None,
        collections: Optional[List[str]] = None,
        ids: Optional[List[str]] = None,
        filter: Optional[Dict[str, Any]] = None,
        filter_lang: str = DEFAULT_FILTER_LANG,
        sortby: Optional[str] = None,
        limit: int = pycsw_config.default_limit,
    ) -> List[Dict[str, Any]]:
        """Run a STAC item search, following `rel=next` until `limit` is met."""
        if filter is not None:
            self.validate_filter(filter)
            request = PageRequest(
                "POST",
                f"{self.url}/search",
                body=self._post_body(
                    bbox, datetime, collections, ids, filter, filter_lang, sortby, limit
                ),
            )
        else:
            request = PageRequest(
                "GET",
                f"{self.url}/search",
                params=self._get_params(
                    bbox, datetime, collections, ids, sortby, limit
                ),
            )

        features: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        seen_hrefs: Set[str] = set()
        pages = 0

        while True:
            payload = self._fetch(request)
            page = payload.get("features", [])
            pages += 1

            for item in page:
                # offset paging can repeat an item when server ordering is unstable
                item_id = item.get("id")
                if item_id is not None:
                    if item_id in seen_ids:
                        continue
                    seen_ids.add(item_id)
                features.append(item)

            if len(features) >= limit or not page:
                break

            next_request = self._next_request(payload, seen_hrefs)
            if next_request is None:
                break

            if pages >= self.max_pages:
                logger.warning(
                    "pycsw search stopped after max_pages=%d with %d of %s matched "
                    "items; raise TITILER_PYCSW_MAX_PAGES or lower `limit`",
                    self.max_pages,
                    len(features),
                    payload.get("numberMatched", "?"),
                )
                break

            request = next_request

        return features[:limit]

    def _fetch(self, request: PageRequest) -> Dict[str, Any]:
        """Issue one page request and return the decoded FeatureCollection."""
        if request.method == "POST":
            resp = self._client.post(
                request.url, params=request.params, json=request.body
            )
        else:
            resp = self._client.get(request.url, params=request.params)

        if resp.status_code >= 500:
            raise PyCSWSTACServerError(
                f"pycsw STAC search failed ({resp.status_code}): {resp.text[:ERROR_BODY_CHARS]}"
            )

        if resp.status_code >= 400:
            raise PyCSWSTACError(
                f"pycsw STAC search failed ({resp.status_code}): {resp.text[:ERROR_BODY_CHARS]}"
            )

        return resp.json()

    @staticmethod
    def _next_request(
        payload: Dict[str, Any], seen_hrefs: Set[str]
    ) -> Optional[PageRequest]:
        """Build the follow-up request from a `rel=next` link, if there is one."""
        for link in payload.get("links", []):
            if link.get("rel") != "next":
                continue

            href = link.get("href")
            # a server that hands back a link it already gave us would loop forever
            if not href or href in seen_hrefs:
                return None

            seen_hrefs.add(href)
            method = str(link.get("method") or "GET").upper()

            return PageRequest(method, href, body=link.get("body"))

        return None

    @staticmethod
    def _post_body(
        bbox: Optional[BBox],
        datetime: Optional[str],
        collections: Optional[List[str]],
        ids: Optional[List[str]],
        filter: Dict[str, Any],
        filter_lang: str,
        sortby: Optional[str],
        limit: int,
    ) -> Dict[str, Any]:
        """Build the JSON body for `POST /search`."""
        body: Dict[str, Any] = {
            "limit": limit,
            "filter": filter,
            "filter-lang": filter_lang,
        }
        if bbox is not None:
            body["bbox"] = [round(c, BBOX_PRECISION) for c in bbox]
        if datetime:
            body["datetime"] = datetime
        if collections:
            body["collections"] = collections
        if ids:
            body["ids"] = ids
        if sortby:
            body["sortby"] = [
                {
                    "field": sortby.lstrip("+-"),
                    "direction": "desc" if sortby.startswith("-") else "asc",
                }
            ]

        return body

    @staticmethod
    def _get_params(
        bbox: Optional[BBox],
        datetime: Optional[str],
        collections: Optional[List[str]],
        ids: Optional[List[str]],
        sortby: Optional[str],
        limit: int,
    ) -> Dict[str, Any]:
        """Build the query string for `GET /search`."""
        params: Dict[str, Any] = {"limit": limit}
        if bbox is not None:
            params["bbox"] = ",".join(str(round(c, BBOX_PRECISION)) for c in bbox)
        if datetime:
            params["datetime"] = datetime
        if collections:
            params["collections"] = ",".join(collections)
        if ids:
            params["ids"] = ",".join(ids)
        if sortby:
            field = sortby.lstrip("+-")
            params["sortby"] = f"-{field}" if sortby.startswith("-") else field

        return params
