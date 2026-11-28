"""Load and normalise role A's tables into one monthly panel (Day 1, task B1.1).

Design notes
------------
* Inputs are read-only from ``member_A/data``; nothing here writes to that tree.
* Every derived quantity carries a comment saying which disclosed column it comes
  from, so ``docs/assumptions.md`` can point at a single place.
* Decimal source values are read as float. That is safe at this scale: the largest
  figure is 1.9e9, so float64 keeps ~1e-7 dollars of resolution, far below the
  two-cent disclosure rounding that member A already tolerates.
* The ``collection_index`` mapping is the one non-obvious piece. Member A's
  periods are Deere fiscal collection periods that do NOT align with calendar
  months (they tile contiguously, 28 to 35 days each), while
  ``OriginalSchedule`` is a calendar-month contract schedule. We therefore
  advance through the schedule by date: the number of schedule months that have
  already *started* when a period's collection window opens. Four candidate
  rules (month ended before start, month started before start, ``k+1``
  heuristic, month ended on/before start) all reproduce the Note Value path
  within 0.02pp of each other, so the choice is not load-bearing; we use the
  date rule because it is derived rather than assumed.
"""
from __future__ import annotations

import calendar
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "member_A" / "data"
REFERENCE = ROOT / "member_B" / "reference"

TRANCHE_ORDER = ("A-1", "A-2A", "A-2B", "A-3", "A-4")

# Schedule C (Sale and Servicing Agreement) labels fiscal months by name, and the
# fiscal year rolls over in November. The label - not the cut-off month - names the
# collection month: the October fiscal period of FY2026 ends on 1-Nov-26.
_LABEL_MONTH = {
    "november": 11, "december": 12, "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
}

_POOL_COLS_MONEY = (
    "pool_balance note_value overcollateralization prepayment prepayment_cumulative "
    "net_loss net_loss_cumulative total_distribution cash_receipts repurchases investment_income "
    "buyback administration_fee review_fee_priority2 note_interest note_principal "
    "principal_shortfall interest_shortfall reserve_start reserve_deposit reserve_to_notes "
    "reserve_to_certificate reserve_end reserve_target servicing_fee_paid indenture_trustee_fee "
    "owner_trustee_fee review_fee_priority9 certificate_payment pool_balance_start "
    "note_value_start gross_losses accrued_loss_change recoveries_signed"
).split()

_POOL_COLS_RATE = "sofr a2b_rate weighted_apr net_loss_cumulative_ratio delinquency_60plus delinquency_trigger".split()


def _read(name: str) -> pd.DataFrame:
    path = DATA / f"{name}.csv"
    if not path.exists():
        raise SystemExit(f"missing input table: {path}")
    return pd.read_csv(path, encoding="utf-8-sig")


def month_end(ym: str) -> date:
    year, month = (int(v) for v in ym.split("-"))
    return date(year, month, calendar.monthrange(year, month)[1])


def month_start(ym: str) -> date:
    year, month = (int(v) for v in ym.split("-"))
    return date(year, month, 1)


def load_package() -> dict:
    """Read every table role A shipped and return a plain dict of frames."""
    pool = _read("PoolPerformance")
    note_cf = _read("NoteCashflows")
    delinquency = _read("Delinquency")
    notes = _read("Notes")
    schedule = _read("OriginalSchedule")
    composition = _read("PoolComposition")
    initial = _read("InitialPool")
    validation = _read("Validation")
    term_map = _read("TermMapping")

    for col in _POOL_COLS_MONEY:
        pool[col] = pd.to_numeric(pool[col], errors="raise")
    for col in _POOL_COLS_RATE:
        pool[col] = pd.to_numeric(pool[col], errors="raise")

    for col in ("opening_balance", "principal_paid", "interest_paid", "closing_balance", "coupon_rate"):
        note_cf[col] = pd.to_numeric(note_cf[col], errors="raise")

    schedule["gross_scheduled_usd"] = pd.to_numeric(schedule["gross_scheduled_usd"], errors="raise")

    initial_fields = dict(zip(initial["item"].astype(str), initial["value"]))
    initial_num = {}
    for key, value in initial_fields.items():
        try:
            initial_num[key] = float(value)
        except (TypeError, ValueError):
            initial_num[key] = value

    return dict(
        pool=pool,
        note_cf=note_cf,
        delinquency=delinquency,
        notes=notes,
        schedule=schedule,
        composition=composition,
        initial=initial,
        initial_num=initial_num,
        validation_a=validation,
        term_map=term_map,
    )


