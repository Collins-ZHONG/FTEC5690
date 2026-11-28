"""Priority-of-payments waterfall for John Deere Owner Trust 2024-B.

The order below is taken verbatim from the prospectus ("Priority of Payments",
printed p91). Two features matter and are easy to get wrong:

1. **Principal ranks ahead of the servicing fee.** Item 4 is principal on the
   notes; the servicing fee is item 6. The prospectus attaches a proviso: if JDCC
   or an affiliate is *not* the servicer, the fee moves to the front. JDCC is the
   servicer here, so the subordinated order applies.
2. **The asset representations review fee is capped** at $200,000 in any calendar
   year (item 2); the excess falls to item 9. Both were zero in all 29 periods,
   so this only matters for completeness.

Principal is driven by the Note Value, not by a loan ledger::

    principal distributable = (Note Value at start - Note Value at end)
                              + principal carry-over shortfall

and is then *capped by available cash*. The difference becomes the new
carry-over shortfall. Historically the shortfall was zero in all 29 periods, and
G4 asserts that it stays zero when the model replays history. Under stress the
shortfall is the channel through which losses reach the notes: a loss lowers Note
Value, which raises the principal *claim* without raising cash.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# (label, prospectus item)
PRIORITY_LABELS = {
    1: "administration fee",
    2: "asset representations review fees, aggregate cap $200,000 per calendar year",
    3: "interest on the notes",
    4: "principal on the notes, class by class",
    5: "deposit to the reserve account",
    6: "servicing fee (subordinated to principal while JDCC is servicer)",
    7: "indenture trustee fees",
    8: "owner trustee fees",
    9: "asset representations review fees above the annual cap",
    10: "remaining amounts to the certificate",
}

PRINCIPAL_RANK_GROUPS = {1: ("A-1",), 2: ("A-2A", "A-2B"), 3: ("A-3",), 4: ("A-4",)}


@dataclass(frozen=True)
class Tranche:
    name: str
    original_balance: float
    fixed_coupon: float | None      # None => floating, read the period reset rate
    day_count_field: str            # "days_30_360" or "days_act_360"
    rank: int
    issue_price: float
    legal_final: str

    def rate(self, row) -> float:
        return float(row["a2b_rate"]) if self.fixed_coupon is None else self.fixed_coupon


def tranches_from_notes(notes: pd.DataFrame) -> list[Tranche]:
    """Build the deal structure from role A's ``Notes.csv``."""
    out = []
    for _, row in notes.iterrows():
        floating = str(row["rate_type"]).strip().lower() == "floating"
        day_count = "days_act_360" if str(row["day_count"]).strip().upper() == "ACT/360" else "days_30_360"
        out.append(
            Tranche(
                name=str(row["tranche"]),
                original_balance=float(row["original_balance_usd"]),
                fixed_coupon=None if floating else float(row["fixed_coupon_decimal"]),
                day_count_field=day_count,
                rank=int(row["principal_rank"]),
                issue_price=float(row["issue_price_per_100"]) / 100.0,
                legal_final=str(row["legal_final_payment"]),
            )
        )
    return out


def allocate_principal(amount: float, openings: dict[str, float], tranches: list[Tranche]) -> dict[str, float]:
    """Apply ``amount`` of principal in the contractual class order.

    Within rank 2 the two A-2 classes share **pro rata by original principal**
    (member A verified this to 1e-9 of a dollar per period), not by current
    balance - that is why ``original_balance`` is the weight here.
    """
    paid = {t.name: 0.0 for t in tranches}
    remaining = max(0.0, float(amount))
    by_name = {t.name: t for t in tranches}
    for rank in sorted(PRINCIPAL_RANK_GROUPS):
        if remaining <= 1e-12:
            break
        group = [by_name[n] for n in PRINCIPAL_RANK_GROUPS[rank] if n in by_name]
        outstanding = {t.name: max(0.0, openings.get(t.name, 0.0)) for t in group}
        total = sum(outstanding.values())
        if total <= 0.0:
            continue
        take = min(remaining, total)
        if len(group) == 1:
            paid[group[0].name] += take
        else:
            weights = {t.name: t.original_balance for t in group}
            weight_total = sum(weights.values())
            shares = {t.name: take * (weights[t.name] / weight_total if weight_total else 1 / len(group)) for t in group}
            # A class cannot receive more than it still has outstanding; spill over.
            capped = {n: min(shares[n], outstanding[n]) for n in shares}
            spare = take - sum(capped.values())
            if spare > 1e-12:
                room = {n: outstanding[n] - capped[n] for n in shares}
                room_total = sum(room.values())
                if room_total > 0:
                    for n in capped:
                        capped[n] += spare * room[n] / room_total
            paid.update(capped)
        remaining -= take
    return paid


