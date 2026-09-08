"""Shared DuckDB connection helper.

DuckDB stands in for the cloud data warehouse (Snowflake in production --
see terraform/) for local development and CI, so the whole pipeline runs
with zero external infrastructure.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

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


def fetch_row(con: duckdb.DuckDBPyConnection, sql: str) -> tuple[Any, ...]:
    """Run a single-row aggregate query and return that row. Raises if the
    query returns no row at all -- for the plain `count(*)`-style aggregates
    this project runs, that would indicate a driver-level bug, not a valid
    empty-table result (an empty table still yields one row with count 0)."""
    row = con.execute(sql).fetchone()
    if row is None:
        raise RuntimeError(f"query returned no row: {sql}")
    return row


def fetch_scalar(con: duckdb.DuckDBPyConnection, sql: str) -> Any:
    return fetch_row(con, sql)[0]
