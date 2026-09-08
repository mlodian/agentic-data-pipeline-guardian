# Terraform: production Snowflake target

This directory is Infrastructure-as-Code for what this project's Snowflake
footprint looks like in a real deployment: a warehouse, a database with
`RAW` / `STAGING` / `MARTS` schemas (mirroring the dbt project's structure),
least-privilege RBAC roles, and dynamic data masking policies for the PII
columns the Guardian agent's own checks flag (`email`, `full_name`).

## Why this isn't applied in this repo's CI or local dev

Local development and this repo's automated tests run entirely against a
local DuckDB file (see `ingestion/db.py`) — zero external infrastructure,
zero cost, fully reproducible. This Terraform code is the declarative
description of the *real* target: apply it against your own Snowflake
account when you have one. Nothing here is wired up to run automatically.

## Applying it

1. Install Terraform >= 1.5.
2. Set credentials as environment variables (never in `.tfvars` or
   committed anywhere):
   ```bash
   export SNOWFLAKE_ACCOUNT="your-account-identifier"
   export SNOWFLAKE_USER="your-user"
   export SNOWFLAKE_PASSWORD="your-password"   # or use SNOWFLAKE_PRIVATE_KEY for key-pair auth
   ```
3. From this directory:
   ```bash
   terraform init
   terraform plan
   terraform apply
   ```
4. Point `dbt_project/profiles.yml` at a new `prod` target using the
   `snowflake` dbt adapter instead of `duckdb`, using
   `GUARDIAN_INGEST_ROLE` as the dbt-running role.
5. Point the ingestion connectors (`ingestion/generate.py` would be
   replaced by real CRM/ERP/HRIS API clients) at the same account, and have
   them run as `GUARDIAN_INGEST_ROLE`.
6. Run the Guardian agent (`agent/guardian_agent.py`) as
   `GUARDIAN_AGENT_READER_ROLE` — it only ever needs read access.

## What's intentionally out of scope here

Raw table DDL isn't managed by Terraform — those tables are created at
pipeline runtime by the ingestion connectors, and dbt owns `STAGING`/
`MARTS` table definitions. Terraform's job here stops at the account-level
scaffolding (compute, namespaces, access control, masking policy
*definitions*) that has to exist before any of that can run. See the note
at the top of `masking_policies.tf` for how a masking policy actually gets
attached to a live column.
