"""Assign/reminder emails mint /checkin URLs, never spark://."""

from types import SimpleNamespace

from jobs.envelopes import _checkin_deep_link


def test_checkin_deep_link_uses_tenant_standing_code():
    tenant = SimpleNamespace(checkin_code="LD-TNBJ8K")
    url = _checkin_deep_link(tenant=tenant)
    assert url == "https://admin.igniteproductions.co/checkin/LD-TNBJ8K"
    assert not url.startswith("spark://")


def test_checkin_deep_link_falls_back_to_event_walkup():
    tenant = SimpleNamespace(checkin_code="")
    event = SimpleNamespace(walkup_code="EVT12345")
    url = _checkin_deep_link(tenant=tenant, event=event)
    assert url.endswith("/checkin/EVT12345")
    assert url.startswith("https://")


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
