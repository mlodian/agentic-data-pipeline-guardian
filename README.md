# Agentic Data Pipeline Guardian

An autonomous agent that watches a data warehouse for schema drift,
data-quality problems, and cross-system disagreement — scores how bad things
are, works out what breaks downstream, quarantines rows it can prove are
invalid, and escalates anything that needs a human.

```
CRM/ERP/HRIS  ──▶  raw tables  ──▶  dbt (staging → marts)
 connectors        (warehouse)                │
                                              ▼
                                   deterministic checks
                            schema drift · nulls · dupes · orphans
                            ranges · freshness · volume z-score
                            PII columns · CRM↔ERP reconciliation
                                              │
                                              ▼
                          risk-weighted health score (0–100)
                          + trend vs. run history
                                              │
                                              ▼
                          lineage: what breaks downstream
                                              │
                                              ▼
                            LangGraph agent (Claude)
                          triage → report → decide
                              │            │
                     quarantine bad rows   open GitHub issue
                              │            │
                              ▼            ▼
                        incident report + run recorded to history
```

## Why I built this

I spend my working life on data-quality assurance for AML compliance — test
frameworks, risk-weighted scoring, anomaly detection over messy
multi-source data. Two things about that work bother me enough that I
wanted to build the answer properly rather than argue about it in design
docs.

The first is that most data-quality tooling stops at detection. It tells you
forty things are wrong and leaves a human to work out which one matters,
what it breaks, and what to do about it. The triage is the expensive part,
and it's the part that gets skipped at 6pm on a Friday.

The second is that "put an LLM on it" usually means putting the LLM in the
worst possible place: making it responsible for *detecting* the problem, so
now your monitoring is non-deterministic and you can't unit test it. That's
backwards. Detection is arithmetic and should be boring, provable, and free.
Judgment — what's urgent, what's related, what a human should do first — is
where a model actually earns its place.

So this is the version I'd want to run for real: every check deterministic
and unit tested, a scoring model you can recompute by hand, lineage that
tells you the blast radius, and exactly one node in the pipeline that calls
a model. It runs against a synthetic warehouse rather than a real one, which
makes it a dry run rather than a deployment — but everything in it is built
to survive being pointed at production, and the parts that aren't yet proven
against real infrastructure are called out as such below rather than glossed
over.

## What it actually does

1. **Ingest** (`ingestion/generate.py`) — simulates CRM, ERP, and HRIS
   connectors landing data into raw tables. In production these would be
   real Salesforce/NetSuite/Workday-style API clients; the interesting part
   of this system is what happens *after* ingestion, so these are simple
   synthetic generators with flags to inject a schema change, data-quality
   problems, or a cross-system reconciliation break on demand.

   ERP invoices are generated *from* closed-won CRM deals rather than
   independently. That detail matters: if the two systems invented their own
   numbers they'd never agree, and reconciliation would flag every run and
   get switched off. Because clean data reconciles to the cent, a
   reconciliation finding means something actually broke.

2. **Transform** (`dbt_project/`) — staging and mart models over the raw
   tables, with 21 dbt data tests (not_null, unique, relationships) as the
   first line of defense. `customer_revenue_summary` joins all three mart
   models, which is what gives upstream failures a real blast radius.

3. **Check** (`agent/checks.py`, `agent/reconciliation.py`, config-driven by
   `agent/config.py`) — deterministic, unit-tested Python in three tiers:

   - *Structural*: schema-snapshot diffing against a committed baseline, PII
     column detection.
   - *Row-level*: nulls, uniqueness, referential integrity, value ranges,
     freshness, row-count anomaly, and a z-score volume check that compares
     a table against its own history instead of a fixed percentage.
   - *Cross-system*: reconciliation between the CRM and the ERP — the only
     tier that can fail while every table individually passes.

   Zero LLM calls anywhere in this layer, on purpose.

4. **Score** (`agent/scoring.py`) — turns findings into a risk-weighted
   0–100 health score. Risk is `check weight × severity × table
   criticality`, all declared in config, so any number in a report can be
   recomputed by hand from the findings that produced it.

