"""Unit tests for titiler.pycsw (pycsw STAC API mocked with respx)."""

import json
import logging

import httpx
import pytest
import respx

from titiler.pycsw.client import PyCSWSTACClient
from titiler.pycsw.dependencies import PyCSWQueryParams
from titiler.pycsw.reader import PyCSWSTACReader

STAC_URL = "http://pycsw.test/stac"


def _fake_image():
    """A minimal ImageData standing in for a real raster read."""
    import numpy
    from rio_tiler.models import ImageData

    return ImageData(numpy.ma.MaskedArray(numpy.zeros((1, 256, 256), dtype="uint8")))


ITEM = {
    "type": "Feature",
    "stac_version": "1.0.0",
    "id": "scene-1",
    "collection": "my-collection",
    "bbox": [0, 0, 10, 10],
    "geometry": {
        "type": "Polygon",
        "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
    },
    "properties": {"datetime": "2021-01-01T00:00:00Z"},
    "assets": {"cog": {"href": "s3://bucket/scene-1.tif"}},
}

FEATURE_COLLECTION = {"type": "FeatureCollection", "features": [ITEM]}

# a deliberately short list: pycsw under-reports its own queryables
QUERYABLES = {
    "type": "object",
    "properties": {
        "identifier": {"type": "string"},
        "title": {"type": "string"},
        "cloudcover": {},
    },
}


@pytest.fixture(autouse=True)
def _clear_queryables_cache():
    """The queryables lookup is memoised per URL and every test shares one."""
    PyCSWSTACClient.queryables.cache.clear()
    yield
    PyCSWSTACClient.queryables.cache.clear()


@respx.mock
def test_client_get_search_builds_params():
    """A spatial-only search uses GET and encodes bbox/sortby/limit."""
    route = respx.get(f"{STAC_URL}/search").mock(
        return_value=httpx.Response(200, json=FEATURE_COLLECTION)
    )

    client = PyCSWSTACClient(STAC_URL, client=httpx.Client())
    features = client.search(
        bbox=(0, 0, 10, 10),
        collections=["my-collection"],
        sortby="-datetime",
        limit=50,
    )

    assert len(features) == 1
    assert features[0]["id"] == "scene-1"

    request = route.calls.last.request
    assert request.method == "GET"
    assert request.url.params["bbox"] == "0,0,10,10"
    assert request.url.params["collections"] == "my-collection"
    assert request.url.params["sortby"] == "-datetime"
    assert request.url.params["limit"] == "50"


@respx.mock
def test_client_post_search_when_filter():
    """A CQL2 filter switches the search to POST with a JSON body."""
    respx.get(f"{STAC_URL}/queryables").mock(
        return_value=httpx.Response(200, json=QUERYABLES)
    )
    route = respx.post(f"{STAC_URL}/search").mock(
        return_value=httpx.Response(200, json=FEATURE_COLLECTION)
    )

    client = PyCSWSTACClient(STAC_URL, client=httpx.Client())
    # `cloudcover`, not the STAC spelling `eo:cloud_cover`, which pycsw rejects
    cql2 = {"op": "=", "args": [{"property": "cloudcover"}, 0]}
    features = client.search(bbox=(0, 0, 10, 10), filter=cql2, sortby="-datetime")

    assert len(features) == 1
    request = route.calls.last.request
    assert request.method == "POST"
    body = json.loads(request.content)
    assert body["filter"] == cql2
    assert body["filter-lang"] == "cql2-json"
    assert body["bbox"] == [0, 0, 10, 10]
    assert body["sortby"] == [{"field": "datetime", "direction": "desc"}]


@respx.mock
def test_backend_get_assets_returns_items():
    """The mosaic backend returns STAC item dicts from a bbox search."""
    from titiler.pycsw.backend import PyCSWBackend

    respx.get(f"{STAC_URL}/search").mock(
        return_value=httpx.Response(200, json=FEATURE_COLLECTION)
    )

    client = PyCSWSTACClient(STAC_URL, client=httpx.Client())
    with PyCSWBackend(client=client) as backend:
        # unique bbox to avoid the module-level TTLCache from other tests
        assets = backend.get_assets(
            1.111, 2.222, 3.333, 4.444, query={"collections": ["my-collection"]}
        )

    assert len(assets) == 1
    assert assets[0]["assets"]["cog"]["href"] == "s3://bucket/scene-1.tif"


