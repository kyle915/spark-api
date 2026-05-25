"""
Tests for the Wingspan API client.

The client is HTTP-mocked here — we don't actually call Wingspan.
The contract under test:
  - is_connected() reflects WINGSPAN_API_KEY presence
  - list_payroll_periods / list_payments return [] without a key
    instead of raising (so resolvers can fall through cleanly)
  - Field aliasing handles snake_case + camelCase + nested payee
"""

import pytest
from unittest.mock import AsyncMock, patch

from django.test import override_settings

from wingspan import client


@override_settings(WINGSPAN_API_KEY="", WINGSPAN_MOCK=False)
def test_not_connected_when_key_absent():
    assert client.is_connected() is False


@override_settings(WINGSPAN_API_KEY="ws_test_abc", WINGSPAN_MOCK=False)
def test_connected_when_key_set():
    assert client.is_connected() is True


@override_settings(WINGSPAN_API_KEY="ws_test_abc", WINGSPAN_MOCK=True)
def test_mock_mode_treated_as_not_connected():
    # Mock mode hides live data even when a key is set.
    assert client.is_connected() is False


@pytest.mark.asyncio
@override_settings(WINGSPAN_API_KEY="", WINGSPAN_MOCK=False)
async def test_list_periods_returns_empty_without_key():
    out = await client.list_payroll_periods()
    assert out == []


@pytest.mark.asyncio
@override_settings(WINGSPAN_API_KEY="", WINGSPAN_MOCK=False)
async def test_list_payments_returns_empty_without_key():
    out = await client.list_payments()
    assert out == []


@pytest.mark.asyncio
@override_settings(
    WINGSPAN_API_KEY="ws_test_abc",
    WINGSPAN_API_BASE="https://api.wingspan.app",
    WINGSPAN_MOCK=False,
)
async def test_list_periods_parses_payload():
    fake_payload = {
        "results": [
            {
                "id": "wsp_001",
                "label": "May 13 – 19",
                "startDate": "2026-05-13",
                "endDate": "2026-05-19",
                "payDate": "2026-05-22",
                "status": "open",
                "totalAmount": "1240.50",
                "contractorCount": 6,
            },
            {
                # Alternate keys (payrollId, name, total, count)
                "payrollId": "wsp_002",
                "name": "May 6 – 12",
                "status": "paid",
                "total": 980,
                "count": 5,
            },
        ]
    }
    with patch.object(client, "_request", new=AsyncMock(return_value=fake_payload)):
        out = await client.list_payroll_periods(limit=5)

    assert len(out) == 2
    assert out[0].id == "wsp_001"
    assert out[0].label == "May 13 – 19"
    assert out[0].status == "open"
    assert out[0].total_amount == 1240.5
    assert out[0].contractor_count == 6
    # 2nd row uses the fallback aliases
    assert out[1].id == "wsp_002"
    assert out[1].label == "May 6 – 12"
    assert out[1].total_amount == 980.0
    assert out[1].contractor_count == 5


@pytest.mark.asyncio
@override_settings(
    WINGSPAN_API_KEY="ws_test_abc",
    WINGSPAN_MOCK=False,
)
async def test_list_payments_handles_nested_payee():
    fake_payload = {
        "data": [
            {
                "id": "pay_001",
                "amount": "120.00",
                "status": "sent",
                "payDate": "2026-05-22",
                "payrollId": "wsp_001",
                "memo": "May 13 shift",
                "contractor": {
                    "firstName": "Maya",
                    "lastName": "Rodriguez",
                    "email": "maya@example.com",
                },
            },
            {
                "id": "pay_002",
                "contractorName": "Jordan T.",
                "amount": 87.5,
                "status": "pending",
            },
        ]
    }
    with patch.object(client, "_request", new=AsyncMock(return_value=fake_payload)):
        out = await client.list_payments()

    assert len(out) == 2
    assert out[0].contractor_name == "Maya Rodriguez"
    assert out[0].contractor_email == "maya@example.com"
    assert out[0].amount == 120.0
    assert out[0].period_id == "wsp_001"
    assert out[0].memo == "May 13 shift"
    assert out[1].contractor_name == "Jordan T."
    assert out[1].amount == 87.5


@pytest.mark.asyncio
@override_settings(
    WINGSPAN_API_KEY="ws_test_abc",
    WINGSPAN_MOCK=False,
)
async def test_list_payments_filter_passes_period_id():
    captured = {}

    async def fake_request(method, path, *, params=None):
        captured["method"] = method
        captured["path"] = path
        captured["params"] = params
        return {"results": []}

    with patch.object(client, "_request", new=fake_request):
        await client.list_payments(period_id="wsp_001", limit=10)

    assert captured["method"] == "GET"
    assert captured["path"] == "/v1/payments"
    assert captured["params"] == {"limit": 10, "periodId": "wsp_001"}
