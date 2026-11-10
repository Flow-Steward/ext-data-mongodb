#!/bin/sh
# Reproducible MongoDB-only Toolbox build with bounded egress and canonical Extended JSON.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TOOLBOX_TAG=v1.3.0
TOOLBOX_COMMIT=b0010060a939455d91e4f8ba62dc3e72b1892469
GO_IMAGE=golang:1.26.10-bookworm@sha256:5de7c99eb78ca5e515a4c340235d0955ec5964a5071c492c15503d7c267da473
PATCH=scripts/patches/0001-flowsteward-mongodb-safety.patch

BUILD_OWNER=${FS_BUILD_OWNER:-fs-nosql-mongodb-20261102}
MOD_VOLUME=flow-steward-db-batch-go-cache
BUILD_VOLUME=flow-steward-db-batch-build-cache

docker run --rm --name "$BUILD_OWNER-native-build" --label "owner=$BUILD_OWNER" \
  --memory=2g --cpus=1 -e GOMAXPROCS=1 \
  -e TOOLBOX_TAG="$TOOLBOX_TAG" -e TOOLBOX_COMMIT="$TOOLBOX_COMMIT" \
  -e GO_IMAGE="$GO_IMAGE" -e PATCH="$PATCH" -e PATCH_CONTENT="$(cat "$ROOT/$PATCH")" \
  -v "$MOD_VOLUME:/go/pkg/mod" -v "$BUILD_VOLUME:/root/.cache/go-build" \
  -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
  -v "$ROOT:/out" "$GO_IMAGE" sh -c '
    set -eu
    git clone --quiet --filter=blob:none --branch "$TOOLBOX_TAG" --depth 1 \
      https://github.com/googleapis/mcp-toolbox.git /src
    test "$(git -C /src rev-parse HEAD)" = "$TOOLBOX_COMMIT"
    cd /src
    git apply /out/scripts/patches/0000-security-baseline.patch
    printf "%s\n" "7f367f79f68cc28cfcb8c7d36c5533ef79f5016aaeba4443a795a46132105fb5  go.mod" "ca2ace78373b79e7c2b048e69ef22abfb93f1b3a537b7f035ad7c1a0c28542e9  go.sum" | sha256sum -c -
    export GOFLAGS=-mod=readonly GOTOOLCHAIN=local
    sha256sum go.mod go.sum > /tmp/fs-security-module-lock
    printf "%s\n" "$PATCH_CONTENT" > /tmp/mongodb.patch
    git apply /tmp/mongodb.patch
    awk '\''/internal\/(sources|tools)\// {
      if ($2 ~ /github.com\/googleapis\/mcp-toolbox\/internal\/(sources\/mongodb|tools\/mongodb\/mongodb(find|findone|aggregate|insertone|insertmany|updateone|updatemany|deleteone|deletemany))\"$/) print;
      next
    } { print }'\'' cmd/internal/imports.go > /tmp/imports.go
    test "$(grep -c "internal/\(sources\|tools\)/" /tmp/imports.go)" = 10
    mv /tmp/imports.go cmd/internal/imports.go
    gofmt -w internal/sources/mongodb/*.go internal/tools/mongodb/*/*.go
    go test -p 1 ./internal/sources/mongodb ./internal/tools/mongodb/...
    export SOURCE_DATE_EPOCH=1779400183
    for target in linux-amd64 linux-arm64; do
      arch=${target#linux-}
      mkdir -p "/tmp/mongodb-bin/$target"
      CGO_ENABLED=0 GOOS=linux GOARCH="$arch" go build -p 1 -trimpath -buildvcs=false \
        -ldflags "-s -w -buildid= -X github.com/googleapis/mcp-toolbox/cmd.buildType=flowsteward.mongodb -X github.com/googleapis/mcp-toolbox/cmd.commitSha=$TOOLBOX_COMMIT" \
        -o "/tmp/mongodb-bin/$target/toolbox" .
      chmod 0755 "/tmp/mongodb-bin/$target/toolbox"
      echo "[PASS] $target: $(stat -c %s "/tmp/mongodb-bin/$target/toolbox") bytes"
    done
    sha256sum -c /tmp/fs-security-module-lock
    {
      printf "{\n  \"tag\": \"%s\",\n  \"commit\": \"%s\",\n  \"go_image\": \"%s\",\n" "$TOOLBOX_TAG" "$TOOLBOX_COMMIT" "$GO_IMAGE"
      printf "  \"source_date_epoch\": 1779400183,\n  \"patch\": \"%s\",\n  \"patch_sha256\": \"%s\",\n" "$PATCH" "$(sha256sum /tmp/mongodb.patch | cut -d " " -f 1)"
      printf "  \"packages\": [\"github.com/googleapis/mcp-toolbox/internal/sources/mongodb\", \"github.com/googleapis/mcp-toolbox/internal/tools/mongodb/mongodbaggregate\", \"github.com/googleapis/mcp-toolbox/internal/tools/mongodb/mongodbdeletemany\", \"github.com/googleapis/mcp-toolbox/internal/tools/mongodb/mongodbdeleteone\", \"github.com/googleapis/mcp-toolbox/internal/tools/mongodb/mongodbfind\", \"github.com/googleapis/mcp-toolbox/internal/tools/mongodb/mongodbfindone\", \"github.com/googleapis/mcp-toolbox/internal/tools/mongodb/mongodbinsertmany\", \"github.com/googleapis/mcp-toolbox/internal/tools/mongodb/mongodbinsertone\", \"github.com/googleapis/mcp-toolbox/internal/tools/mongodb/mongodbupdatemany\", \"github.com/googleapis/mcp-toolbox/internal/tools/mongodb/mongodbupdateone\"],\n  \"binaries\": {\n"
      comma=""
      for target in linux-amd64 linux-arm64; do
        printf "%s    \"%s\": {\"sha256\": \"%s\", \"size\": %s}" "$comma" "$target" "$(sha256sum "/tmp/mongodb-bin/$target/toolbox" | cut -d " " -f 1)" "$(stat -c %s "/tmp/mongodb-bin/$target/toolbox")"
        comma=",
"
      done
      printf "\n  }\n}\n"
    } > /tmp/mongodb-provenance.json
    # Publish only complete builds; rename each file on its destination filesystem.
    for target in linux-amd64 linux-arm64; do
      mkdir -p "/out/bin/$target"
      pending=$(mktemp "/out/bin/$target/.toolbox.XXXXXX")
      cp "/tmp/mongodb-bin/$target/toolbox" "$pending"
      chmod 0755 "$pending"
      chown "$HOST_UID:$HOST_GID" "$pending"
      mv -f "$pending" "/out/bin/$target/toolbox"
    done
    pending=$(mktemp /out/.toolbox-provenance.XXXXXX)
    cp /tmp/mongodb-provenance.json "$pending"
    chmod 0644 "$pending"
    chown "$HOST_UID:$HOST_GID" "$pending"
    mv -f "$pending" /out/toolbox-provenance.json
  '

python3 "$ROOT/scripts/native_build_info.py"
