resource "snowflake_warehouse" "guardian" {
  name           = var.warehouse_name
  warehouse_size = var.warehouse_size
  auto_suspend   = var.auto_suspend_seconds
  auto_resume    = true
  comment        = "Warehouse for the Agentic Data Pipeline Guardian (ingestion, dbt, and the agent's monitoring queries)."
}
