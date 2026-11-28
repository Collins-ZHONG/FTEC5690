"""One command to rebuild every role-B output that exists so far.

Stages
------
``data``   load role A's package, build the monthly panel, run structural checks
``spike``  calibrate the schedule primitives, replay history, run gates G1-G3
``all``    both (default)

Usage (and the interpreter MUST be given explicitly - ``python`` is not on PATH
in this environment)::

    $py = "C:\\Users\\wcs\\.dsh\\dsh-runtimes\\dsh-primary-runtime\\dependencies\\python\\python.exe"
    & $py member_B\\run_all.py --stage all

Exit code is non-zero if any gate fails, so a caller can gate on it.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

SRC = Path(__file__).resolve().parent / "src"
sys.path.insert(0, str(SRC))

import data_cleaning as dc  # noqa: E402
import charts as ch  # noqa: E402
import metrics as mt  # noqa: E402
import pool_model as pm  # noqa: E402
import stress_test as st  # noqa: E402
import validate as vd  # noqa: E402
import waterfall as wf  # noqa: E402

OUT = Path(__file__).resolve().parent / "outputs"
TABLES = OUT / "tables"
CHARTS = OUT / "charts"
CLOSING_DATE = "2024-06-18"
CERTIFICATE_OC = 39_062_742.82


def _write(frame: pd.DataFrame, name: str) -> Path:
    TABLES.mkdir(parents=True, exist_ok=True)
    path = TABLES / name
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def stage_data() -> tuple[dict, list[Path]]:
    panel = dc.load_all()
    written = [
        _write(panel["pool"], "panel_pool.csv"),
        _write(panel["note_cf"], "panel_note_cashflows.csv"),
        _write(panel["quality"], "quality_B.csv"),
    ]
    print(f"[data] panel {len(panel['pool'])} periods, {len(panel['note_cf'])} tranche-months")
    for _, row in panel["quality"].iterrows():
        print(f"[data]   {row['status']:6s} {row['check']}: {row['detail']}")
    return panel, written


def stage_spike(panel: dict) -> tuple[list[dict], list[Path], dict]:
    cal = pm.calibrate(
        panel["schedule_amount"], panel["balance0"], panel["discount"], panel["schedule_months"]
    )
    checkpoints = cal.checkpoints

    print(f"[spike] seasonal weights by month-of-year: "
          + " ".join(f"{v:.2f}" for v in cal.weights_month_of_year))
    print(f"[spike] pool accrual rate solved = {cal.yield_rate*100:.4f}%/yr "
          f"(schedule exhausts {panel['balance0']:,.2f})")
    print(f"[spike] initial Note Value from schedule = {cal.pv[0]:,.4f} "
          f"vs disclosed {panel['note_value0']:,.2f}")

    # Cross-checks on the basis the gross schedule actually amortises.
    accrued_yield = pm.solve_pool_yield(
        panel["schedule_amount"], panel["pool_including_accruals"], cal.weights_per_month
    )
    flat_yield = pm.solve_pool_yield(panel["schedule_amount"], panel["balance0"])
    print(f"[spike] same solve on accrued-inclusive balance ({panel['pool_including_accruals']:,.2f}) "
          f"= {accrued_yield*100:.4f}%/yr; disclosed weighted APR = {panel['weighted_apr0']*100:.4f}%")
    print(f"[spike] flat-accrual variant (no seasonality) = {flat_yield*100:.4f}%/yr "
          f"-> kept only as a regression reference")

    replay = pm.replay(panel, cal)
    rows = vd.gate_g1(panel, cal) + vd.gate_g2(panel, cal) + vd.gate_g3(replay)
    summary = vd.gate_summary(rows)

    calibration = pd.DataFrame(
        [
            dict(item="pool_yield_solved", value=cal.yield_rate, unit="decimal/year",
                 note="annual yield at which the 73-month contract schedule exhausts the cut-off principal"),
            dict(item="pool_yield_accrued_basis", value=accrued_yield, unit="decimal/year",
                 note="same solve on pool balance including accrued interest; compare with weighted APR"),
            dict(item="weighted_apr_cutoff", value=panel["weighted_apr0"], unit="decimal/year",
                 note="disclosed prospectus printed p54"),
            dict(item="note_value_discount", value=panel["discount"], unit="decimal/year",
                 note="indenture Note Value discount rate"),
            dict(item="balance0_ex_accrued", value=panel["balance0"], unit="USD",
                 note="opening principal, 99.1 section 4"),
            dict(item="balance0_incl_accrued", value=panel["pool_including_accruals"], unit="USD",
                 note="prospectus printed p54"),
            dict(item="note_value0", value=cal.pv[0], unit="USD",
                 note="PV of schedule at 8.85%/12"),
            dict(item="note_value0_disclosed", value=panel["note_value0"], unit="USD",
                 note="prospectus printed p92"),
            dict(item="scheduled_principal_total", value=float(cal.scheduled_principal.sum()), unit="USD", note="G2"),
            dict(item="scheduled_interest_total", value=float(cal.scheduled_interest.sum()), unit="USD", note="G2"),
            dict(item="pool_yield_flat_accrual", value=flat_yield, unit="decimal/year",
                 note="regression reference: the pre-seasonality solve"),
            dict(item="scheduled_principal_min", value=float(cal.scheduled_principal.min()), unit="USD",
                 note="must be non-negative for the split to be admissible"),
            dict(item="replay_mean_abs_error_pct", value=float(replay["note_value_error_pct"].abs().mean()),
                 unit="percent", note="G3"),
            dict(item="replay_max_abs_error_pct", value=float(replay["note_value_error_pct"].abs().max()),
                 unit="percent", note="G3"),
            dict(item="replay_terminal_error_pct", value=float(replay["note_value_error_pct"].iloc[-1]),
                 unit="percent", note="G3"),
        ]
        + [
            dict(item=f"seasonal_weight_month_{m:02d}", value=float(v), unit="weight, mean 1",
                 note="month-of-year collection intensity recovered from the schedule")
            for m, v in zip(range(1, 13), cal.weights_month_of_year)
        ]
    )

    written = [
        _write(calibration, "calibration.csv"),
        _write(replay, "replay_note_value.csv"),
        _write(pm.history_facts(panel), "history_facts.csv"),
        _write(pd.DataFrame(rows), "Validation_B.csv"),
        _write(summary, "gate_summary.csv"),
        _write(replay[["period", "pool_balance_start", "balance_no_prepay", "survival", "rho",
                       "pv_remaining", "removed_pv_cumulative", "note_value_pred", "note_value_actual",
                       "note_value_error_pct", "removal_from_balance", "removal_from_components",
                       "removal_gap"]], "replay_focus.csv"),
    ]
    return rows, written, dict(calibration=calibration, replay=replay, summary=summary, cal=cal)


def stage_day2(panel: dict) -> tuple[list[dict], list[Path], dict]:
    """Gate G4/G5: replay history through the model waterfall, then measure it."""
    notes = panel["package"]["notes"]
    tranches = wf.tranches_from_notes(notes)
    pool = panel["pool"]

    # The first period aggregates two collection months, so the contract charges
    # two servicing-fee bases and the second one is not disclosed. Taking the
    # disclosed fee for that single period keeps the certificate residual exact.
    override = {str(pool["period"].iloc[0]): float(pool["servicing_fee_paid"].iloc[0])}

    tranche_flows, period_flows = wf.run(
        pool,
        tranches,
        reserve_target=panel["reserve0"],
        servicing_override=override,
    )

    model_returns = mt.tranche_returns(tranche_flows, tranches, pool, start=CLOSING_DATE, price_basis="issue")
    model_par = mt.tranche_returns(tranche_flows, tranches, pool, start=CLOSING_DATE, price_basis="par")

    # Independent reference: the same measures computed straight from disclosed flows.
    reference_flows = panel["note_cf"][
        ["period", "tranche", "coupon_rate", "opening_balance", "principal_paid", "interest_paid", "closing_balance"]
    ].copy()
    reference_returns = mt.tranche_returns(reference_flows, tranches, pool, start=CLOSING_DATE, price_basis="issue")

    rows = vd.gate_g4(tranche_flows, period_flows, panel) + vd.gate_g5(model_returns, reference_returns)

    print(f"[day2] waterfall replayed {len(pool)} periods x {len(tranches)} classes")
    print(f"[day2] principal shortfall total  {period_flows['principal_shortfall'].sum():,.2f} "
          f"(disclosed: {pool['principal_shortfall'].sum():,.2f})")
    print(f"[day2] interest shortfall total   {period_flows['interest_shortfall'].sum():,.2f} "
          f"(disclosed: {pool['interest_shortfall'].sum():,.2f})")
    worst = period_flows.assign(g=(period_flows["certificate"]
                                   - pool["certificate_payment"].to_numpy()).abs()).nlargest(3, "g")
    print("[day2] largest certificate residuals: "
          + ", ".join(f"{r.period} {r.g:,.6f}" for r in worst.itertuples()))
    print()
    print("realised measures (from disclosed flows, ACT/365, price = issue):")
    print(f"  {'class':6}{'price':>9}{'WAL(y)':>9}{'IRR':>9}{'principal':>18}{'interest':>16}{'outstanding':>16}  last pay")
    for _, r in reference_returns.iterrows():
        irr_text = f"{r['irr']*100:7.3f}%" if np.isfinite(r["irr"]) else "   n/a "
        print(f"  {r['tranche']:6}{r['price']:9.5f}{r['wal_years']:9.4f}{irr_text:>9}"
              f"{r['total_principal']:18,.0f}{r['total_interest']:16,.0f}{r['final_balance']:16,.0f}  {r['last_payment']}")

    written = [
        _write(tranche_flows, "waterfall_tranches.csv"),
        _write(period_flows, "waterfall_periods.csv"),
        _write(model_returns, "returns_historical_issue.csv"),
        _write(model_par, "returns_historical_par.csv"),
        _write(reference_returns, "returns_reference_disclosed.csv"),
        _write(pd.DataFrame(rows), "Validation_day2.csv"),
    ]
    return rows, written, dict(tranche_flows=tranche_flows, period_flows=period_flows,
                               returns=model_returns, reference=reference_returns)


def stage_feedback(panel: dict) -> tuple[list[Path], dict]:
    """Outputs requested by role A's reply: split comparison, alignment, repurchases.

    A4 asked for the smooth split and a one-period alignment shift to be kept
    alongside the seasonal split, so the report can show that the seasonal choice
    is a modelling decision rather than a fitting trick. A3 clarified that the
    repurchase *cash* (32.05m, which includes interest) must not be used as a
    principal removal; the *principal* figure is 20.34m.
    """
    pool = panel["pool"]
    schedule, balance0, discount = panel["schedule_amount"], panel["balance0"], panel["discount"]

    # --- 1. split comparison (B3) -------------------------------------------- #
    split_rows = []
    for label, seasonal in (("seasonal (adopted)", True), ("smooth accrual", False)):
        cal = pm.calibrate(schedule, balance0, discount, panel["schedule_months"], seasonal=seasonal)
        replay = pm.replay(panel, cal)
        err = replay["note_value_error_pct"]
        split_rows.append(
            dict(
                split=label,
                accrual_rate=cal.yield_rate,
                accrual_rate_accrued_basis=pm.solve_pool_yield(
                    schedule, panel["pool_including_accruals"], cal.weights_per_month
                ),
                note_value_mean_abs_pct=float(err.abs().mean()),
                note_value_max_abs_pct=float(err.abs().max()),
                note_value_terminal_pct=float(err.iloc[-1]),
                min_scheduled_principal=float(cal.scheduled_principal.min()),
            )
        )
    split_frame = pd.DataFrame(split_rows)

    # --- 2. alignment sensitivity (A4) --------------------------------------- #
    align_rows = []
    for mapping in ("collection_index", "collection_index_heuristic"):
        for shift in (-1, 0, 1):
            trial = pool.copy()
            idx = trial[mapping].to_numpy(dtype=int) + shift
            trial["collection_index"] = np.clip(idx, 0, len(schedule))
            for label, seasonal in (("seasonal", True), ("smooth", False)):
                cal = pm.calibrate(schedule, balance0, discount, panel["schedule_months"], seasonal=seasonal)
                err = pm.replay({"pool": trial}, cal)["note_value_error_pct"]
                align_rows.append(
                    dict(mapping=mapping, shift_periods=shift, split=label,
                         note_value_mean_abs_pct=float(err.abs().mean()),
                         note_value_max_abs_pct=float(err.abs().max()),
                         note_value_terminal_pct=float(err.iloc[-1]))
                )
    align_frame = pd.DataFrame(align_rows)

    # --- 3. repurchase and removal reconciliation (A3 / Q6) ------------------ #
    bridge_path = dc.REFERENCE / "repurchase_bridge.csv"
    bridge = pd.read_csv(bridge_path, encoding="utf-8-sig") if bridge_path.exists() else pd.DataFrame()
    cal = pm.calibrate(schedule, balance0, discount, panel["schedule_months"])
    replay = pm.replay(panel, cal).dropna(subset=["removal_from_balance"])
    prepay = float(pool["prepayment"].sum())
    net_loss = float(pool["net_loss"].sum())
    repurchase_cash = float(pool["repurchases"].sum())
    repurchase_principal = float(bridge["reported_purchased_receivables_principal_usd"].sum()) if len(bridge) else float("nan")
    implied = float(replay["removal_from_balance"].sum())
    recon = pd.DataFrame(
        [
            dict(item="balance_implied_removal_28_periods", value=implied, unit="USD",
                 note="fiscal months with a known successor"),
            dict(item="prepayment_plus_net_loss", value=prepay + net_loss, unit="USD",
                 note="A3: this is the comparison role A recommends"),
            dict(item="gap_vs_prepayment_plus_net_loss", value=implied - (prepay + net_loss), unit="USD",
                 note="A9: diagnostic only, not an identity"),
            dict(item="administrative_repurchase_cash", value=repurchase_cash, unit="USD",
                 note="99.3 item 1(b): includes interest to period end - must NOT be treated as principal"),
            dict(item="reported_purchased_receivables_principal", value=repurchase_principal, unit="USD",
                 note="99.3 item 14(b)(i): the principal figure A3 points to"),
            dict(item="gap_vs_prepayment_netloss_plus_repurchase_principal",
                 value=implied - (prepay + net_loss + repurchase_principal), unit="USD",
                 note="A3 warned this cannot prove prepayment already contains repurchases"),
            dict(item="correlation_implied_vs_prepayment",
                 value=float(np.corrcoef(replay["removal_from_balance"], replay["prepayment"] +
                                         replay["net_loss"])[0, 1]), unit="correlation",
                 note="A4: 0.40 cannot identify a cross-period cause"),
        ]
    )

    written = [
        _write(split_frame, "feedback_split_comparison.csv"),
        _write(align_frame, "feedback_alignment_sensitivity.csv"),
        _write(recon, "feedback_removal_reconciliation.csv"),
    ]
    print("[feedback] split comparison (role A, B3):")
    for _, r in split_frame.iterrows():
        print(f"  {r['split']:18s} rate {r['accrual_rate']*100:6.3f}% / accrued {r['accrual_rate_accrued_basis']*100:6.3f}%"
              f"  NV mean {r['note_value_mean_abs_pct']:.3f}%  max {r['note_value_max_abs_pct']:.3f}%"
              f"  terminal {r['note_value_terminal_pct']:+.3f}%")
    print("[feedback] alignment sensitivity (role A, A4): mean |NV error| over shift -1/0/+1")
    for mapping in ("collection_index", "collection_index_heuristic"):
        for split in ("seasonal", "smooth"):
            vals = align_frame[(align_frame.mapping == mapping) & (align_frame.split == split)]
            print(f"  {mapping:30s} {split:9s} " + "  ".join(f"{v:6.3f}%" for v in vals.note_value_mean_abs_pct))
    print("[feedback] removal reconciliation (role A, A3):")
    for _, r in recon.iterrows():
        if r["unit"] == "USD":
            print(f"  {r['item']:58s} {r['value']:>18,.2f}")
    return written, dict(split=split_frame, alignment=align_frame, reconciliation=recon)


def stage_day3(panel: dict) -> tuple[list[dict], list[Path], dict]:
    """Scenarios, the two-way grid, charts and the legal-final check."""
    notes = panel["package"]["notes"]
    tranches = wf.tranches_from_notes(notes)
    pool = panel["pool"]
    cal = pm.calibrate(panel["schedule_amount"], panel["balance0"], panel["discount"], panel["schedule_months"])

    override = {str(pool["period"].iloc[0]): float(pool["servicing_fee_paid"].iloc[0])}
    historical, _ = wf.run(pool, tranches, reserve_target=panel["reserve0"], servicing_override=override)

    case_set = st.scenarios(panel)
    results = {name: st.run_scenario(panel, cal, tranches, a, historical_flows=historical)
               for name, a in case_set.items()}

    base_rate = st.realised_loss_rate(panel)
    scenario_rows = []
    for name, result in results.items():
        returns = result["returns"].set_index("tranche")
        returns_issue = result["returns_issue"].set_index("tranche")
        legal = result["legal_final"].set_index("tranche")
        for t in ("A-3", "A-4"):
            scenario_rows.append(
                dict(
                    scenario=name,
                    assumptions=result["assumptions"].label,
                    cpr=result["assumptions"].cpr,
                    annual_loss_rate=result["assumptions"].annual_loss_rate,
                    call_enabled=result["assumptions"].call_enabled,
                    clean_up_call=result["call_date"] or "",
                    tranche=t,
                    legal_final=t,
                    wal_years=float(returns.loc[t, "wal_years"]),
                    irr_par=float(returns.loc[t, "irr"]),
                    irr_issue=float(returns_issue.loc[t, "irr"]),
                    cleared_by=legal.loc[t, "cleared_by"] or "",
                    principal_shortfall=float(legal.loc[t, "shortfall"]),
                    certificate_total=float(result["period_flows"]["certificate"].sum()),
                )
            )
    scenario_frame = pd.DataFrame(scenario_rows)

    # Grid: 1x to 10x the realised loss rate, CPR 0-16%.
    # Span far enough to find where the 2.5% overcollateralisation is exhausted:
    # at 10x the realised loss rate the notes are still whole, so a grid capped
    # there would say nothing about credit risk.
    loss_grid = [round(base_rate * m, 6) for m in (1, 2, 4, 8, 12, 16, 24)]
    cpr_grid = [0.0, 0.04, 0.06, 0.08, 0.10, 0.16]
    grid = st.run_grid(panel, cal, tranches, loss_rates=loss_grid, cprs=cpr_grid,
                       call_enabled=False, historical_flows=historical)

    # Loss-absorption ladder: who pays for each increment of loss. This is the
    # table the report's section 5.3 leans on instead of a bare "stress has little
    # effect" statement.
    ladder = st.loss_absorption(panel, cal, tranches, loss_rates=loss_grid, cpr=0.10,
                                historical_flows=historical)

    print(f"[day3] realised loss rate used for Base = {base_rate*100:.3f}%/yr")
    print(f"  {'scenario':32s}{'call':>9}{'A-3 WAL':>9}{'A-3 IRR':>9}{'A-4 WAL':>9}{'A-4 IRR':>9}{'A-3 cleared':>12}")
    for name, result in results.items():
        r = result["returns"].set_index("tranche")
        l = result["legal_final"].set_index("tranche")
        print(f"  {name:32s}{result['call_date'] or 'no call':>9}"
              f"{r.loc['A-3','wal_years']:9.3f}{r.loc['A-3','irr']*100:8.3f}%"
              f"{r.loc['A-4','wal_years']:9.3f}{r.loc['A-4','irr']*100:8.3f}%"
              f"{l.loc['A-3','cleared_by'] or '-':>12}")
    top = grid["annual_loss_rate"].max()
    print(f"[day3] grid {len(grid)} cells, loss up to {top*100:.2f}%/yr ({top/base_rate:.0f}x realised)")
    for cls, col in (("A-3", "a3_shortfall"), ("A-4", "a4_shortfall")):
        hit = grid[grid[col] > 1.0]
        if not len(hit):
            print(f"[day3]   {cls}: no principal shortfall in any cell")
        else:
            for r in hit.itertuples():
                cleared = r.a4_cleared_by if cls == "A-4" else r.a3_cleared_by
                cleared = "" if cleared is None or (isinstance(cleared, float) and cleared != cleared) else cleared
                print(f"[day3]   {cls}: SHORTFALL {getattr(r, col):,.0f} at "
                      f"loss {r.annual_loss_rate*100:.2f}%/yr ({r.annual_loss_rate/base_rate:.0f}x), "
                      f"CPR {r.cpr*100:.0f}%, "
                      + (f"cleared {cleared}" if cleared else "still open when the contract schedule runs out (2030-05)"))
    print("[day3] loss absorption (CPR 10%, no call): absorbed by certificate / reserve / notes")
    for r in ladder.itertuples():
        print(f"  {r.multiple_of_realised:5.1f}x  loss +{r.loss_increase_vs_first:>13,.0f}"
              f"  cert {r.absorbed_by_certificate:>13,.0f}  reserve {r.absorbed_by_reserve:>12,.0f}"
              f"  notes {r.absorbed_by_notes:>10,.0f}")
    wiped = grid.loc[grid["certificate_total"] <= 1.0, "annual_loss_rate"]
    print(f"[day3]   certificate cash reaches zero at {wiped.min()*100:.2f}%/yr = "
          f"{wiped.min()/base_rate:.1f}x realised"
          if len(wiped) else "[day3]   certificate cash stays positive across the whole grid")

    # ---------------- charts ---------------- #
    charts_written = []
    try:
        actual = pool[["period", "note_value"]].copy()
        actual["model"] = np.nan
        base_path = results["Base"]["path"]
        line_frame = pd.concat(
            [
                pd.DataFrame(dict(period=pool["period"], actual=pool["note_value"], model=np.nan)),
                pd.DataFrame(dict(period=base_path["period"], actual=np.nan,
                                  model=base_path["note_value_end"])),
            ],
            ignore_index=True,
        )
        charts_written.append(
            ch.save(
                ch.line_chart(line_frame, "period",
                              {"actual": "disclosed Note Value", "model": "Base projection"},
                              title="Note Value: disclosed history and Base projection",
                              ylabel="USD"),
                CHARTS / "01_note_value_path.svg",
            )
        )
        balance_rows = []
        for name in ("Base", "Moderate", "Severe"):
            path = results[name]["path"]
            for _, r in path.iterrows():
                balance_rows.append(dict(period=r["period"], scenario=name, balance=r["pool_balance_start"]))
        # wide form for the line chart
        wide = pd.DataFrame(balance_rows).pivot(index="period", columns="scenario", values="balance").reset_index()
        charts_written.append(
            ch.save(
                ch.line_chart(wide, "period", {c: c for c in ("Base", "Moderate", "Severe")},
                              title="Projected pool balance by scenario", ylabel="USD"),
                CHARTS / "02_pool_balance_scenarios.svg",
            )
        )
        grid_wal = grid.assign(loss_pct=(grid["annual_loss_rate"] * 100).round(2),
                               cpr_pct=(grid["cpr"] * 100).round(0).astype(int))
        charts_written.append(
            ch.save(
                ch.heatmap(grid_wal, row="loss_pct", column="cpr_pct", value="a3_wal",
                           title="A-3 WAL (years) - no clean-up call", fmt="{:.2f}",
                           unit_note="rows: annual net-loss rate %　columns: CPR %"),
                CHARTS / "03_heatmap_a3_wal.svg",
            )
        )
        charts_written.append(
            ch.save(
                ch.heatmap(grid_wal, row="loss_pct", column="cpr_pct", value="a4_wal",
                           title="A-4 WAL (years) - no clean-up call", fmt="{:.2f}",
                           unit_note="rows: annual net-loss rate %　columns: CPR %"),
                CHARTS / "04_heatmap_a4_wal.svg",
            )
        )
        cert = grid_wal.assign(cert_m=(grid_wal["certificate_total"] / 1e6).round(2))
        charts_written.append(
            ch.save(
                ch.heatmap(cert, row="loss_pct", column="cpr_pct", value="cert_m",
                           title="Certificate cash (USD millions) - no clean-up call", fmt="{:.1f}",
                           unit_note="rows: annual net-loss rate %　columns: CPR %"),
                CHARTS / "05_heatmap_certificate.svg",
            )
        )
        names = [n for n in results if "no clean-up call" not in n]
        charts_written.append(
            ch.save(
                ch.bar_chart(
                    names,
                    {"A-3": [results[n]["returns"].set_index("tranche").loc["A-3", "wal_years"] for n in names],
                     "A-4": [results[n]["returns"].set_index("tranche").loc["A-4", "wal_years"] for n in names]},
                    title="WAL by scenario (years, clean-up call available)", ylabel="years",
                ),
                CHARTS / "06_wal_by_scenario.svg",
            )
        )
        print(f"[day3] wrote {len(charts_written)} SVG charts to {CHARTS}")
    except Exception as exc:  # charts must never block the numbers
        print(f"[day3] CHART FAILURE: {exc!r}")

    rows = vd.gate_g6(results, grid, base_rate)
    written = [
        _write(scenario_frame, "scenario_results.csv"),
        _write(grid, "grid_surface.csv"),
        _write(ladder, "loss_absorption.csv"),
    ] + charts_written
    return rows, written, dict(results=results, grid=grid, scenarios=scenario_frame)


def print_summary(summary: pd.DataFrame, replay: pd.DataFrame) -> bool:
    print()
    print("gate  pass fail missing  ok  description")
    for _, row in summary.iterrows():
        print(f"{row['gate']:4s} {row['pass']:5d} {row['fail']:4d} {row['missing']:7d}  "
              f"{'yes' if row['ok'] else 'NO '}  {row['description']}")
    ok = bool(summary["ok"].all())

    print()
    print("Note Value replay (worst five periods):")
    worst = replay.reindex(replay["note_value_error_pct"].abs().sort_values(ascending=False).index).head(5)
    for _, row in worst.iterrows():
        print(f"  {row['period']}  predicted {row['note_value_pred']:>18,.2f}  "
              f"actual {row['note_value_actual']:>18,.2f}  error {row['note_value_error_pct']:+.3f}%")

    print()
    print("Removal reconciliation (balance-implied vs disclosed components):")
    implied = float(replay["removal_from_balance"].dropna().sum())
    both = float((replay["prepayment"] + replay["net_loss"] + replay["repurchases"]).sum())
    no_rep = float((replay["prepayment"] + replay["net_loss"]).sum())
    print(f"  balance-implied removal                  = {implied:>18,.2f}")
    print(f"  prepayment + net_loss + repurchases      = {both:>18,.2f}   gap {implied - both:>+16,.2f}")
    print(f"  prepayment + net_loss                    = {no_rep:>18,.2f}   gap {implied - no_rep:>+16,.2f}")
    print("  no variant closes exactly: the certificate never splits collections into principal and interest,")
    print("  so the model carries this as a documented limitation rather than an identity.")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["data", "spike", "feedback", "day2", "day3", "all"], default="all")
    args = parser.parse_args(argv)

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    panel, written = stage_data() if args.stage in ("data", "all", "day2", "feedback") else (dc.load_all(), [])
    ok = True
    all_rows: list[dict] = []
    if args.stage in ("feedback", "all"):
        fb_written, _ = stage_feedback(panel)
        written += fb_written
    if args.stage in ("spike", "all"):
        rows, spike_written, bundle = stage_spike(panel)
        written += spike_written
        all_rows += rows
        ok = print_summary(bundle["summary"], bundle["replay"]) and ok
    if args.stage in ("day2", "day3", "all"):
        rows2, day2_written, _ = stage_day2(panel)
        written += day2_written
        all_rows += rows2
    if args.stage in ("day3", "all"):
        rows3, day3_written, _ = stage_day3(panel)
        written += day3_written
        all_rows += rows3
    if all_rows:
        summary = vd.gate_summary(all_rows)
        ok = bool(summary["ok"].all())
        vd.write(all_rows, TABLES / "Validation_B.csv")
        _write(summary, "gate_summary.csv")
        print()
        print("gate  pass fail missing  ok  description")
        for _, row in summary.iterrows():
            print(f"{row['gate']:4s} {row['pass']:5d} {row['fail']:4d} {row['missing']:7d}  "
                  f"{'yes' if row['ok'] else 'NO '}  {row['description']}")
    else:
        summary = None

    print()
    print(f"ran at {started}; wrote {len(written) + 2} tables to {TABLES}")
    if not ok:
        print("\nRESULT: one or more gates FAILED - do not use these outputs for the report", file=sys.stderr)
        return 1
    print("\nRESULT: all gates passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
