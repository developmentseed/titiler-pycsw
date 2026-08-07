"""titiler.pycsw mosaic backend.

A cogeo-mosaic `BaseBackend` that mosaics on-the-fly from a pycsw STAC API:
every request issues an item-search with the request geometry's bbox, then
mosaics the returned assets. There is no persisted MosaicJSON.
"""

from typing import Any, Dict, List, Optional, Tuple, Type

import attr
import httpx
from cachetools import TTLCache, cached
from cachetools.keys import hashkey
from cogeo_mosaic.backends import BaseBackend
from cogeo_mosaic.errors import NoAssetFoundError
from cogeo_mosaic.mosaic import MosaicJSON
from morecantile import Tile, TileMatrixSet
from rasterio.crs import CRS
from rasterio.features import bounds as feature_bounds
from rasterio.warp import transform_bounds, transform_geom
from rio_tiler.constants import WEB_MERCATOR_TMS, WGS84_CRS
from rio_tiler.errors import PointOutsideBounds
from rio_tiler.models import ImageData
from rio_tiler.mosaic import mosaic_reader
from rio_tiler.tasks import MAX_THREADS, create_tasks, filter_tasks
from rio_tiler.types import BBox

from titiler.pycsw.client import PyCSWSTACClient, PyCSWSTACServerError
from titiler.pycsw.reader import PyCSWSTACReader
from titiler.pycsw.settings import CacheSettings, PyCSWSettings, RetrySettings
from titiler.pycsw.utils import retry

cache_config = CacheSettings()
retry_config = RetrySettings()
pycsw_config = PyCSWSettings()

RETRYABLE_EXCEPTIONS = (httpx.TransportError, PyCSWSTACServerError)


