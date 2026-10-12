"""Persistence. MemoryStore for tests and local runs, PostgresStore for Supabase.

Both implement store.base.Store and are held to the same behaviour by one
conformance suite (tests/test_store_conformance.py), run against each.
"""
