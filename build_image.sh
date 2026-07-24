#!/usr/bin/env sh

set -eu


SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VERSION=`cat "$SCRIPT_DIR/VERSION"`

echo "VERSION: $VERSION"
IMAGE_NAME="harbor.gdalpha.com/alpha-ai-mcp/mcp-proxy:$VERSION"



printf 'Building Docker image %s...\n' "$IMAGE_NAME"
docker build \
  --file "$SCRIPT_DIR/Dockerfile" \
  --tag "$IMAGE_NAME" \
  "$SCRIPT_DIR"

printf 'Built Docker image: %s\n' "$IMAGE_NAME"

docker push "$IMAGE_NAME"