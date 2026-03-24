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


# ── Stage 2: Fetch chdman + deps from Arch Linux packages ────────────
FROM debian:bookworm-slim AS chdman-fetch

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates xz-utils zstd \
    && rm -rf /var/lib/apt/lists/*

ARG TARGETARCH
ARG CHDMAN_VERSION=0.286

# Helper: download an Arch package, trying revision -2 then -1.
# Arch x86_64 uses .pkg.tar.zst; Arch ARM aarch64 uses .pkg.tar.xz.
# Usage: fetch_arch_pkg <package-name> <version>
# Outputs: /tmp/<package-name>.pkg.tar
RUN cat <<'FETCH' > /usr/local/bin/fetch-arch-pkg && chmod +x /usr/local/bin/fetch-arch-pkg
#!/bin/sh
set -e
PKG="$1"; VER="$2"
if [ "$TARGETARCH" = "amd64" ]; then
    ARCH=x86_64; EXT=zst
    MIRROR="https://geo.mirror.pkgbuild.com/extra/os/x86_64"
elif [ "$TARGETARCH" = "arm64" ]; then
    ARCH=aarch64; EXT=xz
    MIRROR="http://mirror.archlinuxarm.org/aarch64/extra"
else
    echo "Unsupported architecture: $TARGETARCH" >&2; exit 1
fi
for REV in 2 1; do
    URL="${MIRROR}/${PKG}-${VER}-${REV}-${ARCH}.pkg.tar.${EXT}"
    echo "Trying ${URL}"
    if curl -fSL "$URL" -o "/tmp/${PKG}.pkg.tar.${EXT}"; then
        case "$EXT" in
            zst) zstd -d "/tmp/${PKG}.pkg.tar.${EXT}" -o "/tmp/${PKG}.pkg.tar" ;;
            xz)  xz -d "/tmp/${PKG}.pkg.tar.${EXT}" ;;
        esac
        return 0
    fi
done
echo "Failed to download ${PKG}" >&2; exit 1
FETCH

# Fetch chdman binary and its Arch-specific shared libs.
# Bookworm ships libFLAC.so.12 and libutf8proc.so.2, but chdman needs
# libFLAC.so.14 (FLAC 1.5.x) and libutf8proc.so.3 from Arch.
RUN fetch-arch-pkg mame-tools  "$CHDMAN_VERSION" \
    && fetch-arch-pkg flac       1.5.0 \
    && fetch-arch-pkg libutf8proc 2.11.3 \
    && mkdir -p /out/lib \
    && tar xf /tmp/mame-tools.pkg.tar  -C /tmp usr/bin/chdman \
    && tar xf /tmp/flac.pkg.tar        -C /tmp usr/lib/libFLAC.so.14 usr/lib/libFLAC.so.14.0.0 \
    && tar xf /tmp/libutf8proc.pkg.tar -C /tmp usr/lib/libutf8proc.so.3 usr/lib/libutf8proc.so.3.2.3 \
    && mv /tmp/usr/bin/chdman      /out/ \
    && mv /tmp/usr/lib/libFLAC*    /out/lib/ \
    && mv /tmp/usr/lib/libutf8proc* /out/lib/ \
    && chmod +x /out/chdman \
    && rm -rf /tmp/*.pkg.tar /tmp/usr


# ── Stage 3: Runtime image ────────────────────────────────────────────
FROM python:3.11-slim-bookworm

# Add non-free for unrar
RUN sed -i 's/Components: main/Components: main non-free/' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        p7zip-full \
        unrar \
        liblz4-1 libuv1 zlib1g \
        libsdl2-2.0-0 libogg0 libzstd1 \
    && rm -rf /var/lib/apt/lists/*

# Copy maxcso from builder
COPY --from=builder /usr/local/bin/maxcso /usr/local/bin/maxcso

# Copy chdman and its Arch-sourced shared libs
COPY --from=chdman-fetch /out/chdman /usr/local/bin/chdman
COPY --from=chdman-fetch /out/lib/   /usr/local/lib/
RUN ldconfig

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
