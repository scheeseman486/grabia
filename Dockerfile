# ── Stage 1: Build maxcso from source ─────────────────────────────────
FROM debian:trixie AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git ca-certificates \
        liblz4-dev libuv1-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

ARG MAXCSO_VERSION=v1.13.0
RUN git clone --depth 1 --branch ${MAXCSO_VERSION} \
        https://github.com/unknownbrackets/maxcso.git /tmp/maxcso \
    && cd /tmp/maxcso \
    && make -j"$(nproc)" \
    && cp maxcso /usr/local/bin/maxcso \
    && rm -rf /tmp/maxcso


# ── Stage 2: Runtime image ────────────────────────────────────────────
FROM python:3.13-slim-trixie

# Add non-free for unrar; mame-tools provides chdman
RUN sed -i 's/Components: main/Components: main non-free/' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        mame-tools \
        p7zip-full \
        unrar \
        liblz4-1 libuv1 zlib1g \
    && rm -rf /var/lib/apt/lists/*

# Copy maxcso from builder
COPY --from=builder /usr/local/bin/maxcso /usr/local/bin/maxcso

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY static/ ./static/
COPY templates/ ./templates/

# shitman: Atari Jaguar CD → BigPImage converter (pure Python, no deps)
ADD https://raw.githubusercontent.com/scheeseman486/shitman/main/shitman.py /usr/local/bin/shitman.py
RUN chmod +x /usr/local/bin/shitman.py

RUN mkdir -p /app/data /app/downloads /app/processed /tempstorage

ENV GRABIA_HOST=0.0.0.0
ENV GRABIA_PORT=5000
ENV GRABIA_DATA_DIR=/app/data

# Direct all temp files (chdman, 7z, maxcso, unrar, Python tempfile) to
# /tempstorage so they land on a real disk volume instead of the container's
# overlay filesystem (docker.img).  Mount a host path to /tempstorage in
# your docker-compose or Unraid template.
ENV TMPDIR=/tempstorage
ENV TEMP=/tempstorage
ENV TMP=/tempstorage

EXPOSE 5000

# Ensure files created in mounted volumes are accessible via SMB shares
CMD ["sh", "-c", "umask 0000 && exec python app.py"]
