-- Read-only views for analysts. The service computes the same numbers in
-- Python (coach/second_attempts.py) so it works without a database; a
-- conformance test holds the two to the same answer.
--
-- security_invoker: a view normally runs as its owner, which would hand back
-- exactly what RLS refuses the caller. With it, the caller's own grants and
-- policies apply.

-- One row per line per attempt that was actually checked (attempt 0 is "no
-- commentary yet" and never counts).
create or replace view line_attempts with (security_invoker = true) as
select
    l.sheet_id,
    l.close_period,
    l.line_id,
    l.attempt_number,
    min(l.ts)                                                          as first_checked_at,
    array_agg(distinct l.criterion_id) filter (where l.result = 'fail') as failed_criteria,
    bool_or(l.result = 'fail')                                          as any_fail
from log l
where l.attempt_number >= 1
group by l.sheet_id, l.close_period, l.line_id, l.attempt_number;

-- A second attempt counts when a line failed a criterion on attempt k and came
-- back as attempt k + 1, which only happens after its commentary changed.
create or replace view second_attempts with (security_invoker = true) as
select
    a.sheet_id,
    a.close_period,
    f.criterion_id,
    count(distinct a.line_id) as lines_with_second_attempt
from line_attempts a
cross join lateral unnest(a.failed_criteria) as f(criterion_id)
where exists (
    select 1 from line_attempts b
    where b.sheet_id = a.sheet_id
      and b.line_id = a.line_id
      and b.attempt_number = a.attempt_number + 1
)
group by a.sheet_id, a.close_period, f.criterion_id;

revoke all on line_attempts, second_attempts from public;
grant select on line_attempts, second_attempts to coach_service;

do $$
declare
    r text;
begin
    foreach r in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = r) then
            execute format('revoke all on line_attempts, second_attempts from %I', r);
        end if;
    end loop;
end;
$$;
