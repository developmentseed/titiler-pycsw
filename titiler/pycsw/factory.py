"""titiler.pycsw MosaicTiler factory.

Registers dynamic mosaic endpoints backed by a pycsw STAC API. Subclasses
`titiler.core.factory.TilerFactory` to reuse the standard rendering
dependencies, but builds a `PyCSWBackend` per request instead of opening a
dataset path.
"""

from typing import Callable, Dict, List, Literal, Optional, Type
from urllib.parse import urlencode

import rasterio
from attrs import define, field
from fastapi import Body, Depends, HTTPException, Path, Query
from geojson_pydantic import Feature
from pydantic import Field
from rio_tiler.constants import WGS84_CRS
from rio_tiler.mosaic.methods.base import MosaicMethodBase
from rio_tiler.utils import CRS_to_uri
from starlette.requests import Request
from starlette.responses import Response
from typing_extensions import Annotated

from titiler.core.dependencies import (
    AssetsBidxExprParams,
    CoordCRSParams,
    DefaultDependency,
    DstCRSParams,
)
from titiler.core.factory import TilerFactory, img_endpoint_params
from titiler.core.models.mapbox import TileJSON
from titiler.core.resources.enums import ImageType, OptionalHeader
from titiler.mosaic.factory import (
    MOSAIC_STRICT_ZOOM,
    MOSAIC_THREADS,
    PixelSelectionParams,
)
from titiler.pycsw.backend import PyCSWBackend
from titiler.pycsw.dependencies import (
    BackendParams,
    PyCSWQueryParams,
    PyCSWSearchOptions,
)


