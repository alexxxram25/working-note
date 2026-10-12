# Working Note: build and test targets.
#
# Recipes are indented with real tabs (macOS ships GNU Make 3.81, which has no
# .RECIPEPREFIX). Every verification target prints a RAN: marker, because a
# check that quietly did nothing looks exactly like a check that passed.

PY        := $(CURDIR)/.venv/bin/python
PYTEST    := $(CURDIR)/.venv/bin/pytest
NODE      ?= node
# Any of these can be set on the command line, e.g. make venv PYTHON=/path/to/python3.11
PYTHON    ?= $(shell command -v python3.11 2>/dev/null)
# Python 3.9, the oldest the service supports: macOS's system interpreter is 3.9.
PY39      ?= /usr/bin/python3
# Postgres 17's binaries: Homebrew's postgresql@17 if it's there, otherwise pg_config's.
PG_BIN    ?= $(shell p=$$(brew --prefix postgresql@17 2>/dev/null); \
               if [ -x "$$p/bin/initdb" ]; then echo "$$p/bin"; else pg_config --bindir 2>/dev/null; fi)
PGDATA    ?= $(CURDIR)/.pgdata
PGPORT    ?= 54329
TEST_DSN  := postgresql://postgres@localhost:$(PGPORT)/coach_test

.PHONY: help venv seed test test-all test-39 test-js test-e2e \
        db-up db-down db-reset db-test lock verify

help:
	@echo "make venv | test | test-all | test-39 | db-up | db-test | db-down | test-js | test-e2e"
	@echo "     seed | lock | verify"

venv:
	@test -n "$(PYTHON)" || (echo "Python 3.11 is missing: install it, or run make venv PYTHON=/path/to/python3.11" && exit 1)
	$(PYTHON) -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r backend/requirements-dev.txt
	$(PY) -m pip install -q -e backend
	@echo "RAN: venv ready"

# db/seed.sql from the starter Criteria and the Settings defaults ------------------

seed:
	$(PY) scripts/write_seed.py

# Python tests -------------------------------------------------------------------

# Fast: everything that needs no database. Postgres tests report as skipped.
test:
	cd backend && $(PYTEST) -q

# The gate: the whole suite against a fresh local Postgres, with the coverage floor.
test-all: db-reset
	cd backend && COACH_TEST_DSN=$(TEST_DSN) $(PYTEST) -q --cov=coach --cov-fail-under=90

# Compatibility run on Python 3.9, non-DB tests only. Says SKIPPED, loudly, when there is
# no 3.9 with pytest to run it on, rather than passing without having run.
test-39:
	@if $(PY39) -c 'import sys, pytest; sys.exit(sys.version_info[:2] != (3, 9))' 2>/dev/null; then \
	  cd backend && $(PY39) -m pytest -q -m "not postgres and not e2e and not live" -p no:cacheprovider; \
	else \
	  echo "SKIPPED: test-39 needs Python 3.9 with the dev requirements; set PY39=/path/to/python3.9"; \
	fi

# Local Postgres 17 (throwaway cluster in .pgdata, localhost only) ---------------

# TCP on localhost only, and no Unix socket: its path would sit inside .pgdata, and a clone
# in a deep folder makes that longer than macOS allows (103 bytes).
db-up:
	@test -x "$(PG_BIN)/initdb" || (echo "Postgres 17 is missing: brew install postgresql@17, or set PG_BIN to the folder with initdb" && exit 1)
	@if [ ! -d $(PGDATA) ]; then $(PG_BIN)/initdb -D $(PGDATA) -U postgres --auth=trust -E UTF8 >/dev/null; fi
	@LC_ALL=en_US.UTF-8 $(PG_BIN)/pg_ctl -D $(PGDATA) -o "-p $(PGPORT) -k '' -c listen_addresses=localhost" -l $(PGDATA)/server.log -w status >/dev/null 2>&1 || \
	  LC_ALL=en_US.UTF-8 $(PG_BIN)/pg_ctl -D $(PGDATA) -o "-p $(PGPORT) -k '' -c listen_addresses=localhost" -l $(PGDATA)/server.log -w start >/dev/null
	@echo "RAN: postgres up on localhost:$(PGPORT)"

db-down:
	@$(PG_BIN)/pg_ctl -D $(PGDATA) -m fast -w stop >/dev/null 2>&1 || true
	@echo "RAN: postgres stopped"

# Drop and recreate the test database, add the Supabase role shim, migrate, seed.
db-reset: db-up
	@$(PG_BIN)/dropdb -h localhost -p $(PGPORT) -U postgres --if-exists coach_test
	@$(PG_BIN)/createdb -h localhost -p $(PGPORT) -U postgres coach_test
	@$(PG_BIN)/psql -q -v ON_ERROR_STOP=1 -h localhost -p $(PGPORT) -U postgres -d coach_test -f db/local_supabase_shim.sql
	DATABASE_URL=$(TEST_DSN) $(PY) scripts/apply_migrations.py --seed
	@echo "RAN: coach_test reset, migrated and seeded"

db-test: db-reset
	COACH_ENV_FILE=none DATABASE_URL=$(TEST_DSN) $(PY) scripts/verify_db.py
	cd backend && COACH_TEST_DSN=$(TEST_DSN) $(PYTEST) -q -m postgres

# Apps Script ----------------------------------------------------------------------

test-js:
	$(NODE) --test apps_script/test/unit.test.mjs

# The Apps Script driven over HTTP, and the production shape on Postgres.
test-e2e: db-up
	cd backend && COACH_TEST_DSN=$(TEST_DSN) $(PYTEST) -q -m e2e -s

# Lockfile ---------------------------------------------------------------------------

lock:
	cd backend && $(CURDIR)/.venv/bin/pip-compile --quiet --generate-hashes --allow-unsafe -o requirements.lock requirements.txt
	@echo "RAN: requirements.lock regenerated"

# Everything, in dependency order. Stops at the first failure.
verify: db-test test-all test-39 test-js test-e2e
	@echo "RAN: verify complete"
