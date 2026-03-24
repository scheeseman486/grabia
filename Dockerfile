# ── Stage 1: Build maxcso from source ─────────────────────────────────
FROM debian:bookworm AS builder

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


# ── Stage 2: Fetch chdman from Arch Linux packages ───────────────────
FROM debian:bookworm-slim AS chdman-fetch

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates xz-utils zstd \
    && rm -rf /var/lib/apt/lists/*

ARG TARGETARCH
ARG CHDMAN_VERSION=0.286

# Arch Linux x86_64 uses .pkg.tar.zst; Arch Linux ARM aarch64 uses .pkg.tar.xz
# Package revisions may differ between architectures — try -2 then -1
RUN set -e; \
    if [ "$TARGETARCH" = "amd64" ]; then \
        ARCH_PKG="x86_64"; \
        MIRROR="https://geo.mirror.pkgbuild.com/extra/os/x86_64"; \
        for REV in 2 1; do \
            URL="${MIRROR}/mame-tools-${CHDMAN_VERSION}-${REV}-${ARCH_PKG}.pkg.tar.zst"; \
            echo "Trying ${URL}"; \
            if curl -fSL "$URL" -o /tmp/mame-tools.pkg.tar.zst; then \
                zstd -d /tmp/mame-tools.pkg.tar.zst -o /tmp/mame-tools.pkg.tar; \
                break; \
            fi; \
        done; \
    elif [ "$TARGETARCH" = "arm64" ]; then \
        ARCH_PKG="aarch64"; \
        MIRROR="http://mirror.archlinuxarm.org/aarch64/extra"; \
        for REV in 2 1; do \
            URL="${MIRROR}/mame-tools-${CHDMAN_VERSION}-${REV}-${ARCH_PKG}.pkg.tar.xz"; \
            echo "Trying ${URL}"; \
            if curl -fSL "$URL" -o /tmp/mame-tools.pkg.tar.xz; then \
                xz -d /tmp/mame-tools.pkg.tar.xz; \
                mv /tmp/mame-tools.pkg.tar /tmp/mame-tools.pkg.tar; \
                break; \
            fi; \
        done; \
    else \
        echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1; \
    fi; \
    tar xf /tmp/mame-tools.pkg.tar -C /tmp usr/bin/chdman \
    && mv /tmp/usr/bin/chdman /usr/local/bin/chdman \
    && chmod +x /usr/local/bin/chdman \
    && rm -rf /tmp/mame-tools.pkg.tar /tmp/usr


# ── Stage 3: Runtime image ────────────────────────────────────────────
FROM python:3.11-slim-bookworm

# Add non-free for unrar
RUN sed -i 's/Components: main/Components: main non-free/' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        p7zip-full \
        unrar \
        liblz4-1 libuv1 zlib1g \
        libsdl2-2.0-0 libflac12 libutf8proc2 libogg0 \
    && rm -rf /var/lib/apt/lists/*

# Copy maxcso from builder
COPY --from=builder /usr/local/bin/maxcso /usr/local/bin/maxcso

# Copy chdman from Arch Linux package
COPY --from=chdman-fetch /usr/local/bin/chdman /usr/local/bin/chdman

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
