#!/usr/bin/env python3
"""Verify the Coach schema's security posture. Read-only: every probe that
writes runs inside a transaction that is rolled back.

    python scripts/verify_db.py      # DATABASE_URL from the environment or backend/.env

DATABASE_URL is the owner's login. COACH_SERVICE_DATABASE_URL, when set, is
the login the deployed service uses (a member of coach_service), and probe 9
runs as it.

Catalog checks (what Postgres says is configured):
  1. every table exists and has RLS enabled;
  2. the only policies are coach_service's own; none name anon, authenticated
     or PUBLIC;
  3. anon and authenticated hold none of the eight table privileges on any
     table or view (MAINTAIN is probed: it only exists from PostgreSQL 17);
  4. every append-only table has its row and truncate triggers, enabled;
  5. coach_service may insert into the Log but not update, delete or truncate
     any append-only table;
  6. views run with security_invoker.

Probes (what Postgres actually does), each of which must trip:
  7. UPDATE, DELETE and TRUNCATE on log raise SQLSTATE 42501, as the
     connecting role (the table owner on Supabase and locally);
  8. as coach_service (when this login may assume it): adding a Log row works;
     disabling the Log's triggers, UPDATE, DELETE and TRUNCATE are refused;
  9. as the service's own login, if COACH_SERVICE_DATABASE_URL is set: it is
     a member of coach_service, not a superuser, can't bypass RLS, owns no
     table, and passes the same probes as 8. On Supabase, where the owner
     login can't assume coach_service, this is how 8 gets run.

A probe that does not raise is a failure, not a pass. The script prints its
own check count; the number is not written down anywhere else on purpose.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
from psycopg import errors

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from coach import envfile  # noqa: E402
from coach.dsn import DsnError, connect  # noqa: E402

TABLES = [
    "team_roles", "sheet_keys", "settings", "lines", "line_commentary",
    "criteria_snapshots", "criteria_history", "calibrations", "runs",
    "run_locks", "log", "overrides", "jev_responses", "criteria_health",
]
APPEND_ONLY = [
    "log", "overrides", "jev_responses", "line_commentary",
    "criteria_history", "calibrations", "criteria_snapshots",
]
VIEWS = ["line_attempts", "second_attempts"]
BROWSER_ROLES = ["anon", "authenticated"]
PRIVILEGES = ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "MAINTAIN"]

checks = 0
failures = []
skipped = []


def check(ok: bool, label: str) -> None:
    global checks
    checks += 1
    if not ok:
        failures.append(label)
        print("  FAIL  " + label)


def service_probes(cur, who: str) -> None:
    """What the service's role must be able to do to the Log, and what it must not."""
    run_id = str(uuid.uuid4())
    cur.execute("savepoint service_insert")
    try:
        cur.execute(
            "insert into runs (run_id, sheet_id, role, auth_method, attribution_verified, status,"
            " settings, question_builder_version, model_pin, jev_mode)"
            " values (%s, 'verify-db-probe', 'reviewer', 'local', false, 'running', '{}',"
            " 'probe', 'probe', 'probe')", (run_id,))
        cur.execute(
            "insert into log (sheet_id, close_period, line_id, run_id, attempt_number,"
            " commentary_hash, criterion_id, result)"
            " values ('verify-db-probe', '2000-01', 'probe|2000-01|MoM|0', %s, 1, 'sha256:0', 'C1', 'pass')",
            (run_id,))
        check(True, who + " can add a run and a Log row")
    except psycopg.Error as exc:
        check(False, "{0} could not add a Log row ({1})".format(who, exc.sqlstate))
    cur.execute("rollback to savepoint service_insert")
    refusals = {
        "disable the Log's triggers": "alter table log disable trigger log_append_only_rows",
        "UPDATE on log": "update log set result = 'fail' where run_id = %s",
        "DELETE on log": "delete from log where run_id = %s",
        "TRUNCATE on log": "truncate log",
    }
    for label, statement in refusals.items():
        cur.execute("savepoint service_probe")
        try:
            cur.execute(statement, (run_id,) if "%s" in statement else None)
        except errors.InsufficientPrivilege:
            check(True, "{0}: {1} refused".format(who, label))
        except psycopg.Error as exc:
            check(False, "{0}: {1} refused with {2} instead of 42501".format(who, label, exc.sqlstate))
        else:
            check(False, "{0}: {1} was ALLOWED".format(who, label))
        cur.execute("rollback to savepoint service_probe")


