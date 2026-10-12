-- Local tests only. Never apply to Supabase.
--
-- Supabase ships these roles; a plain Postgres does not. Creating them locally
-- means the revokes in 0003 and the privilege checks in verify_db.py run
-- against something real instead of skipping. Supabase grants anon and
-- authenticated broad default privileges on public; the defaults below mimic
-- that so the revokes have something to remove.

do $$
declare
    r text;
begin
    foreach r in array array['anon', 'authenticated', 'service_role'] loop
        if not exists (select 1 from pg_roles where rolname = r) then
            execute format('create role %I nologin', r);
        end if;
    end loop;
end;
$$;

grant usage on schema public to anon, authenticated, service_role;
alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;
alter default privileges in schema public grant all on functions to anon, authenticated, service_role;
