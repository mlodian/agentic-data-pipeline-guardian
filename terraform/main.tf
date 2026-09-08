# Provider configuration is intentionally credential-free here. Snowflake
# credentials come from environment variables the provider reads
# automatically (SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, and either
# SNOWFLAKE_PASSWORD or key-pair auth via SNOWFLAKE_PRIVATE_KEY) so nothing
# secret ever needs to live in this repo or in Terraform state variables.
#
# This file is NOT applied by the Guardian agent or its CI workflow -- it's
# the declarative target for what a real deployment's Snowflake footprint
# looks like. Local dev and this repo's tests run entirely against DuckDB
# (see ingestion/db.py); this is what you'd `terraform apply` once you have
# a Snowflake account to point it at. See terraform/README.md.
provider "snowflake" {
  role = "SYSADMIN"
}
