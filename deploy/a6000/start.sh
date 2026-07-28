#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$project_dir"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example. Review production secrets before exposing services."
fi

docker compose -f docker-compose.yml -f docker-compose.a6000.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.a6000.yml ps

