-- Flux Commentary Coach: core schema.
--
-- The service is the only writer. The sheet never talks to the database; it
-- talks to the service, which verifies who is calling before it reads or
-- writes anything here. Commentary text is stored (line_commentary) so a Log
-- hash can always be resolved to the words that were graded.
--
-- Every table is keyed by sheet_id as well as line_id: one deployment can serve
-- several copies of the sheet, and a Line ID is only unique within one.

create table if not exists team_roles (
    email         text primary key
                  check (email = lower(email) and position('@' in email) > 1),
    role          text not null check (role in ('preparer', 'reviewer')),
    display_name  text,
    active        boolean not null default true,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
comment on table team_roles is
    'Who may use the service and as what. Roles are enforced by the service, not just hidden in the menu.';

-- Fallback auth: per-sheet keys the service can scope, rate-limit and revoke.
-- Only a SHA-256 of the key is stored; the key itself is shown once at issue.
create table if not exists sheet_keys (
    key_id                 text primary key,
    key_hash               text not null unique,
    sheet_id               text not null,
    role_ceiling           text not null check (role_ceiling in ('preparer', 'reviewer')),
    label                  text,
    rate_limit_per_minute  integer not null default 30 check (rate_limit_per_minute > 0),
    created_at             timestamptz not null default now(),
    created_by             text,
    revoked_at             timestamptz,
    last_used_at           timestamptz
);

-- Deployment defaults live under sheet_id '*'. The sheet's Settings tab is the
-- working copy; each run snapshots the settings it used into runs.settings.
create table if not exists settings (
    sheet_id    text not null default '*',
    key         text not null,
    value       jsonb not null,
    updated_at  timestamptz not null default now(),
    primary key (sheet_id, key)
);

create table if not exists lines (
    sheet_id          text not null,
    line_id           text not null,
    entity            text not null,
    close_period      text not null,
    comparison_basis  text not null,
    account           text not null,
    slice             text,
    prior             numeric,
    current           numeric,
    variance_usd      numeric,
    variance_pct      numeric,
    first_seen_at     timestamptz not null default now(),
    first_seen_run    uuid,
    last_seen_at      timestamptz not null default now(),
    primary key (sheet_id, line_id)
);
create index if not exists lines_period_idx on lines (sheet_id, close_period);
comment on column lines.account is 'Display text when the line was first seen. Display only: results key on line_id.';

create table if not exists line_commentary (
    sheet_id         text not null,
    line_id          text not null,
    commentary_hash  text not null,
    commentary       text not null,
    first_seen_at    timestamptz not null default now(),
    first_seen_run   uuid,
    primary key (sheet_id, line_id, commentary_hash),
    foreign key (sheet_id, line_id) references lines (sheet_id, line_id)
);

create table if not exists criteria_snapshots (
    id               bigint generated always as identity primary key,
    sheet_id         text not null,
    content_hash     text not null,
    criteria         jsonb not null,
    captured_at      timestamptz not null default now(),
    captured_by      text,
    captured_in_run  uuid,
    unique (sheet_id, content_hash)
);
comment on table criteria_snapshots is
    'The whole Criteria tab, each time any criterion changes. sheet_id ''*'' holds the shipped defaults.';

create table if not exists criteria_history (
    id                       bigint generated always as identity primary key,
    sheet_id                 text not null,
    criterion_id             text not null,
    version                  integer not null check (version >= 1),
    content                  jsonb not null,
    content_hash             text not null,
    calibration_fingerprint  text,
    fields_changed           text[] not null default '{}',
    diff                     jsonb not null default '{}'::jsonb,
    calibration_impact       text not null,
    changed_by               text,
    detected_by              text,
    detected_in_run          uuid,
    snapshot_id              bigint references criteria_snapshots (id),
    created_at               timestamptz not null default now(),
    unique (sheet_id, criterion_id, version)
);

-- A criterion is calibrated only while a row here matches its current
-- fingerprint: served model snapshot + question-builder version + pass
-- condition + threshold. Editing feedback, rationale or the example touches
-- none of those four, so it never invalidates a calibration.
create table if not exists calibrations (
    id                        bigint generated always as identity primary key,
    criterion_id              text not null,
    fingerprint               text not null,
    model_snapshot            text not null,
    question_builder_version  text not null,
    passes_when_hash          text not null,
    threshold                 numeric(4, 3) not null check (threshold > 0 and threshold < 1),
    status                    text not null check (status in ('calibrated', 'uncalibrated')),
    metrics                   jsonb not null,
    labeled_set_hash          text not null,
    label_source              text not null check (label_source in ('synthetic', 'team')),
    created_at                timestamptz not null default now(),
    created_by                text
);
create index if not exists calibrations_lookup_idx
    on calibrations (criterion_id, fingerprint, created_at desc);

create table if not exists runs (
    run_id                    uuid primary key,
    sheet_id                  text not null,
    close_period              text,
    started_by                text,
    role                      text not null check (role in ('preparer', 'reviewer')),
    auth_method               text not null check (auth_method in ('google', 'sheet_key', 'local')),
    attribution_verified      boolean not null,
    started_at                timestamptz not null default now(),
    finished_at               timestamptz,
    status                    text not null check (status in ('running', 'completed', 'failed', 'expired')),
    settings                  jsonb not null,
    criteria_snapshot_id      bigint references criteria_snapshots (id),
    question_builder_version  text not null,
    model_pin                 text not null,
    jev_mode                  text not null,
    lines_received            integer not null default 0,
    lines_evaluated           integer not null default 0,
    jev_requests              integer not null default 0,
    stale_line_ids            text[] not null default '{}',
    written_count             integer,
    error                     text
);
create index if not exists runs_sheet_idx on runs (sheet_id, started_at desc);

-- One row per sheet while a run holds the write lease. Acquired atomically with
-- insert ... on conflict ... where expires_at < now(), so an abandoned lease
-- (the script died mid-run) frees itself instead of locking the sheet forever.
create table if not exists run_locks (
    sheet_id     text primary key,
    run_id       uuid not null,
    holder       text,
    acquired_at  timestamptz not null default now(),
    expires_at   timestamptz not null
);

-- The Log. Insert-only (0002 enforces it). Column order follows the spec's Log
-- tab, then the provenance columns that make each row explainable on its own.
create table if not exists log (
    id                        bigint generated always as identity primary key,
    ts                        timestamptz not null default now(),
    sheet_id                  text not null,
    close_period              text not null,
    line_id                   text not null,
    run_id                    uuid not null references runs (run_id),
    attempt_number            integer not null check (attempt_number >= 0),
    commentary_hash           text not null,
    criterion_id              text not null,
    result                    text not null check (result in
                                  ('pass', 'fail', 'abstain', 'skipped', 'missing', 'blocked', 'prompt', 'error')),
    jev_probability           numeric(5, 4) check (jev_probability between 0 and 1),
    criterion_version         integer,
    question_builder_version  text,
    model_version             text,
    calibration_status        text,
    jev_source                text check (jev_source in ('live', 'cache', 'replay')),
    threshold                 numeric(4, 3),
    detail                    jsonb
);
create index if not exists log_line_idx on log (sheet_id, line_id, id);
create index if not exists log_period_idx on log (sheet_id, close_period, criterion_id);
create index if not exists log_run_idx on log (run_id);

create table if not exists overrides (
    id                    bigint generated always as identity primary key,
    created_at            timestamptz not null default now(),
    sheet_id              text not null,
    close_period          text not null,
    line_id               text not null,
    kind                  text not null check (kind in ('flag_wrong', 'missed_issue')),
    criterion_id          text,
    not_covered           boolean not null default false,
    note                  text,
    note_hash             text not null,
    submitted_by          text,
    role                  text not null check (role in ('preparer', 'reviewer')),
    attribution_verified  boolean not null,
    matched_flag          boolean,
    check ((criterion_id is null) = not_covered),
    check (not (kind = 'flag_wrong' and not_covered)),
    -- Resubmitting the same override is a no-op, so the script can send every
    -- override on the tab each time without double counting.
    unique nulls not distinct (sheet_id, line_id, kind, criterion_id, not_covered, note_hash, submitted_by)
);
create index if not exists overrides_period_idx on overrides (sheet_id, close_period);

-- Per-question Jev answers keyed on the exact request fingerprint. Seeded from
-- the committed recordings; a rerun of unchanged commentary reads from here.
create table if not exists jev_responses (
    fingerprint     text primary key,
    model_snapshot  text not null,
    question_id     text not null,
    probability     numeric(5, 4) not null check (probability between 0 and 1),
    source          text not null check (source in ('live', 'recording')),
    input_tokens    integer,
    cost_usd        numeric(12, 8),
    created_at      timestamptz not null default now()
);

create table if not exists criteria_health (
    id             bigint generated always as identity primary key,
    health_run_id  uuid not null,
    sheet_id       text not null,
    close_window   text not null,
    criterion_id   text not null,
    signal         text not null check (signal in
                       ('never_fails', 'always_fails', 'near_threshold', 'flag_wrong',
                        'missed_issue', 'not_covered', 'stale')),
    evidence       jsonb not null,
    suggestion     text not null,
    generated_at   timestamptz not null default now(),
    generated_by   text,
    resolution     text check (resolution in ('edit', 'retire', 'keep')),
    resolved_by    text,
    resolved_at    timestamptz
);
create index if not exists criteria_health_sheet_idx on criteria_health (sheet_id, generated_at desc);