def main() -> int:
    envfile.load()
    dsn = os.environ.get("DATABASE_URL", "").strip()
    service_dsn = os.environ.get("COACH_SERVICE_DATABASE_URL", "").strip()
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    with connect(dsn, prepare_threshold=None, autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute("show server_version_num")
        version = int(cur.fetchone()[0])
        privileges = [p for p in PRIVILEGES if p != "MAINTAIN" or version >= 170000]
        if version < 170000:
            skipped.append("MAINTAIN privilege (server older than PostgreSQL 17)")

        # 1. tables and RLS
        cur.execute(
            "select c.relname, c.relrowsecurity from pg_class c"
            " join pg_namespace n on n.oid = c.relnamespace"
            " where n.nspname = 'public' and c.relkind = 'r'"
        )
        rls = dict(cur.fetchall())
        for table in TABLES:
            check(table in rls, "table {0} exists".format(table))
            check(bool(rls.get(table)), "RLS enabled on {0}".format(table))

        # 2. policies
        cur.execute(
            "select tablename, policyname, roles::text[] from pg_policies where schemaname = 'public'"
        )
        policies = cur.fetchall()
        for table, name, roles in policies:
            check(name == "coach_service_all" and list(roles) == ["coach_service"],
                  "policy {0}.{1} is coach_service-only (roles={2})".format(table, name, roles))
        covered = {t for t, _, _ in policies}
        for table in TABLES:
            check(table in covered, "coach_service policy present on {0}".format(table))

        # 3. browser roles hold nothing
        cur.execute("select rolname from pg_roles where rolname = any(%s)", (BROWSER_ROLES,))
        present = [r for (r,) in cur.fetchall()]
        if not present:
            skipped.append("anon/authenticated privilege checks (roles absent: not Supabase, no shim)")
        for role in present:
            for relation in TABLES + VIEWS:
                for privilege in privileges:
                    cur.execute("select has_table_privilege(%s, %s, %s)",
                                (role, "public." + relation, privilege))
                    check(not cur.fetchone()[0],
                          "{0} lacks {1} on {2}".format(role, privilege, relation))

        # 4. append-only triggers
        cur.execute(
            "select c.relname, t.tgname, t.tgenabled from pg_trigger t"
            " join pg_class c on c.oid = t.tgrelid where not t.tgisinternal"
        )
        triggers = {(rel, name): enabled for rel, name, enabled in cur.fetchall()}
        for table in APPEND_ONLY:
            for suffix in ("_append_only_rows", "_append_only_truncate"):
                key = (table, table + suffix)
                check(key in triggers and triggers[key] == "O",
                      "trigger {0}{1} present and enabled".format(table, suffix))

        # 5. coach_service privileges
        cur.execute("select 1 from pg_roles where rolname = 'coach_service'")
        has_service_role = cur.fetchone() is not None
        check(has_service_role, "coach_service role exists")
        if has_service_role:
            cur.execute("select has_table_privilege('coach_service', 'public.log', 'INSERT')")
            check(cur.fetchone()[0], "coach_service may insert into log")
            for table in APPEND_ONLY:
                for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
                    cur.execute("select has_table_privilege('coach_service', %s, %s)",
                                ("public." + table, privilege))
                    check(not cur.fetchone()[0],
                          "coach_service lacks {0} on {1}".format(privilege, table))

        # 6. views run as the invoker
        cur.execute(
            "select c.relname, coalesce(c.reloptions, '{}') from pg_class c"
            " join pg_namespace n on n.oid = c.relnamespace"
            " where n.nspname = 'public' and c.relkind = 'v'"
        )
        options = dict(cur.fetchall())
        for view in VIEWS:
            check("security_invoker=true" in (options.get(view) or []),
                  "view {0} uses security_invoker".format(view))

    # 7 and 8 need transactions.
    with connect(dsn, prepare_threshold=None, autocommit=False) as conn:
        cur = conn.cursor()
        run_id = str(uuid.uuid4())
        cur.execute(
            "insert into runs (run_id, sheet_id, role, auth_method, attribution_verified, status,"
            " settings, question_builder_version, model_pin, jev_mode)"
            " values (%s, 'verify-db-probe', 'reviewer', 'local', false, 'running', '{}',"
            " 'probe', 'probe', 'probe')",
            (run_id,),
        )
        cur.execute(
            "insert into log (sheet_id, close_period, line_id, run_id, attempt_number,"
            " commentary_hash, criterion_id, result)"
            " values ('verify-db-probe', '2000-01', 'probe|2000-01|MoM|0', %s, 1, 'sha256:0',"
            " 'C1', 'pass')",
            (run_id,),
        )
        probes = {
            "UPDATE on log": "update log set result = 'fail' where run_id = %s",
            "DELETE on log": "delete from log where run_id = %s",
            "TRUNCATE on log": "truncate log",
        }
        for label, statement in probes.items():
            cur.execute("savepoint probe")
            try:
                cur.execute(statement, (run_id,) if "%s" in statement else None)
            except errors.InsufficientPrivilege:
                check(True, label + " refused with 42501")
            except psycopg.Error as exc:  # refused, but for the wrong reason
                check(False, "{0} refused with {1} instead of 42501".format(label, exc.sqlstate))
            else:
                check(False, label + " was ALLOWED")
            cur.execute("rollback to savepoint probe")

        cur.execute("savepoint as_service")
        try:
            cur.execute("set local role coach_service")
        except psycopg.Error:
            cur.execute("rollback to savepoint as_service")
            if not service_dsn:
                skipped.append("coach_service probes (this login cannot assume coach_service; set "
                               "COACH_SERVICE_DATABASE_URL to run them as the service's own login)")
        else:
            service_probes(cur, "coach_service")
            cur.execute("rollback to savepoint as_service")
        conn.rollback()

    if service_dsn:
        # 9. The login the deployed service uses: what it is, and what it can't do to the Log.
        with connect(service_dsn, prepare_threshold=None, autocommit=False) as conn:
            cur = conn.cursor()
            cur.execute(
                "select current_user, r.rolsuper, r.rolbypassrls,"
                " pg_has_role(current_user, 'coach_service', 'USAGE'),"
                " exists (select 1 from pg_tables where schemaname = 'public' and tableowner = current_user)"
                " from pg_roles r where r.rolname = current_user")
            user, superuser, bypassrls, member, owns = cur.fetchone()
            check(member, "the service login is a member of coach_service")
            check(not superuser, "the service login is not a superuser")
            check(not bypassrls, "the service login cannot bypass row-level security")
            check(not owns, "the service login owns no table")
            service_probes(cur, "the service login")
            conn.rollback()
        print("RAN: Log probes as the service login ({0})".format(user))

    for item in skipped:
        print("  SKIP  " + item)
    if failures:
        print("verify_db: {0} of {1} checks FAILED".format(len(failures), checks))
        return 1
    print("RAN: verify_db {0}/{0} checks passed, {1} skipped".format(checks, len(skipped)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DsnError as exc:  # already worded without the connection string
        print(exc, file=sys.stderr)
        sys.exit(2)
