-- Personalization & ranking feature tables.
--
-- Written in the common subset of DuckDB and Snowflake SQL so the same file
-- runs locally (DuckDB over canonical parquet) and in production (Snowflake
-- dynamic tables refreshed hourly; see deploy/snowflake/features_dynamic.sql).
-- {{ASOF}} is a DATE literal: features only see data strictly before it, which
-- keeps offline evaluation honest (no peeking at the sessions being scored).
--
-- Each statement is preceded by a `-- name: <table>` marker.

-- name: product_popularity
SELECT l.sku,
       COUNT(DISTINCT CASE WHEN o.order_date >= {{ASOF}} - INTERVAL '90 days' THEN o.order_id END)  AS orders_90d,
       COUNT(DISTINCT o.order_id)                                                                    AS orders_365d,
       COUNT(DISTINCT o.account_id)                                                                  AS accounts_365d,
       SUM(l.qty)                                                                                    AS qty_365d,
       SUM(l.ext_price)                                                                              AS revenue_365d
FROM order_lines l
JOIN orders o ON o.order_id = l.order_id
WHERE o.order_date <  {{ASOF}}
  AND o.order_date >= {{ASOF}} - INTERVAL '365 days'
GROUP BY l.sku;

-- name: product_popularity_region
SELECT l.sku, b.region, COUNT(DISTINCT o.order_id) AS orders_365d
FROM order_lines l
JOIN orders o   ON o.order_id = l.order_id
JOIN branches b ON b.branch_id = o.branch_id
WHERE o.order_date < {{ASOF}} AND o.order_date >= {{ASOF}} - INTERVAL '365 days'
GROUP BY l.sku, b.region;

-- name: account_product
-- "Buy again" signal: how often and how recently this account bought this SKU.
SELECT o.account_id, l.sku,
       COUNT(DISTINCT o.order_id) AS n_orders,
       SUM(l.qty)                 AS qty,
       MAX(o.order_date)          AS last_date,
       MIN(o.order_date)          AS first_date
FROM order_lines l
JOIN orders o ON o.order_id = l.order_id
WHERE o.order_date < {{ASOF}}
GROUP BY o.account_id, l.sku;

-- name: account_family
-- Share of an account's order lines by product family (trade mix).
SELECT account_id, family, n_lines,
       n_lines * 1.0 / SUM(n_lines) OVER (PARTITION BY account_id) AS share
FROM (
  SELECT o.account_id, p.family, COUNT(*) AS n_lines
  FROM order_lines l
  JOIN orders o   ON o.order_id = l.order_id
  JOIN products p ON p.sku = l.sku
  WHERE o.order_date < {{ASOF}}
  GROUP BY o.account_id, p.family
) t;

-- name: account_brand
-- Brand loyalty within a family: share of the account's lines in that family.
SELECT account_id, family, brand, n_lines,
       n_lines * 1.0 / SUM(n_lines) OVER (PARTITION BY account_id, family) AS share
FROM (
  SELECT o.account_id, p.family, p.brand, COUNT(*) AS n_lines
  FROM order_lines l
  JOIN orders o   ON o.order_id = l.order_id
  JOIN products p ON p.sku = l.sku
  WHERE o.order_date < {{ASOF}}
  GROUP BY o.account_id, p.family, p.brand
) t;

-- name: segment_family
-- Cohort prior for cold-start accounts: what does this trade segment buy?
SELECT a.segment, p.family, COUNT(*) AS n_lines,
       COUNT(*) * 1.0 / SUM(COUNT(*)) OVER (PARTITION BY a.segment) AS share
FROM order_lines l
JOIN orders o   ON o.order_id = l.order_id
JOIN accounts a ON a.account_id = o.account_id
JOIN products p ON p.sku = l.sku
WHERE o.order_date < {{ASOF}} AND o.order_date >= {{ASOF}} - INTERVAL '365 days'
GROUP BY a.segment, p.family;

-- name: segment_product
SELECT a.segment, l.sku, COUNT(DISTINCT o.account_id) AS n_accounts
FROM order_lines l
JOIN orders o   ON o.order_id = l.order_id
JOIN accounts a ON a.account_id = o.account_id
WHERE o.order_date < {{ASOF}} AND o.order_date >= {{ASOF}} - INTERVAL '365 days'
GROUP BY a.segment, l.sku
HAVING COUNT(DISTINCT o.account_id) >= 2;

-- name: account_views
SELECT account_id, sku, COUNT(*) AS views_90d, MAX(ts) AS last_view
FROM events
WHERE event_type = 'view' AND ts < {{ASOF}} AND ts >= {{ASOF}} - INTERVAL '90 days'
GROUP BY account_id, sku;

-- name: query_popularity
-- Typeahead and head-query caching. Normalization happens in Python (the
-- trade analyzer); here we only count raw strings.
SELECT LOWER(TRIM(query)) AS query, COUNT(*) AS n, COUNT(DISTINCT account_id) AS n_accounts
FROM search_log
WHERE ts < {{ASOF}}
GROUP BY LOWER(TRIM(query))
HAVING COUNT(*) >= 1;
