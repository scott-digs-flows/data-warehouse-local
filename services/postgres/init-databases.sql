-- Databases created on first Postgres start.
--   lakekeeper -> Iceberg REST catalog metadata (internal; do not query directly)
--   analytics  -> synced copy of the lake, queried by the BI app (Tier-2 engine)
CREATE DATABASE lakekeeper;
CREATE DATABASE analytics;