def run(
    panel: pd.DataFrame,
    tranches: list[Tranche],
    *,
    cash: np.ndarray | None = None,
    reserve_target: float,
    administration_fee: float = 100.0,
    servicing_rate: float = 0.01,
    servicing_override: dict[str, float] | None = None,
    initial_balances: dict[str, float] | None = None,
    draw_reserve: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run the waterfall over ``panel``'s periods.

    ``cash`` overrides the total distribution amount: omit it to replay history
    from disclosed cash (gate G4), supply it to run a projected scenario.

    ``initial_balances`` sets the opening class balances. It is required for any
    forward run: without it the waterfall would restart classes at their original
    size and silently double-count the principal already repaid.

    ``servicing_override`` exists for one documented case: the first period
    aggregates two collection months, so the contract charges *two* monthly bases
    and the second one (the end-of-May pool balance) is not disclosed on the
    certificate. Supplying the disclosed fee for that single period keeps the
    residual-to-certificate test exact for all 29 periods instead of hiding a
    $1.35m convention gap inside it.

    Returns ``(tranche_flows, period_flows)``; ``period_flows`` keeps the full
    priority stack so any single line can be compared with the certificate.
    """
    n = len(panel)
    total_cash = (
        panel["total_distribution"].to_numpy(dtype=float) if cash is None else np.asarray(cash, dtype=float)
    )
    overrides = servicing_override or {}

    opening = {t.name: t.original_balance for t in tranches}
    if initial_balances:
        opening.update({k: float(v) for k, v in initial_balances.items()})
    reserve = float(panel["reserve_start"].iloc[0])
    carry_interest = {t.name: 0.0 for t in tranches}
    carry_principal = 0.0

    tranche_rows: list[dict] = []
    period_rows: list[dict] = []

    for k in range(n):
        row = panel.iloc[k]
        period = str(row["period"])
        available = float(total_cash[k])

        # --- item 3: interest on the notes -------------------------------- #
        interest_due = {
            t.name: opening[t.name] * t.rate(row) * float(row[t.day_count_field]) / 360.0 + carry_interest[t.name]
            for t in tranches
        }

        # --- item 4: principal on the notes -------------------------------- #
        # The Note Value decline is the *claim*, but the notes cannot be asked to
        # repay more principal than they still have outstanding. Without this cap
        # the overcollateralisation would keep generating a claim after the notes
        # are gone and the carry-over shortfall would compound without bound.
        notes_outstanding = sum(opening.values())
        principal_distributable = min(
            max(0.0, float(row["note_value_start"]) - float(row["note_value"])) + carry_principal,
            notes_outstanding,
        )

        # --- items 1-2: fees ahead of the notes ---------------------------- #
        administration_paid = min(available, administration_fee)
        available -= administration_paid
        review_paid = min(available, float(row["review_fee_priority2"]))
        available -= review_paid

        # --- the reserve account is the backstop for interest + principal --- #
        reserve_to_notes = 0.0
        if draw_reserve:
            need = sum(interest_due.values()) + principal_distributable
            if available < need and reserve > 0.0:
                reserve_to_notes = min(reserve, need - available)
                reserve -= reserve_to_notes
                available += reserve_to_notes

        # --- pay interest (pro rata if short) ------------------------------ #
        due_total = sum(interest_due.values())
        ratio = min(1.0, available / due_total) if due_total > 0 else 1.0
        interest_paid = {t.name: interest_due[t.name] * ratio for t in tranches}
        available -= sum(interest_paid.values())

        # --- pay principal (class order, capped by cash) ------------------- #
        pay_total = min(available, principal_distributable)
        principal_paid = allocate_principal(pay_total, opening, tranches)
        available -= sum(principal_paid.values())
        principal_shortfall = principal_distributable - sum(principal_paid.values())

        # --- item 5: top the reserve back up ------------------------------- #
        reserve_deposit = min(available, max(0.0, reserve_target - reserve))
        reserve += reserve_deposit
        available -= reserve_deposit

        # --- item 6: servicing fee ------------------------------------------ #
        if period in overrides:
            servicing_due = float(overrides[period])
        else:
            servicing_due = float(row["pool_balance_start"]) * servicing_rate / 12.0
        servicing_paid = min(available, servicing_due)
        available -= servicing_paid

        # --- items 7-9: trustees and the review fee above the cap ----------- #
        indenture_paid = min(available, float(row["indenture_trustee_fee"]))
        available -= indenture_paid
        owner_paid = min(available, float(row["owner_trustee_fee"]))
        available -= owner_paid
        review9_paid = min(available, float(row["review_fee_priority9"]))
        available -= review9_paid

        # --- item 10: residual to the certificate --------------------------- #
        certificate = available
        available = 0.0

        interest_shortfall = sum(interest_due.values()) - sum(interest_paid.values())
        carry_principal = principal_shortfall
        for t in tranches:
            carry_interest[t.name] = interest_due[t.name] - interest_paid[t.name]

        used = (
            administration_paid + review_paid + sum(interest_paid.values())
            + sum(principal_paid.values()) + reserve_deposit + servicing_paid
            + indenture_paid + owner_paid + review9_paid + certificate
        )
        for t in tranches:
            closing = opening[t.name] - principal_paid[t.name]
            tranche_rows.append(
                dict(
                    period=period,
                    tranche=t.name,
                    coupon=t.rate(row),
                    opening_balance=opening[t.name],
                    interest_due=interest_due[t.name],
                    interest_paid=interest_paid[t.name],
                    principal_paid=principal_paid[t.name],
                    closing_balance=closing,
                    interest_shortfall=carry_interest[t.name],
                )
            )
            opening[t.name] = closing

        period_rows.append(
            dict(
                period=period,
                total_distribution=float(total_cash[k]),
                administration_fee=administration_paid,
                review_fee_priority2=review_paid,
                note_interest=sum(interest_paid.values()),
                note_principal=sum(principal_paid.values()),
                reserve_to_notes=reserve_to_notes,
                reserve_deposit=reserve_deposit,
                reserve_end=reserve,
                servicing_fee=servicing_paid,
                indenture_trustee_fee=indenture_paid,
                owner_trustee_fee=owner_paid,
                review_fee_priority9=review9_paid,
                certificate=certificate,
                principal_distributable=principal_distributable,
                principal_shortfall=principal_shortfall,
                interest_shortfall=interest_shortfall,
                note_value_start=float(row["note_value_start"]),
                note_value_end=float(row["note_value"]),
                notes_outstanding=sum(opening.values()),
                cash_used_gap=used - (float(total_cash[k]) + reserve_to_notes),
            )
        )

    return pd.DataFrame(tranche_rows), pd.DataFrame(period_rows)
