"""Score the blind set under today's C1 and candidate A, against PREREGISTRATION.md."""
import csv, sys
from pathlib import Path
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "backend"))
from coach import labeled_set
from coach.calibration import evaluate
from coach.checks.materiality import variance_of
from coach.jev.questions import build_question, build_state, question_fingerprint
from coach.jev.replay import load_recordings
from coach.models import Criterion

B = Path(__file__).resolve().parent
MODEL = "typesafe/jev-1.13-20260917"
rec = load_recordings(B / "blind_recordings.jsonl")
items = labeled_set.from_csv(B / "blind_labels.csv", ["C1"])
reason = {r["text_id"]: r["reason"] for r in csv.DictReader(open(B / "blind_labels.csv"))}
THRESHOLDS = {"current": 0.91, "a": 0.47}


def probs(name):
    crit = Criterion.from_sheet_row(next(csv.DictReader(open(B / "criteria_c1_{0}.csv".format(name)))))
    q = build_question(crit)
    out = {}
    for it in items:
        v = variance_of(it.line.prior, it.line.current, it.line.variance_usd, it.line.variance_pct)
        out[it.text_id] = rec[question_fingerprint(MODEL, build_state(it.line, v.usd, v.pct), q)].probability
    return out


def auc(p):
    P = [p[i.text_id] for i in items if i.labels["C1"] == "pass"]
    F = [p[i.text_id] for i in items if i.labels["C1"] == "fail"]
    return sum((a > b) + 0.5 * (a == b) for a in P for b in F) / (len(P) * len(F))


def at(p, t):
    passes = [i for i in items if i.labels["C1"] == "pass"]
    fails = [i for i in items if i.labels["C1"] == "fail"]
    kept = sum(1 for i in passes if p[i.text_id] >= t)
    caught = sum(1 for i in fails if p[i.text_id] < t)
    return {"bal_acc": (kept / len(passes) + caught / len(fails)) / 2, "pass_recall": kept / len(passes),
            "fails_caught": caught, "passes_kept": kept, "near": sum(1 for v in p.values() if abs(v - t) <= 0.05)}


P = {c: probs(c) for c in THRESHOLDS}
res = {c: dict(at(P[c], t), auc=auc(P[c]), t=t) for c, t in THRESHOLDS.items()}
for c, r in res.items():
    print("{0:>7} @ {t:.2f}: balanced accuracy {bal_acc:.3f}, AUC {auc:.3f}, fails caught {fails_caught}/18, "
          "passes kept {passes_kept}/18, within 0.05 of threshold {near}/36".format(c, **r))
for t in (0.43, 0.70):
    r = at(P["a"], t)
    print("  A sensitivity @ {0:.2f}: balanced accuracy {bal_acc:.3f}, fails caught {fails_caught}/18, passes kept {passes_kept}/18".format(t, **r))
r1 = res["a"]["bal_acc"] >= res["current"]["bal_acc"] + 0.05
r2 = res["a"]["auc"] >= res["current"]["auc"]
r3 = res["a"]["pass_recall"] >= 0.80
print("R1 balanced accuracy +0.05: {0}  R2 AUC: {1}  R3 pass recall >= 0.80: {2}".format(*["PASS" if x else "FAIL" for x in (r1, r2, r3)]))
print("DECISION: {0}".format("ship A (subject to R4)" if r1 and r2 and r3 else "keep today's C1"))
print("\nper text (label | today | A):")
for i in items:
    a, c = P["a"][i.text_id], P["current"][i.text_id]
    lab = i.labels["C1"]
    wrong = lambda p, t: ("pass" if p >= t else "fail") != lab
    print("  {0} {1:4} | {2:.2f}{3} | {4:.2f}{5} | {6}".format(i.text_id, lab, c, "*" if wrong(c, 0.91) else " ",
                                                            a, "*" if wrong(a, 0.47) else " ", reason[i.text_id][7:]))
print("(* = wrong at that wording's threshold)")
