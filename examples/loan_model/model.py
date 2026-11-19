"""A small loan-loss model of the kind an AI assistant might write when asked.

It looks reasonable and runs without errors, but it has one planted bug: `monthly_payment`
uses the annual interest rate as if it were monthly. The README shows how recording the run
with mydbg exposes it in a minute, and how c.check() confirms the fix changed nothing else.

Run it:       python model.py
Record it:    python ../../mydbg.py record model.py
"""
from helpers import expected_loss, stress_pd

LOANS = [
    {"id": "A1", "balance": 250_000, "rate": 0.055, "years": 25, "pd": 0.010, "lgd": 0.20},
    {"id": "B7", "balance": 40_000, "rate": 0.089, "years": 5, "pd": 0.035, "lgd": 0.60},
    {"id": "C3", "balance": 120_000, "rate": 0.062, "years": 15, "pd": 0.018, "lgd": 0.35},
]
SCENARIO = {"name": "severe", "pd_multiplier": 2.5}


def monthly_payment(balance, annual_rate, years):
    """Level payment of an amortizing loan."""
    r = annual_rate                      # BUG: should be annual_rate / 12
    n = years * 12
    return balance * r / (1 - (1 + r) ** -n)


def portfolio_report(loans, scenario):
    rows = []
    for loan in loans:
        pay = monthly_payment(loan["balance"], loan["rate"], loan["years"])
        pd_s = stress_pd(loan["pd"], scenario["pd_multiplier"])
        el = expected_loss(loan["balance"], pd_s, loan["lgd"])
        rows.append({"id": loan["id"], "payment": round(pay, 2), "pd_stressed": pd_s, "expected_loss": round(el, 2)})
    return rows


if __name__ == "__main__":
    report = portfolio_report(LOANS, SCENARIO)
    for row in report:
        print(row)
    print("total expected loss:", round(sum(r["expected_loss"] for r in report), 2))
