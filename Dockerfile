# Flex Bulk on a server (Fly.io or any Docker host). Code only: the data folder (the item, stock and price files and
# what's built from them, saved orders, dashboard settings) lives on a volume mounted at /data.
FROM python:3.11-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY requirements-lock.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements-lock.txt

FROM python:3.11-slim
# libgomp1: LightGBM. tesseract-ocr: printed scans (handwriting is read by Claude when ANTHROPIC_API_KEY is set).
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 tesseract-ocr && rm -rf /var/lib/apt/lists/*
COPY --from=build /install /usr/local
RUN pip install --no-cache-dir pytesseract
WORKDIR /app
COPY webbsearch webbsearch
COPY flexbulk flexbulk
COPY config config
COPY sql sql
COPY samples samples
COPY VERSION VERSION
COPY deploy/fly-start.sh /app/fly-start.sh
ENV WEBB_DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["/bin/sh", "/app/fly-start.sh"]
