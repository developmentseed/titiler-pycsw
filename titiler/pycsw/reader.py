"""titiler.pycsw STAC reader.

Adapted from titiler.pgstac's `CustomSTACReader` (MIT License).
"""

from typing import Any, Dict, Optional, Type
from urllib.parse import urlsplit

import attr
import rasterio
from morecantile import TileMatrixSet
from rio_tiler.constants import WEB_MERCATOR_TMS, WGS84_CRS
from rio_tiler.errors import InvalidAssetName
from rio_tiler.io import Reader
from rio_tiler.io.base import BaseReader, MultiBaseReader
from rio_tiler.types import AssetInfo

from titiler.pycsw.settings import PyCSWSettings

pycsw_config = PyCSWSettings()


def default_asset_base_url() -> str:
    """Origin used to resolve root-relative asset hrefs."""
    if pycsw_config.asset_base_url:
        return pycsw_config.asset_base_url.rstrip("/")

    parts = urlsplit(pycsw_config.stac_api_url)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"

    return ""


def resolve_href(href: str, base_url: str) -> str:
    """Turn a root-relative or protocol-relative asset href into a URL."""
    if not href or not base_url:
        return href

    if href.startswith("//"):
        return f"{urlsplit(base_url).scheme or 'https'}:{href}"

    if href.startswith("/"):
        return f"{base_url}{href}"

    return href


@attr.s
class PyCSWSTACReader(MultiBaseReader):
    """STAC Reader backed by a pycsw STAC Item dict.

    Reads directly from the Item returned by the search, avoiding an extra HTTP
    fetch per item.
    """

    input: Dict[str, Any] = attr.ib()

    tms: TileMatrixSet = attr.ib(default=WEB_MERCATOR_TMS)
    minzoom: int = attr.ib()
    maxzoom: int = attr.ib()

    reader: Type[BaseReader] = attr.ib(default=Reader)
    reader_options: Dict = attr.ib(factory=dict)

    asset_base_url: Optional[str] = attr.ib(factory=default_asset_base_url)

    ctx: Any = attr.ib(default=rasterio.Env)

    def __attrs_post_init__(self) -> None:
        """Set reader spatial infos and list of valid assets."""
        self.bounds = self.input["bbox"]
        self.crs = WGS84_CRS
        self.assets = list(self.input["assets"])

    @minzoom.default
    def _minzoom(self) -> int:
        return self.tms.minzoom

    @maxzoom.default
    def _maxzoom(self) -> int:
        return self.tms.maxzoom

    def _get_asset_info(self, asset: str) -> AssetInfo:
        """Validate asset name and return asset's info."""
        if asset not in self.assets:
            raise InvalidAssetName(f"{asset} is not valid")

        asset_meta = self.input["assets"][asset]
        info = AssetInfo(
            url=resolve_href(asset_meta["href"], self.asset_base_url or "")
        )

        if "file:header_size" in asset_meta:
            info["env"] = {
                "GDAL_INGESTED_BYTES_AT_OPEN": asset_meta["file:header_size"]
            }

        return info
