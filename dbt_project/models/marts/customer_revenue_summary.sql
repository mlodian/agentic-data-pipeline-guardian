-- The model the whole pipeline exists to protect: one row per customer,
-- combining CRM pipeline with ERP billing. It depends on all three upstream
-- marts, which is why a single upstream failure has a blast radius rather
-- than a blast point -- agent/lineage.py is what makes that visible in the
-- incident report instead of something you find out from a stakeholder.
with customers as (
    select * from {{ ref('dim_customers') }}
),

deals as (
    select
        customer_id,
        count(*) as total_deals,
        count(*) filter (where deal_stage = 'closed_won') as won_deals,
        coalesce(sum(amount) filter (where deal_stage = 'closed_won'), 0) as booked_revenue
    from {{ ref('fct_deals') }}
    group by 1
),

invoices as (
    select
        customer_id,
        count(*) as total_invoices,
        coalesce(sum(amount) filter (where status <> 'void'), 0) as invoiced_revenue,
        coalesce(sum(amount) filter (where status = 'paid'), 0) as collected_revenue
    from {{ ref('fct_invoices') }}
    group by 1
)

select
    c.customer_id,
    c.company_name,
    c.region,
    c.account_owner,
    coalesce(d.total_deals, 0) as total_deals,
    coalesce(d.won_deals, 0) as won_deals,
    coalesce(d.booked_revenue, 0) as booked_revenue,
    coalesce(i.total_invoices, 0) as total_invoices,
    coalesce(i.invoiced_revenue, 0) as invoiced_revenue,
    coalesce(i.collected_revenue, 0) as collected_revenue,
    -- The reconciliation gap, materialised as a column so it is visible in
    -- the warehouse itself and not only in the Guardian's report.
    coalesce(d.booked_revenue, 0) - coalesce(i.invoiced_revenue, 0) as revenue_variance
from customers c
left join deals d on c.customer_id = d.customer_id
left join invoices i on c.customer_id = i.customer_id