@respx.mock
def test_client_get_search_normalises_ascending_sortby():
    """A leading `+` is stripped; pycsw 500s on `+datetime`."""
    route = respx.get(f"{STAC_URL}/search").mock(
        return_value=httpx.Response(200, json=FEATURE_COLLECTION)
    )

    client = PyCSWSTACClient(STAC_URL, client=httpx.Client())
    client.search(bbox=(0, 0, 10, 10), sortby="+datetime")
    assert route.calls.last.request.url.params["sortby"] == "datetime"

    client.search(bbox=(0, 0, 10, 10), sortby="-datetime")
    assert route.calls.last.request.url.params["sortby"] == "-datetime"


@respx.mock
def test_backend_point_returns_item_value_pairs():
    """Point results pair unhashable STAC item dicts with their values."""
    from unittest.mock import patch

    from titiler.pycsw.backend import PyCSWBackend

    respx.get(f"{STAC_URL}/search").mock(
        return_value=httpx.Response(200, json=FEATURE_COLLECTION)
    )

    client = PyCSWSTACClient(STAC_URL, client=httpx.Client())
    with PyCSWBackend(client=client) as backend:
        with patch.object(backend, "reader") as mock_reader:
            mock_reader.return_value.__enter__.return_value.point.return_value = "pt"
            values = backend.point(5.5551, 6.6661, threads=0)

    assert len(values) == 1
    item, pts = values[0]
    assert item["id"] == "scene-1"
    assert pts == "pt"


def test_query_params_dependency_parses_inputs():
    """PyCSWQueryParams parses CSV lists and CQL2-JSON filter strings."""
    query = PyCSWQueryParams(
        collections="c1,c2",
        ids="a,b",
        datetime="2021-01-01T00:00:00Z/..",
        filter=json.dumps({"op": "=", "args": [{"property": "x"}, 1]}),
        sortby="-datetime",
    )

    assert query["collections"] == ["c1", "c2"]
    assert query["ids"] == ["a", "b"]
    assert query["datetime"] == "2021-01-01T00:00:00Z/.."
    assert query["filter"]["op"] == "="
    assert query["filter_lang"] == "cql2-json"
    assert query["sortby"] == "-datetime"


def test_query_params_defaults_to_configured_sortby():
    """No params -> only the configured default sort key."""
    assert PyCSWQueryParams() == {"sortby": "-datetime"}


def test_query_params_empty_sortby_is_empty_dict():
    """An empty sortby lets pycsw pick the order."""
    assert PyCSWQueryParams(sortby="") == {}


@respx.mock
def test_app_tilejson_and_health(monkeypatch):
    """End-to-end: app boots, tilejson renders, health checks pycsw."""
    monkeypatch.setenv("TITILER_PYCSW_STAC_API_URL", STAC_URL)
    # health check pings the STAC landing page
    respx.get(STAC_URL).mock(return_value=httpx.Response(200, json={}))

    # import after env is set so settings pick it up
    import importlib

    import titiler.pycsw.main as main_mod

    importlib.reload(main_mod)
    from starlette.testclient import TestClient

    with TestClient(main_mod.app) as client:
        r = client.get(
            "/mosaic/WebMercatorQuad/tilejson.json",
            params={"collections": "demo", "assets": "cog"},
        )
        assert r.status_code == 200, r.text
        tj = r.json()
        assert tj["minzoom"] == 0 and tj["maxzoom"] == 24
        assert "/mosaic/tiles/WebMercatorQuad/{z}/{x}/{y}" in tj["tiles"][0]
        assert "collections=demo" in tj["tiles"][0]

        h = client.get("/healthz")
        assert h.status_code == 200
        assert h.json()["pycsw_online"] is True


