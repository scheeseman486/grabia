# ── Stage 1: Build chdman and maxcso from source ──────────────────────
FROM debian:bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git ca-certificates \
        python3 \
        liblz4-dev libuv1-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# ── chdman (from MAME) ───────────────────────────────────────────────
ARG MAME_VERSION=mame0273
RUN git clone --depth 1 --branch ${MAME_VERSION} \
        https://github.com/mamedev/mame.git /tmp/mame \
    && cd /tmp/mame \
    && make TOOLS=1 SUBTARGET=tiny SOURCES=src/mame/tiny.cpp \
        REGENIE=1 -j"$(nproc)" \
    && cp build/release/bin/chdman /usr/local/bin/chdman \
    && rm -rf /tmp/mame

# ── maxcso ────────────────────────────────────────────────────────────
ARG MAXCSO_VERSION=v1.13.0
RUN git clone --depth 1 --branch ${MAXCSO_VERSION} \
        https://github.com/unknownbrackets/maxcso.git /tmp/maxcso \
    && cd /tmp/maxcso \
    && cmake -B build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build -j"$(nproc)" \
    && cp build/maxcso /usr/local/bin/maxcso \
    && rm -rf /tmp/maxcso


# ── Stage 2: Runtime image ────────────────────────────────────────────
FROM python:3.11-slim-bookworm

# Add non-free for unrar, install runtime deps
RUN sed -i 's/Components: main/Components: main non-free/' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        p7zip-full \
        unrar \
        liblz4-1 libuv1 zlib1g \
    && rm -rf /var/lib/apt/lists/*

# Copy compiled binaries from builder
COPY --from=builder /usr/local/bin/chdman /usr/local/bin/chdman
COPY --from=builder /usr/local/bin/maxcso /usr/local/bin/maxcso

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY static/ ./static/
COPY templates/ ./templates/

RUN mkdir -p /app/data /app/downloads

ENV GRABIA_HOST=0.0.0.0
ENV GRABIA_PORT=5000
ENV GRABIA_DATA_DIR=/app/data

EXPOSE 5000

CMD ["python", "app.py"]
