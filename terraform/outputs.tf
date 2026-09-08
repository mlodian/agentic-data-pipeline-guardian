output "warehouse_name" {
  value = snowflake_warehouse.guardian.name
}

output "database_name" {
  value = snowflake_database.guardian.name
}

output "ingest_role_name" {
  value = snowflake_role.guardian_ingest.name
}

output "agent_reader_role_name" {
  value = snowflake_role.guardian_agent_reader.name
}

output "pii_unmasked_role_name" {
  value = snowflake_role.guardian_pii_unmasked.name
}
