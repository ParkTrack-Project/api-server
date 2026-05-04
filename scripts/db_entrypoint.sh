#!/bin/bash

set -euo pipefail

MAIN_DB_URL="postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST:-postgres}:${POSTGRES_PORT:-5432}/${POSTGRES_DB}?sslmode=disable"
TEST_DB_URL="postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST:-postgres}:${POSTGRES_PORT:-5432}/${POSTGRES_TEST_DB}?sslmode=disable"

echo "test0"

psql "postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/postgres?sslmode=disable" \
-v ON_ERROR_STOP=1 <<SQL
DROP DATABASE IF EXISTS ${POSTGRES_TEST_DB} WITH (FORCE);
CREATE DATABASE ${POSTGRES_TEST_DB};
SQL

echo "test1"

seqwall staircase \
--postgres-url "$TEST_DB_URL" \
--migrations-path ./migrations/up \
--upgrade "bash scripts/ci-up-one.sh {current_migration}" \
--downgrade "bash scripts/ci-down-one.sh {current_migration}"

echo "test2"

if [ -n "${MIGRATION_VERSION:-}" ]; then
	migrate -path ./migrations -database "$MAIN_DB_URL" goto "$MIGRATION_VERSION"
else
	migrate -path ./migrations -database "$MAIN_DB_URL" up
fi