@respx.mock
def test_app_bbox_and_feature_routes(monkeypatch):
    """/bbox and /feature reach the backend, which returns 204 on no assets."""
    monkeypatch.setenv("TITILER_PYCSW_STAC_API_URL", STAC_URL)
    respx.get(f"{STAC_URL}/search").mock(
        return_value=httpx.Response(
            200, json={"type": "FeatureCollection", "features": []}
        )
    )

    import importlib

    import titiler.pycsw.main as main_mod

    importlib.reload(main_mod)
    from starlette.testclient import TestClient

    with TestClient(main_mod.app) as client:
        r = client.get("/mosaic/bbox/0,0,1,1.png", params={"assets": "cog"})
        assert r.status_code == 204, r.text

        r = client.post(
            "/mosaic/feature.png",
            params={"assets": "cog"},
            json={
                "type": "Feature",
                "properties": {},
                "geometry": ITEM["geometry"],
            },
        )
        assert r.status_code == 204, r.text


@respx.mock
def test_app_maps_pycsw_errors_to_status_codes(monkeypatch):
    """pycsw 4xx surfaces as 400 and 5xx as 502, not a bare 500."""
    monkeypatch.setenv("TITILER_PYCSW_STAC_API_URL", STAC_URL)

    import importlib

    import titiler.pycsw.main as main_mod

    importlib.reload(main_mod)
    from starlette.testclient import TestClient

    # bboxes are unique per assertion to sidestep the module-level TTLCache
    with TestClient(main_mod.app, raise_server_exceptions=False) as client:
        respx.get(f"{STAC_URL}/search").mock(
            return_value=httpx.Response(400, text="bad filter")
        )
        r = client.get("/mosaic/bbox/11,11,12,12.png", params={"assets": "cog"})
        assert r.status_code == 400, r.text

        respx.get(f"{STAC_URL}/search").mock(
            return_value=httpx.Response(500, text="boom")
        )
        r = client.get("/mosaic/bbox/13,13,14,14.png", params={"assets": "cog"})
        assert r.status_code == 502, r.text


def test_cql2_property_names_walks_nested_expressions():
    """Property names are collected from anywhere in the CQL2 tree."""
    from titiler.pycsw.client import cql2_property_names

    assert cql2_property_names({"op": "=", "args": [{"property": "title"}, "x"]}) == {
        "title"
    }
    nested = {
        "op": "and",
        "args": [
            {"op": "<", "args": [{"property": "cloudcover"}, 20]},
            {"op": "in", "args": [{"property": "collections"}, ["a", "b"]]},
        ],
    }
    assert cql2_property_names(nested) == {"cloudcover", "collections"}
    assert cql2_property_names({"op": "=", "args": [1, 2]}) == set()


@respx.mock
def test_queryables_union_covers_pycsw_under_reporting():
    """`collections`/`datetime` work but are not advertised, so they must pass."""
    from titiler.pycsw.client import PYCSW_CORE_QUERYABLES

    respx.get(f"{STAC_URL}/queryables").mock(
        return_value=httpx.Response(200, json=QUERYABLES)
    )
    client = PyCSWSTACClient(STAC_URL, client=httpx.Client())

    known = client.known_queryables()
    assert {"collections", "datetime", "off_nadir"} <= known
    assert PYCSW_CORE_QUERYABLES <= known
    assert "date_publication" not in known


@respx.mock
def test_validate_filter_rejects_stac_property_names():
    """A STAC spelling is rejected up front with a pointer to the pycsw name."""
    from titiler.pycsw.client import PyCSWInvalidFilterError

    respx.get(f"{STAC_URL}/queryables").mock(
        return_value=httpx.Response(200, json=QUERYABLES)
    )
    client = PyCSWSTACClient(STAC_URL, client=httpx.Client())

    with pytest.raises(PyCSWInvalidFilterError) as excinfo:
        client.search(
            bbox=(0, 0, 10, 10),
            filter={"op": "=", "args": [{"property": "eo:cloud_cover"}, 0]},
        )

    message = str(excinfo.value)
    assert "eo:cloud_cover" in message
    assert "`eo:cloud_cover` -> `cloudcover`" in message
    assert "cloudcover" in message


