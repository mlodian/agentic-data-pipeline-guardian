import duckdb
import pytest


@pytest.fixture
def con():
    """A fresh in-memory DuckDB connection per test, with the 'raw' schema
    created. Nothing touches the real project database file."""
    connection = duckdb.connect(":memory:")
    connection.execute("CREATE SCHEMA raw")
    yield connection
    connection.close()
