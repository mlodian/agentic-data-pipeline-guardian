variable "environment" {
  description = "Deployment environment name (dev, staging, prod). Used as a suffix/tag so multiple environments can coexist on one Snowflake account."
  type        = string
  default     = "dev"
}

variable "warehouse_name" {
  description = "Name of the Snowflake virtual warehouse the Guardian pipeline (dbt + the agent's read queries) runs on."
  type        = string
  default     = "GUARDIAN_WH"
}

variable "warehouse_size" {
  description = "Snowflake warehouse size. X-Small is intentionally the default -- this workload is small, scheduled, and cost-sensitive, not a always-on analytics warehouse."
  type        = string
  default     = "XSMALL"
}

variable "auto_suspend_seconds" {
  description = "Seconds of inactivity before the warehouse auto-suspends. Kept short since the pipeline runs on a schedule (see .github/workflows/guardian.yml), not continuously."
  type        = number
  default     = 60
}

variable "database_name" {
  description = "Name of the Snowflake database the ingestion connectors land raw data into and dbt builds staging/marts models on top of."
  type        = string
  default     = "GUARDIAN_DB"
}

variable "pii_masked_role" {
  description = "Role name that is allowed to see unmasked PII columns (email, full_name). Everyone else gets the masked value -- see masking_policies.tf."
  type        = string
  default     = "GUARDIAN_PII_UNMASKED_ROLE"
}