@respx.mock
def test_validate_filter_allows_known_queryables():
    """A valid pycsw field name reaches `POST /search` untouched."""
    respx.get(f"{STAC_URL}/queryables").mock(
        return_value=httpx.Response(200, json=QUERYABLES)
    )
    route = respx.post(f"{STAC_URL}/search").mock(
        return_value=httpx.Response(200, json=FEATURE_COLLECTION)
    )

    client = PyCSWSTACClient(STAC_URL, client=httpx.Client())
    cql2 = {"op": "<", "args": [{"property": "cloudcover"}, 20]}
    assert len(client.search(bbox=(4, 4, 5, 5), filter=cql2)) == 1
    assert json.loads(route.calls.last.request.content)["filter"] == cql2


@respx.mock
def test_validate_filter_degrades_when_queryables_unreachable():
    """An unreachable /queryables must not block an otherwise valid search."""
    respx.get(f"{STAC_URL}/queryables").mock(side_effect=httpx.ConnectError("boom"))
    route = respx.post(f"{STAC_URL}/search").mock(
        return_value=httpx.Response(200, json=FEATURE_COLLECTION)
    )

    client = PyCSWSTACClient(STAC_URL, client=httpx.Client())
    cql2 = {"op": "=", "args": [{"property": "identifier"}, "scene-1"]}
    assert len(client.search(bbox=(6, 6, 7, 7), filter=cql2)) == 1
    assert route.called


@respx.mock
def test_app_maps_invalid_filter_to_400(monkeypatch):
    """An unknown queryable surfaces as a 400 naming the valid alternatives."""
    monkeypatch.setenv("TITILER_PYCSW_STAC_API_URL", STAC_URL)
    respx.get(f"{STAC_URL}/queryables").mock(
        return_value=httpx.Response(200, json=QUERYABLES)
    )

    import importlib

    import titiler.pycsw.main as main_mod

    importlib.reload(main_mod)
    from starlette.testclient import TestClient

    with TestClient(main_mod.app, raise_server_exceptions=False) as client:
        r = client.get(
            "/mosaic/bbox/21,21,22,22.png",
            params={
                "assets": "cog",
                "filter": json.dumps(
                    {"op": "=", "args": [{"property": "collection"}, "demo"]}
                ),
            },
        )
        assert r.status_code == 400, r.text
        assert "collections" in r.json()["detail"]


def _page(ids, next_href=None, matched=None, method=None, body=None):
    """Build a FeatureCollection page, optionally carrying a `rel=next` link."""
    links = [{"rel": "self", "href": f"{STAC_URL}/search"}]
    if next_href:
        link = {"rel": "next", "href": next_href}
        if method:
            link["method"] = method
        if body is not None:
            link["body"] = body
        links.append(link)

    return {
        "type": "FeatureCollection",
        "numberMatched": matched if matched is not None else len(ids),
        "numberReturned": len(ids),
        "features": [{**ITEM, "id": i} for i in ids],
        "links": links,
    }


@respx.mock
def test_search_follows_next_until_limit():
    """A server capping records per page is paged through up to `limit`."""
    respx.get(f"{STAC_URL}/search", params={"offset": "2"}).mock(
        return_value=httpx.Response(
            200,
            json=_page(["c", "d"], next_href=f"{STAC_URL}/search?offset=4", matched=6),
        )
    )
    respx.get(f"{STAC_URL}/search", params={"offset": "4"}).mock(
        return_value=httpx.Response(200, json=_page(["e", "f"], matched=6))
    )
    respx.get(f"{STAC_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json=_page(["a", "b"], next_href=f"{STAC_URL}/search?offset=2", matched=6),
        )
    )

    client = PyCSWSTACClient(STAC_URL, client=httpx.Client())
    features = client.search(bbox=(0, 0, 10, 10), limit=6)

    assert [f["id"] for f in features] == ["a", "b", "c", "d", "e", "f"]


