"""titiler.pycsw STAC reader.

Adapted from titiler.pgstac's `CustomSTACReader` (MIT License).
"""

from typing import Any, Dict, Type

import attr
import rasterio
from morecantile import TileMatrixSet
from rio_tiler.constants import WEB_MERCATOR_TMS, WGS84_CRS
from rio_tiler.errors import InvalidAssetName
from rio_tiler.io import Reader
from rio_tiler.io.base import BaseReader, MultiBaseReader
from rio_tiler.types import AssetInfo


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
        info = AssetInfo(url=asset_meta["href"])

        if "file:header_size" in asset_meta:
            info["env"] = {
                "GDAL_INGESTED_BYTES_AT_OPEN": asset_meta["file:header_size"]
            }

        return info
