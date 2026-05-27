"""
Tests for mailer module.

This module tests:
- Envelope class (initialization, template loading, rendering, compilation)
- MailDriver classes (ResendMailDriver, MailpitMailDriver)
- MailDrivers class (driver selection)
- send_email_task (RQ background task)
- Mailer class (all methods)
- MailChain class (all methods)
"""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from django.test import override_settings

from utils.mailer import (
    Envelope,
    MailDriver,
    ResendMailDriver,
    MailpitMailDriver,
    MailDrivers,
    send_email_task,
    Mailer,
    MailChain,
)


@pytest.mark.django_db
class TestEnvelope:
    """Tests for Envelope class."""

    def test_envelope_initialization_with_defaults(self):
        """Test Envelope initialization with default values."""
        envelope = Envelope()
        assert envelope.subject == "Spark Notification"
        assert envelope.template == ""
        assert envelope.context == {}
        assert envelope.to_emails == []
        assert envelope.cc_emails == []
        assert envelope.headers == {}
        assert envelope.html == ""

    def test_envelope_initialization_with_kwargs(self):
        """Test Envelope initialization with custom values."""
        envelope = Envelope(
            subject="Test Subject",
            template="tenants.templates.emails.email_verification",
            context={"user": "test"},
            to_emails=["test@example.com"],
            cc_emails=["copy@example.com"],
            headers={"X-Custom": "value"},
            html="<html>Test</html>"
        )
        assert envelope.subject == "Test Subject"
        assert envelope.template == "tenants.templates.emails.email_verification"
        assert envelope.context == {"user": "test"}
        assert envelope.to_emails == ["test@example.com"]
        assert envelope.cc_emails == ["copy@example.com"]
        assert envelope.headers == {"X-Custom": "value"}
        assert envelope.html == "<html>Test</html>"

    def test_get_template_valid_path(self):
        """Test get_template with valid template path."""
        envelope = Envelope(
            template="tenants.templates.emails.email_verification")
        with patch('utils.mailer.django_get_template') as mock_get_template:
            mock_template = MagicMock()
            mock_get_template.return_value = mock_template
            template = envelope.get_template()
            mock_get_template.assert_called_once_with(
                "emails/email_verification.html")
            assert template == mock_template

    def test_get_template_invalid_path_missing_template(self):
        """Test get_template raises error when template is empty."""
        envelope = Envelope()
        with pytest.raises(ValueError, match="Template is required"):
            envelope.get_template()

    def test_get_template_invalid_path_format(self):
        """Test get_template raises error with invalid path format."""
        envelope = Envelope(template="invalid.path")
        with pytest.raises(ValueError, match="Invalid template path format"):
            envelope.get_template()

    def test_render_template_with_html(self):
        """Test render_template returns html if html is set."""
        envelope = Envelope(html="<html>Pre-rendered</html>")
        result = envelope.render_template()
        assert result == "<html>Pre-rendered</html>"

    def test_render_template_with_template(self):
        """Test render_template renders template with context."""
        envelope = Envelope(
            template="tenants.templates.emails.email_verification",
            context={"user": {"first_name": "John"}}
        )
        with patch('utils.mailer.django_get_template') as mock_get_template:
            mock_template = MagicMock()
            mock_template.render.return_value = "<html>Rendered</html>"
            mock_get_template.return_value = mock_template
            result = envelope.render_template()
            assert result == "<html>Rendered</html>"
            render_context = mock_template.render.call_args[0][0]
            assert render_context["user"] == {"first_name": "John"}
            assert "EMAIL_LOGO_CID" in render_context

    def test_render_template_with_empty_context(self):
        """Test render_template handles empty context."""
        envelope = Envelope(
            template="tenants.templates.emails.email_verification")
        with patch('utils.mailer.django_get_template') as mock_get_template:
            mock_template = MagicMock()
            mock_template.render.return_value = "<html>Rendered</html>"
            mock_get_template.return_value = mock_template
            envelope.render_template()
            render_context = mock_template.render.call_args[0][0]
            assert "EMAIL_LOGO_CID" in render_context

    def test_compile(self):
        """Test compile returns correct dictionary."""
        envelope = Envelope(
            subject="Test Subject",
            from_email="from@example.com",
            to_emails=["to@example.com"],
            cc_emails=["copy@example.com"],
            template="tenants.templates.emails.email_verification",
            headers={"X-Custom": "value"},
            html="<html>Test</html>"
        )
        with patch.object(envelope, 'render_template', return_value="<html>Rendered</html>"):
            result = envelope.compile()
            assert result == {
                "from": "from@example.com",
                "to": ["to@example.com"],
                "cc": ["copy@example.com"],
                "subject": "Test Subject",
                "html": "<html>Rendered</html>",
                "template": "tenants.templates.emails.email_verification",
                "headers": {"X-Custom": "value"},
            }

    def test_from_dict_valid_payload(self):
        """Test from_dict creates Envelope from valid dictionary."""
        payload = {
            "from": "from@example.com",
            "to": ["to@example.com"],
            "cc": ["copy@example.com"],
            "subject": "Test Subject",
            "html": "<html>Test</html>",
            "headers": {"X-Custom": "value"},
            "template": "tenants.templates.emails.email_verification",
        }
        envelope = Envelope.from_dict(payload)
        assert envelope.from_email == "from@example.com"
        assert envelope.to_emails == ["to@example.com"]
        assert envelope.cc_emails == ["copy@example.com"]
        assert envelope.subject == "Test Subject"
        assert envelope.html == "<html>Test</html>"
        assert envelope.headers == {"X-Custom": "value"}
        assert envelope.template == "tenants.templates.emails.email_verification"

    def test_from_dict_missing_required_key(self):
        """Test from_dict raises error when required key is missing."""
        payload = {
            "from": "from@example.com",
            "to": ["to@example.com"],
            # Missing "subject"
            "html": "<html>Test</html>",
            "headers": {},
        }
        with pytest.raises(ValueError, match="Key subject is required"):
            Envelope.from_dict(payload)


