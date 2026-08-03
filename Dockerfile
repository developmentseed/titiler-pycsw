FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# system deps for rasterio/GDAL wheels are bundled; nothing extra needed for
# the manylinux wheels, but curl is handy for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY titiler ./titiler

RUN python -m pip install --upgrade pip \
    && python -m pip install ".[uvicorn]"

EXPOSE 8081

CMD ["uvicorn", "titiler.pycsw.main:app", "--host", "0.0.0.0", "--port", "8081"]