@respx.mock
def test_search_stops_at_limit_mid_page():
    """Pagination stops as soon as `limit` is met and truncates the last page."""
    respx.get(f"{STAC_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json=_page(
                ["a", "b", "c"], next_href=f"{STAC_URL}/search?offset=3", matched=99
            ),
        )
    )

    client = PyCSWSTACClient(STAC_URL, client=httpx.Client())
    features = client.search(bbox=(0, 0, 10, 10), limit=2)

    assert [f["id"] for f in features] == ["a", "b"]


@respx.mock
def test_search_honours_max_pages(caplog):
    """max_pages bounds tile latency and says so instead of silently truncating."""
    respx.get(f"{STAC_URL}/search", params={"offset": "1"}).mock(
        return_value=httpx.Response(
            200,
            json=_page(["b"], next_href=f"{STAC_URL}/search?offset=2", matched=500),
        )
    )
    respx.get(f"{STAC_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json=_page(["a"], next_href=f"{STAC_URL}/search?offset=1", matched=500),
        )
    )

    client = PyCSWSTACClient(STAC_URL, client=httpx.Client(), max_pages=2)
    with caplog.at_level(logging.WARNING, logger="titiler.pycsw.client"):
        features = client.search(bbox=(0, 0, 10, 10), limit=500)

    assert [f["id"] for f in features] == ["a", "b"]
    assert "max_pages=2" in caplog.text
    assert "500" in caplog.text


@respx.mock
def test_search_stops_on_repeated_next_link():
    """A server echoing the same next link must not spin forever."""
    route = respx.get(f"{STAC_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json=_page(["a"], next_href=f"{STAC_URL}/search?offset=1", matched=99),
        )
    )

    client = PyCSWSTACClient(STAC_URL, client=httpx.Client(), max_pages=50)
    features = client.search(bbox=(0, 0, 10, 10), limit=99)

    assert [f["id"] for f in features] == ["a"]
    assert route.call_count == 2


@respx.mock
def test_search_deduplicates_items_across_pages():
    """Unstable server ordering must not feed the same item to the mosaic twice."""
    respx.get(f"{STAC_URL}/search", params={"offset": "2"}).mock(
        return_value=httpx.Response(200, json=_page(["b", "c"], matched=3))
    )
    respx.get(f"{STAC_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json=_page(["a", "b"], next_href=f"{STAC_URL}/search?offset=2", matched=3),
        )
    )

    client = PyCSWSTACClient(STAC_URL, client=httpx.Client())
    features = client.search(bbox=(0, 0, 10, 10), limit=10)

    assert [f["id"] for f in features] == ["a", "b", "c"]


@respx.mock
def test_search_paginates_post_with_link_body():
    """A filtered search pages via POST, reusing the body pycsw echoes back."""
    original = {"op": "like", "args": [{"property": "identifier"}, "%"]}
    respx.get(f"{STAC_URL}/queryables").mock(
        return_value=httpx.Response(200, json=QUERYABLES)
    )
    page_two = respx.post(f"{STAC_URL}/search", params={"offset": "1"}).mock(
        return_value=httpx.Response(200, json=_page(["b"], matched=2))
    )
    respx.post(f"{STAC_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json=_page(
                ["a"],
                next_href=f"{STAC_URL}/search?offset=1",
                matched=2,
                method="POST",
                body={"limit": 1, "filter": original, "filter-lang": "cql2-json"},
            ),
        )
    )

    client = PyCSWSTACClient(STAC_URL, client=httpx.Client())
    features = client.search(bbox=(0, 0, 10, 10), filter=original, limit=2)

    assert [f["id"] for f in features] == ["a", "b"]
    assert page_two.calls.last.request.method == "POST"
    assert json.loads(page_two.calls.last.request.content)["filter"] == original