5. **Remember** (`agent/history.py`) — every run is persisted to the
   warehouse: score, findings, and row counts. That's what makes the next
   run able to say "health dropped 67 points since yesterday" and what feeds
   the z-score check its distribution.

6. **Correlate** (`agent/lineage.py`) — maps each finding onto the dbt models
   downstream of it. Five findings across three raw tables that all land on
   the same mart are one incident, not five, and the report says so.

7. **Reason** (`agent/guardian_agent.py`) — a LangGraph state machine.
   Exactly one node calls Claude, via tool-calling, to synthesize everything
   above into a prioritized incident report and make two bounded decisions:
   escalate, and quarantine. Runs fully without an API key — the
   deterministic fallback is what CI exercises on every push.

8. **Act** (`agent/remediation.py`) — quarantines rows it can prove are
   invalid (a negative invoice amount, a null required key) into a
   `quarantine` schema with the run id and reason attached, so every removed
   row is recoverable with a single SELECT. Nothing moves unless
   `GUARDIAN_ENABLE_QUARANTINE=true`; the default is a costed dry run.

9. **Provision** (`terraform/`) — the Snowflake equivalent of the local
   DuckDB setup: database, warehouse, roles and grants, and column-level
   masking policies for the PII the checks flag.

## Key technical decisions

**Detection is deterministic; the LLM only does judgment.** Nothing in the
check, scoring, lineage, or reconciliation layers calls a model. A
monitoring system whose detection path depends on an API call has a single
point of failure it didn't need, and one you can't unit test. With no API
key at all, seven of the eight graph nodes behave identically and the eighth
falls back to a deterministic report.

**The health score is linear and inspectable, not clever.** It starts at 100
and subtracts `weight × severity × criticality` per finding. A smarter
saturating curve would model reality slightly better and would cost the one
property that makes the number usable in a compliance context: anyone can
recompute it by hand and see exactly which findings drove it.

**The score measures state; the trend is a separate finding.** Health
degradation is computed after scoring and appended as its own finding
without re-scoring. Folding the trend back into the score would make the
number depend on itself.

**The agent is told what its action costs before it decides.** The
quarantine plan is costed — rows counted, read-only — in `correlate_node`,
before `reason_node` runs. So whichever path decides to quarantine, model or
fallback, it does so knowing exactly how many rows would move, rather than
authorising an action whose size is only discovered afterwards.

**Every side effect is opt-in and reversible.** Quarantine copies rows
before deleting them and is gated behind an env var. GitHub issue creation
is gated behind another. The agent can be wrong without being expensive.

**Lineage is declared, not inferred.** The graph in `config.LINEAGE_EDGES`
is hand-written, which is only safe if something fails loudly when a model
is renamed. `tests/test_lineage.py` asserts in both directions that every
declared node exists as a real `.sql` file and every real model appears in
the map.

## Major challenges

