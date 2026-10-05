#!/usr/bin/env bash
# 构建 + 导出 + 体积校验（<=1GB）
set -euo pipefail

IMAGE_NAME="${1:-police-legal-agent}"
TAG="${2:-latest}"

docker build -t "${IMAGE_NAME}:${TAG}" .

OUT="${IMAGE_NAME}.tar"
docker save "${IMAGE_NAME}:${TAG}" -o "${OUT}"

SIZE=$(du -m "${OUT}" | cut -f1)
echo "导出完成: ${OUT} (${SIZE} MB)"
if [ "${SIZE}" -gt 1024 ]; then
    echo "错误：镜像超过 1GB 限制" >&2
    exit 1
fi
echo "体积校验通过"
