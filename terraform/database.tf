resource "snowflake_database" "guardian" {
  name    = var.database_name
  comment = "Warehouse for CRM/ERP/HRIS data ingested by the Guardian pipeline (environment: ${var.environment})."
}

# Mirrors the raw / staging / marts split used locally by dbt against
# DuckDB (see dbt_project/), so switching the dbt target from `dev`
# (DuckDB) to a Snowflake target is a profiles.yml change, not a remodel.
resource "snowflake_schema" "raw" {
  database = snowflake_database.guardian.name
  name     = "RAW"
  comment  = "Landing tables written directly by ingestion connectors. Untouched, upstream-shaped data."
}

resource "snowflake_schema" "staging" {
  database = snowflake_database.guardian.name
  name     = "STAGING"
  comment  = "dbt staging models: light cleanup, one-to-one with raw sources."
}

resource "snowflake_schema" "marts" {
  database = snowflake_database.guardian.name
  name     = "MARTS"
  comment  = "dbt mart models: the conformed, business-facing tables the Guardian agent and BI tools query."
}
