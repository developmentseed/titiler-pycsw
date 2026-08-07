"""titiler.pycsw STAC API client."""

from typing import Any, Dict, List, Optional

import httpx
from rio_tiler.types import BBox

# how much of an error response body to keep in the raised exception
ERROR_BODY_CHARS = 500


class PyCSWSTACError(Exception):
    """pycsw STAC API returned an error response."""


class PyCSWSTACServerError(PyCSWSTACError):
    """pycsw STAC API returned a 5xx response (retryable)."""


PYCSW_STATUS_CODES = {
    PyCSWSTACError: 400,
    PyCSWSTACServerError: 502,
}


class PyCSWSTACClient:
    """Synchronous STAC item-search client for pycsw."""

    def __init__(
        self,
        url: str,
        client: Optional[httpx.Client] = None,
        timeout: float = 30.0,
    ) -> None:
        """Initialize the client.

        Args:
            url: base URL of the pycsw STAC API, e.g. `https://host/stac`.
            client: pre-configured `httpx.Client` to share a connection pool with.
            timeout: request timeout in seconds, used when `client` is not given.

        """
        self.url = url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    def close(self) -> None:
        """Close the underlying HTTP client if we own it."""
        if self._owns_client:
            self._client.close()

    def search(
        self,
        bbox: Optional[BBox] = None,
        datetime: Optional[str] = None,
        collections: Optional[List[str]] = None,
        ids: Optional[List[str]] = None,
        filter: Optional[Dict[str, Any]] = None,
        filter_lang: str = "cql2-json",
        sortby: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Run a STAC item search and return Item dicts.

        CQL2-JSON travels in the request body, so a `filter` switches the search
        to `POST /search`; everything else goes through `GET /search`.
        """
        if filter is not None:
            body = self._post_body(
                bbox, datetime, collections, ids, filter, filter_lang, sortby, limit
            )
            resp = self._client.post(f"{self.url}/search", json=body)
        else:
            params = self._get_params(bbox, datetime, collections, ids, sortby, limit)
            resp = self._client.get(f"{self.url}/search", params=params)

        if resp.status_code >= 500:
            raise PyCSWSTACServerError(
                f"pycsw STAC search failed ({resp.status_code}): {resp.text[:ERROR_BODY_CHARS]}"
            )

        if resp.status_code >= 400:
            raise PyCSWSTACError(
                f"pycsw STAC search failed ({resp.status_code}): {resp.text[:ERROR_BODY_CHARS]}"
            )

        return resp.json().get("features", [])

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
            body["bbox"] = [round(c, 8) for c in bbox]
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
            params["bbox"] = ",".join(str(round(c, 8)) for c in bbox)
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
