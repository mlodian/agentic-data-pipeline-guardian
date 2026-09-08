# --- Roles -------------------------------------------------------------
# Three roles, least-privilege by default: the pipeline itself only needs
# to write to RAW and read/build STAGING+MARTS; the Guardian agent only
# ever needs read access to run its checks; everything else (schema
# changes, warehouse sizing) stays with SYSADMIN.

resource "snowflake_role" "guardian_ingest" {
  name    = "GUARDIAN_INGEST_ROLE"
  comment = "Used by ingestion connectors (CRM/ERP/HRIS) and dbt to write raw data and build staging/mart models."
}

resource "snowflake_role" "guardian_agent_reader" {
  name    = "GUARDIAN_AGENT_READER_ROLE"
  comment = "Used by the Guardian agent's monitoring queries. Read-only, by design -- the agent should never be able to mutate the warehouse it's watching."
}

# --- Warehouse + database usage ----------------------------------------

resource "snowflake_grant_privileges_to_role" "ingest_warehouse_usage" {
  privileges = ["USAGE", "OPERATE"]
  role_name  = snowflake_role.guardian_ingest.name
  on_account_object {
    object_type = "WAREHOUSE"
    object_name = snowflake_warehouse.guardian.name
  }
}

resource "snowflake_grant_privileges_to_role" "reader_warehouse_usage" {
  privileges = ["USAGE"]
  role_name  = snowflake_role.guardian_agent_reader.name
  on_account_object {
    object_type = "WAREHOUSE"
    object_name = snowflake_warehouse.guardian.name
  }
}

resource "snowflake_grant_privileges_to_role" "ingest_database_usage" {
  privileges = ["USAGE"]
  role_name  = snowflake_role.guardian_ingest.name
  on_account_object {
    object_type = "DATABASE"
    object_name = snowflake_database.guardian.name
  }
}

resource "snowflake_grant_privileges_to_role" "reader_database_usage" {
  privileges = ["USAGE"]
  role_name  = snowflake_role.guardian_agent_reader.name
  on_account_object {
    object_type = "DATABASE"
    object_name = snowflake_database.guardian.name
  }
}

# --- Schema-level privileges --------------------------------------------

resource "snowflake_grant_privileges_to_role" "ingest_raw_write" {
  privileges        = ["USAGE", "CREATE TABLE", "MODIFY"]
  role_name         = snowflake_role.guardian_ingest.name
  on_schema {
    schema_name = "\"${snowflake_database.guardian.name}\".\"${snowflake_schema.raw.name}\""
  }
}

resource "snowflake_grant_privileges_to_role" "ingest_staging_marts_write" {
  for_each = toset([snowflake_schema.staging.name, snowflake_schema.marts.name])

  privileges = ["USAGE", "CREATE TABLE", "CREATE VIEW", "MODIFY"]
  role_name  = snowflake_role.guardian_ingest.name
  on_schema {
    schema_name = "\"${snowflake_database.guardian.name}\".\"${each.value}\""
  }
}

resource "snowflake_grant_privileges_to_role" "reader_all_schemas" {
  for_each = toset([snowflake_schema.raw.name, snowflake_schema.staging.name, snowflake_schema.marts.name])

  privileges = ["USAGE"]
  role_name  = snowflake_role.guardian_agent_reader.name
  on_schema {
    schema_name = "\"${snowflake_database.guardian.name}\".\"${each.value}\""
  }
}

resource "snowflake_grant_privileges_to_role" "reader_select_all_tables" {
  for_each = toset([snowflake_schema.raw.name, snowflake_schema.staging.name, snowflake_schema.marts.name])

  privileges = ["SELECT"]
  role_name  = snowflake_role.guardian_agent_reader.name
  on_schema_object {
    future {
      object_type_plural = "TABLES"
      in_schema           = "\"${snowflake_database.guardian.name}\".\"${each.value}\""
    }
  }
}
