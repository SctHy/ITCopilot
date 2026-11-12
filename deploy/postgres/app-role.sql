-- Application role for row-level security (ADR 0023). Run once as a superuser, e.g.
--   docker compose exec -T postgres psql -U itc -d it_copilot -v app_password="'change-me'" -f - < deploy/postgres/app-role.sql
-- then point IT_COPILOT_DATABASE_URL at itc_app. Superusers and BYPASSRLS roles ignore every policy,
-- so the application must not connect as POSTGRES_USER (the image creates it as a superuser).
-- itc_app owns the tables (migrations run at startup) and is subject to them through FORCE.

CREATE EXTENSION IF NOT EXISTS vector;          -- needs a superuser; the application role cannot create it

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'itc_app') THEN
        CREATE ROLE itc_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB;
    END IF;
END $$;
ALTER ROLE itc_app PASSWORD :app_password;

GRANT CONNECT, CREATE ON DATABASE it_copilot TO itc_app;
GRANT USAGE, CREATE ON SCHEMA public TO itc_app;

-- Existing deployments: hand the tables created by the old superuser connection to itc_app.
DO $$
DECLARE t record;
BEGIN
    FOR t IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP
        EXECUTE format('ALTER TABLE public.%I OWNER TO itc_app', t.tablename);
    END LOOP;
    FOR t IN SELECT sequence_name FROM information_schema.sequences WHERE sequence_schema = 'public' LOOP
        EXECUTE format('ALTER SEQUENCE public.%I OWNER TO itc_app', t.sequence_name);
    END LOOP;
    FOR t IN SELECT p.oid::regprocedure AS fn FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = 'public' AND p.proname LIKE 'it\_copilot\_%' LOOP
        EXECUTE format('ALTER FUNCTION %s OWNER TO itc_app', t.fn);
    END LOOP;
END $$;
