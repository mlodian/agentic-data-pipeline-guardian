# Dynamic Data Masking for the exact columns agent/checks.py flags as PII
# (see PII_COLUMN_PATTERNS) -- raw.hris_employees.email and .full_name.
# This is the direct Snowflake-side answer to what the Guardian agent's
# PII check surfaces as a WARNING: "this column looks like PII, confirm
# it's masked."
#
# Note on scope: the RAW/STAGING/MARTS *schemas* are IaC-managed here, but
# the tables inside RAW are created at pipeline runtime by the ingestion
# connectors (dbt owns STAGING/MARTS). Masking policies are defined here as
# code, but attaching one to a specific table column happens with an ALTER
# TABLE ... MODIFY COLUMN ... SET MASKING POLICY statement run as part of
# the ingestion connector's setup step (or via a `snowflake_table` Terraform
# resource, if you choose to bring raw table DDL under Terraform too) --
# not inline here, since the tables don't exist at `terraform apply` time.

resource "snowflake_role" "guardian_pii_unmasked" {
  name    = "GUARDIAN_PII_UNMASKED_ROLE"
  comment = "The only role that sees unmasked PII (email, full_name). Compliance/admin use only -- everyone else, including GUARDIAN_AGENT_READER_ROLE, sees the masked value."
}

resource "snowflake_masking_policy" "mask_email" {
  name     = "MASK_EMAIL"
  database = snowflake_database.guardian.name
  schema   = snowflake_schema.raw.name

  signature {
    column {
      name = "val"
      type = "VARCHAR"
    }
  }
  return_data_type = "VARCHAR"

  body = <<-EOT
    case
      when is_role_in_session('${snowflake_role.guardian_pii_unmasked.name}') then val
      else regexp_replace(val, '^.+@', '***@')
    end
  EOT

  comment = "Masks everything before the @ in an email address unless the querying role is GUARDIAN_PII_UNMASKED_ROLE."
}

resource "snowflake_masking_policy" "mask_full_name" {
  name     = "MASK_FULL_NAME"
  database = snowflake_database.guardian.name
  schema   = snowflake_schema.raw.name

  signature {
    column {
      name = "val"
      type = "VARCHAR"
    }
  }
  return_data_type = "VARCHAR"

  body = <<-EOT
    case
      when is_role_in_session('${snowflake_role.guardian_pii_unmasked.name}') then val
      else '***MASKED***'
    end
  EOT

  comment = "Fully masks employee full_name unless the querying role is GUARDIAN_PII_UNMASKED_ROLE."
}
