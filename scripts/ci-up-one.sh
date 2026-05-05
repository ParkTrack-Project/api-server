#!/bin/bash
set -euo pipefail

MIGRATION_FILE="$1"

VERSION=$(basename "$MIGRATION_FILE" | sed 's/^0*//' | cut -d'_' -f1)

TEST_DB_URL="postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST:-postgres}:${POSTGRES_PORT:-5432}/${POSTGRES_TEST_DB}?sslmode=disable"

migrate -path ./migrations -database "$TEST_DB_URL" goto "$VERSION"

#migrate -path ./migrations -database "$TEST_DB_URL" up 1