@attr.s
class PyCSWBackend(BaseBackend):
    """pycsw STAC API Mosaic Backend."""

    client: PyCSWSTACClient = attr.ib()

    # not tied to a MosaicJSON, so any TMS works
    tms: TileMatrixSet = attr.ib(default=WEB_MERCATOR_TMS)
    minzoom: int = attr.ib()
    maxzoom: int = attr.ib()

    reader: Type[PyCSWSTACReader] = attr.ib(init=False, default=PyCSWSTACReader)
    reader_options: Dict = attr.ib(factory=dict)

    bounds: BBox = attr.ib(default=(-180, -90, 180, 90))
    crs: CRS = attr.ib(default=WGS84_CRS)
    geographic_crs: CRS = attr.ib(default=WGS84_CRS)

    mosaic_def: MosaicJSON = attr.ib(init=False)
    input: str = attr.ib(default="pycsw", init=False)

    _backend_name = "pycsw"

    def __attrs_post_init__(self) -> None:
        """Construct a placeholder MosaicJSON."""
        self.mosaic_def = MosaicJSON(
            mosaicjson="0.0.3",
            name=self.input,
            bounds=self.bounds,
            minzoom=self.minzoom,
            maxzoom=self.maxzoom,
            tiles={},
        )

    @minzoom.default
    def _minzoom(self) -> int:
        return self.tms.minzoom

    @maxzoom.default
    def _maxzoom(self) -> int:
        return self.tms.maxzoom

    def write(self, overwrite: bool = True) -> None:
        """Not used."""

    def update(self) -> None:
        """Not used."""

    def _read(self) -> MosaicJSON:
        """Not used."""

    @property
    def _quadkeys(self) -> List[str]:
        return []

    def assets_for_tile(
        self, x: int, y: int, z: int, **kwargs: Any
    ) -> List[Dict[str, Any]]:
        """Retrieve assets for a tile."""
        bbox = self.tms.bounds(Tile(x, y, z))
        return self.get_assets(*bbox, **kwargs)

    def assets_for_bbox(
        self,
        xmin: float,
        ymin: float,
        xmax: float,
        ymax: float,
        coord_crs: CRS = WGS84_CRS,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """Retrieve assets for a bbox."""
        if coord_crs != WGS84_CRS:
            xmin, ymin, xmax, ymax = transform_bounds(
                coord_crs, WGS84_CRS, xmin, ymin, xmax, ymax
            )
        return self.get_assets(xmin, ymin, xmax, ymax, **kwargs)

    def assets_for_point(
        self,
        lng: float,
        lat: float,
        coord_crs: CRS = WGS84_CRS,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """Retrieve assets for a point."""
        if coord_crs != WGS84_CRS:
            lng, lat, _, _ = transform_bounds(coord_crs, WGS84_CRS, lng, lat, lng, lat)
        return self.get_assets(lng, lat, lng, lat, **kwargs)

    @cached(  # type: ignore
        TTLCache(maxsize=cache_config.maxsize, ttl=cache_config.ttl),
        key=lambda self, xmin, ymin, xmax, ymax, query=None, limit=None: hashkey(
            self.client.url,
            round(xmin, 8),
            round(ymin, 8),
            round(xmax, 8),
            round(ymax, 8),
            repr(query),
            limit,
        ),
    )
    @retry(
        tries=retry_config.retry,
        delay=retry_config.delay,
        exceptions=RETRYABLE_EXCEPTIONS,
    )
    def get_assets(
        self,
        xmin: float,
        ymin: float,
        xmax: float,
        ymax: float,
        query: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Find STAC items intersecting the bbox.

        `query` carries the non-spatial search parameters assembled by the
        `PyCSWQueryParams` dependency. Items without a bbox or assets cannot be
        read and are dropped.
        """
        items = self.client.search(
            bbox=(xmin, ymin, xmax, ymax),
            limit=limit or pycsw_config.default_limit,
            **(query or {}),
        )
        return [item for item in items if item.get("bbox") and item.get("assets")]

    def tile(
        self,
        tile_x: int,
        tile_y: int,
        tile_z: int,
        pycsw_query: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
        reverse: bool = False,
        **kwargs: Any,
    ) -> Tuple[ImageData, List[Dict[str, Any]]]:
        """Create a mosaic tile."""
        mosaic_assets = self.assets_for_tile(
            tile_x, tile_y, tile_z, query=pycsw_query, limit=limit
        )
        if not mosaic_assets:
            raise NoAssetFoundError(
                f"No assets found for tile {tile_z}-{tile_x}-{tile_y}"
            )

        if reverse:
            mosaic_assets = list(reversed(mosaic_assets))

        def _reader(item: Dict[str, Any], x: int, y: int, z: int, **kwargs: Any):
            with self.reader(item, tms=self.tms, **self.reader_options) as src_dst:
                return src_dst.tile(x, y, z, **kwargs)

        return mosaic_reader(mosaic_assets, _reader, tile_x, tile_y, tile_z, **kwargs)

    def point(
        self,
        lon: float,
        lat: float,
        pycsw_query: Optional[Dict[str, Any]] = None,
        coord_crs: CRS = WGS84_CRS,
        limit: Optional[int] = None,
        **kwargs: Any,
    ) -> List:
        """Read a point value from every covering item."""
        mosaic_assets = self.assets_for_point(
            lon, lat, coord_crs=coord_crs, query=pycsw_query, limit=limit
        )
        if not mosaic_assets:
            raise NoAssetFoundError(f"No assets found for point ({lon},{lat})")

        def _reader(item: Dict[str, Any], lon: float, lat: float, **kwargs: Any):
            with self.reader(item, **self.reader_options) as src_dst:
                return src_dst.point(lon, lat, **kwargs)

        # STAC items are unhashable, so pair results into a list rather than a dict.
        allowed_exceptions = kwargs.pop("allowed_exceptions", (PointOutsideBounds,))
        threads = kwargs.pop("threads", MAX_THREADS)

        tasks = create_tasks(_reader, mosaic_assets, threads, lon, lat, **kwargs)
        return [
            (item, pts)
            for pts, item in filter_tasks(tasks, allowed_exceptions=allowed_exceptions)
        ]

    def part(
        self,
        bbox: BBox,
        pycsw_query: Optional[Dict[str, Any]] = None,
        dst_crs: Optional[CRS] = None,
        bounds_crs: CRS = WGS84_CRS,
        limit: Optional[int] = None,
        reverse: bool = False,
        **kwargs: Any,
    ) -> Tuple[ImageData, List[Dict[str, Any]]]:
        """Create an image from a bbox."""
        mosaic_assets = self.assets_for_bbox(
            *bbox, coord_crs=bounds_crs, query=pycsw_query, limit=limit
        )
        if not mosaic_assets:
            raise NoAssetFoundError("No assets found for bbox input")

        if reverse:
            mosaic_assets = list(reversed(mosaic_assets))

        def _reader(item: Dict[str, Any], bbox: BBox, **kwargs: Any):
            with self.reader(item, **self.reader_options) as src_dst:
                return src_dst.part(bbox, **kwargs)

        return mosaic_reader(
            mosaic_assets,
            _reader,
            bbox,
            bounds_crs=bounds_crs,
            dst_crs=dst_crs or bounds_crs,
            **kwargs,
        )

    def feature(
        self,
        shape: Dict,
        pycsw_query: Optional[Dict[str, Any]] = None,
        dst_crs: Optional[CRS] = None,
        shape_crs: CRS = WGS84_CRS,
        limit: Optional[int] = None,
        reverse: bool = False,
        **kwargs: Any,
    ) -> Tuple[ImageData, List[Dict[str, Any]]]:
        """Create an image from a GeoJSON geometry.

        The catalogue is searched with the geometry's bbox; rio-tiler then clips
        each asset to the geometry itself.
        """
        if "geometry" in shape:
            shape = shape["geometry"]

        shape_wgs84 = shape
        if shape_crs != WGS84_CRS:
            shape_wgs84 = transform_geom(shape_crs, WGS84_CRS, shape)

        xmin, ymin, xmax, ymax = feature_bounds(shape_wgs84)
        mosaic_assets = self.get_assets(
            xmin, ymin, xmax, ymax, query=pycsw_query, limit=limit
        )
        if not mosaic_assets:
            raise NoAssetFoundError("No assets found for Geometry")

        if reverse:
            mosaic_assets = list(reversed(mosaic_assets))

        def _reader(item: Dict[str, Any], shape: Dict, **kwargs: Any):
            with self.reader(item, **self.reader_options) as src_dst:
                return src_dst.feature(shape, **kwargs)

        return mosaic_reader(
            mosaic_assets,
            _reader,
            shape,
            shape_crs=shape_crs,
            dst_crs=dst_crs or shape_crs,
            **kwargs,
        )
