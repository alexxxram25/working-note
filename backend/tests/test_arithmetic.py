"""The arithmetic check, case by case.

Each row: commentary, variance ($), variance (%) as a fraction or None, prior,
current, expected result, expected reason prefix. Defaults: tolerance $1,000
or 1% of the variance, whichever is larger, plus the rounding each cited
amount implies.
"""

from __future__ import annotations

import pytest

from coach.checks.arithmetic import ABSTAIN, FAIL, PASS, check, parse_amounts

CASES = [
    # --- reconciles ---------------------------------------------------------
    ("single-amount", "Up $48K for the Orion Freight load.", 48000, 0.1, None, None, PASS, "reconciled"),
    ("total-plus-drivers", "Up $188K: Orion ($135K a month), Cobalt seats ($41K a month) and uplifts added $12K.",
     188000, 0.04, None, None, PASS, "reconciled"),
    ("drivers-only", "D&O up $9,000 a month and cyber up $4,500 a month.", 13500, 0.23, None, None, PASS, "reconciled"),
    ("full-dollars", "Increased by $1,485,000 due to the Orion invoice.", 1485000, 0.16, None, None, PASS, "reconciled"),
    ("millions-rounded", "Up $1.87M: Orion ($1.62M) and Cobalt ($0.25M).", 1872000, 0.27, None, None, PASS, "reconciled"),
    ("k-with-decimals", "Hosting rose $48.5K because Orion loaded data.", 48500, 0.12, None, None, PASS, "reconciled"),
    ("lowercase-k", "up $12k from renewals", 12000, 0.2, None, None, PASS, "reconciled"),
    ("usd-prefix", "The lease adds USD 25,000 a month.", 25000, 0.096, None, None, PASS, "reconciled"),
    ("us-dollar-prefix", "US$30K for the sales kickoff.", 30000, 0.3, None, None, PASS, "reconciled"),
    ("thousand-word", "$30 thousand for the kickoff in Denver.", 30000, 0.3, None, None, PASS, "reconciled"),
    ("million-word-precision", "$1.2 million from the Orion contract.", 1234567, 0.2, None, None, PASS, "reconciled"),
    ("mm-scale", "$1.5MM from the Orion contract.", 1500000, 0.2, None, None, PASS, "reconciled"),
    ("bn-scale", "A $1.1bn facility drawdown.", 1100000000, 0.5, None, None, PASS, "reconciled"),
    ("rounding-at-the-edge", "$1.2M from the Orion contract.", 1249000, 0.2, None, None, PASS, "reconciled"),
    ("decrease-with-down-words", "Down $45K: $13K of bonus taxes, and the wage base cut employer FICA by $32K.",
     -45000, -0.13, None, None, PASS, "reconciled"),
    ("per-unit-after", "Up $42K: three account executives at $14K a month each.", 42000, 0.08, None, None, PASS,
     "reconciled"),
    ("per-unit-before", "Down $25K after two quarters of cost per lead above $900.", -25000, -0.26, None, None, PASS,
     "reconciled"),
    ("percent-matches-variance", "Hosting went up $65K, or 17%, on June.", 65000, 0.17016, None, None, PASS,
     "reconciled"),
    ("percent-with-decimals", "Payroll taxes rose $34K, or 10.9%, versus June.", 34000, 0.10897, None, None, PASS,
     "reconciled"),
    ("balance-set-aside", "Trued up to H1 payouts ($48K); the accrual returns to $120K in August.",
     48000, 0.4, 120000, 168000, PASS, "reconciled"),
    ("both-balances-set-aside", "Down $109K: July carried the summit ($118K); August has only a $9K deposit.",
     -109000, -0.92, 118000, 9000, PASS, "reconciled"),
    ("driver-equal-to-balance-as-cited", "Down $24K: we recovered $18K and the reserve fell ($6K).",
     -24000, -4.0, 6000, -18000, PASS, "reconciled"),
    ("unexplained-counts", "Up $100K: Vendor A $70K; the other $30K is still unexplained.", 100000, 0.3, None, None,
     PASS, "reconciled"),
    ("total-and-same-driver", "Up $55K because the MS-3 milestone released $55K of revenue.", 55000, 0.29, None,
     None, PASS, "reconciled"),
    ("true-up-is-not-up", "July's one-time $48K true-up did not recur.", -48000, -0.29, None, None, PASS,
     "reconciled"),
    ("a-month-is-a-driver", "Two engineers joined at $34K a month.", 34000, 0.05, None, None, PASS, "reconciled"),
    ("per-month-is-a-driver", "Two engineers joined at $34K per month.", 34000, 0.05, None, None, PASS, "reconciled"),
    ("bare-numbers-ignored", "Taxes on the 8 hires from Jul 15 added $21K (PR-0715, Q3 2026).", 21000, 0.07, None,
     None, PASS, "reconciled"),
    ("parentheses-are-not-negative", "Vendor A ($80K) and Vendor B ($40K).", 120000, 0.5, None, None, PASS,
     "reconciled"),
    ("dates-ranges-ignored", "The backfill ran from Jul 8 to Jul 19 ($52K) and storage added $13K.", 65000, 0.17,
     None, None, PASS, "reconciled"),
    ("comparator-across-sentences", "A cost $27K. Against plan, B cost $13K.", 40000, 0.4, None, None, PASS,
     "reconciled"),
    ("exact-amounts-zero-tolerance", "$9,000 for D&O and $4,500 for cyber.", 13500, 0.23, None, None, PASS,
     "reconciled"),
    ("variance-only-amid-hours", "Down $22K as hours fell (186 hours against 410 in August).", -22000, -0.11,
     None, None, PASS, "reconciled"),
    # --- does not add up ----------------------------------------------------
    ("drivers-short", "Up $136K: legal ($64K), bonus ($48K) and booth costs ($14K).", 136000, 0.096, None, None,
     FAIL, "mismatch"),
    ("drivers-over", "Up $50K: vendor A $40K and vendor B $20K.", 50000, 0.2, None, None, FAIL, "mismatch"),
    ("single-partial", "Driven by the Vendor A renewal ($80K).", 120000, 0.2, None, None, FAIL, "mismatch"),
    ("wrong-total", "Up $90K on the Orion contract.", 120000, 0.2, None, None, FAIL, "mismatch"),
    ("rounding-not-enough", "$1.2M from the Orion contract.", 1300000, 0.2, None, None, FAIL, "mismatch"),
    ("exact-dollars-short", "$7,500 for D&O and $4,000 for cyber.", 13500, 0.23, None, None, FAIL, "mismatch"),
    ("near-miss-beyond-tolerance", "Vendor A $10K and vendor B $12.5K.", 25000, 0.2, None, None, FAIL, "mismatch"),
    ("overlapping-citations-are-ambiguous", "Vendor A $10K and vendor B $10.5K.", 25000, 0.2, None, None, ABSTAIN,
     "abstain:ambiguous"),
    # --- abstains -------------------------------------------------------------
    ("no-amounts", "Increase due to timing.", 28000, 0.43, None, None, ABSTAIN, "abstain:no_amounts"),
    ("range-dash", "Somewhere around $40K-$50K from the backfill.", 45000, 0.1, None, None, ABSTAIN, "abstain:range"),
    ("range-to", "Between estimates of $40K to $50K.", 45000, 0.1, None, None, ABSTAIN, "abstain:range"),
    ("range-en-dash", "Roughly $40–50K from the backfill.", 45000, 0.1, None, None, ABSTAIN, "abstain:range"),
    ("range-between", "Somewhere between $1M and $2M of exposure.", 1500000, 0.1, None, None, ABSTAIN,
     "abstain:range"),
    ("percent-rate", "Pinecrest placed four hires at a 20% fee ($61K) and Ardent billed $13K.", 74000, 1.947, None,
     None, ABSTAIN, "abstain:percentage"),
    ("percent-without-variance-pct", "Up 50% ($10K) on a new account.", 10000, None, 0, 10000, ABSTAIN,
     "abstain:percentage"),
    ("percent-word", "Rose 12 percent ($33K) at renewal.", 33000, 0.0075, None, None, ABSTAIN, "abstain:percentage"),
    ("offset", "Down $38K: backfill ended ($52K), partly offset by Orion ($14K).", -38000, -0.085, None, None,
     ABSTAIN, "abstain:offset"),
    ("net-of", "$50K net of credits from the vendor.", 50000, 0.2, None, None, ABSTAIN, "abstain:offset"),
    ("partly-reversed", "Accrual of $40K, partly reversed in the month.", 30000, 0.2, None, None, ABSTAIN,
     "abstain:offset"),
    ("explicit-negative", "A -$5K adjustment and $30K of new spend.", 25000, 0.2, None, None, ABSTAIN,
     "abstain:negative"),
    ("dollar-negative", "An adjustment of $-5K and $30K of new spend.", 25000, 0.2, None, None, ABSTAIN,
     "abstain:negative"),
    ("minus-word", "New spend of $30K minus $5K of credits.", 25000, 0.2, None, None, ABSTAIN, "abstain:negative"),
    ("versus", "Down $55K: billed $27K in August versus $82K in July.", -55000, -0.38, None, None, ABSTAIN,
     "abstain:comparison"),
    ("vs-dot", "Billed $27K vs. $82K last month.", -55000, -0.38, None, None, ABSTAIN, "abstain:comparison"),
    ("compared-to", "Billed $27K compared to $82K in July.", -55000, -0.38, None, None, ABSTAIN,
     "abstain:comparison"),
    ("pound-sign", "£12K for the London office, recurring.", 15000, 0.25, None, None, ABSTAIN,
     "abstain:currency"),
    ("gbp-code", "GBP 12,000 for the London office.", 15000, 0.25, None, None, ABSTAIN, "abstain:currency"),
    ("opposing-direction", "Down $20K: vendor A $14K lower, vendor B $10K higher and vendor C $16K lower.",
     -20000, -0.2, None, None, ABSTAIN, "abstain:direction"),
    ("added-on-a-decrease", "Down $43K: no milestone this month ($55K) and T&M work added $12K.", -43000, -0.18,
     None, None, ABSTAIN, "abstain:"),
    ("repeated-amounts", "Down $37K: Ardent ($13K) and Fenwick ($13K) ended, and the job board saved $11K.",
     -37000, -0.58, None, None, ABSTAIN, "abstain:ambiguous"),
    ("coverage-limit-context", "A $5M cyber layer adding $7,500 a month.", 7500, 0.105, 71500, 79000, ABSTAIN,
     "abstain:oversize"),
    ("capitalized-base-context", "Amortizing its $1.33M base over three years ($37K a month).", 37000, 0.8, 46000,
     83000, ABSTAIN, "abstain:oversize"),
    ("per-unit-only", "Three hires at $14K each.", 42000, 0.08, None, None, ABSTAIN, "abstain:no_amounts"),
    ("balance-only", "The accrual is $120K now.", 48000, 0.4, 120000, 168000, ABSTAIN, "abstain:no_amounts"),
]


