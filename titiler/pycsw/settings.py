"""titiler.pycsw API settings."""

from typing import Annotated, List, Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class ApiSettings(BaseSettings):
    """API settings."""

    name: str = "titiler-pycsw"
    cors_origins: Annotated[List[str], NoDecode] = ["*"]
    cachecontrol: str = "public, max-age=3600"
    root_path: str = ""
    debug: bool = False

    model_config = SettingsConfigDict(
        env_prefix="TITILER_PYCSW_",
        env_file=".env",
        extra="ignore",
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origin(cls, v: object) -> List[str]:
        """Parse a comma-separated list of CORS origins."""
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",")]

        return list(v)  # type: ignore[arg-type]


class PyCSWSettings(BaseSettings):
    """pycsw STAC API connection settings."""

    stac_api_url: str = "http://localhost:8000/stac"
    default_limit: Annotated[int, Field(ge=1, le=10_000)] = 100
    default_sortby: str = "-datetime"
    request_timeout: Annotated[float, Field(ge=0.0)] = 30.0

    # bounds the sequential `rel=next` round trips one tile can incur
    max_pages: Annotated[int, Field(ge=1, le=1_000)] = 10

    # base for assets published as server paths; empty uses the catalogue origin
    asset_base_url: Optional[str] = None

    # CRS to run a second item-search in, for items whose bbox is not lon/lat
    alt_search_crs: Optional[str] = None

    model_config = SettingsConfigDict(
        env_prefix="TITILER_PYCSW_",
        env_file=".env",
        extra="ignore",
    )


class CacheSettings(BaseSettings):
    """Cache settings for pycsw search responses."""

    ttl: int = 300
    maxsize: int = 512
    disable: bool = False

    model_config = SettingsConfigDict(
        env_prefix="TITILER_PYCSW_CACHE_",
        env_file=".env",
        extra="ignore",
    )

    @model_validator(mode="after")
    def check_enable(self) -> "CacheSettings":
        """Turn off the cache."""
        if self.disable:
            self.ttl = 0
            self.maxsize = 0

        return self


class RetrySettings(BaseSettings):
    """Retry settings for transient failures."""

    retry: Annotated[int, Field(ge=0)] = 3
    delay: Annotated[float, Field(ge=0.0)] = 0.0

    model_config = SettingsConfigDict(
        env_prefix="TITILER_PYCSW_",
        env_file=".env",
        extra="ignore",
    )