@respx.mock
def test_search_without_next_link_is_a_single_request():
    """An uncapped catalogue still costs exactly one round trip."""
    route = respx.get(f"{STAC_URL}/search").mock(
        return_value=httpx.Response(200, json=_page(["a", "b"], matched=2))
    )

    client = PyCSWSTACClient(STAC_URL, client=httpx.Client())
    assert len(client.search(bbox=(0, 0, 10, 10), limit=100)) == 2
    assert route.call_count == 1


def test_resolve_href_only_rewrites_relative_paths():
    """Absolute hrefs are left alone; server paths gain the catalogue origin."""
    from titiler.pycsw.reader import resolve_href

    base = "https://host.example"
    assert resolve_href("/data/x.tif", base) == "https://host.example/data/x.tif"
    assert resolve_href("//cdn.example/x.tif", base) == "https://cdn.example/x.tif"
    assert resolve_href("https://other/x.tif", base) == "https://other/x.tif"
    assert resolve_href("s3://bucket/x.tif", base) == "s3://bucket/x.tif"
    # a bare relative path would resolve against the item, which we do not know
    assert resolve_href("rel/x.tif", base) == "rel/x.tif"
    # no base configured: leave the href untouched rather than invent one
    assert resolve_href("/data/x.tif", "") == "/data/x.tif"


def test_default_asset_base_url_falls_back_to_catalogue_origin(monkeypatch):
    """The origin serving the catalogue is where such assets have always been."""
    import importlib

    import titiler.pycsw.reader as reader_mod

    monkeypatch.setenv("TITILER_PYCSW_STAC_API_URL", "https://cat.example/pycsw/stac")
    monkeypatch.delenv("TITILER_PYCSW_ASSET_BASE_URL", raising=False)
    importlib.reload(reader_mod)
    assert reader_mod.default_asset_base_url() == "https://cat.example"

    monkeypatch.setenv("TITILER_PYCSW_ASSET_BASE_URL", "https://data.example/")
    importlib.reload(reader_mod)
    assert reader_mod.default_asset_base_url() == "https://data.example"

    monkeypatch.delenv("TITILER_PYCSW_ASSET_BASE_URL", raising=False)
    importlib.reload(reader_mod)


def test_reader_resolves_relative_asset_href():
    """The reader hands rio-tiler a URL, not the catalogue's server path."""
    from titiler.pycsw.reader import PyCSWSTACReader

    item = {
        "id": "scene-1",
        "bbox": [0, 0, 10, 10],
        "assets": {"asset": {"href": "/data/2026/scene.tif"}},
    }
    reader = PyCSWSTACReader(item, asset_base_url="https://cat.example")
    assert (
        reader._get_asset_info("asset")["url"]
        == "https://cat.example/data/2026/scene.tif"
    )


PROJECTED_ITEM = {
    "type": "Feature",
    "id": "proj-1",
    "bbox": [368221.6, 4184874.9, 385110.7, 4195264.4],
    "properties": {"proj:code": "EPSG:2100"},
    "assets": {"sme": {"href": "https://host/sme.tif"}},
}


def test_bbox_is_wgs84_distinguishes_lonlat_from_metres():
    from titiler.pycsw.reader import bbox_is_wgs84

    assert bbox_is_wgs84([23.6, 37.8, 23.9, 38.2]) is True
    assert bbox_is_wgs84([-180, -90, 180, 90]) is True
    assert bbox_is_wgs84([368221.6, 4184874.9, 385110.7, 4195264.4]) is False
    assert bbox_is_wgs84(None) is False
    assert bbox_is_wgs84([1, 2]) is False


def test_item_crs_lets_the_bbox_numbers_win_over_proj_code():
    """`proj:code` is the ASSET's CRS; a lon/lat bbox stays WGS84 regardless.

    Reading it the other way round put well-formed items at projected
    coordinates and made every tile miss them.
    """
    from rio_tiler.constants import WGS84_CRS

    from titiler.pycsw.reader import item_crs

    wgs84_item = {
        "bbox": [24.4, 41.4, 24.7, 42.4],
        "properties": {"proj:code": "EPSG:32634"},
    }
    assert item_crs(wgs84_item) == WGS84_CRS
    assert item_crs(wgs84_item, fallback="EPSG:2100") == WGS84_CRS


