#!/bin/sh
# ===========================================================================
# The services' database login (audit SEC-002, 18 Nov 2026).
#
# Run ONCE by the timescaledb image, on an empty data directory, as the owner
# (POSTGRES_USER). It creates:
#
#   algotrader_app   NOLOGIN - the role the migrations GRANT to. Created here
#                    only if absent; the migration that grants to it creates it
#                    the same way, so either order works.
#   algotrader_svc   LOGIN, a MEMBER of algotrader_app, owning nothing. The
#                    services connect as this. Every right it has comes from
#                    algotrader_app: no UPDATE or DELETE on decision_log or
#                    strategy_trial, no UPDATE of a strategy's document, no
#                    INSERT of an approval.
#
# Before this the services connected as POSTGRES_USER - the table OWNER - and
# every grant that "enforced" append-only audit was in force for nobody.
#
# The password arrives as ALGOTRADER_APP_DB_PASSWORD and is handed to psql as a
# variable, quoted BY psql (:'app_password'), so no shell or SQL string is ever
# built from it. It is not echoed. A database initialised before this script
# existed has no algotrader_svc: run the same statements by hand (DEPLOYMENT.md).
#
# No `set -u` or `set -e`: the image SOURCES a script that is not executable
# into its own shell, where those options would outlive this file. The checks
# are explicit instead, and psql stops on the first error (ON_ERROR_STOP).
# ===========================================================================

app_user="${ALGOTRADER_APP_DB_USER:-algotrader_svc}"
if [ -z "${ALGOTRADER_APP_DB_PASSWORD:-}" ]; then
    echo "10-app-login.sh: ALGOTRADER_APP_DB_PASSWORD is not set; refusing to create a login without one" >&2
    exit 1
fi

psql -v ON_ERROR_STOP=1 --no-psqlrc --quiet \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v app_user="$app_user" \
     -v app_password="$ALGOTRADER_APP_DB_PASSWORD" <<'SQL' || exit 1
SELECT 'CREATE ROLE algotrader_app NOLOGIN'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'algotrader_app')\gexec
CREATE ROLE :"app_user" LOGIN PASSWORD :'app_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT
    IN ROLE algotrader_app;
SQL
