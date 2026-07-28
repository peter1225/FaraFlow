#!/usr/bin/env bash
set -euo pipefail

echo "== NVIDIA driver =="
nvidia-smi

echo "== Docker =="
docker version --format '{{.Server.Version}}'
docker compose version

echo "== NVIDIA container runtime =="
docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi

echo "A6000 preflight checks passed."