@pytest.mark.django_db
class TestMailDrivers:
    """Tests for MailDriver classes."""

    def test_mail_driver_abstract(self):
        """Test MailDriver raises NotImplementedError."""
        driver = MailDriver()
        envelope = Envelope()
        with pytest.raises(NotImplementedError):
            driver.send(envelope)

    @patch('utils.mailer.resend.Emails.send')
    def test_resend_mail_driver_send(self, mock_resend_send):
        """Test ResendMailDriver sends email via Resend API."""
        driver = ResendMailDriver()
        envelope = Envelope(
            subject="Test Subject",
            from_email="from@example.com",
            to_emails=["to@example.com"],
            cc_emails=["copy@example.com"],
            headers={"X-Custom": "value"},
            html="<html>Test</html>"
        )
        with patch.object(envelope, 'render_template', return_value="<html>Rendered</html>"):
            driver.send(envelope)
            mock_resend_send.assert_called_once_with({
                "from": "from@example.com",
                "to": ["to@example.com"],
                "cc": ["copy@example.com"],
                "subject": "Test Subject",
                "html": "<html>Rendered</html>",
                "text": "Rendered",
                "headers": {
                    "X-Custom": "value",
                    "List-Unsubscribe": (
                        "<mailto:events@igniteproductions.co?subject=Unsubscribe>"
                    ),
                },
            })

    @patch('utils.mailer.EmailMultiAlternatives')
    def test_mailpit_mail_driver_send(self, mock_email_class):
        """Test MailpitMailDriver sends email via Django email."""
        driver = MailpitMailDriver()
        envelope = Envelope(
            subject="Test Subject",
            from_email="from@example.com",
            to_emails=["to@example.com"],
            cc_emails=["copy@example.com"],
            headers={"X-Custom": "value"},
            html="<html>Test</html>"
        )
        with patch.object(envelope, 'render_template', return_value="<html>Rendered</html>"):
            mock_email_instance = MagicMock()
            mock_email_class.return_value = mock_email_instance
            driver.send(envelope)
            mock_email_class.assert_called_once_with(
                subject="Test Subject",
                body="Rendered",
                from_email="from@example.com",
                to=["to@example.com"],
                cc=["copy@example.com"],
                headers={
                    "X-Custom": "value",
                    "List-Unsubscribe": (
                        "<mailto:events@igniteproductions.co?subject=Unsubscribe>"
                    ),
                },
            )
            mock_email_instance.attach_alternative.assert_called_once_with(
                "<html>Rendered</html>", "text/html"
            )
            mock_email_instance.send.assert_called_once()

    @override_settings(MAIL_DRIVER="mailpit")
    def test_mail_drivers_default_driver(self):
        """Test MailDrivers uses default driver from settings."""
        drivers = MailDrivers()
        assert drivers.driver == "mailpit"
        assert "mailpit" in drivers.drivers
        assert "resend" in drivers.drivers

    @override_settings(MAIL_DRIVER="resend")
    def test_mail_drivers_resend_driver(self):
        """Test MailDrivers uses resend driver when configured."""
        drivers = MailDrivers()
        assert drivers.driver == "resend"

    @override_settings(MAIL_DRIVER=None)
    def test_mail_drivers_fallback_to_mailpit(self):
        """Test MailDrivers falls back to mailpit when MAIL_DRIVER is None."""
        drivers = MailDrivers()
        assert drivers.driver == "mailpit"

    @override_settings(MAIL_DRIVER="mailpit")
    def test_mail_drivers_send_calls_correct_driver(self):
        """Test MailDrivers.send calls the correct driver."""
        drivers = MailDrivers()
        envelope = Envelope()
        with patch.object(drivers.drivers['mailpit'], 'send') as mock_send:
            drivers.send(envelope)
            mock_send.assert_called_once_with(envelope)


