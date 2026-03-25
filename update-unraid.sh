#!/bin/bash
# Update Grabia on Unraid: pull latest image, recreate container.
set -e

IMAGE="ghcr.io/scheeseman486/grabia:latest"
NAME="Grabia"

echo "Pulling latest image..."
docker pull "$IMAGE"

echo "Stopping and removing old container..."
docker stop "$NAME" 2>/dev/null || true
docker rm "$NAME" 2>/dev/null || true

echo "Starting new container..."
docker run -d \
  --name="$NAME" \
  --net=bridge \
  --pids-limit 2048 \
  -e HOST_OS="Unraid" \
  -e HOST_HOSTNAME="Notflix" \
  -e HOST_CONTAINERNAME="$NAME" \
  -l net.unraid.docker.managed=dockerman \
  -l net.unraid.docker.webui='http://[IP]:[PORT:5000]' \
  -l net.unraid.docker.icon='https://raw.githubusercontent.com/scheeseman486/grabia/main/static/grabia_icon.png' \
  -p 5000:5000/tcp \
  -v /mnt/user/appdata/grabia:/app/data:rw \
  -v /mnt/user/grabia_downloads:/root/ia-downloads:rw \
  -v /mnt/user/grabia_temp:/tempstorage:rw \
  "$IMAGE"

echo "Done. Grabia updated."
