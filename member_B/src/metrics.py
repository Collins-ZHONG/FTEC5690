"""Time-weighted and money-weighted return measures.

Two measures, two questions:

* **WAL** (weighted average life) answers *when* the principal comes back. It is
  the principal-weighted average time from the closing date to each repayment,
  in years on an ACT/365 basis. It ignores interest entirely, which is the point:
  it is a duration statement, not a return statement.
* **IRR** answers *what annual return* an investor earns at a stated purchase
  price. With no prepayment penalty the timing of principal is the whole story,
  which is why the two measures always travel together.

The purchase price must be stated explicitly - the same cash flows are a very
different IRR at par and at 99.98. ``PRICE_BASES`` fixes the three conventions
used in the report.

Sign convention: cash flows are negative at purchase and positive on receipt;
``irr`` returns the annual rate that zeroes the NPV using ACT/365 exponents,
which matches the basis used for the WAL and for the note interest accrual.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

DAYS_PER_YEAR = 365.0

# Three price conventions, so the report can never be vague about "the" IRR.
PRICE_BASES = {
    "issue": "primary market: the issue price on the cover of the prospectus",
    "par": "face value 100, used to compare scenarios on the same footing",
    "secondary": "assumed secondary-market price (default par, stated when used)",
}


def day_fraction(start: str | pd.Timestamp, end: str | pd.Timestamp) -> float:
    return (pd.Timestamp(end) - pd.Timestamp(start)).days / DAYS_PER_YEAR


def weighted_average_life(principal: np.ndarray, dates, start: str | pd.Timestamp) -> float:
    """Principal-weighted average repayment time in years from ``start``."""
    principal = np.asarray(principal, dtype=float)
    total = principal.sum()
    if total <= 0:
        return float("nan")
    times = np.array([day_fraction(start, d) for d in dates])
    return float((principal / total * times).sum())


def irr(flows, dates, start: str | pd.Timestamp, tol: float = 1e-10) -> float:
    """Annual IRR on an ACT/365 basis; returns NaN when no sign change exists.

    ``flows`` must be signed: negative on the purchase date, positive on receipt.
    Bisection is used rather than Newton because the flow pattern is unusual
    (a single outflow followed by many inflows plus a large redemption on the
    clean-up call), and bisection cannot diverge.
    """
    flows = np.asarray(flows, dtype=float)
    if flows.size == 0 or not (np.any(flows > 0) and np.any(flows < 0)):
        return float("nan")
    times = np.array([day_fraction(start, d) for d in dates])

    def npv(rate: float) -> float:
        return float(np.sum(flows / (1.0 + rate) ** times))

    lo, hi = -0.99, 10.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return float("nan")
    for _ in range(300):
        mid = 0.5 * (lo + hi)
        f_mid = npv(mid)
        if f_mid == 0 or hi - lo < tol:
            return 0.5 * (lo + hi)
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)


@dataclass
class TrancheReturns:
    tranche: str
    original_balance: float
    price: float
    wal_years: float
    irr: float
    total_interest: float
    total_principal: float
    final_balance: float
    last_payment: str | None


def tranche_returns(
    tranche_flows: pd.DataFrame,
    tranches,
    panel: pd.DataFrame,
    *,
    start: str = "2024-06-18",
    price_basis: str = "issue",
    purchase_prices: dict[str, float] | None = None,
) -> pd.DataFrame:
    """WAL and IRR per tranche from a waterfall's per-tranche cash flows.

    A class that is still outstanding at the end of the panel has no IRR: its
    remaining principal is a future cash flow that only a projection can supply.
    Those rows are returned with ``irr = NaN`` and ``final_balance > 0`` so they
    cannot be mistaken for a complete result.
    """
    payment_date = dict(zip(panel["period"].astype(str), pd.to_datetime(panel["payment_date"])))
    flow = tranche_flows.copy()
    flow["date"] = flow["period"].map(payment_date)
    rows = []
    for t in tranches:
        sub = flow[flow["tranche"] == t.name].sort_values("date")
        if sub.empty:
            continue
        principal = sub["principal_paid"].to_numpy(dtype=float)
        wal = weighted_average_life(principal, sub["date"].tolist(), start)
        price = (
            t.issue_price
            if price_basis == "issue"
            else (1.0 if price_basis == "par" else float((purchase_prices or {}).get(t.name, 1.0)))
        )
        amounts = (sub["principal_paid"] + sub["interest_paid"]).to_numpy(dtype=float)
        dates_list = [pd.Timestamp(start)] + sub["date"].tolist()
        flows = [-price * t.original_balance] + list(amounts)
        final_balance = float(sub["closing_balance"].iloc[-1])
        paid = sub.loc[sub["principal_paid"] > 0, "period"]
        rows.append(
            TrancheReturns(
                tranche=t.name,
                original_balance=t.original_balance,
                price=price,
                wal_years=wal,
                irr=float("nan") if final_balance > 0.005 else irr(flows, dates_list, start),
                total_interest=float(sub["interest_paid"].sum()),
                total_principal=float(sub["principal_paid"].sum()),
                final_balance=final_balance,
                last_payment=str(paid.iloc[-1]) if len(paid) else None,
            )
        )
    return pd.DataFrame([r.__dict__ for r in rows])


def clear_month(principal: np.ndarray, periods) -> str | None:
    """First period in which principal is fully repaid, else None."""
    principal = np.asarray(principal, dtype=float)
    cum = np.cumsum(principal)
    if cum.size == 0 or cum[-1] <= 0:
        return None
    return str(list(periods)[int(np.argmax(cum >= cum[-1] - 0.005))])
