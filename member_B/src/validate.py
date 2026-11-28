"""Executable acceptance gates G1-G3 for role B (plan section 4.10).

Every gate writes rows in role A's ``Validation.csv`` shape
(``period, check, difference, tolerance, status``) plus two extra columns so a
reader can see the two sides. ``run_all.py`` exits non-zero if any gate fails,
which is deliberate: no chart is produced from an unvalidated model.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

GATE_DESCRIPTIONS = {
    "G1": "identities that anchor the model to disclosed values",
    "G2": "calibration reproduces the contract schedule it claims to model",
    "G3": "history replay rebuilds the disclosed Note Value path",
    "G4": "waterfall replays the disclosed class-by-class cash flows",
    "G5": "WAL and IRR reproduce the realised figures from disclosed flows",
}

# Thresholds. G3 numbers are set just above what the architecture achieves with
# the seasonal split (mean 0.246%, max 0.659%, terminal -0.024%); they were
# tightened from the flat-accrual version's 0.75/1.25/0.75 to catch regressions.
G3_MEAN_ABS_PCT = 0.40
G3_MAX_ABS_PCT = 0.90
G3_TERMINAL_ABS_PCT = 0.25
G1_PV_TOLERANCE = 0.005
G1_MONEY_TOLERANCE = 0.021
# G4: the certificate is a residual, so it inherits the disclosure rounding of
# every line above it (five interest lines at 2.1 cents each).
G4_LINE_TOLERANCE = 0.021
G4_CERTIFICATE_TOLERANCE = 0.15
# G5: 1 basis point on the IRR, half a month on the WAL.
G5_IRR_TOLERANCE = 0.0001
G5_WAL_TOLERANCE = 0.05


def _row(gate, period, check, difference, tolerance, status, left=None, right=None, note=""):
    return dict(
        gate=gate,
        period=period,
        check=check,
        difference=difference,
        tolerance=tolerance,
        status=status,
        left=left,
        right=right,
        note=note,
    )


def _judge(gate, period, check, left, right, tolerance, note=""):
    if left is None or right is None or (isinstance(left, float) and np.isnan(left)):
        return _row(gate, period, check, None, tolerance, "missing", left, right, note)
    difference = float(left) - float(right)
    status = "pass" if abs(difference) <= tolerance else "fail"
    return _row(gate, period, check, difference, tolerance, status, float(left), float(right), note)


# --------------------------------------------------------------------------- #
def gate_g1(panel: dict, cal) -> list[dict]:
    """Four identities that tie the model to disclosed dollars."""
    pool = panel["pool"]
    rows = []

    # 1. Initial Note Value is the 8.85% PV of the contract schedule.
    rows.append(
        _judge("G1", "2024-04-28", "initial Note Value = PV(schedule @ 8.85%/12)",
               cal.pv[0], panel["note_value0"], G1_PV_TOLERANCE,
               "prospectus printed p92 and p59")
    )

    # 2. Principal distributed each period equals the fall in Note Value.
    for k in range(len(pool)):
        rows.append(
            _judge("G1", pool["period"].iloc[k], "principal paid = Note Value decline",
                   pool["note_value_start"].iloc[k] - pool["note_value"].iloc[k],
                   pool["note_principal"].iloc[k], G1_MONEY_TOLERANCE)
        )

    # 3. Overcollateralisation is constant = initial Note Value - original principal.
    target_oc = panel["note_value0"] - panel["original_notes_principal"]
    for k in range(len(pool)):
        rows.append(
            _judge("G1", pool["period"].iloc[k], "overcollateralisation constant",
                   pool["note_value"].iloc[k] - panel["notes_outstanding"].iloc[k],
                   target_oc, 0.51,
                   "discloses $39,062,743; residual is one-cent disclosure rounding")
        )

    # 4. Servicing fee is 1%/yr on the opening pool balance. The first period is
    #    excluded from the strict test because it aggregates two collection months
    #    (2024-04-29..2024-06-23) and the contract therefore charges two monthly
    #    bases; the second base is not disclosed, so we only check that the
    #    implied base is a plausible (positive, smaller than opening) balance.
    for k in range(1, len(pool)):
        rows.append(
            _judge("G1", pool["period"].iloc[k], "servicing fee = opening pool balance / 1200",
                   pool["pool_balance_start"].iloc[k] / 1200.0,
                   pool["servicing_fee_paid"].iloc[k], G1_MONEY_TOLERANCE)
        )
    implied_second_base = pool["servicing_fee_paid"].iloc[0] * 1200.0 - pool["pool_balance_start"].iloc[0]
    opening = pool["pool_balance_start"].iloc[0]
    rows.append(
        _row("G1", pool["period"].iloc[0], "first period servicing fee implies two monthly bases",
             implied_second_base, opening, "pass" if 0 < implied_second_base < opening else "fail",
             left=implied_second_base, right=opening,
             note="implied end-of-May base; contract charges two bases for the stub period")
    )

    # 5. Total distribution is the sum of its four disclosed sources.
    for k in range(len(pool)):
        rows.append(
            _judge("G1", pool["period"].iloc[k], "total distribution = sum of sources",
                   pool[["cash_receipts", "repurchases", "investment_income", "buyback"]].iloc[k].sum(),
                   pool["total_distribution"].iloc[k], 0.011)
        )
    return rows


def gate_g2(panel: dict, cal) -> list[dict]:
    """The calibrated amortisation must be self-consistent with the schedule."""
    rows = []
    schedule = panel["schedule_amount"]

    # The solved yield must exhaust the cut-off principal exactly.
    rows.append(
        _judge("G2", "schedule", "no-prepay terminal balance", cal.balance_no_prepay[-1], 0.0, 0.01,
               f"solved pool yield {cal.yield_rate*100:.4f}%")
    )
    # Principal repaid over the life must equal the opening principal.
    rows.append(
        _judge("G2", "schedule", "scheduled principal over life", cal.scheduled_principal.sum(),
               panel["balance0"], 0.01)
    )
    # Splitting the schedule into principal + interest must reproduce it exactly.
    rows.append(
        _judge("G2", "schedule", "principal + interest = gross scheduled payments",
               float(cal.scheduled_principal.sum() + cal.scheduled_interest.sum()),
               float(schedule.sum()), 0.01)
    )
    # No negative principal inside the schedule (tolerance is one cent: the solved
    # terminal balance is zero to machine precision, so the last month can be a
    # micro-dollar negative).
    rows.append(
        _judge("G2", "schedule", "minimum monthly principal is non-negative",
               float(cal.scheduled_principal.min()), 0.0, 0.01)
    )
    # Note Value must fall monotonically as scheduled payments are consumed.
    rows.append(
        _judge("G2", "schedule", "PV of remaining schedule never increases",
               float((np.diff(cal.pv) > 1e-6).sum()), 0.0, 0.0)
    )
    return rows


def gate_g3(replay: pd.DataFrame) -> list[dict]:
    """Accept or reject the survival-factor architecture on 29 months of data."""
    rows = []
    err = replay["note_value_error_pct"].to_numpy(dtype=float)
    mean_abs = float(np.abs(err).mean())
    max_abs = float(np.abs(err).max())
    terminal = float(err[-1])

    rows.append(_row("G3", replay["period"].iloc[-1], "mean |Note Value error| %",
                     mean_abs, G3_MEAN_ABS_PCT, "pass" if mean_abs <= G3_MEAN_ABS_PCT else "fail",
                     note=f"{len(err)} periods"))
    worst = int(np.abs(err).argmax())
    rows.append(_row("G3", replay["period"].iloc[worst], "max |Note Value error| %",
                     max_abs, G3_MAX_ABS_PCT, "pass" if max_abs <= G3_MAX_ABS_PCT else "fail",
                     note="worst period"))
    rows.append(_row("G3", replay["period"].iloc[-1], "terminal Note Value error %",
                     terminal, G3_TERMINAL_ABS_PCT, "pass" if abs(terminal) <= G3_TERMINAL_ABS_PCT else "fail",
                     note="2026-11 vs disclosed"))

    # Per-period rows keep the same schema so the table can be plotted.
    for _, row in replay.iterrows():
        rows.append(_row("G3", row["period"], "Note Value replay error %",
                         float(row["note_value_error_pct"]), G3_MAX_ABS_PCT,
                         "pass" if abs(row["note_value_error_pct"]) <= G3_MAX_ABS_PCT else "fail",
                         left=float(row["note_value_pred"]), right=float(row["note_value_actual"])))
    return rows


def gate_g4(tranche_flows: pd.DataFrame, period_flows: pd.DataFrame, panel: dict) -> list[dict]:
    """Replay history through the model waterfall and compare line by line.

    This is the gate that catches a *misread clause*: a wrong class order, a
    wrong proration basis or a wrong fee rank can leave the total intact while
    misallocating between classes, so it compares every class-month to the cent.
    """
    pool = panel["pool"]
    disclosed = panel["note_cf"]
    rows = []
    merged = tranche_flows.merge(
        disclosed[["period", "tranche", "opening_balance", "principal_paid", "interest_paid", "closing_balance"]],
        on=["period", "tranche"],
        how="left",
        suffixes=("", "_disclosed"),
        validate="one_to_one",
    )
    if merged["principal_paid_disclosed"].isna().any():
        rows.append(_row("G4", "-", "every modelled class-month has a disclosed counterpart",
                         float(merged["principal_paid_disclosed"].isna().sum()), 0.0, "fail"))
    for _, row in merged.iterrows():
        rows.append(_judge("G4", row["period"], f"{row['tranche']} principal paid",
                           row["principal_paid"], row["principal_paid_disclosed"], G4_LINE_TOLERANCE))
        rows.append(_judge("G4", row["period"], f"{row['tranche']} interest paid",
                           row["interest_paid"], row["interest_paid_disclosed"], G4_LINE_TOLERANCE))
        rows.append(_judge("G4", row["period"], f"{row['tranche']} closing balance",
                           row["closing_balance"], row["closing_balance_disclosed"], G4_LINE_TOLERANCE))

    # The certificate is a residual, so it tests the whole stack at once.
    for _, row in period_flows.iterrows():
        rows.append(_judge("G4", row["period"], "certificate residual",
                           row["certificate"], float(pool.loc[pool["period"] == row["period"], "certificate_payment"].iloc[0]),
                           G4_CERTIFICATE_TOLERANCE))
        rows.append(_judge("G4", row["period"], "cash fully allocated",
                           row["cash_used_gap"], 0.0, 0.011))
        rows.append(_judge("G4", row["period"], "principal shortfall stays zero",
                           row["principal_shortfall"], 0.0, 0.011))
        rows.append(_judge("G4", row["period"], "interest shortfall stays zero",
                           row["interest_shortfall"], 0.0, 0.011))
    return rows


def gate_g5(model_returns: pd.DataFrame, reference_returns: pd.DataFrame) -> list[dict]:
    """WAL and IRR must agree with the values computed from disclosed flows."""
    rows = []
    merged = model_returns.merge(reference_returns, on="tranche", suffixes=("", "_ref"))
    for _, row in merged.iterrows():
        if not np.isfinite(row["wal_years"]) and not np.isfinite(row["wal_years_ref"]):
            rows.append(_row("G5", row["tranche"], "WAL (years)", None, G5_WAL_TOLERANCE, "pass",
                             note="no principal repaid yet, so WAL is undefined for both series"))
        else:
            rows.append(_judge("G5", row["tranche"], "WAL (years)",
                               row["wal_years"], row["wal_years_ref"], G5_WAL_TOLERANCE,
                               "ACT/365 from the 2024-06-18 closing date"))
        if np.isfinite(row["irr"]) and np.isfinite(row["irr_ref"]):
            rows.append(_judge("G5", row["tranche"], "IRR at issue price",
                               row["irr"], row["irr_ref"], G5_IRR_TOLERANCE))
        else:
            rows.append(_row("G5", row["tranche"], "IRR at issue price", None, G5_IRR_TOLERANCE,
                             "pass", note="not redeemable within the observed window: no IRR"))
        rows.append(_judge("G5", row["tranche"], "total principal repaid",
                           row["total_principal"], row["total_principal_ref"], G4_LINE_TOLERANCE))
    return rows


def _text(value) -> str:
    """Human-readable form for an optional date-like cell (NaN and '' mean 'never')."""
    if value is None:
        return "still open when the schedule runs out"
    if isinstance(value, float) and value != value:
        return "still open when the schedule runs out"
    return str(value) if str(value).strip() else "never within the horizon"


def gate_g6(results: dict, grid: pd.DataFrame, base_loss_rate: float) -> list[dict]:
    """Scenario sanity: nothing goes negative, cash closes, and the call is plausible.

    This gate cannot prove the projection is *right* - only G3/G4 do that for the
    mechanics. What it can do is catch the failure modes that projections
    actually suffer from: a class paying principal it does not have, cash that
    does not add up, a call date that contradicts the issuer's own expectation, or
    a grid that is not monotone in its own drivers.
    """
    rows = []
    for name, result in results.items():
        flows = result["tranche_flows"]
        negative = int((flows["closing_balance"] < -0.011).sum())
        rows.append(_row("G6", name, "no negative class balance", float(negative), 0.0,
                         "pass" if negative == 0 else "fail"))
        periods = result["period_flows"]
        ordinary = periods[periods.get("is_clean_up_call", False) != True] if "is_clean_up_call" in periods else periods
        gap = float(ordinary["cash_used_gap"].abs().max()) if len(ordinary) else 0.0
        rows.append(_row("G6", name, "cash fully allocated (max gap)", gap, 0.011,
                         "pass" if gap <= 0.011 else "fail"))
        cert = float(periods["certificate"].sum())
        rows.append(_row("G6", name, "certificate cash non-negative", cert, 0.0,
                         "pass" if cert >= -0.011 else "fail"))
        legal = result["legal_final"]
        breached = int((~legal["cleared"]).sum())
        rows.append(_row("G6", name, "every class cleared by its legal final date", float(breached), 0.0,
                         "pass" if breached == 0 else "fail",
                         note="; ".join(f"{r.tranche}:{r.cleared_by}" for r in legal.itertuples())))

    base = results.get("Base")
    if base is not None and base["call_date"]:
        # The issuer estimated its clean-up call on the 43rd payment date following
        # the closing date, i.e. 2028-01 (first payment date 2024-07-15). The model
        # re-solves from the 2026-11 freeze rather than from closing, so a one-period
        # difference is expected; what must hold is that the call lands in the right
        # neighbourhood and that the threshold crossing is reported honestly.
        plausible = "2026-12" <= base["call_date"] <= "2029-06"
        rows.append(_row("G6", "Base", "clean-up call date is plausible", float(base["call_date"].replace("-", "")),
                         202906.0, "pass" if plausible else "fail",
                         note=(f"issuer's estimate = 43rd payment date = 2028-01; model = payment date 44 "
                               f"({base['call_date']}), one period later")))

    # The grid must respond in the direction theory requires, and any cell where a
    # class actually takes a loss has to be surfaced - not silently averaged away.
    # Note that A-3 and A-4 behave differently: A-3 is shortfall-free across the
    # whole surface, while A-4 breaks in exactly one corner cell (no voluntary
    # prepayment combined with the highest loss rate). That asymmetry is a result,
    # not a defect, so it is reported rather than failed.
    if len(grid):
        for cls, col in (("A-3", "a3_shortfall"), ("A-4", "a4_shortfall")):
            monotone = True
            for _, block in grid.groupby("cpr"):
                if (block.sort_values("annual_loss_rate")[col].diff().dropna() < -1.0).any():
                    monotone = False
                    break
            rows.append(_row("G6", "grid", f"{cls} shortfall is non-decreasing in loss",
                             0.0 if monotone else 1.0, 0.0, "pass" if monotone else "fail"))
            hit = grid[grid[col] > 1.0]
            if len(hit):
                worst = hit.iloc[0]
                rows.append(_row("G6", "grid", f"{cls} cells with principal shortfall",
                                 float(len(hit)), 42.0, "pass",
                                 note=(f"{cls} takes ${worst[col]:,.0f} at loss "
                                       f"{worst['annual_loss_rate']*100:.2f}%/yr, CPR {worst['cpr']*100:.0f}%; "
                                       f"cleared_by = {_text(worst.get('a4_cleared_by'))}")))
            else:
                rows.append(_row("G6", "grid", f"{cls} cells with principal shortfall",
                                 0.0, 42.0, "pass", note=f"{cls} is whole across the whole surface"))
        breach = grid.loc[grid["a3_shortfall"] > 1.0, "annual_loss_rate"]
        rows.append(_row("G6", "grid", "loss rate at which A-3 first takes a loss",
                         float(breach.min()) if len(breach) else float("nan"), 1.0, "pass",
                         note=f"Base loss rate {base_loss_rate*100:.3f}%/yr; a finite value means the notes are not risk-free"))
    return rows


def gate_summary(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    summary = (
        frame.groupby("gate")["status"]
        .value_counts()
        .unstack(fill_value=0)
        .reindex(columns=["pass", "fail", "missing"], fill_value=0)
    )
    summary["ok"] = (summary.get("fail", 0) == 0) & (summary.get("missing", 0) == 0)
    summary["description"] = [GATE_DESCRIPTIONS.get(g, "") for g in summary.index]
    return summary.reset_index()


def write(rows: list[dict], path) -> None:
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
