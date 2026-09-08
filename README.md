# Agentic Data Pipeline Guardian

An autonomous agent that watches a data warehouse for schema drift and
data-quality problems, triages what it finds with Claude, and escalates
anything critical — extending a production AML data-quality framework
(anomaly detection, risk-weighted test cases, health scoring) into a
self-monitoring agentic workflow.

```
CRM/ERP/HRIS  ──▶  raw tables  ──▶  dbt (staging → marts)
 connectors        (warehouse)                │
                                               ▼
                                    deterministic checks
                                 (schema drift, nulls, dupes,
                                  orphans, ranges, freshness,
                                  PII column detection)
                                               │
                                               ▼
                                   LangGraph agent (Claude)
                                 triage → incident report
                                 CRITICAL? ──▶ open GitHub issue
```

## Why this project exists

This is one of three portfolio projects built to demonstrate the skills a
Senior Agentic Data Engineer role asks for that weren't yet visible in my
work history: hands-on agent-building (LangChain/LangGraph, Claude
tool-calling), Infrastructure-as-Code, and the modern
ingestion-to-warehouse-to-BI stack (dbt, cloud warehouses). Rather than a
generic tutorial project, this one is a direct extension of real production
work: a 92%-accuracy ML anomaly detection pipeline built for AML
transaction-monitoring data quality, and a rules-validation framework that
took manual-sampling QA to 100% automated test coverage. The "risk-weighted
test cases + health scoring" pattern from that work is the same pattern
`agent/checks.py` and `agent/config.py` implement here, just pointed at a
general-purpose warehouse instead of AML data.

## What it actually does

1. **Ingest** (`ingestion/generate.py`) — simulates CRM, ERP, and HRIS
   upstream connectors landing data into raw warehouse tables. (In
   production these would be real Salesforce/NetSuite/Workday-style API
   clients; the point of this repo is what happens *after* ingestion, so
   these are intentionally simple synthetic generators — including flags
   to inject a realistic schema change or data-quality problems on demand,
   for demoing what the Guardian catches.)
2. **Transform** (`dbt_project/`) — dbt staging and mart models on top of
   the raw tables, with 21 built-in dbt tests (not_null, unique,
   relationships) as a first line of defense.
3. **Check** (`agent/checks.py`, config-driven by `agent/config.py`) —
   deterministic, unit-tested Python: schema-snapshot diffing against a
   committed baseline, null-rate / uniqueness / referential-integrity /
   value-range / freshness / row-count-anomaly checks, and PII-column
   detection. Zero LLM calls in this layer on purpose — it has to be
   trustworthy on its own.
4. **Reason** (`agent/guardian_agent.py`) — a LangGraph state machine.
   One node calls Claude (via tool-calling, `langchain-anthropic`) to
   synthesize the findings into a prioritized, human-readable incident
   report and decide whether to escalate. Runs fully without an API key
   too (`--dry-run`, or just no `ANTHROPIC_API_KEY` set) via a
   deterministic fallback, so the whole pipeline is always runnable and
   testable in CI regardless of billing status.
5. **Escalate** — for CRITICAL findings, opens a GitHub issue via the `gh`
   CLI (gated behind `GUARDIAN_OPEN_GH_ISSUES=true` so local runs never
   spam a real tracker).
6. **Govern** (`terraform/`) — the production Snowflake target: warehouse,
   database/schema layout matching the dbt structure, least-privilege RBAC
   roles, and dynamic data-masking policies for the exact columns the PII
   check flags. Not applied by this repo's CI (no cloud account behind
   this sandbox) — see `terraform/README.md` for why and how to apply it
   for real.

See `docs/examples/sample-report-critical-schema-drift.md` for a real
report the agent produced against injected schema drift + data-quality
issues.

## Running it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1. Seed a clean baseline and capture what "good" looks like
python -m ingestion.generate --seed 1
python -m agent.run_checks --snapshot-baseline

# 2. Build + test the warehouse with dbt
cd dbt_project && DBT_PROFILES_DIR=. dbt build && cd ..

# 3. Run the checks on their own
python -m agent.run_checks

# 4. Run the full agent (deterministic mode, no API key needed)
python -m agent.guardian_agent --dry-run

# 5. See it actually catch something: inject drift + DQ issues, then re-run
python -m ingestion.generate --seed 2 --inject-schema-drift --inject-dq-issues
python -m agent.guardian_agent --dry-run
```

To use Claude for the reasoning step instead of the deterministic fallback,
copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY`, then drop
`--dry-run`.

```bash
pytest tests/ -v   # 23 tests: unit tests on every check, integration tests on the graph
```

## Mapping to the job description

| JD requirement | Where |
|---|---|
| Build/deploy/maintain autonomous AI agent workflows (LangChain/LangGraph, Claude) | `agent/guardian_agent.py` — LangGraph `StateGraph` with conditional routing, Claude tool-calling |
| Agentic data ingestion from CRM/ERP/HRIS to a cloud warehouse | `ingestion/generate.py` (connector-shaped, per-system) → `raw` schema |
| Detect, isolate, and address schema changes and pipeline issues | `agent/checks.py::diff_schema` + the full deterministic check suite |
| Terraform / Infrastructure-as-Code | `terraform/` — warehouse, database, RBAC, masking policies |
| NL requests → dbt models, optimized SQL, automated BI | `dbt_project/` (this project's flagship for that bullet is project #2, the NL-to-Analytics agent) |
| Automation for pipeline management, monitoring, governance | `.github/workflows/guardian.yml` scheduled run |
| Intelligent data monitoring, RBAC governance, PII masking, lineage | `agent/checks.py::detect_pii_columns` + `terraform/rbac.tf` + `terraform/masking_policies.tf` |
| Advanced Python/SQL | Throughout — see `agent/`, `ingestion/`, `dbt_project/models/` |

## Project structure

```
ingestion/        synthetic CRM/ERP/HRIS connectors -> raw warehouse tables
dbt_project/       staging + marts models, dbt tests
agent/             deterministic checks, config, LangGraph agent
tests/             pytest: unit tests on checks, integration tests on the graph
terraform/         production Snowflake IaC: warehouse, RBAC, masking
.github/workflows/ CI (tests + dbt) and the scheduled Guardian run
docs/examples/     a real incident report the agent produced
```
