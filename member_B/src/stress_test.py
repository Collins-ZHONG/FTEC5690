"""Scenario engine: Base / Moderate / Severe, the two-way grid, and the
legal-final maturity check.

Design decisions that come straight out of the project record:

* **Base loss is calibrated on realised same-age net loss** (role A, B1). The
  issuer's own 44-month curve is kept as a separate *comparison* scenario,
  because the realised loss (0.79% of the initial balance) is about 2.3x the
  issuer's same-age assumption (0.34% at fiscal month 30).
* **Base speed is 10% CPR**, an integer scenario assumption, not a fitted value
  (role A, A1/B1). 6%, 8.45% (the arithmetic midpoint of the two raw estimates)
  and the issuer's 16% are run as sensitivities.
* **No event-of-default state machine** (user decision, 2026-11-26). The
  accelerated pro-rata waterfall is therefore not modelled; the legal-final check
  reports the shortfall and the report states that sequential priority makes the
  A-4 tail loss an upper bound.
* **The overcollateralisation is never an extra cash inflow at the clean-up
  call** (role A, closing note). The call is modelled as a single purchase of the
  remaining receivables at their Note Value; the notes are redeemed at par plus
  accrued interest and whatever is left is the certificate's residual.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

import metrics as mt
import data_cleaning as dc
import pool_model as pm
import waterfall as wf

CALL_THRESHOLD_FRACTION = 0.10
PROJECTION_MONTHS = 84          # covers the schedule tail to 2030-05 and then some


def realised_loss_rate(panel: dict) -> float:
    """Annualised net-loss rate as a fraction of the opening pool balance.

    Role A's B1: calibrate Base on the *same-age* realised net loss rather than on
    the issuer's assumption. Averaging the monthly ratio over the observed window
    gives a rate that, held flat, reproduces the 0.79% cumulative figure.
    """
    pool = panel["pool"]
    return float(12.0 * (pool["net_loss"] / pool["pool_balance_start"]).mean())


def issuer_loss_curve() -> pd.Series:
    """The issuer's 44-month cumulative net-loss assumption, by fiscal month.

    Kept for the report's comparison table even though the scenario itself scales
    the realised rate: cumulative loss is linear in the loss rate for a given
    balance path, so scaling by 0.34/0.79 reproduces the issuer same-age level.
    """
    ref = pd.read_csv(dc.REFERENCE / "issuer_loss_curve.csv", encoding="utf-8-sig")
    return ref.set_index("fiscal_month_since_cutoff")["cumulative_net_loss_pct_initial_pool"]


def scenarios(panel: dict) -> dict[str, pm.ProjectionAssumptions]:
    """Base plus the two stress cases, each with and without the clean-up call.

    The call/no-call split is not cosmetic. Once the call threshold is met the
    servicer can purchase the remaining receivables at their Note Value, which
    redeems the notes at par and releases the overcollateralisation to the
    certificate. Because the overcollateralisation (2.50% of the initial Note
    Value) is much larger than plausible cumulative losses, the *call decision*
    dominates both duration and noteholder return; loss and prepayment stress only
    start to matter on the no-call branch.
    """
    base = realised_loss_rate(panel)
    out = {}
    for tag, cpr_mult, loss_mult in (("Base", 1.0, 1.0), ("Moderate", 0.8, 1.5), ("Severe", 0.6, 2.0)):
        for call in (True, False):
            label = f"{tag}" + ("" if call else " (no clean-up call)")
            out[label] = pm.ProjectionAssumptions(
                label=label,
                cpr=0.10 * cpr_mult,
                annual_loss_rate=base * loss_mult,
                call_enabled=call,
            )
    out["Issuer Assumption"] = pm.ProjectionAssumptions(
        label="Issuer assumption: 16% CPR, issuer same-age loss",
        cpr=0.16,
        annual_loss_rate=base * 0.34 / 0.79,
        call_enabled=True,
    )
    return out


def _future_panel(path: pd.DataFrame, reserve_start: float) -> pd.DataFrame:
    """Reshape a projected pool path into the columns the waterfall expects."""
    out = pd.DataFrame(
        dict(
            period=path["period"],
            note_value_start=path["note_value_start"],
            note_value=path["note_value_end"],
            total_distribution=path["cash_collected"],
            pool_balance_start=path["pool_balance_start"],
            days_30_360=30,
            days_act_360=30,
            a2b_rate=0.0,
            review_fee_priority2=0.0,
            indenture_trustee_fee=0.0,
            owner_trustee_fee=0.0,
            review_fee_priority9=0.0,
            reserve_start=reserve_start if len(path) else np.nan,
        )
    )
    out.loc[out.index > 0, "reserve_start"] = np.nan
    return out


def _call_period(period: str, outstanding: pd.Series, note_value: float, reserve: float,
                 tranches: list[wf.Tranche]) -> tuple[list[dict], dict]:
    """Clean-up call: one purchase at Note Value, notes redeemed at par + accrued.

    The purchase price is the Note Value, which already contains the
    overcollateralisation - adding the OC again would double count it.
    """
    tranche_rows = []
    interest_total = 0.0
    for t in tranches:
        balance = float(outstanding.get(t.name, 0.0))
        if balance <= 0.0:
            continue
        interest = balance * float(t.fixed_coupon or 0.0) * 30.0 / 360.0
        interest_total += interest
        tranche_rows.append(
            dict(period=period, tranche=t.name, coupon=float(t.fixed_coupon or 0.0),
                 opening_balance=balance, interest_due=interest, interest_paid=interest,
                 principal_paid=balance, closing_balance=0.0, interest_shortfall=0.0)
        )
    principal_total = sum(r["principal_paid"] for r in tranche_rows)
    cash = note_value + reserve
    certificate = cash - interest_total - principal_total
    period_row = dict(
        period=period,
        total_distribution=cash,
        administration_fee=0.0,
        review_fee_priority2=0.0,
        note_interest=interest_total,
        note_principal=principal_total,
        reserve_to_notes=0.0,
        reserve_deposit=0.0,
        reserve_end=0.0,
        servicing_fee=0.0,
        indenture_trustee_fee=0.0,
        owner_trustee_fee=0.0,
        review_fee_priority9=0.0,
        certificate=certificate,
        principal_distributable=principal_total,
        principal_shortfall=0.0,
        interest_shortfall=0.0,
        note_value_start=note_value,
        note_value_end=0.0,
        notes_outstanding=0.0,
        cash_used_gap=0.0,
        is_clean_up_call=True,
    )
    return tranche_rows, period_row


def run_scenario(
    panel: dict,
    cal: pm.Calibration,
    tranches: list[wf.Tranche],
    assumptions: pm.ProjectionAssumptions,
    historical_flows: pd.DataFrame | None = None,
) -> dict:
    """Project one scenario end to end and measure it.

    ``historical_flows`` are the disclosed-octet cash flows already replayed by the
    waterfall in Day 2. They must be prepended to the projection: an IRR is a
    whole-life measure, and computing it on the projection alone would treat a
    note bought in June 2024 as if it were bought at the freeze date.
    """
    idx0, balance0, note_value0 = pm.opening_state(panel)
    periods = pm.future_periods(str(panel["pool"]["period"].iloc[-1]), PROJECTION_MONTHS)

    path = pm.project(
        cal,
        schedule_index_start=idx0,
        balance0=balance0,
        note_value0=note_value0,
        periods=periods,
        assumptions=assumptions,
    )
    if path.empty:
        raise RuntimeError("projection produced no periods")

    callable_rows = path.index[path["callable_now"]]
    call_at = int(callable_rows[0]) if len(callable_rows) else None

    ordinary = path if call_at is None else path.iloc[:call_at]
    start_balances = None
    if historical_flows is not None and len(historical_flows):
        last = historical_flows.iloc[-len(tranches):]
        start_balances = dict(zip(last["tranche"], last["closing_balance"]))
    tranche_flows = pd.DataFrame()
    period_flows = pd.DataFrame()
    if len(ordinary):
        reserve0 = float(panel["pool"]["reserve_start"].iloc[-1])
        future = _future_panel(ordinary, reserve0)
        tranche_flows, period_flows = wf.run(
            future,
            tranches,
            reserve_target=panel["reserve0"],
            cash=future["total_distribution"].to_numpy(),
            initial_balances=start_balances,
        )

    if call_at is not None:
        call_period = str(path.iloc[call_at]["period"])
        if len(tranche_flows):
            outstanding = tranche_flows.iloc[-len(tranches):].set_index("tranche")["closing_balance"]
            reserve = float(period_flows["reserve_end"].iloc[-1])
        else:
            outstanding = pd.Series({t.name: t.original_balance for t in tranches})
            reserve = float(panel["pool"]["reserve_start"].iloc[-1])
        note_value = float(path.iloc[call_at]["note_value_start"])
        call_tranches, call_row = _call_period(call_period, outstanding, note_value, reserve, tranches)
        tranche_flows = pd.concat([tranche_flows, pd.DataFrame(call_tranches)], ignore_index=True)
        period_flows = pd.concat([period_flows, pd.DataFrame([call_row])], ignore_index=True)
        call_date = call_period
    else:
        call_date = None

    # Whole-life flows = history (disclosed and replayed) + projection.
    if historical_flows is not None and len(historical_flows):
        combined = pd.concat([historical_flows, tranche_flows], ignore_index=True)
    else:
        combined = tranche_flows

    periods_all = list(dict.fromkeys(combined["period"]))
    known = dict(zip(panel["pool"]["period"].astype(str), pd.to_datetime(panel["pool"]["payment_date"])))
    dates = pd.DataFrame(
        dict(period=periods_all,
             payment_date=[known.get(p, pd.Timestamp(f"{p}-15")) for p in periods_all])
    )
    returns = mt.tranche_returns(combined, tranches, dates, start="2024-06-18", price_basis="par")
    returns_issue = mt.tranche_returns(combined, tranches, dates, start="2024-06-18", price_basis="issue")

    return dict(
        assumptions=assumptions,
        path=path,
        tranche_flows=combined,
        projected_flows=tranche_flows,
        period_flows=period_flows,
        returns=returns,
        returns_issue=returns_issue,
        call_date=call_date,
        legal_final=legal_final_check(combined, tranches),
    )


def legal_final_check(tranche_flows: pd.DataFrame, tranches: list[wf.Tranche]) -> pd.DataFrame:
    """Is each class fully repaid by its legal final payment date?

    Role A's B2 advice, kept even though the accelerated waterfall is out of
    scope: report the gap per class, with principal due / paid / short separately.
    """
    rows = []
    for t in tranches:
        sub = tranche_flows[tranche_flows["tranche"] == t.name].sort_values("period")
        final_period = str(t.legal_final)[:7]
        if sub.empty:
            rows.append(dict(tranche=t.name, legal_final=t.legal_final,
                             outstanding_at_final=t.original_balance, principal_due=t.original_balance,
                             principal_paid=0.0, shortfall=t.original_balance, cleared=False,
                             cleared_by=None))
            continue
        by_final = sub[sub["period"] <= final_period]
        paid_by_final = float(by_final["principal_paid"].sum())
        outstanding = float(sub["closing_balance"].iloc[-1])
        cleared = sub.loc[sub["closing_balance"] <= 0.005, "period"]
        cleared_by = str(cleared.iloc[0]) if len(cleared) else None
        rows.append(
            dict(
                tranche=t.name,
                legal_final=t.legal_final,
                outstanding_at_final=outstanding,
                principal_due=t.original_balance,
                principal_paid=paid_by_final,
                shortfall=t.original_balance - paid_by_final,
                cleared=bool(cleared_by is not None and cleared_by <= final_period),
                cleared_by=cleared_by,
            )
        )
    return pd.DataFrame(rows)


def loss_absorption(
    panel: dict,
    cal: pm.Calibration,
    tranches: list[wf.Tranche],
    *,
    loss_rates: list[float],
    cpr: float = 0.10,
    historical_flows: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Where each dollar of loss lands: excess spread -> reserve -> OC -> certificate -> notes.

    This is the table that turns "stress has little effect" into an explanation
    rather than an absence of findings. It walks the loss rate up and reports how
    much of each layer has been used, so the reader can see the ordering of
    protection and the point at which the last layer, the notes, is reached.
    """
    initial_oc = float(panel["note_value0"]) - float(panel["original_notes_principal"])
    initial_reserve = float(panel["reserve0"])
    base_loss = realised_loss_rate(panel)
    rows = []
    for loss in loss_rates:
        assumption = pm.ProjectionAssumptions(
            label=f"absorption loss={loss*100:.2f}%", cpr=cpr, annual_loss_rate=loss, call_enabled=False
        )
        result = run_scenario(panel, cal, tranches, assumption, historical_flows=historical_flows)
        path, period_flows = result["path"], result["period_flows"]
        legal = result["legal_final"]
        rows.append(
            dict(
                loss_rate=loss,
                multiple_of_realised=loss / base_loss if base_loss else np.nan,
                cumulative_loss=float(path["net_loss"].sum()),
                reserve_drawn=initial_reserve - float(period_flows["reserve_end"].iloc[-1]),
                certificate_cash=float(period_flows["certificate"].sum()),
                note_principal_shortfall=float(legal["shortfall"].sum()),
                note_interest_shortfall=float(period_flows["interest_shortfall"].sum()),
            )
        )
    frame = pd.DataFrame(rows).sort_values("loss_rate").reset_index(drop=True)
    # Attribute the *increase* in loss, measured against the lowest loss case, to
    # the layers that paid for it. Measured as increments, not levels: reporting
    # "OC consumed" as a level would be wrong, because in a run to maturity the
    # overcollateralisation is released to the certificate rather than used up.
    first = frame.iloc[0]
    frame["loss_increase_vs_first"] = frame["cumulative_loss"] - first["cumulative_loss"]
    frame["certificate_reduction_vs_first"] = first["certificate_cash"] - frame["certificate_cash"]
    frame["absorbed_by_certificate"] = frame["certificate_reduction_vs_first"].round(2)
    frame["absorbed_by_reserve"] = frame["reserve_drawn"].round(2)
    frame["absorbed_by_notes"] = (frame["note_principal_shortfall"] + frame["note_interest_shortfall"]).round(2)
    return frame


