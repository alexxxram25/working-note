-- Deny by default.
--
-- 1. RLS on every table. The only policies are for coach_service; none exist
--    for Supabase's browser roles, so RLS gives them nothing.
-- 2. anon and authenticated hold no privilege on any table, sequence or
--    function here. Nothing in this project uses PostgREST: the service talks
--    to Postgres directly. Turning the Data API off in Supabase removes the
--    surface entirely; these revokes hold even if someone turns it back on.
-- 3. coach_service: an optional least-privilege login for the service. It can
--    insert into the append-only tables and read them, but not update, delete
--    or truncate them, and it cannot disable their triggers because it does
--    not own them. Created NOLOGIN; an admin gives it a password to use it.
--
-- Supabase-only roles are guarded with IF EXISTS so the same migration runs on
-- a plain Postgres for local tests.

do $$
declare
    t text;
begin
    foreach t in array array[
        'team_roles', 'sheet_keys', 'settings', 'lines', 'line_commentary',
        'criteria_snapshots', 'criteria_history', 'calibrations', 'runs',
        'run_locks', 'log', 'overrides', 'jev_responses', 'criteria_health'
    ] loop
        execute format('alter table %I enable row level security', t);
    end loop;
end;
$$;

do $$
declare
    r text;
begin
    foreach r in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = r) then
            execute format('revoke all on all tables in schema public from %I', r);
            execute format('revoke all on all sequences in schema public from %I', r);
            execute format('revoke all on all functions in schema public from %I', r);
            execute format('alter default privileges in schema public revoke all on tables from %I', r);
            execute format('alter default privileges in schema public revoke all on sequences from %I', r);
            execute format('alter default privileges in schema public revoke all on functions from %I', r);
        end if;
    end loop;
end;
$$;

-- Functions are executable by PUBLIC by default; this one only ever runs as a
-- trigger, so nobody needs to call it.
revoke all on function coach_forbid_mutation() from public;

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'coach_service') then
        create role coach_service nologin;
    end if;
end;
$$;

grant usage on schema public to coach_service;

grant select, insert on
    log, overrides, jev_responses, line_commentary,
    criteria_history, calibrations, criteria_snapshots
    to coach_service;
grant select, insert, update on lines, runs, criteria_health to coach_service;
grant select, insert, update, delete on run_locks to coach_service;
grant select on team_roles, settings to coach_service;
grant select on sheet_keys to coach_service;
grant update (last_used_at) on sheet_keys to coach_service;
grant usage, select on all sequences in schema public to coach_service;

do $$
declare
    t text;
begin
    foreach t in array array[
        'team_roles', 'sheet_keys', 'settings', 'lines', 'line_commentary',
        'criteria_snapshots', 'criteria_history', 'calibrations', 'runs',
        'run_locks', 'log', 'overrides', 'jev_responses', 'criteria_health'
    ] loop
        execute format('drop policy if exists coach_service_all on %I', t);
        execute format(
            'create policy coach_service_all on %I for all to coach_service using (true) with check (true)', t);
    end loop;
end;
$$;
