"""Shared DuckDB connection helper.

DuckDB stands in for the cloud data warehouse (Snowflake in production --
see terraform/) for local development and CI, so the whole pipeline runs
with zero external infrastructure.
"""
from __future__ import annotations

import os
from pathlib import Path

import duckdb

DEFAULT_DB_PATH = "./data/guardian.duckdb"


def get_db_path() -> str:
    return os.environ.get("GUARDIAN_DB_PATH", DEFAULT_DB_PATH)


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    db_path = get_db_path()
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(db_path, read_only=read_only)
    if not read_only:
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    return con
