#!/bin/bash

set -eo

TEST_DB_URL="postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_TEST_DB}?sslmode=disable"


migrate -path ./migrations/down -database "$TEST_DB_URL" down 1
