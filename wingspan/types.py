from __future__ import annotations

import strawberry


@strawberry.type
class WingspanPayrollPeriod:
    id: strawberry.ID
    label: str
    starts_at: str | None = None
    ends_at: str | None = None
    pay_date: str | None = None
    status: str | None = None
    total_amount: float | None = None
    contractor_count: int | None = None


@strawberry.type
class WingspanPayment:
    id: strawberry.ID
    contractor_name: str | None = None
    contractor_email: str | None = None
    amount: float | None = None
    status: str | None = None
    pay_date: str | None = None
    period_id: str | None = None
    memo: str | None = None


@strawberry.type
class WingspanStatus:
    """Surface whether the integration is wired so the front-end can
    render a setup nudge instead of an empty list when there's no key.
    """

    connected: bool
    # Helpful text for the admin when not connected — never includes
    # the key itself.
    message: str