def load_schedule_c() -> pd.DataFrame:
    """Fiscal-month calendar from the Sale and Servicing Agreement, Schedule C.

    Role A supplied this after the first hand-off. It replaces the heuristic
    "count the calendar months that have started" mapping with an exact join: the
    contract's fiscal periods are 4-4-5 style months, and several cut-offs spill
    into the following calendar week (e.g. the October 2026 period ends 1-Nov-26).

    Returns columns ``plan_month`` (the calendar month the collection belongs to,
    matching ``OriginalSchedule``) and ``cutoff_date``.
    """
    path = REFERENCE / "schedule_C.csv"
    if not path.exists():
        raise SystemExit(f"missing Schedule C: {path}")
    raw = pd.read_csv(path, encoding="utf-8-sig")
    # Schedule C writes "Sept" for September in some rows; %b expects "Sep".
    cutoff_text = raw["cutoff_date"].astype(str).str.strip().str.replace("Sept", "Sep", regex=False)
    raw["cutoff_date"] = pd.to_datetime(cutoff_text, format="%d-%b-%y", errors="raise")
    label = raw["plan_month_label"].astype(str).str.strip().str.lower().map(_LABEL_MONTH)
    if label.isna().any():
        bad = raw.loc[label.isna(), "plan_month_label"].unique()
        raise SystemExit(f"unrecognised fiscal month labels in Schedule C: {bad}")
    # The calendar year comes from the cut-off: a period ending 1-Nov-26 is still
    # the October period, and its year is 2026.
    plan_month = [f"{d.year}-{m:02d}" for d, m in zip(raw["cutoff_date"], label)]
    out = pd.DataFrame(dict(fiscal_year=raw["fiscal_year"], fiscal_month=raw["fiscal_month"],
                            plan_month=plan_month, cutoff_date=raw["cutoff_date"]))
    return out.sort_values("cutoff_date").reset_index(drop=True)


def collection_index(collection_start: pd.Series, schedule_months: list[str]) -> np.ndarray:
    """Fallback mapping: count schedule months already started at each collection start.

    Kept only for the alignment sensitivity that role A asked for (A4); the panel
    uses ``collection_index_schedule_c``.
    """
    starts = [month_start(m) for m in schedule_months]
    out = []
    for value in pd.to_datetime(collection_start):
        this = value.date()
        out.append(sum(1 for s in starts if s < this))
    return np.asarray(out, dtype=int)


def collection_index_schedule_c(collection_end: pd.Series, schedule_c: pd.DataFrame,
                                schedule_months: list[str]) -> np.ndarray:
    """Number of fiscal months collected by the close of each period.

    ``out[k]`` counts Schedule C cut-offs on or before period ``k``'s
    ``collection_end``, restricted to months that exist in ``OriginalSchedule``.
    """
    available = set(schedule_months)
    cutoffs = [
        d.date() for d, m in zip(schedule_c["cutoff_date"], schedule_c["plan_month"]) if m in available
    ]
    out = []
    for value in pd.to_datetime(collection_end):
        this = value.date()
        out.append(sum(1 for c in cutoffs if c <= this))
    return np.asarray(out, dtype=int)


