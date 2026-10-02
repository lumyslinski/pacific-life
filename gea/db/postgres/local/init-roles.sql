-- Local development only: mounted into the postgres container of `docker compose --profile gea`.
-- It does what the platform (Terraform / RDS) does in a real environment: the group roles
-- and one login for each. The schema itself is created by `alembic upgrade head` (service gea-migrate).
CREATE ROLE gea_app NOLOGIN;
CREATE ROLE gea_relay NOLOGIN;
CREATE ROLE gea_api LOGIN PASSWORD 'gea_api' IN ROLE gea_app;
CREATE ROLE gea_worker LOGIN PASSWORD 'gea_worker' IN ROLE gea_relay;
CREATE ROLE calc_audit NOLOGIN;
CREATE ROLE calc_audit_exporter LOGIN PASSWORD 'calc_audit_exporter' IN ROLE calc_audit;
