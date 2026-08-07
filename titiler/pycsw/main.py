"""TiTiler + pycsw FastAPI application."""

import logging
from contextlib import asynccontextmanager
from typing import Dict

import httpx
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from titiler.core.errors import DEFAULT_STATUS_CODES, add_exception_handlers
from titiler.core.factory import AlgorithmFactory, TMSFactory
from titiler.core.middleware import CacheControlMiddleware
from titiler.mosaic.errors import MOSAIC_STATUS_CODES
from titiler.pycsw import __version__ as titiler_pycsw_version
from titiler.pycsw.client import PYCSW_STATUS_CODES, PyCSWSTACClient
from titiler.pycsw.factory import MosaicTilerFactory
from titiler.pycsw.settings import ApiSettings, PyCSWSettings

logging.getLogger("rio-tiler").setLevel(logging.ERROR)

settings = ApiSettings()
pycsw_settings = PyCSWSettings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Share one HTTP client and pycsw STAC client for the app lifetime."""
    http_client = httpx.Client(
        timeout=pycsw_settings.request_timeout,
        headers={"User-Agent": f"titiler-pycsw/{titiler_pycsw_version}"},
    )
    app.state.http_client = http_client
    app.state.pycsw_client = PyCSWSTACClient(
        url=pycsw_settings.stac_api_url,
        client=http_client,
    )
    try:
        yield
    finally:
        http_client.close()


app = FastAPI(
    title=settings.name,
    description="Dynamic tiling backed by a pycsw STAC API.",
    version=titiler_pycsw_version,
    root_path=settings.root_path,
    lifespan=lifespan,
)

add_exception_handlers(app, DEFAULT_STATUS_CODES)
add_exception_handlers(app, MOSAIC_STATUS_CODES)
add_exception_handlers(app, PYCSW_STATUS_CODES)

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

app.add_middleware(CacheControlMiddleware, cachecontrol=settings.cachecontrol)

mosaic = MosaicTilerFactory(router_prefix="/mosaic", name="mosaic")
app.include_router(mosaic.router, tags=["Mosaic"], prefix="/mosaic")

app.include_router(TMSFactory().router, tags=["Tiling Schemes"])
app.include_router(AlgorithmFactory().router, tags=["Algorithms"])


@app.get("/healthz", description="Health Check", tags=["Health Check"])
def ping() -> Dict:
    """Check that the pycsw STAC API is reachable."""
    try:
        resp = app.state.http_client.get(pycsw_settings.stac_api_url)
        pycsw_online = resp.status_code < 500
    except httpx.HTTPError:
        pycsw_online = False

    return {
        "pycsw_online": pycsw_online,
        "pycsw_stac_api_url": pycsw_settings.stac_api_url,
    }