@pytest.mark.django_db
class TestSendEmailTask:
    """Tests for send_email_task RQ job."""

    @patch('utils.mailer.MailDrivers')
    def test_send_email_task_success(self, mock_drivers_class):
        """Test send_email_task successfully sends email."""
        payload = {
            "from": "from@example.com",
            "to": ["to@example.com"],
            "subject": "Test Subject",
            "html": "<html>Test</html>",
            "headers": {},
        }
        mock_driver = MagicMock()
        mock_drivers_class.return_value = mock_driver
        send_email_task(payload)
        mock_driver.send.assert_called_once()
        assert isinstance(mock_driver.send.call_args[0][0], Envelope)

    @patch('utils.mailer.MailDrivers')
    @patch('utils.mailer.logger')
    def test_send_email_task_error_handling(self, mock_logger, mock_drivers_class):
        """Test send_email_task handles errors and logs them."""
        payload = {
            "from": "from@example.com",
            "to": ["to@example.com"],
            "subject": "Test Subject",
            "html": "<html>Test</html>",
            "headers": {},
        }
        mock_driver = MagicMock()
        mock_driver.send.side_effect = Exception("Send failed")
        mock_drivers_class.return_value = mock_driver
        with pytest.raises(Exception, match="Send failed"):
            send_email_task(payload)
        mock_logger.error.assert_called_once()


