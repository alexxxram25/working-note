-- Append-only tables.
--
-- A trigger refuses UPDATE and DELETE row by row and TRUNCATE per statement,
-- for every role, the table owner included. Revokes say the same thing in the
-- privilege system. The trigger is what makes it hold for the owner; the
-- least-privilege coach_service role (0003) is what stops anyone from simply
-- disabling the trigger, since that takes ownership.
--
-- SQLSTATE 42501 (insufficient_privilege) so callers can tell "you may not do
-- this here" apart from a bug.

create or replace function coach_forbid_mutation() returns trigger
language plpgsql as $$
begin
    raise exception 'table "%" is append-only: % is not allowed', tg_table_name, tg_op
        using errcode = 'insufficient_privilege';
end;
$$;

do $$
declare
    t text;
begin
    foreach t in array array[
        'log', 'overrides', 'jev_responses', 'line_commentary',
        'criteria_history', 'calibrations', 'criteria_snapshots'
    ] loop
        execute format('drop trigger if exists %I on %I', t || '_append_only_rows', t);
        execute format(
            'create trigger %I before update or delete on %I '
            'for each row execute function coach_forbid_mutation()',
            t || '_append_only_rows', t);
        execute format('drop trigger if exists %I on %I', t || '_append_only_truncate', t);
        execute format(
            'create trigger %I before truncate on %I '
            'for each statement execute function coach_forbid_mutation()',
            t || '_append_only_truncate', t);
        execute format('revoke update, delete, truncate on %I from public', t);
    end loop;
end;
$$;
