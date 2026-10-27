"""The Extended Catalog (v2.3): availability tiers, -EC items, lead times, freight and special orders.

  branchmap   each branch's same-day zone (drive time), region and serving CD, and transfer days
  suppliers   per manufacturer: factory-stock and lead-time feeds, on-time record, ship-from warehouses, free-freight
              minimum (synthetic; production reads them from Snowflake)
  catalog     the -EC items: synthetic, built from this copy's catalog, with their own search index
  tiers       the tier of an item for a branch and a quantity, and its delivery estimate
  ranking     the availability score, the guardrails and the dashboard's settings
  freight     ShipHawk-shaped rating (UPS and LTL) and free-freight (FFA) by manufacturer
  cart        a cart planned by how it arrives, with freight, return terms and the ERP lines and POs it makes
  special     special-order (tier 4) families and quote requests

See docs/specs/2026-10-24-extended-catalog-design.md."""