def run_grid(
    panel: dict,
    cal: pm.Calibration,
    tranches: list[wf.Tranche],
    *,
    loss_rates: list[float],
    cprs: list[float],
    call_enabled: bool = False,
    historical_flows: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Two-way sensitivity: net-loss rate (rows) x CPR (columns).

    The grid is the answer to "why this number?" - a single scenario can always be
    questioned, a surface cannot. Default is the **no-call** branch because that is
    where duration and credit risk actually bind: with the call available, the
    overcollateralisation redeems the notes at par and the stress only shows up in
    the certificate's return.
    """
    rows = []
    for loss in loss_rates:
        for cpr in cprs:
            assumption = pm.ProjectionAssumptions(
                label=f"grid loss={loss*100:.2f}% cpr={cpr*100:.0f}%",
                cpr=cpr,
                annual_loss_rate=loss,
                call_enabled=call_enabled,
            )
            result = run_scenario(panel, cal, tranches, assumption, historical_flows=historical_flows)
            returns = result["returns"].set_index("tranche")
            final = result["legal_final"].set_index("tranche")
            rows.append(
                dict(
                    annual_loss_rate=loss,
                    cpr=cpr,
                    call_enabled=call_enabled,
                    call_period=result["call_date"] or "",
                    a3_wal=float(returns.loc["A-3", "wal_years"]),
                    a4_wal=float(returns.loc["A-4", "wal_years"]),
                    a3_irr=float(returns.loc["A-3", "irr"]),
                    a4_irr=float(returns.loc["A-4", "irr"]),
                    a3_shortfall=float(final.loc["A-3", "shortfall"]),
                    a4_shortfall=float(final.loc["A-4", "shortfall"]),
                    a3_cleared_by=final.loc["A-3", "cleared_by"] or "",
                    a4_cleared_by=final.loc["A-4", "cleared_by"] or "",
                    certificate_total=float(result["period_flows"]["certificate"].sum()),
                )
            )
    return pd.DataFrame(rows)
