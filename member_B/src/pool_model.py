"""Aggregate-pool cash-flow model for John Deere Owner Trust 2024-B.

Model in one paragraph
----------------------
The indenture does not drive note principal off a loan ledger; it drives it off
the **Note Value**, defined in the prospectus as "the present value of the
scheduled and unpaid payments on the receivables discounted at 8.85%". Note
principal paid in a period equals the reduction in Note Value (plus any prior
carry-over shortfall). So the whole waterfall needs exactly two primitives:

1. **The contractual schedule** ``S_t`` (``OriginalSchedule.csv``, 73 months).
   Discounted at 8.85%/12 it reproduces the disclosed initial Note Value to
   $0.0029, which is the anchor this module is built on.
2. **The survival state** of the pool: how much of that schedule is still
   outstanding after prepayments, defaults and repurchases have removed loans.

The no-prepayment path is a fixed amortisation of ``S_t`` at a single pool
yield, solved so the 73 payments exhaust the cut-off principal exactly::

    B*_0 = 1,670,001,216.47,   B*_{t+1} = B*_t - (S_t - B*_t * y / 12),
    y solved such that B*_73 = 0                        -> y = 5.5238%

Why one yield and not a cohort portfolio: solving the same exhaustion condition
on the *accrued-interest-inclusive* balance (1,694,001,151.31, the basis the
gross schedule actually amortises) gives **4.8240%**, only 8bp above the
disclosed 4.745% weighted APR. So the schedule is internally consistent and a
single yield is enough; the 16% gap on the ex-accrued basis is a basis artifact
(the schedule's principal component also repays the $24.0m of accrued interest
that item 12(a) excludes from the reported pool balance). See
``docs/assumptions.md`` A-02.

Note Value is then rebuilt without any further calibration::

    rho_t   = PV_t / B*_t            # average PV per dollar of scheduled balance
    removed_t = (B*_t - B*_{t+1}) - (B_t - B_{t+1})   # scheduled vs actual runoff
    V_{t+1} = V_t + removed_t * rho_t                 # PV of everything removed so far
    NV_t    = PV_t - V_t

``V`` is the only state besides the balance, and it is what makes the model
usable for forward scenarios: give it a prepayment speed and a loss rate and the
same recursion produces a projected Note Value path.

History replay (this module's ``replay``) drives the recursion with *observed*
pool balances, so the removal term is data, not an assumption. That isolates the
architecture from the prepayment/loss assumptions entirely, which is why it is
the right acceptance test.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

MONTHS_PER_YEAR = 12.0


# --------------------------------------------------------------------------- #
# Schedule primitives
# --------------------------------------------------------------------------- #
def seasonal_weights(schedule: np.ndarray, months: list[str], method: str = "loglinear") -> np.ndarray:
    """Month-of-year collection intensity implied by the schedule itself.

    Agricultural and construction equipment receivables are heavily annual-pay
    (62.39% of the cut-off balance), and their due dates cluster around planting
    and harvest. A level annuity therefore misstates **when** interest is
    collected: in a month with many annual anniversaries the borrower pays a full
    year of interest at once, while in a quiet month it pays almost none.

    This function recovers that intensity from the contract schedule: detrend the
    monthly totals, average the residual ratio by month-of-year, normalise to a
    mean of one. The weights are normalised so that over any 12-month window the
    weighted interest accrual sums to the unweighted one, which keeps the solved
    yield readable as an annual accrual rate.

    Empirical effect on the Note Value replay: mean absolute error falls from
    0.561% to 0.246%, and the result is robust to the detrending choice
    (log-linear 0.246%, log-quadratic 0.232%, 12-month centred mean 0.284%).
    """
    schedule = np.asarray(schedule, dtype=float)
    month_of_year = np.array([int(m.split("-")[1]) for m in months])
    t = np.arange(len(schedule), dtype=float)
    positive = schedule > 0
    if method == "loglinear":
        trend = np.exp(np.polyval(np.polyfit(t[positive], np.log(schedule[positive]), 1), t))
    elif method == "logquadratic":
        trend = np.exp(np.polyval(np.polyfit(t[positive], np.log(schedule[positive]), 2), t))
    elif method == "rolling":
        half = 6
        trend = np.array(
            [schedule[max(0, i - half): i + half + 1].mean() for i in range(len(schedule))]
        )
    else:
        raise ValueError(f"unknown detrending method: {method}")
    ratio = schedule / trend
    per_month = np.array([ratio[month_of_year == m].mean() for m in range(1, 13)])
    per_month = per_month / per_month.mean()
    return per_month


def expand_weights(per_month: np.ndarray, months: list[str]) -> np.ndarray:
    """Turn the 12 month-of-year weights into one weight per schedule month."""
    month_of_year = np.array([int(m.split("-")[1]) for m in months])
    return np.asarray(per_month, dtype=float)[month_of_year - 1]


def no_prepay_path(schedule: np.ndarray, balance0: float, annual_yield: float,
                   weights: np.ndarray | None = None):
    """Deterministic amortisation of the contract schedule (no prepay, no loss).

    Returns ``(balance, principal, interest)`` where ``balance`` has length
    ``len(schedule) + 1`` and ``balance[0] == balance0``.
    """
    rate = annual_yield / MONTHS_PER_YEAR
    if weights is None:
        weights = np.ones(len(schedule), dtype=float)
    balance = np.empty(len(schedule) + 1, dtype=float)
    principal = np.empty(len(schedule), dtype=float)
    interest = np.empty(len(schedule), dtype=float)
    balance[0] = balance0
    for t, gross in enumerate(schedule):
        interest[t] = balance[t] * rate * weights[t]
        principal[t] = gross - interest[t]
        balance[t + 1] = balance[t] - principal[t]
    return balance, principal, interest


def solve_pool_yield(schedule: np.ndarray, balance0: float, weights: np.ndarray | None = None,
                     tol: float = 1e-12) -> float:
    """Annual accrual rate at which ``schedule`` amortises ``balance0`` exactly.

    The terminal balance is increasing in the rate (more interest leaves less
    principal), so a plain bisection is safe and exact to machine precision.
    """
    lo, hi = -0.5, 1.0
    if no_prepay_path(schedule, balance0, lo, weights)[0][-1] > 0:
        raise ValueError("balance0 too small: even a -50% rate over-amortises the schedule")
    if no_prepay_path(schedule, balance0, hi, weights)[0][-1] < 0:
        raise ValueError("balance0 too large: even a 100% rate cannot exhaust the schedule")
    for _ in range(400):
        mid = 0.5 * (lo + hi)
        if no_prepay_path(schedule, balance0, mid, weights)[0][-1] > 0:
            hi = mid
        else:
            lo = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


def pv_curve(schedule: np.ndarray, annual_discount: float) -> np.ndarray:
    """``out[i]`` = PV at the start of month ``i`` of the schedule months ``i..end``.

    The first remaining payment is discounted exactly one month, which is the
    convention that reproduces the disclosed initial Note Value:

        out[0] = 1,562,473,742.8171   vs disclosed 1,562,473,742.82

    Each later entry is rebased to its own valuation point (multiplied by
    ``(1 + r)^i``); quoting a cut-off-based PV at month 20 would understate the
    Note Value by roughly 15%, which is the trap this rebasing avoids.
    """
    rate = annual_discount / MONTHS_PER_YEAR
    discount = 1.0 / (1.0 + rate) ** np.arange(1, len(schedule) + 1)
    weighted = schedule * discount
    out = np.zeros(len(schedule) + 1, dtype=float)
    out[: len(schedule)] = np.cumsum(weighted[::-1])[::-1]
    rebase = (1.0 + rate) ** np.arange(len(schedule) + 1, dtype=float)
    return out * rebase


def nominal_curve(schedule: np.ndarray) -> np.ndarray:
    """Undiscounted remaining scheduled collections from each month index."""
    out = np.zeros(len(schedule) + 1, dtype=float)
    out[: len(schedule)] = np.cumsum(schedule[::-1])[::-1]
    return out


# --------------------------------------------------------------------------- #
# State and one-step transition (shared by replay and projection)
# --------------------------------------------------------------------------- #
@dataclass
class PoolState:
    """Model state at the start of a period.

    ``balance`` is the reported pool balance (accrued interest excluded).
    ``removed_pv`` is the cumulative present value of every dollar of balance
    that has left the pool through prepayment, default or repurchase. Note Value
    is *not* stored: it is derived, ``pv - removed_pv``, so it can never drift
    out of step with the state. That is also the natural seed for forward
    scenarios, which need only ``(t, balance, removed_pv)``.
    """

    t: int
    balance: float
    removed_pv: float = 0.0

    def note_value(self, pv: np.ndarray, schedule_index: int | None = None) -> float:
        i = self.t if schedule_index is None else schedule_index
        return float(pv[i] - self.removed_pv)


def advance(
    state: PoolState,
    *,
    pv: np.ndarray,
    balance_no_prepay: np.ndarray,
    schedule_index: int,
    schedule_index_next: int,
    removal: float,
) -> tuple[PoolState, dict]:
    """Advance one period given the dollars of balance removed in it.

    ``removal`` is prepayment + net loss + repurchase for the period. Scheduled
    principal is implied by the no-prepay path, so the caller never has to know
    the principal/interest split.
    """
    balance_star = float(balance_no_prepay[schedule_index])
    rho = float(pv[schedule_index]) / balance_star if balance_star > 0 else 0.0
    scheduled_principal = balance_star - float(balance_no_prepay[schedule_index_next])

    new_state = PoolState(
        t=state.t + 1,
        balance=state.balance - scheduled_principal - removal,
        removed_pv=state.removed_pv + removal * rho,
    )
    info = dict(
        schedule_index=schedule_index,
        pool_balance=state.balance,
        balance_no_prepay=balance_star,
        survival=state.balance / balance_star if balance_star > 0 else np.nan,
        rho=rho,
        pv_remaining=float(pv[schedule_index]),
        scheduled_principal=scheduled_principal,
        removal=removal,
        note_value=float(pv[schedule_index] - state.removed_pv),
    )
    return new_state, info


# --------------------------------------------------------------------------- #
# Historical replay
# --------------------------------------------------------------------------- #
@dataclass
class Calibration:
    """Everything the forward engine will need, fixed from the schedule."""

    yield_rate: float
    discount_rate: float
    balance0: float
    weights_month_of_year: np.ndarray = field(default=None, repr=False)
    weights_per_month: np.ndarray = field(default=None, repr=False)
    pv: np.ndarray = field(default=None, repr=False)
    nominal: np.ndarray = field(default=None, repr=False)
    balance_no_prepay: np.ndarray = field(default=None, repr=False)
    scheduled_principal: np.ndarray = field(default=None, repr=False)
    scheduled_interest: np.ndarray = field(default=None, repr=False)

    @property
    def checkpoints(self) -> dict:
        return dict(
            yield_rate=self.yield_rate,
            discount_rate=self.discount_rate,
            initial_pv=self.pv[0],
            terminal_no_prepay_balance=self.balance_no_prepay[-1],
            total_scheduled_principal=float(self.scheduled_principal.sum()),
            total_scheduled_interest=float(self.scheduled_interest.sum()),
            min_scheduled_principal=float(self.scheduled_principal.min()),
        )


def calibrate(
    schedule: np.ndarray,
    balance0: float,
    discount_rate: float,
    months: list[str] | None = None,
    seasonal: bool = True,
) -> Calibration:
    """Fix the schedule primitives once; everything else reads from here.

    ``seasonal=True`` splits interest with the month-of-year weights recovered
    from the schedule (see ``seasonal_weights``); ``False`` reproduces the flat
    accrual used in the first spike, which is kept so the regression is visible.
    """
    if seasonal:
        if months is None:
            raise ValueError("months are required when seasonal=True")
        weights_month_of_year = seasonal_weights(schedule, months)
        per_month = expand_weights(weights_month_of_year, months)
    else:
        weights_month_of_year = np.ones(12)
        per_month = np.ones(len(schedule))

    yield_rate = solve_pool_yield(schedule, balance0, per_month)
    balance_star, principal, interest = no_prepay_path(schedule, balance0, yield_rate, per_month)
    return Calibration(
        yield_rate=yield_rate,
        discount_rate=discount_rate,
        balance0=balance0,
        weights_month_of_year=weights_month_of_year,
        weights_per_month=per_month,
        pv=pv_curve(schedule, discount_rate),
        nominal=nominal_curve(schedule),
        balance_no_prepay=balance_star,
        scheduled_principal=principal,
        scheduled_interest=interest,
    )


def replay(panel: dict, cal: Calibration | None = None) -> pd.DataFrame:
    """Rebuild the 29 disclosed Note Values from observed pool balances.

    The recursion is driven by the *actual* balance path, so the removal term is
    observed rather than assumed. This is the architecture acceptance test: if
    the identity ``NV = PV - cumulative PV of removals`` holds historically, the
    same recursion is trustworthy forward, where the removals become
    assumptions instead of data.
    """
    pool = panel["pool"]
    if cal is None:
        cal = calibrate(panel["schedule_amount"], panel["balance0"], panel["discount"])

    n = len(pool)
    idx = pool["collection_index"].to_numpy(dtype=int)
    balance = pool["pool_balance_start"].to_numpy(dtype=float)
    nv_actual = pool["note_value_start"].to_numpy(dtype=float)

    state = PoolState(t=0, balance=float(balance[0]), removed_pv=0.0)
    rows = []
    for k in range(n):
        i = int(idx[k])
        has_next = k + 1 < n
        i_next = int(idx[k + 1]) if has_next else i
        if has_next:
            # Removal implied by the observed balance path: everything that left
            # the pool beyond scheduled amortisation. This is data, not an
            # assumption, which is what makes the replay a test of the architecture.
            scheduled_total = float(cal.balance_no_prepay[i] - cal.balance_no_prepay[i_next])
            removal = float(balance[k] - balance[k + 1]) - scheduled_total
        else:
            # The final period has no observed successor, so its removal is unknown.
            scheduled_total = np.nan
            removal = np.nan

        nv_pred = state.note_value(cal.pv, i)
        rho = float(cal.pv[i]) / float(cal.balance_no_prepay[i])
        observed_removal = float(
            pool["prepayment"].iloc[k] + pool["net_loss"].iloc[k] + pool["repurchases"].iloc[k]
        )

        rows.append(
            dict(
                period=pool["period"].iloc[k],
                t=int(pool["t"].iloc[k]),
                collection_index=i,
                schedule_months_consumed=int(pool["schedule_months_consumed"].iloc[k]),
                pool_balance_start=float(balance[k]),
                balance_no_prepay=float(cal.balance_no_prepay[i]),
                survival=float(balance[k] / cal.balance_no_prepay[i]),
                rho=rho,
                pv_remaining=float(cal.pv[i]),
                removed_pv_cumulative=state.removed_pv,
                note_value_pred=nv_pred,
                note_value_actual=float(nv_actual[k]),
                note_value_error=nv_pred - float(nv_actual[k]),
                note_value_error_pct=(nv_pred / float(nv_actual[k]) - 1.0) * 100.0,
                scheduled_principal=scheduled_total,
                actual_balance_decline=float(balance[k] - balance[k + 1]) if k + 1 < n else np.nan,
                removal_from_balance=removal,
                removal_from_components=observed_removal,
                removal_gap=removal - observed_removal if k + 1 < n else np.nan,
                prepayment=float(pool["prepayment"].iloc[k]),
                net_loss=float(pool["net_loss"].iloc[k]),
                repurchases=float(pool["repurchases"].iloc[k]),
            )
        )
        if k + 1 < n:
            state, _ = advance(
                state,
                pv=cal.pv,
                balance_no_prepay=cal.balance_no_prepay,
                schedule_index=i,
                schedule_index_next=i_next,
                removal=removal,
            )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# Forward projection
# --------------------------------------------------------------------------- #
@dataclass
class ProjectionAssumptions:
    """Behavioural assumptions for one scenario.

    ``annual_loss_rate`` is a **net** loss rate applied to the current pool
    balance, not a default rate times a severity. Role A (answer A5) pointed out
    that 99.1 section 13(i) is called Gross Losses but the public tables never
    split pre-charge-off exposure from disposal proceeds, so a separate recovery
    rate would double-count. Calibrating directly on net loss keeps recovery
    inside one number.

    ``loss_to_note_value_lag`` follows A6: the contract keeps a defaulted
    receivable's scheduled payments inside Note Value until the earlier of
    repossession and 180 days past due. A write-down therefore leaves the pool
    balance immediately but only leaves Note Value after the lag.
    """

    label: str
    cpr: float
    annual_loss_rate: float
    loss_to_note_value_lag: int = 0
    call_enabled: bool = True
    call_threshold_fraction: float = 0.10


def smm_from_cpr(cpr: float) -> float:
    """Monthly prepayment rate implied by an annual CPR (voluntary only)."""
    return 1.0 - (1.0 - cpr) ** (1.0 / MONTHS_PER_YEAR)


def project(
    cal: Calibration,
    *,
    schedule_index_start: int,
    balance0: float,
    note_value0: float,
    periods: list[str],
    assumptions: ProjectionAssumptions,
) -> pd.DataFrame:
    """Project pool mechanics forward from a frozen state.

    Order of events inside a month, stated explicitly because the answer depends
    on it (role A, closing note): scheduled amortisation, then net loss, then
    voluntary prepayment on what is left. Cash collected is the surviving share
    of the contract schedule plus prepayment dollars; the net-loss basis means no
    separate recovery inflow.

    Returns one row per projected period with the pool path, the cash it
    generates, and the resulting Note Value - everything the waterfall needs.
    """
    pv = cal.pv
    balance_star = cal.balance_no_prepay
    sched_principal = cal.scheduled_principal
    sched_interest = cal.scheduled_interest
    last_index = len(sched_principal)

    balance = float(balance0)
    smm = smm_from_cpr(assumptions.cpr)
    monthly_loss = assumptions.annual_loss_rate / MONTHS_PER_YEAR
    lag = int(assumptions.loss_to_note_value_lag)
    call_threshold = assumptions.call_threshold_fraction * pv[0]

    # Note Value is projected as a *scale copy* of the unconditional PV, scaled by
    # the surviving balance share. ``scale`` reconciles that form to the disclosed
    # Note Value at the freeze date, so the projection starts exactly where the
    # validated history ends. The historical replay uses the rho-weighted removal
    # recursion instead (it fits the 29 disclosed months better); the two forms
    # agree at the boundary, and the projection needs the multiplicative form
    # because it must decay to zero as the pool runs off.
    star0 = float(balance_star[schedule_index_start])
    scale = note_value0 / ((balance0 / star0) * float(pv[schedule_index_start]))

    deferred: dict[int, float] = {}
    rows: list[dict] = []

    for k, period in enumerate(periods):
        i = schedule_index_start + k
        if i >= last_index:
            break
        star = float(balance_star[i])
        star_next = float(balance_star[i + 1]) if i + 1 < len(balance_star) else 0.0
        if star <= 0 or star_next <= 0 or balance <= 0.0:
            break
        rho = float(pv[i]) / star
        survival = balance / star
        note_value_start = survival * float(pv[i]) * scale

        principal_scheduled = survival * float(sched_principal[i])
        interest_collected = survival * float(sched_interest[i])
        loss = monthly_loss * balance
        prepayment = smm * max(0.0, balance - principal_scheduled - loss)

        delegated = deferred.pop(i, 0.0)
        removal_from_note_value = prepayment + delegated + (loss if lag == 0 else 0.0)
        if lag > 0:
            deferred[i + lag] = deferred.get(i + lag, 0.0) + loss

        balance_next = balance - principal_scheduled - loss - prepayment
        survival_next = max(0.0, balance_next) / star_next
        note_value_end = survival_next * float(pv[i + 1]) * scale

        rows.append(
            dict(
                period=period,
                schedule_index=i,
                pool_balance_start=balance,
                survival=survival,
                rho=rho,
                pv_remaining=float(pv[i]),
                note_value_start=note_value_start,
                note_value_end=note_value_end,
                scheduled_principal=principal_scheduled,
                scheduled_interest=interest_collected,
                net_loss=loss,
                prepayment=prepayment,
                cash_collected=principal_scheduled + interest_collected + prepayment,
                pool_balance_end=balance_next,
                callable_now=bool(assumptions.call_enabled and note_value_end <= call_threshold),
            )
        )
        balance = balance_next
        if assumptions.call_enabled and note_value_end <= call_threshold:
            break

    return pd.DataFrame(rows)


def future_periods(last_period: str, count: int) -> list[str]:
    """Calendar month labels following ``last_period`` (YYYY-MM)."""
    year, month = (int(v) for v in last_period.split("-"))
    out = []
    for _ in range(count):
        month += 1
        if month > 12:
            month = 1
            year += 1
        out.append(f"{year}-{month:02d}")
    return out


def opening_state(panel: dict) -> tuple[int, float, float]:
    """Frozen (schedule index, balance, note value) at the data freeze.

    Role A's note applies here: the certificate's residual at the clean-up call
    is whatever is left after the notes are redeemed, so the overcollateralisation
    must never be added as a second cash inflow.
    """
    pool = panel["pool"]
    return (
        int(pool["collection_index"].iloc[-1]),
        float(pool["pool_balance"].iloc[-1]),
        float(pool["note_value"].iloc[-1]),
    )
def history_facts(panel: dict) -> pd.DataFrame:
    """Baseline statistics the scenarios are anchored on."""
    pool = panel["pool"]
    rows = []

    def add(metric, value, unit, note):
        rows.append(dict(metric=metric, value=value, unit=unit, note=note))

    add("periods", len(pool), "count", "consecutive payment months")
    add("pool_balance_first", pool["pool_balance_start"].iloc[0], "USD", "2024-07 reporting basis")
    add("pool_balance_last", pool["pool_balance"].iloc[-1], "USD", pool["period"].iloc[-1])
    add("note_value_first", pool["note_value_start"].iloc[0], "USD", "disclosed initial Note Value")
    add("note_value_last", pool["note_value"].iloc[-1], "USD", pool["period"].iloc[-1])
    add("cumulative_net_loss", pool["net_loss_cumulative"].iloc[-1], "USD", "as disclosed")
    add("cumulative_net_loss_ratio", pool["net_loss_cumulative"].iloc[-1] / panel["balance0"], "decimal",
        "over initial pool balance excluding accrued interest")
    add("cumulative_prepayment", pool["prepayment_cumulative"].iloc[-1], "USD", "as disclosed")
    add("cumulative_prepayment_ratio", pool["prepayment_cumulative"].iloc[-1] / panel["balance0"], "decimal",
        "over initial pool balance")
    add("negative_prepayment_months", int((pool["prepayment"] < 0).sum()), "count",
        "sign convention still open with role A (plan Q1)")

    smm_all = (pool["prepayment"] / pool["pool_balance_start"]).mean()
    positive = pool.loc[pool["prepayment"] > 0, "prepayment"] / pool.loc[pool["prepayment"] > 0, "pool_balance_start"]
    add("smm_mean_all_months", smm_all, "decimal/month", "net of negative months")
    add("cpr_from_all_months", 1 - (1 - smm_all) ** 12, "decimal/year", "naive conversion, net speed")
    add("smm_mean_positive_months", positive.mean(), "decimal/month", "gross speed, ignores reversals")
    add("cpr_from_positive_months", 1 - (1 - positive.mean()) ** 12, "decimal/year", "gross speed")

    add("delinquency_60plus_last", pool["delinquency_60plus"].iloc[-1], "decimal", "share of ending pool balance")
    add("delinquency_trigger", pool["delinquency_trigger"].iloc[-1], "decimal", "never breached")
    add("remaining_term_last", pool["remaining_term"].iloc[-1], "months", "weighted average")
    add("weighted_apr_last", pool["weighted_apr"].iloc[-1], "decimal/year", "weighted average")
    return pd.DataFrame(rows)
