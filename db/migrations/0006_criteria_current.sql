-- Which snapshot each sheet's Criteria tab is on right now.
--
-- criteria_snapshots is append-only and deduplicated by content: a tab that
-- goes A -> B -> A -> B gets two rows, not four. Reading "current" as the
-- highest id said B while the sheet showed A on the third run, and saw no
-- change at all on the fourth, which then recorded nothing. This table names
-- the current snapshot explicitly and is updated by every run that records
-- criteria. It is the one piece of criteria state that is meant to change.

create table if not exists criteria_current (
    sheet_id        text primary key,
    snapshot_id     bigint not null references criteria_snapshots (id),
    content_hash    text not null,
    updated_at      timestamptz not null default now(),
    updated_in_run  uuid
);
comment on table criteria_current is
    'The snapshot each sheet''s Criteria tab currently matches. criteria_snapshots keeps every version ever seen.';

-- Backfill with what the service read as current until now: the highest id per
-- sheet. The shipped defaults (sheet_id '*') are not a sheet.
insert into criteria_current (sheet_id, snapshot_id, content_hash)
select distinct on (sheet_id) sheet_id, id, content_hash
  from criteria_snapshots
 where sheet_id <> '*'
 order by sheet_id, id desc
on conflict (sheet_id) do nothing;

-- The same posture as 0003: RLS on, coach_service only, nothing for the browser roles.
alter table criteria_current enable row level security;
grant select, insert, update on criteria_current to coach_service;
drop policy if exists coach_service_all on criteria_current;
create policy coach_service_all on criteria_current for all to coach_service using (true) with check (true);

do $$
declare
    r text;
begin
    foreach r in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = r) then
            execute format('revoke all on criteria_current from %I', r);
        end if;
    end loop;
end;
$$;