def test_item_crs_reads_the_projection_extension_when_bbox_is_not_lonlat():
    from rasterio.crs import CRS
    from rio_tiler.constants import WGS84_CRS

    from titiler.pycsw.reader import item_crs

    greek = CRS.from_epsg(2100)
    assert item_crs(PROJECTED_ITEM) == greek
    # the spellings actually seen in the wild
    assert item_crs({**PROJECTED_ITEM, "properties": {"proj:code": "2100"}}) == greek
    assert item_crs({**PROJECTED_ITEM, "properties": {"proj:epsg": "2100"}}) == greek
    assert item_crs({**PROJECTED_ITEM, "properties": {"proj:epsg": 2100}}) == greek
    # nothing to go on: only the configured fallback can name it
    bare = {**PROJECTED_ITEM, "properties": {}}
    assert item_crs(bare) == WGS84_CRS
    assert item_crs(bare, fallback="EPSG:2100") == greek
    # a nonsense code must not take the reader down
    assert (
        item_crs({**PROJECTED_ITEM, "properties": {"proj:code": "nope"}}) == WGS84_CRS
    )


def test_reader_bounds_and_crs_agree_for_projected_items():
    """Bounds in metres with a WGS84 crs puts the dataset off the planet."""
    from rasterio.crs import CRS

    from titiler.pycsw.reader import PyCSWSTACReader

    with PyCSWSTACReader(PROJECTED_ITEM) as r:
        assert r.crs == CRS.from_epsg(2100)
        assert r.bounds == PROJECTED_ITEM["bbox"]
        # the sanity check that matters: it lands in Greece, not off-world
        assert 22 < r.get_geographic_bounds(CRS.from_epsg(4326))[0] < 23


@respx.mock
def test_backend_alt_crs_runs_a_second_search_and_merges():
    """Projected items need a search in their own numeric space to be found."""
    from titiler.pycsw.backend import PyCSWBackend

    wgs = {**ITEM, "id": "wgs-1"}
    route = respx.get(f"{STAC_URL}/search").mock(
        side_effect=[
            httpx.Response(200, json={"type": "FeatureCollection", "features": [wgs]}),
            httpx.Response(
                200,
                json={"type": "FeatureCollection", "features": [PROJECTED_ITEM]},
            ),
        ]
    )

    client = PyCSWSTACClient(STAC_URL, client=httpx.Client())
    with PyCSWBackend(client=client, alt_search_crs="EPSG:2100") as backend:
        found = backend.get_assets(23.70, 37.90, 23.75, 37.95, limit=10)

    assert [i["id"] for i in found] == ["wgs-1", "proj-1"]
    assert route.call_count == 2
    # the second search must ask in metres, not degrees
    second = route.calls[1].request.url.params["bbox"]
    assert all(abs(float(v)) > 1000 for v in second.split(","))


@respx.mock
def test_backend_without_alt_crs_searches_once():
    """The extra round trip is opt-in."""
    from titiler.pycsw.backend import PyCSWBackend

    route = respx.get(f"{STAC_URL}/search").mock(
        return_value=httpx.Response(200, json=FEATURE_COLLECTION)
    )

    client = PyCSWSTACClient(STAC_URL, client=httpx.Client())
    with PyCSWBackend(client=client) as backend:
        backend.get_assets(7.70, 7.90, 7.75, 7.95, limit=10)

    assert route.call_count == 1