def build_panel(pkg: dict) -> dict:
    """Return the canonical monthly panel plus the pieces the model needs."""
    pool = pkg["pool"].copy().sort_values("period").reset_index(drop=True)
    pool["period"] = pool["period"].astype(str)
    pool["t"] = np.arange(len(pool))

    schedule_months = [str(m) for m in pkg["schedule"]["collection_month"]]
    schedule_amt = pkg["schedule"]["gross_scheduled_usd"].to_numpy(dtype=float)

    schedule_c = load_schedule_c()

    # Primary mapping: exact Schedule C join keyed on the disclosed collection end.
    consumed = collection_index_schedule_c(pool["collection_end"], schedule_c, schedule_months)
    # ``collection_index`` = fiscal months already collected at the START of the
    # period, i.e. the number consumed by the previous period.
    pool["collection_index"] = np.concatenate([[0], consumed[:-1]]).astype(int)
    pool["schedule_months_consumed"] = consumed - np.concatenate([[0], consumed[:-1]])
    # Fallback mapping kept for the alignment sensitivity (role A, question A4).
    heuristic = collection_index(pool["collection_start"], schedule_months)
    pool["collection_index_heuristic"] = heuristic
    pool["schedule_c_months_consumed_total"] = consumed

    note_cf = pkg["note_cf"].copy()
    note_cf["period"] = note_cf["period"].astype(str)
    note_cf["tranche"] = pd.Categorical(note_cf["tranche"], categories=TRANCHE_ORDER, ordered=True)
    note_cf = note_cf.merge(pool[["period", "t"]], on="period", how="left", validate="many_to_one")
    note_cf = note_cf.sort_values(["t", "tranche"]).reset_index(drop=True)
    # A row is "live" when the class still has principal before or after the payment.
    note_cf["is_live"] = (note_cf["opening_balance"] > 0) | (note_cf["closing_balance"] > 0)

    initial = pkg["initial_num"]
    balance0 = float(initial["pool_excluding_accruals"])
    note_value0 = float(initial["initial_note_value"])
    discount = float(initial["contract_note_value_discount"])

    panel = dict(
        pool=pool,
        note_cf=note_cf,
        schedule_months=schedule_months,
        schedule_amount=schedule_amt,
        balance0=balance0,
        note_value0=note_value0,
        discount=discount,
        accrued_interest=float(initial["accrued_interest"]),
        pool_including_accruals=float(initial["pool_including_accruals"]),
        original_notes_principal=float(initial["original_notes_principal"]),
        reserve0=float(initial["initial_reserve"]),
        weighted_apr0=float(initial["weighted_apr"]),
        weighted_remaining_term0=float(initial["weighted_remaining_term"]),
    )

    panel["tranche_balances"] = (
        note_cf.pivot(index="period", columns="tranche", values="closing_balance")
        .reindex(pool["period"])
        .fillna(0.0)
    )
    panel["notes_outstanding"] = panel["tranche_balances"].sum(axis=1)
    return panel


def quality_report(pkg: dict, panel: dict) -> pd.DataFrame:
    """Cheap structural checks that should hold for any well-formed input."""
    pool = panel["pool"]
    rows = []

    def add(name: str, ok: bool, detail: str) -> None:
        rows.append(dict(check=name, status="pass" if ok else "review", detail=detail))

    months = pd.PeriodIndex(pd.to_datetime(pool["payment_date"]), freq="M")
    consecutive = all((months[i] - months[i - 1]).n == 1 for i in range(1, len(months)))
    add("periods are consecutive months", consecutive,
        f"{len(pool)} periods, {pool.period.iloc[0]}..{pool.period.iloc[-1]}")

    starts = pd.to_datetime(pool["collection_start"]).reset_index(drop=True)
    ends = pd.to_datetime(pool["collection_end"]).reset_index(drop=True)
    contiguous = all(starts[i] == ends[i - 1] + pd.Timedelta(days=1) for i in range(1, len(pool)))
    add("collection periods tile without gaps", contiguous,
        f"first {starts.iloc[0].date()}, last {ends.iloc[-1].date()}")

    add("pool balance chains across periods",
        bool(np.allclose(pool["pool_balance_start"].iloc[1:], pool["pool_balance"].iloc[:-1].to_numpy(), atol=0.01)),
        "pool_balance_start(t) == pool_balance(t-1)")

    add("note value chains across periods",
        bool(np.allclose(pool["note_value_start"].iloc[1:], pool["note_value"].iloc[:-1].to_numpy(), atol=0.01)),
        "note_value_start(t) == note_value(t-1)")

    negative = int((pool["prepayment"] < 0).sum())
    add("prepayment sign is mixed (needs the SA&S definition)", negative == 0,
        f"{negative} of {len(pool)} months negative, sum {pool.loc[pool.prepayment < 0, 'prepayment'].sum():,.2f}")

    coverage = panel["note_cf"].groupby("period")["tranche"].nunique()
    add("every period reports all five tranches", bool((coverage == 5).all()),
        f"min {int(coverage.min())}, max {int(coverage.max())} tranches per period")

    return pd.DataFrame(rows)


def load_all() -> dict:
    """Convenience wrapper: package + panel + quality report."""
    pkg = load_package()
    panel = build_panel(pkg)
    panel["quality"] = quality_report(pkg, panel)
    panel["package"] = pkg
    return panel