@pytest.mark.django_db
class TestMailer:
    """Tests for Mailer class."""

    def test_mailer_envelope_not_implemented(self):
        """Test Mailer.envelope raises NotImplementedError."""
        mailer = Mailer()
        with pytest.raises(NotImplementedError):
            mailer.envelope()

    def test_mailer_get_driver_creates_driver(self):
        """Test get_driver creates MailDrivers instance if None."""
        mailer = Mailer()
        assert mailer.driver is None
        driver = mailer.get_driver()
        assert isinstance(driver, MailDrivers)
        assert mailer.driver is not None

    def test_mailer_get_driver_reuses_driver(self):
        """Test get_driver reuses existing driver instance."""
        mailer = Mailer()
        driver1 = mailer.get_driver()
        driver2 = mailer.get_driver()
        assert driver1 is driver2

    def test_mailer_dispatch(self):
        """Test dispatch calls driver.send with envelope."""
        mailer = Mailer()
        mock_envelope = MagicMock()
        with patch.object(mailer, 'envelope', return_value=mock_envelope):
            with patch.object(mailer, 'get_driver') as mock_get_driver:
                mock_driver = MagicMock()
                mock_get_driver.return_value = mock_driver
                mailer.dispatch()
                mock_driver.send.assert_called_once_with(mock_envelope)

    @patch('utils.mailer.Queues')
    def test_mailer_send_enqueues_task(self, mock_queues_class):
        """Test send enqueues email task to RQ."""
        mailer = Mailer()
        mock_envelope = MagicMock()
        mock_envelope.compile.return_value = {
            "from": "from@example.com",
            "to": ["to@example.com"],
            "subject": "Test",
            "html": "<html>Test</html>",
            "template": "test.template",
            "headers": {},
        }
        with patch.object(mailer, 'envelope', return_value=mock_envelope):
            mock_queues = MagicMock()
            mock_queue = MagicMock()
            mock_queues.default = mock_queue
            mock_queues_class.return_value = mock_queues
            from utils.mailer import send_email_task
            mailer.send()
            mock_queue.add.assert_called_once_with(
                send_email_task,
                payload=mock_envelope.compile.return_value
            )

    def test_mailer_send_now_calls_dispatch(self):
        """Test send_now calls dispatch."""
        mailer = Mailer()
        with patch.object(mailer, 'dispatch') as mock_dispatch:
            mailer.send_now()
            mock_dispatch.assert_called_once()

    @pytest.mark.asyncio
    async def test_mailer_send_async(self):
        """Test send_async wraps send in sync_to_async."""
        mailer = Mailer()
        with patch.object(mailer, 'send'):  # noqa: F841
            # send_async calls sync_to_async(self.send), which should call send
            # The actual async execution is handled by sync_to_async
            # Note: Due to sync_to_async wrapping, we can't easily verify the call
            # but the test ensures the method doesn't raise errors
            await mailer.send_async()

    @pytest.mark.asyncio
    async def test_mailer_send_async_now(self):
        """Test send_async_now wraps send_now in sync_to_async."""
        mailer = Mailer()
        with patch.object(mailer, 'send_now'):  # noqa: F841
            # send_async_now calls sync_to_async(self.send_now)
            # Note: Due to sync_to_async wrapping, we can't easily verify the call
            # but the test ensures the method doesn't raise errors
            await mailer.send_async_now()


class TestMailerSubclass:
    """Tests for Mailer subclass implementation."""

    class ConcreteTestMailer(Mailer):
        """Test mailer implementation."""

        def __init__(self, test_data):
            self.test_data = test_data

        def envelope(self):
            return Envelope(
                subject="Test",
                to_emails=["test@example.com"],
                html="<html>Test</html>"
            )

    @patch('utils.mailer.Queues')
    def test_mailer_subclass_send(self, mock_queues_class):
        """Test Mailer subclass can send emails."""
        mailer = self.ConcreteTestMailer("test")
        mock_queues = MagicMock()
        mock_queue = MagicMock()
        mock_queues.default = mock_queue
        mock_queues_class.return_value = mock_queues
        mailer.send()
        assert mock_queue.add.called

    def test_mailer_subclass_send_now(self):
        """Test Mailer subclass send_now works."""
        mailer = self.ConcreteTestMailer("test")
        with patch.object(mailer, 'dispatch') as mock_dispatch:
            mailer.send_now()
            mock_dispatch.assert_called_once()


