"""RQ is optional on Cloud Run — missing REDIS_URL runs work inline."""

from django.test import override_settings

from utils.queues import Queue, rq_is_enabled


@override_settings(RQ_ENABLED=False)
def test_rq_is_enabled_false_when_unset():
    assert rq_is_enabled() is False


@override_settings(RQ_ENABLED=True)
def test_rq_is_enabled_true_when_configured():
    assert rq_is_enabled() is True


@override_settings(RQ_ENABLED=False)
def test_queue_add_runs_inline_without_redis():
    ran = []

    def _job(value):
        ran.append(value)
        return f"ok:{value}"

    result = Queue("default").add(_job, "mail")
    assert result == "ok:mail"
    assert ran == ["mail"]
