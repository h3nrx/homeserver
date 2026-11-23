# __BRAND__ media service: ffmpeg + yt-dlp (with deno for YouTube's JS checks),
# plus Pillow/pillow-heif/pypdf/poppler for the file converter, LibreOffice for
# PowerPoint/Word/Excel previews in the File Host, and
# onnxruntime for the background remover (model cached in /music/.models).
# The code lives in ./media and is mounted read-only at /app.
FROM python:3.13-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates poppler-utils \
    libreoffice-impress-nogui libreoffice-writer-nogui libreoffice-calc-nogui \
    fonts-dejavu-core fonts-liberation2 fonts-crosextra-carlito fonts-crosextra-caladea \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "yt-dlp[default]" deno pillow pillow-heif pypdf numpy onnxruntime
ENV PYTHONUNBUFFERED=1 HOME=/tmp XDG_CACHE_HOME=/tmp/cache DENO_DIR=/tmp/deno
EXPOSE 8000
CMD ["python3", "/app/server.py"]