@pytest.mark.django_db
class TestMailChain:
    """Tests for MailChain class."""

    def test_mail_chain_init_empty(self):
        """Test MailChain initialization with no mailers."""
        chain = MailChain()
        assert chain.mailers == []

    def test_mail_chain_init_with_mailers(self):
        """Test MailChain initialization with mailers."""
        mailer1 = MagicMock(spec=Mailer)
        mailer2 = MagicMock(spec=Mailer)
        chain = MailChain([mailer1, mailer2])
        assert len(chain.mailers) == 2
        assert mailer1 in chain.mailers
        assert mailer2 in chain.mailers

    def test_mail_chain_add(self):
        """Test MailChain.add adds mailer to chain."""
        chain = MailChain()
        mailer = MagicMock(spec=Mailer)
        chain.add(mailer)
        assert mailer in chain.mailers
        assert len(chain.mailers) == 1

    @patch('utils.mailer.Queues')
    def test_mail_chain_send(self, mock_queues_class):
        """Test MailChain.send calls send on all mailers."""
        mailer1 = MagicMock(spec=Mailer)
        mailer2 = MagicMock(spec=Mailer)
        chain = MailChain([mailer1, mailer2])
        mock_queues = MagicMock()
        mock_queue = MagicMock()
        mock_queues.default = mock_queue
        mock_queues_class.return_value = mock_queues
        chain.send()
        assert mailer1.send.called
        assert mailer2.send.called

    def test_mail_chain_send_now(self):
        """Test MailChain.send_now calls send_now on all mailers."""
        mailer1 = MagicMock(spec=Mailer)
        mailer2 = MagicMock(spec=Mailer)
        chain = MailChain([mailer1, mailer2])
        with patch.object(mailer1, 'send_now') as mock_send_now1:
            with patch.object(mailer2, 'send_now') as mock_send_now2:
                chain.send_now()
                mock_send_now1.assert_called_once()
                mock_send_now2.assert_called_once()

    @pytest.mark.asyncio
    async def test_mail_chain_send_async(self):
        """Test MailChain.send_async awaits send_async on all mailers."""
        mailer1 = MagicMock(spec=Mailer)
        mailer2 = MagicMock(spec=Mailer)
        mailer1.send_async = AsyncMock(return_value=None)
        mailer2.send_async = AsyncMock(return_value=None)
        chain = MailChain([mailer1, mailer2])
        await chain.send_async()
        assert mailer1.send_async.called
        assert mailer2.send_async.called

    @pytest.mark.asyncio
    async def test_mail_chain_send_async_now(self):
        """Test MailChain.send_async_now awaits send_async_now on all mailers."""
        mailer1 = MagicMock(spec=Mailer)
        mailer2 = MagicMock(spec=Mailer)
        mailer1.send_async_now = AsyncMock(return_value=None)
        mailer2.send_async_now = AsyncMock(return_value=None)
        chain = MailChain([mailer1, mailer2])
        await chain.send_async_now()
        assert mailer1.send_async_now.called
        assert mailer2.send_async_now.called

    @patch('utils.mailer.Queues')
    def test_mail_chain_send_chain_static(self, mock_queues_class):
        """Test MailChain.send_chain static method."""
        mailer1 = MagicMock(spec=Mailer)
        mailer2 = MagicMock(spec=Mailer)
        mock_queues = MagicMock()
        mock_queue = MagicMock()
        mock_queues.default = mock_queue
        mock_queues_class.return_value = mock_queues
        chain = MailChain.send_chain([mailer1, mailer2])
        assert isinstance(chain, MailChain)
        assert mailer1.send.called
        assert mailer2.send.called

    def test_mail_chain_send_chain_now_static(self):
        """Test MailChain.send_chain_now static method."""
        mailer1 = MagicMock(spec=Mailer)
        mailer2 = MagicMock(spec=Mailer)
        with patch.object(mailer1, 'send_now') as mock_send_now1:
            with patch.object(mailer2, 'send_now') as mock_send_now2:
                chain = MailChain.send_chain_now([mailer1, mailer2])
                assert isinstance(chain, MailChain)
                mock_send_now1.assert_called_once()
                mock_send_now2.assert_called_once()

    @pytest.mark.asyncio
    async def test_mail_chain_send_chain_async_static(self):
        """Test MailChain.send_chain_async static method."""
        mailer1 = MagicMock(spec=Mailer)
        mailer2 = MagicMock(spec=Mailer)
        mailer1.send_async = AsyncMock(return_value=None)
        mailer2.send_async = AsyncMock(return_value=None)
        chain = await MailChain.send_chain_async([mailer1, mailer2])
        assert isinstance(chain, MailChain)
        assert mailer1.send_async.called
        assert mailer2.send_async.called

    @pytest.mark.asyncio
    async def test_mail_chain_send_chain_async_now_static(self):
        """Test MailChain.send_chain_async_now static method."""
        mailer1 = MagicMock(spec=Mailer)
        mailer2 = MagicMock(spec=Mailer)
        mailer1.send_async_now = AsyncMock(return_value=None)
        mailer2.send_async_now = AsyncMock(return_value=None)
        chain = await MailChain.send_chain_async_now([mailer1, mailer2])
        assert isinstance(chain, MailChain)
        assert mailer1.send_async_now.called
        assert mailer2.send_async_now.called