@respx.mock
def test_backend_alt_crs_skips_bboxes_outside_the_crs_area():
    """`transform_bounds` extrapolates rather than failing outside the CRS.

    Without an area-of-use check every tile panned away from the region would
    spend a second round trip to be told nothing matched.
    """
    from titiler.pycsw.backend import PyCSWBackend

    route = respx.get(f"{STAC_URL}/search").mock(
        return_value=httpx.Response(
            200, json={"type": "FeatureCollection", "features": []}
        )
    )

    client = PyCSWSTACClient(STAC_URL, client=httpx.Client())
    with PyCSWBackend(client=client, alt_search_crs="EPSG:2100") as backend:
        backend.get_assets(-179.9, -89.9, -179.0, -89.0, limit=10)

    assert route.call_count == 1


@respx.mock
def test_tile_skips_items_missing_the_requested_asset():
    """A heterogeneous collection must not fail the tile on one odd item.

    Planetek's AG-SM holds 3 items with an `sme` asset alongside 28 keyed
    `SME_<date>`. rio-tiler allows only TileOutsideBounds by default, so an
    item lacking the asset raised InvalidAssetName and took the whole tile
    down with it.
    """
    from unittest.mock import patch

    from rio_tiler.errors import InvalidAssetName

    from titiler.pycsw.backend import PyCSWBackend

    has_asset = {**ITEM, "id": "has-it"}
    lacks_asset = {
        **ITEM,
        "id": "lacks-it",
        "assets": {"something_else": {"href": "s3://bucket/other.tif"}},
    }
    respx.get(f"{STAC_URL}/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "FeatureCollection",
                "features": [lacks_asset, has_asset],
            },
        )
    )

    client = PyCSWSTACClient(STAC_URL, client=httpx.Client())
    with PyCSWBackend(client=client) as backend:

        def fake_tile(self, *args, **kwargs):
            if "cog" not in self.input["assets"]:
                raise InvalidAssetName("cog is not valid")
            return _fake_image()

        with patch.object(PyCSWSTACReader, "tile", fake_tile):
            image, used = backend.tile(
                2318, 1580, 12, limit=10, assets=["cog"], threads=0
            )

    # the odd item drops out; the tile still renders from the one that has it
    assert [i["id"] for i in used] == ["has-it"]
    assert image is not None


@respx.mock
def test_empty_tiles_are_not_cached(monkeypatch):
    """An empty mosaic describes the search, not the tile.

    Cached for an hour, a tile that came back empty during an outage stays
    blank long after the cause is fixed — indistinguishable from a broken
    tiler.
    """
    monkeypatch.setenv("TITILER_PYCSW_STAC_API_URL", STAC_URL)
    respx.get(f"{STAC_URL}/search").mock(
        return_value=httpx.Response(
            200, json={"type": "FeatureCollection", "features": []}
        )
    )

    import importlib

    import titiler.pycsw.main as main_mod

    importlib.reload(main_mod)
    from starlette.testclient import TestClient

    with TestClient(main_mod.app) as client:
        empty = client.get("/mosaic/bbox/31,31,32,32.png", params={"assets": "cog"})
        assert empty.status_code == 204
        assert empty.headers["cache-control"] == "no-store"


def test_queryable_suggestions_stay_quiet_when_unsure():
    """A confident wrong suggestion is worse than none.

    Most STAC spellings resolve by dropping the extension prefix and matching
    closely; the handful pycsw renames outright are tabled. Anything else gets
    the available list and no guess.
    """
    from titiler.pycsw.client import PYCSW_CORE_QUERYABLES, suggest_queryable

    known = set(PYCSW_CORE_QUERYABLES)

    derived = {
        "collection": "collections",
        "eo:cloud_cover": "cloudcover",
        "instruments": "instrument",
        "view:off_nadir": "off_nadir",
        "updated_at": "updated",
    }
    tabled = {
        "id": "identifier",
        "created": "date_creation",
        "start_datetime": "time_begin",
        "end_datetime": "time_end",
    }
    for name, want in {**derived, **tabled}.items():
        assert suggest_queryable(name, known) == want, name

    # `datetime_range` is the trap: close enough to `datetime` to match loosely
    for name in ["nonsense_field", "eo:gsd", "proj:code", "datetime_range"]:
        assert suggest_queryable(name, known) is None, name
