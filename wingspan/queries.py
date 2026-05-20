"""Wingspan GraphQL queries — admin-only, read-only."""

from __future__ import annotations

from typing import List

import strawberry

from utils.graphql.permissions import IsClientOrSparkAdmin

from . import client, types


@strawberry.type
class WingspanQueries:
    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def wingspan_status(self, info: strawberry.Info) -> types.WingspanStatus:
        """Connection check — does this tenant have a key wired?"""
        if client.is_connected():
            return types.WingspanStatus(
                connected=True,
                message="Wingspan integration is live.",
            )
        return types.WingspanStatus(
            connected=False,
            message=(
                "Wingspan is not connected. Set WINGSPAN_API_KEY on Cloud Run "
                "(or WINGSPAN_MOCK=true for an empty-state demo) to enable "
                "Payroll · Hours and Payments · Wingspan."
            ),
        )

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def wingspan_payroll_periods(
        self,
        info: strawberry.Info,
        limit: int = 12,
    ) -> List[types.WingspanPayrollPeriod]:
        """Recent payroll periods, newest first. Returns empty list
        when Wingspan isn't connected — use wingspanStatus to detect.
        """
        try:
            periods = await client.list_payroll_periods(limit=max(1, min(limit, 50)))
        except client.WingspanAPIError:
            return []
        return [
            types.WingspanPayrollPeriod(
                id=strawberry.ID(p.id),
                label=p.label,
                starts_at=p.starts_at,
                ends_at=p.ends_at,
                pay_date=p.pay_date,
                status=p.status,
                total_amount=p.total_amount,
                contractor_count=p.contractor_count,
            )
            for p in periods
        ]

    @strawberry.field(permission_classes=[IsClientOrSparkAdmin])
    async def wingspan_payments(
        self,
        info: strawberry.Info,
        period_id: strawberry.ID | None = None,
        limit: int = 50,
    ) -> List[types.WingspanPayment]:
        """Recent payments / disbursements. Filter to a single period
        with `periodId`. Empty list when not connected.
        """
        try:
            payments = await client.list_payments(
                period_id=str(period_id) if period_id else None,
                limit=max(1, min(limit, 200)),
            )
        except client.WingspanAPIError:
            return []
        return [
            types.WingspanPayment(
                id=strawberry.ID(p.id),
                contractor_name=p.contractor_name,
                contractor_email=p.contractor_email,
                amount=p.amount,
                status=p.status,
                pay_date=p.pay_date,
                period_id=p.period_id,
                memo=p.memo,
            )
            for p in payments
        ]
