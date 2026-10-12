-- The cache column default goes back to generation 1.
--
-- 0005 set precision_generation's default to 2, the generation the new service
-- reads. A service built before 0005 omits the column and inserts the
-- probability rounded to four decimals, so between the migration and the
-- deployment every such row landed in generation 2 looking exact, and a
-- cached rerun could pass a line the live run had failed. The default is now
-- 1: anything that does not name its generation is legacy precision. The
-- service names its generation explicitly on every write (coach/store/postgres.py),
-- and it reads a generation that was never a column default.
alter table jev_responses alter column precision_generation set default 1;

comment on column jev_responses.precision_generation is
    '1: legacy or unnamed precision (rounded to four decimals), never read. '
    '2: was the column default between 0005 and 0007, so may hold rounded legacy writes; never read. '
    '3: written by the service with the probability as received.';