**The history schema collided with the database catalog.** The DuckDB file
is `guardian.duckdb`, so DuckDB already had a catalog named `guardian` —
which made every `guardian.run_history` reference ambiguous ("use a fully
qualified path like guardian.guardian") and broke every run the moment
history was introduced. Renaming the schema to `guardian_history` fixed it;
`tests/test_history.py` pins the name so it can't come back.

**The first quarantine reports lied.** The plan was rendered into the report
before the rows were counted, so the report said "0 rows" while the run
summary said 7. The fix wasn't just moving the count earlier — it was
realising the agent should know the size of an action *before* authorising
it, which is now where the costing step lives.

**Reconciliation only means something if clean data reconciles.** The first
version generated invoices independently of deals, so the two systems
disagreed by construction and the check fired on every run. A check that
always fires is worse than no check. Deriving invoices from closed-won deals
made clean data reconcile exactly, which is what gives a reconciliation
finding its meaning.

**The monitor died at exactly the moment it mattered.** Simulating the worst
case -- an upstream system dropping `crm_customers`, the dimension every mart
joins to -- raised a `CatalogException` out of the foreign-key check and killed
the entire run. The alarm died with the building. Two guards fixed it: a missing
foreign-key target now becomes a CRITICAL `foreign_key_unverifiable` finding
(losing the ability to verify a key is itself the finding, and a silent skip
would look identical to a clean run), and a reconciliation whose source has
vanished degrades to `reconciliation_unavailable` rather than raising. Both are
regression-tested.

**The sandboxed dev environment blocks the Anthropic API outright.**
Confirmed as an infrastructure restriction rather than an application bug by
making the identical call from an unrestricted environment. Live
verification of the Claude reasoning path ran from there instead of being
skipped.

## Testing

126 tests, all deterministic, none requiring an API key or network:

| Suite | Tests | What it protects |
|---|---|---|
| `test_checks.py` | 29 | Every check family: that it fires when it should, stays quiet when it shouldn't, and scales severity sensibly |
| `test_scoring.py` | 19 | The score is ordered, weighted, bounded, and hand-recomputable |
| `test_guardian_agent.py` | 18 | Graph routing end to end: clean, dirty, remediable |
| `test_lineage.py` | 16 | Blast-radius maths, and that the declared graph matches the real dbt models |
| `test_remediation.py` | 16 | The safety properties: opt-in, declared rules only, copy before delete, recoverable |
| `test_history.py` | 15 | Runs persist faithfully; degradation is judged against the previous run |
| `test_reconciliation.py` | 13 | Clean books stay silent; each way two systems can disagree surfaces separately |

Plus 21 dbt data tests over the models themselves.

## What isn't proven yet

Worth stating plainly rather than leaving to be discovered:

- **The Terraform is written, not applied.** `terraform/` targets the
  Snowflake provider and is real configuration, but it has not been run
  against a live Snowflake account — the pipeline runs on DuckDB locally and
  in CI.
- **The connectors are synthetic.** No real Salesforce/NetSuite/Workday API
  client exists here; the generators simulate their shape and their failure
  modes.
- **The Claude path is verified, not battle-tested.** It has been run live
  against the real API, and the transcript is in `docs/examples/` — but "ran
  correctly on a real run" is a smaller claim than "ran unattended for six
  months".

## Project structure

```
agentic-data-pipeline-guardian/
├── ingestion/
│   ├── db.py                  # DuckDB connection helper
│   └── generate.py            # synthetic CRM/ERP/HRIS + fault injection
├── dbt_project/
│   └── models/
│       ├── staging/           # 4 staging views
│       └── marts/             # dim_customers, fct_deals, fct_invoices,
│                              # customer_revenue_summary
├── agent/
│   ├── config.py              # check rules, risk weights, lineage, quarantine rules
│   ├── models.py              # Finding, Severity
│   ├── checks.py              # deterministic check library
│   ├── reconciliation.py      # cross-system checks
│   ├── scoring.py             # risk-weighted health score
│   ├── history.py             # run persistence + trend detection
│   ├── lineage.py             # downstream impact / blast radius
│   ├── remediation.py         # quarantine (opt-in, reversible)
│   ├── run_checks.py          # runs every check tier
│   └── guardian_agent.py      # the 8-node LangGraph pipeline
├── terraform/                 # Snowflake: db, warehouse, RBAC, masking policies
├── tests/                     # 126 tests
└── docs/
    ├── examples/              # real, unedited live-agent transcript
    └── incident_reports/      # reports the agent has written
```

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

# 5. See it actually catch something: inject drift, DQ issues and a
#    CRM/ERP reconciliation break, then re-run
python -m ingestion.generate --seed 2 \
    --inject-schema-drift --inject-dq-issues --inject-reconciliation-break
python -m agent.guardian_agent --dry-run

# 6. Or go straight to the worst case in one command: every injector plus an
#    upstream table drop. A real run of this against the Claude API is in
#    docs/examples/live-run-critical-incident.md
python -m ingestion.generate --seed 2 --scenario critical
python -m agent.guardian_agent --dry-run
```

To use Claude for the reasoning step instead of the deterministic fallback,
copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY`, then drop
`--dry-run`.

Two side effects are opt-in:

```bash
GUARDIAN_ENABLE_QUARANTINE=true python -m agent.guardian_agent   # actually isolate bad rows
GUARDIAN_OPEN_GH_ISSUES=true     python -m agent.guardian_agent   # actually open an issue
```

```bash
pytest tests/ -v   # 126 tests
```
