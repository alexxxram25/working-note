"""Working Note engine (the Coach service).

The Coach checks whether flux commentary is reviewable against the team's own
stated standard. It never writes the explanation, never writes the feedback a
preparer sees (that text comes verbatim from the Criteria tab), and never
decides whether the business explanation is true. Everything in this package
is organised around keeping those three promises checkable in code.
"""

__version__ = "0.1.0"
