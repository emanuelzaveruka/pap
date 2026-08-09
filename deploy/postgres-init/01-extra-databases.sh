#!/bin/sh
# Create the databases GlitchTip and Healthchecks need alongside the app's own.
#
# The postgres image creates only POSTGRES_DB, and scripts in
# /docker-entrypoint-initdb.d run ONLY on first initialisation of an empty data
# directory. If you add this to a stack whose pgdata volume already exists, it
# will not run — create the databases by hand:
#
#   docker compose exec postgres createdb -U "$POSTGRES_USER" glitchtip
#   docker compose exec postgres createdb -U "$POSTGRES_USER" healthchecks
set -eu

for db in glitchtip healthchecks; do
    echo "creating database ${db}"
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
        SELECT 'CREATE DATABASE ${db}'
         WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${db}')\gexec
        GRANT ALL PRIVILEGES ON DATABASE ${db} TO "$POSTGRES_USER";
SQL
done
