-- Cached Jev answers at the precision Jev gave them.
--
-- jev_responses.probability was numeric(5, 4), so every cached answer came back
-- rounded to four decimals. A live answer of 0.89996 against a threshold of
-- 0.900 fails the line; the cached rerun of the same text read 0.9000 and
-- passed it. The column becomes double precision, which holds the float the
-- service received.
--
-- Rows written before this migration hold rounded values. The table is
-- append-only, so they stay where they are; they become precision generation 1
-- and the service reads only generation 2. The primary key widens to
-- (fingerprint, precision_generation) so a stale rounded row cannot block the
-- exact answer for the same question from being cached.

alter table jev_responses
    add column precision_generation smallint not null default 1 check (precision_generation >= 1);
alter table jev_responses alter column precision_generation set default 2;
alter table jev_responses alter column probability type double precision;
alter table jev_responses drop constraint jev_responses_pkey;
alter table jev_responses add primary key (fingerprint, precision_generation);

comment on column jev_responses.precision_generation is
    '1: probability was stored rounded to four decimals (before 0005), never read. 2: stored as received.';
