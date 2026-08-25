"""Shared DB config for the recon modules — environment-driven with prod defaults.

The staging instance (port 8090, STAGING=1) sets RECON_DB_HOST/PORT/USER/PASSWORD/NAME
to the staging MallPlus database. Prod keeps the built-in defaults, so the codebase
stays identical across environments — enhancements flow to both automatically.
"""
import os
import psycopg2

DB_CONFIG = {
    "host": os.environ.get("RECON_DB_HOST", "8.216.88.209"),
    "port": int(os.environ.get("RECON_DB_PORT", "5432")),
    "user": os.environ.get("RECON_DB_USER", "mpbi_fcro_so"),
    "password": os.environ.get("RECON_DB_PASSWORD", "3a&AuWieNtAgEE97Sw2D8F2"),
    "dbname": os.environ.get("RECON_DB_NAME", "mallplus"),
}


def get_db():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    return conn
