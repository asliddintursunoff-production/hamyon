#!/usr/bin/env sh
# Copy the local docker-compose database (transactions, debts, answers) into Railway's Postgres.
#
#   scripts/copy_db_to_railway.sh 'postgresql://postgres:...@....proxy.rlwy.net:PORT/railway'
#
# Use the Postgres service's DATABASE_PUBLIC_URL (Railway → Postgres → Variables).
# Run it BEFORE the first Railway deploy of the app, or with the Railway app stopped.
# Everything already in the Railway database is replaced.
set -eu
TARGET="${1:?usage: $0 <railway DATABASE_PUBLIC_URL>}"
cd "$(dirname "$0")/.."
echo "Dumping local database…"
docker compose exec -T db pg_dump -U finance -d finance --clean --if-exists --no-owner --no-privileges > data/railway-export.sql
echo "Restoring into Railway…"
docker compose exec -T db psql "$TARGET" -v ON_ERROR_STOP=1 -q < data/railway-export.sql
rm -f data/railway-export.sql
echo "Done."