@define(kw_only=True)
class MosaicTilerFactory(TilerFactory):
    """Dynamic mosaic tiler factory for a pycsw STAC API."""

    layer_dependency: Type[DefaultDependency] = AssetsBidxExprParams

    backend_dependency: Type[DefaultDependency] = BackendParams
    search_options_dependency: Type[DefaultDependency] = PyCSWSearchOptions
    query_dependency: Callable[..., Dict] = PyCSWQueryParams
    pixel_selection_dependency: Callable[..., MosaicMethodBase] = PixelSelectionParams

    optional_headers: List[OptionalHeader] = field(factory=list)

    def register_routes(self) -> None:
        """Register the mosaic endpoints."""
        self._tile_routes()
        self._tilejson_routes()
        self._point_routes()
        self._part_routes()

    def _image_headers(self, image, assets) -> Dict[str, str]:
        """Build the response headers shared by every image endpoint."""
        headers: Dict[str, str] = {}
        if OptionalHeader.x_assets in self.optional_headers:
            headers["X-Assets"] = ",".join(item["id"] for item in assets)

        if image.bounds is not None:
            headers["Content-Bbox"] = ",".join(map(str, image.bounds))
        if uri := CRS_to_uri(image.crs):
            headers["Content-Crs"] = f"<{uri}>"

        return headers

    def _tile_routes(self) -> None:
        @self.router.get(
            "/tiles/{tileMatrixSetId}/{z}/{x}/{y}",
            operation_id=f"{self.operation_prefix}getTile",
            **img_endpoint_params,
            tags=["Mosaic Tiles"],
        )
        @self.router.get(
            "/tiles/{tileMatrixSetId}/{z}/{x}/{y}.{format}",
            operation_id=f"{self.operation_prefix}getTileWithFormat",
            **img_endpoint_params,
            tags=["Mosaic Tiles"],
        )
        @self.router.get(
            "/tiles/{tileMatrixSetId}/{z}/{x}/{y}@{scale}x",
            operation_id=f"{self.operation_prefix}getTileWithScale",
            **img_endpoint_params,
            tags=["Mosaic Tiles"],
        )
        @self.router.get(
            "/tiles/{tileMatrixSetId}/{z}/{x}/{y}@{scale}x.{format}",
            operation_id=f"{self.operation_prefix}getTileWithScaleAndFormat",
            **img_endpoint_params,
            tags=["Mosaic Tiles"],
        )
        def tile(
            tileMatrixSetId: Annotated[
                Literal[tuple(self.supported_tms.list())],
                Path(description="Identifier for a supported TileMatrixSet"),
            ],
            z: Annotated[int, Path(description="Tile zoom level (Z)")],
            x: Annotated[int, Path(description="Tile column (X)")],
            y: Annotated[int, Path(description="Tile row (Y)")],
            pycsw_query=Depends(self.query_dependency),
            search_options=Depends(self.search_options_dependency),
            scale: Annotated[
                int,
                Field(
                    gt=0, le=4, description="Tile size scale. 1=256x256, 2=512x512..."
                ),
            ] = 1,
            format: Annotated[
                ImageType,
                Field(
                    description="Default will be automatically defined if the output image needs a mask (png) or not (jpeg)."
                ),
            ] = None,
            layer_params=Depends(self.layer_dependency),
            dataset_params=Depends(self.dataset_dependency),
            pixel_selection=Depends(self.pixel_selection_dependency),
            post_process=Depends(self.process_dependency),
            colormap=Depends(self.colormap_dependency),
            render_params=Depends(self.render_dependency),
            reader_params=Depends(self.reader_dependency),
            backend_params=Depends(self.backend_dependency),
            env=Depends(self.environment_dependency),
        ) -> Response:
            """Create a mosaic map tile from a pycsw STAC search."""
            tms = self.supported_tms.get(tileMatrixSetId)

            with rasterio.Env(**env):
                with PyCSWBackend(
                    client=backend_params.client,
                    tms=tms,
                    reader_options={**reader_params.as_dict()},
                ) as src_dst:
                    if MOSAIC_STRICT_ZOOM and (
                        z < src_dst.minzoom or z > src_dst.maxzoom
                    ):
                        raise HTTPException(
                            400,
                            f"Invalid ZOOM level {z}. Should be between {src_dst.minzoom} and {src_dst.maxzoom}",
                        )

                    image, assets = src_dst.tile(
                        x,
                        y,
                        z,
                        pycsw_query=pycsw_query,
                        limit=search_options.limit,
                        reverse=search_options.reverse,
                        pixel_selection=pixel_selection,
                        tilesize=scale * 256,
                        threads=MOSAIC_THREADS,
                        **layer_params.as_dict(),
                        **dataset_params.as_dict(),
                    )

            if post_process:
                image = post_process(image)

            content, media_type = self.render_func(
                image,
                output_format=format,
                colormap=colormap,
                **render_params.as_dict(),
            )

            return Response(
                content,
                media_type=media_type,
                headers=self._image_headers(image, assets),
            )

    def _tilejson_routes(self) -> None:
        @self.router.get(
            "/{tileMatrixSetId}/tilejson.json",
            response_model=TileJSON,
            responses={200: {"description": "Return a tilejson"}},
            response_model_exclude_none=True,
            operation_id=f"{self.operation_prefix}getTileJSON",
            tags=["Mosaic Tiles"],
        )
        def tilejson(
            request: Request,
            tileMatrixSetId: Annotated[
                Literal[tuple(self.supported_tms.list())],
                Path(description="Identifier for a supported TileMatrixSet"),
            ],
            pycsw_query=Depends(self.query_dependency),
            search_options=Depends(self.search_options_dependency),
            tile_format: Annotated[
                Optional[ImageType], Query(description="Output image type.")
            ] = None,
            tile_scale: Annotated[
                int, Query(gt=0, le=4, description="Tile size scale (1=256px).")
            ] = 1,
            minzoom: Annotated[
                Optional[int], Query(description="Overwrite default minzoom.")
            ] = None,
            maxzoom: Annotated[
                Optional[int], Query(description="Overwrite default maxzoom.")
            ] = None,
            layer_params=Depends(self.layer_dependency),
            dataset_params=Depends(self.dataset_dependency),
            pixel_selection=Depends(self.pixel_selection_dependency),
            backend_params=Depends(self.backend_dependency),
        ) -> Dict:
            """Return a TileJSON document for a pycsw STAC search."""
            route_params = {
                "z": "{z}",
                "x": "{x}",
                "y": "{y}",
                "scale": tile_scale,
                "tileMatrixSetId": tileMatrixSetId,
            }
            if tile_format:
                route_params["format"] = tile_format.value

            tiles_url = self.url_for(request, "tile", **route_params)

            qs_key_to_remove = [
                "tilematrixsetid",
                "tile_format",
                "tile_scale",
                "minzoom",
                "maxzoom",
            ]
            qs = [
                (key, value)
                for (key, value) in request.query_params._list
                if key.lower() not in qs_key_to_remove
            ]
            if qs:
                tiles_url += f"?{urlencode(qs)}"

            tms = self.supported_tms.get(tileMatrixSetId)
            with PyCSWBackend(client=backend_params.client, tms=tms) as src_dst:
                return {
                    "bounds": src_dst.get_geographic_bounds(
                        tms.rasterio_geographic_crs
                    ),
                    "minzoom": minzoom if minzoom is not None else src_dst.minzoom,
                    "maxzoom": maxzoom if maxzoom is not None else src_dst.maxzoom,
                    "tiles": [tiles_url],
                }

    def _point_routes(self) -> None:
        @self.router.get(
            "/point/{lon},{lat}",
            operation_id=f"{self.operation_prefix}getDataForPoint",
            tags=["Mosaic Point"],
        )
        def point(
            lon: Annotated[float, Path(description="Longitude")],
            lat: Annotated[float, Path(description="Latitude")],
            pycsw_query=Depends(self.query_dependency),
            search_options=Depends(self.search_options_dependency),
            coord_crs=Depends(CoordCRSParams),
            layer_params=Depends(self.layer_dependency),
            dataset_params=Depends(self.dataset_dependency),
            reader_params=Depends(self.reader_dependency),
            backend_params=Depends(self.backend_dependency),
            env=Depends(self.environment_dependency),
        ) -> Dict:
            """Get pixel values for a point across the mosaic."""
            with rasterio.Env(**env):
                with PyCSWBackend(
                    client=backend_params.client,
                    reader_options={**reader_params.as_dict()},
                ) as src_dst:
                    values = src_dst.point(
                        lon,
                        lat,
                        pycsw_query=pycsw_query,
                        coord_crs=coord_crs or WGS84_CRS,
                        limit=search_options.limit,
                        threads=MOSAIC_THREADS,
                        **layer_params.as_dict(),
                        **dataset_params.as_dict(),
                    )

            return {
                "coordinates": [lon, lat],
                "values": [
                    {
                        "item": item["id"],
                        "values": pts.data.tolist() if pts else None,
                        "band_names": pts.band_names if pts else None,
                    }
                    for item, pts in values
                ],
            }

    def _part_routes(self) -> None:
        @self.router.get(
            "/bbox/{minx},{miny},{maxx},{maxy}.{format}",
            operation_id=f"{self.operation_prefix}getDataForBoundingBoxWithFormat",
            **img_endpoint_params,
            tags=["Mosaic Images"],
        )
        @self.router.get(
            "/bbox/{minx},{miny},{maxx},{maxy}/{width}x{height}.{format}",
            operation_id=f"{self.operation_prefix}getDataForBoundingBoxWithSizesAndFormat",
            **img_endpoint_params,
            tags=["Mosaic Images"],
        )
        def bbox_image(
            minx: Annotated[float, Path(description="Bounding box min X")],
            miny: Annotated[float, Path(description="Bounding box min Y")],
            maxx: Annotated[float, Path(description="Bounding box max X")],
            maxy: Annotated[float, Path(description="Bounding box max Y")],
            pycsw_query=Depends(self.query_dependency),
            search_options=Depends(self.search_options_dependency),
            format: Annotated[
                ImageType,
                Field(
                    description="Default will be automatically defined if the output image needs a mask (png) or not (jpeg)."
                ),
            ] = None,
            layer_params=Depends(self.layer_dependency),
            dataset_params=Depends(self.dataset_dependency),
            image_params=Depends(self.img_part_dependency),
            dst_crs=Depends(DstCRSParams),
            coord_crs=Depends(CoordCRSParams),
            pixel_selection=Depends(self.pixel_selection_dependency),
            post_process=Depends(self.process_dependency),
            colormap=Depends(self.colormap_dependency),
            render_params=Depends(self.render_dependency),
            reader_params=Depends(self.reader_dependency),
            backend_params=Depends(self.backend_dependency),
            env=Depends(self.environment_dependency),
        ) -> Response:
            """Create an image from a bbox."""
            with rasterio.Env(**env):
                with PyCSWBackend(
                    client=backend_params.client,
                    reader_options={**reader_params.as_dict()},
                ) as src_dst:
                    image, assets = src_dst.part(
                        [minx, miny, maxx, maxy],
                        pycsw_query=pycsw_query,
                        limit=search_options.limit,
                        reverse=search_options.reverse,
                        dst_crs=dst_crs,
                        bounds_crs=coord_crs or WGS84_CRS,
                        pixel_selection=pixel_selection,
                        threads=MOSAIC_THREADS,
                        **layer_params.as_dict(),
                        **image_params.as_dict(),
                        **dataset_params.as_dict(),
                    )

            if post_process:
                image = post_process(image)

            content, media_type = self.render_func(
                image,
                output_format=format,
                colormap=colormap,
                **render_params.as_dict(),
            )

            return Response(
                content,
                media_type=media_type,
                headers=self._image_headers(image, assets),
            )

        @self.router.post(
            "/feature",
            operation_id=f"{self.operation_prefix}postDataForGeoJSON",
            **img_endpoint_params,
            tags=["Mosaic Images"],
        )
        @self.router.post(
            "/feature.{format}",
            operation_id=f"{self.operation_prefix}postDataForGeoJSONWithFormat",
            **img_endpoint_params,
            tags=["Mosaic Images"],
        )
        @self.router.post(
            "/feature/{width}x{height}.{format}",
            operation_id=f"{self.operation_prefix}postDataForGeoJSONWithSizesAndFormat",
            **img_endpoint_params,
            tags=["Mosaic Images"],
        )
        def feature_image(
            geojson: Annotated[Feature, Body(description="GeoJSON Feature.")],
            pycsw_query=Depends(self.query_dependency),
            search_options=Depends(self.search_options_dependency),
            format: Annotated[
                ImageType,
                Field(
                    description="Default will be automatically defined if the output image needs a mask (png) or not (jpeg)."
                ),
            ] = None,
            layer_params=Depends(self.layer_dependency),
            dataset_params=Depends(self.dataset_dependency),
            image_params=Depends(self.img_part_dependency),
            dst_crs=Depends(DstCRSParams),
            coord_crs=Depends(CoordCRSParams),
            pixel_selection=Depends(self.pixel_selection_dependency),
            post_process=Depends(self.process_dependency),
            colormap=Depends(self.colormap_dependency),
            render_params=Depends(self.render_dependency),
            reader_params=Depends(self.reader_dependency),
            backend_params=Depends(self.backend_dependency),
            env=Depends(self.environment_dependency),
        ) -> Response:
            """Create an image from a GeoJSON feature."""
            with rasterio.Env(**env):
                with PyCSWBackend(
                    client=backend_params.client,
                    reader_options={**reader_params.as_dict()},
                ) as src_dst:
                    image, assets = src_dst.feature(
                        geojson.model_dump(exclude_none=True),
                        pycsw_query=pycsw_query,
                        limit=search_options.limit,
                        reverse=search_options.reverse,
                        dst_crs=dst_crs,
                        shape_crs=coord_crs or WGS84_CRS,
                        pixel_selection=pixel_selection,
                        threads=MOSAIC_THREADS,
                        **layer_params.as_dict(),
                        **image_params.as_dict(),
                        **dataset_params.as_dict(),
                    )

            if post_process:
                image = post_process(image)

            content, media_type = self.render_func(
                image,
                output_format=format,
                colormap=colormap,
                **render_params.as_dict(),
            )

            return Response(
                content,
                media_type=media_type,
                headers=self._image_headers(image, assets),
            )
