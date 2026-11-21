"""Assign/reminder emails mint /checkin URLs on client, never spark:// or admin."""

from types import SimpleNamespace

from events.event_confirmations import public_page_base, recap_url_for
from jobs.envelopes import _checkin_deep_link


def test_checkin_deep_link_uses_tenant_standing_code():
    tenant = SimpleNamespace(checkin_code="LD-TNBJ8K")
    url = _checkin_deep_link(tenant=tenant)
    assert url == recap_url_for(tenant)
    assert url == f"{public_page_base()}/checkin/LD-TNBJ8K"
    assert "client.igniteproductions.co" in url
    assert "admin.igniteproductions.co" not in url
    assert not url.startswith("spark://")


def test_checkin_deep_link_falls_back_to_event_walkup():
    tenant = SimpleNamespace(checkin_code="")
    event = SimpleNamespace(walkup_code="EVT12345")
    url = _checkin_deep_link(tenant=tenant, event=event)
    assert url == f"{public_page_base()}/checkin/EVT12345"
    assert "admin.igniteproductions.co" not in url


def test_checkin_deep_link_empty_when_no_code():
    assert _checkin_deep_link(tenant=SimpleNamespace(checkin_code="")) == ""
    assert _checkin_deep_link(tenant=None) == ""


def test_assign_and_reminder_templates_cta_checkin_not_spark_app():
    from pathlib import Path

    templates = Path(__file__).resolve().parents[1] / "templates" / "emails"
    files = [
        "ambassador_assigned_to_job.html",
        "ambassador_assigned_to_job_default.html",
        "ambassador_job_event_reminder.html",
        "ambassador_job_event_reminder_liquid_death.html",
        "ambassador_job_event_reminder_3h.html",
        "ambassador_job_event_reminder_3h_liquid_death.html",
    ]
    for name in files:
        html = (templates / name).read_text()
        assert "Open Spark App" not in html
        assert "Open In Spark" not in html
        assert "Open the Spark App" not in html
        assert "Open check-in" in html
        assert 'target="_blank"' in html
        assert "noopener" in html


def test_recap_preview_link_uses_standing_code_and_walkup_fallback():
    from jobs.envelopes import _recap_preview_link

    tenant = SimpleNamespace(id=1, checkin_code="LD-TNBJ8K", checkin_timesheet_only=False)
    assert _recap_preview_link(tenant=tenant) == (
        f"{public_page_base()}/checkin/LD-TNBJ8K/preview"
    )
    no_code = SimpleNamespace(id=1, checkin_code="", checkin_timesheet_only=False)
    event = SimpleNamespace(walkup_code="EVT12345", event_type=None)
    assert _recap_preview_link(tenant=no_code, event=event) == (
        f"{public_page_base()}/checkin/EVT12345/preview"
    )
    timesheet = SimpleNamespace(id=1, checkin_code="LD-TNBJ8K", checkin_timesheet_only=True)
    assert _recap_preview_link(tenant=timesheet, event=event) == ""
    assert _recap_preview_link(tenant=None) == ""


def test_reminder_templates_offer_the_recap_preview():
    from django.template.loader import render_to_string

    preview = f"{public_page_base()}/checkin/LD-TNBJ8K/preview"
    for name in (
        "ambassador_job_event_reminder",
        "ambassador_job_event_reminder_3h",
        "ambassador_job_event_reminder_liquid_death",
        "ambassador_job_event_reminder_3h_liquid_death",
    ):
        html = render_to_string(
            f"emails/{name}.html",
            {"deep_link": f"{public_page_base()}/checkin/LD-TNBJ8K", "preview_link": preview},
        )
        assert "Preview your recap" in html, name
        assert f'href="{preview}"' in html, name
        assert "On the day of your shift," in html, name
        assert "review the event recap form" not in html, name
        bare = render_to_string(
            f"emails/{name}.html",
            {"deep_link": f"{public_page_base()}/checkin/LD-TNBJ8K", "preview_link": ""},
        )
        assert "Preview your recap" not in bare, name