@pytest.mark.parametrize(
    "text,variance,pct,prior,current,expected,reason",
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_arithmetic(text, variance, pct, prior, current, expected, reason):
    result = check(text, variance, pct, prior, current)
    assert result.result == expected, (result.reason, result.detail)
    assert result.reason.startswith(reason), (result.reason, result.detail)
    assert result.detail


def test_case_table_is_broad():
    assert len(CASES) >= 60
    outcomes = {c[6] for c in CASES}
    assert outcomes == {PASS, FAIL, ABSTAIN}


def test_zero_tolerance_still_allows_citation_rounding():
    result = check("$9,000 for D&O and $4,500 for cyber.", 13500, 0.23, None, None,
                   tolerance_usd=0, tolerance_pct=0)
    assert result.result == PASS


def test_tolerance_setting_widens_and_narrows():
    text = "Vendor A $40K and vendor B $57K."
    assert check(text, 100000, 0.1, None, None, tolerance_usd=1000).result == FAIL
    assert check(text, 100000, 0.1, None, None, tolerance_usd=4000).result == PASS


def test_parse_reports_precision_from_the_citation():
    amounts = {a.raw: a for a in parse_amounts("$1.2M, $48.5K, $7,500 and $12K")}
    assert amounts["$1.2M"].precision == pytest.approx(50000)
    assert amounts["$48.5K"].precision == pytest.approx(50)
    assert amounts["$7,500"].precision == pytest.approx(0.5)
    assert amounts["$12K"].precision == pytest.approx(500)


def test_failure_detail_states_the_sums_not_advice():
    result = check("Up $136K: legal ($64K), bonus ($48K) and booth costs ($14K).", 136000, 0.096, None, None)
    assert "$126,000" in result.detail and "$136,000" in result.detail
    assert "should" not in result.detail.lower()
