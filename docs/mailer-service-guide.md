# Mailer Service Guide

This document provides comprehensive documentation for the Mailer service in the Spark API project. It explains how to create, configure, and use mailers to send emails.

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Configuration](#configuration)
- [Creating a Mailer](#creating-a-mailer)
- [Email Templates](#email-templates)
- [Sending Methods](#sending-methods)
- [MailChain: Sending Multiple Emails](#mailchain-sending-multiple-emails)
- [Examples](#examples)
- [Best Practices](#best-practices)
- [Troubleshooting](#troubleshooting)

---

## Overview

The Mailer service is a flexible, reusable email sending system built on top of Django's template system and RQ (Redis Queue) for asynchronous processing. It supports multiple email drivers (Mailpit for development, Resend for production) and provides a clean, object-oriented interface for sending emails.

### Key Features

- **Template-Based**: Uses Django templates for HTML email rendering
- **Asynchronous**: Emails are sent via RQ workers in the background
- **Multiple Drivers**: Supports Mailpit (development) and Resend (production)
- **Chain Support**: Send multiple emails in sequence using MailChain
- **Context Variables**: Pass dynamic data to templates
- **Retry Logic**: Automatic retry with exponential backoff on failures

---

## Architecture

The Mailer service consists of several key components:

### Components

1. **Envelope**: Represents an email with subject, recipients, template, and context
2. **Mailer**: Base class for creating specific email mailers
3. **MailDriver**: Abstract interface for email delivery (ResendMailDriver, MailpitMailDriver)
4. **MailDrivers**: Factory class that selects the appropriate driver based on settings
5. **MailChain**: Utility for sending multiple emails in sequence
6. **send_email_task**: RQ background task that processes email sending

### Flow Diagram

```
Mailer.send() 
  → Envelope.compile() 
    → Queues.default.add(send_email_task)
      → RQ Worker picks up task
        → send_email_task(payload)
          → Envelope.from_dict(payload)
            → MailDrivers.send(envelope)
              → MailDriver.send() (Resend or Mailpit)
```

---

## Configuration

### Environment Variables

Configure the mailer service in your `.env` file or environment:

```bash
# Email driver: "mailpit" (development) or "resend" (production)
MAIL_DRIVER=mailpit

# Resend API key (required if using Resend driver)
RESEND_API_KEY=re_xxxxxxxxxxxxx

# Default from email address
DEFAULT_FROM_EMAIL=Spark <onboarding@resend.dev>
```

### Settings

The mailer service reads configuration from `config/settings.py`:

```python
MAIL_DRIVER = env("MAIL_DRIVER", default="mailpit")  # mailpit, resend
RESEND_API_KEY = env("RESEND_API_KEY", default="")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="Spark <onboarding@resend.dev>")
```

### Email Backend (for Mailpit)

When using Mailpit, configure Django's email backend:

```python
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = "localhost"
EMAIL_PORT = 1025  # Mailpit default port
```

### RQ Workers

Make sure RQ workers are running to process email tasks:

```bash
# Start RQ worker
uv run python manage.py rqworker default

# Or with multiple queues
uv run python manage.py rqworker high default low
```

**Note for macOS**: If you encounter fork() errors, set this environment variable:

```bash
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
```

---

## Creating a Mailer

### Basic Structure

To create a new mailer, extend the `Mailer` class and implement the `envelope()` method:

```python
from utils.mailer import Envelope, Mailer
from myapp.models import MyModel


class MyCustomMailer(Mailer):
    """
    Custom mailer for sending specific emails.
    
    Usage:
        mailer = MyCustomMailer(user, data)
        mailer.send()
    """
    
    def __init__(self, user, data):
        self.user = user
        self.data = data
    
    def envelope(self) -> Envelope:
        return Envelope(
            subject="Your Email Subject",
            template="myapp.templates.emails.my_template",
            to_emails=[self.user.email],
            context={
                "user": self.user,
                "data": self.data,
            },
            # Optional: override default from_email
            from_email="Custom Sender <custom@example.com>",
            # Optional: add custom headers
            headers={
                "X-Custom-Header": "value",
            }
        )
```

### Required Components

1. **`__init__` method**: Store data needed for the email
2. **`envelope()` method**: Return an `Envelope` instance with:
   - `subject`: Email subject line
   - `template`: Template path (see [Email Templates](#email-templates))
   - `to_emails`: List of recipient email addresses
   - `context`: Dictionary of variables to pass to the template
   - `from_email`: (Optional) Override default sender
   - `headers`: (Optional) Custom email headers

### Example: User Welcome Email

```python
from utils.mailer import Envelope, Mailer
from tenants.models import User


class WelcomeMailer(Mailer):
    """Send welcome email to new users."""
    
    def __init__(self, user: User):
        self.user = user
    
    def envelope(self) -> Envelope:
        return Envelope(
            subject="Welcome to Spark!",
            template="tenants.templates.emails.welcome",
            to_emails=[self.user.email],
            context={
                "user": self.user,
                "login_url": "https://app.spark.com/login",
            }
        )
```

### Example: Notification to Multiple Recipients

```python
from utils.mailer import Envelope, Mailer
from events.models import Event


class EventNotificationMailer(Mailer):
    """Notify multiple users about an event."""
    
    def __init__(self, event: Event):
        self.event = event
    
    def envelope(self) -> Envelope:
        # Get all users who should be notified
        from tenants.models import TenantedUser, Role
        
        users = TenantedUser.objects.filter(
            tenant=self.event.tenant,
            role__slug=Role.CLIENT_SLUG
        ).select_related("user")
        
        to_emails = [user.user.email for user in users]
        
        return Envelope(
            subject=f"New Event: {self.event.name}",
            template="events.templates.emails.event_notification",
            to_emails=to_emails,
            context={
                "event": self.event,
            }
        )
```

---

## Email Templates

### Template Path Format

Templates must follow this naming convention:

```
app.templates.emails.template_name
```

For example:
- `tenants.templates.emails.email_verification`
- `ambassadors.templates.emails.event_application`
- `events.templates.emails.event_notification`

The system automatically converts this to the file path:
```
app/templates/emails/template_name.html
```

### Template Structure

Email templates should extend a base template and use Django template syntax:

```django
{% extends "emails/base.html" %}

{% block content %}
<div class="content">
  <div class="greeting">
    {% if user.first_name %}
      Hello, {{ user.first_name }}!
    {% else %}
      Hello!
    {% endif %}
  </div>
  
  <div class="welcome-message">
    <p>Your custom email content here.</p>
    <p>You can use any Django template tags and filters.</p>
    
    <a href="{{ action_url }}" class="button">Click Here</a>
  </div>
</div>
{% endblock %}
```

### Base Template

All email templates should extend `emails/base.html`, which provides:
- HTML structure
- CSS styling
- Responsive design
- Common email client compatibility

### Template Context

Variables passed in the `context` dictionary are available in the template:

```python
# In your mailer
context={
    "user": self.user,
    "event": self.event,
    "custom_data": {"key": "value"},
}
```

```django
<!-- In your template -->
{{ user.first_name }}
{{ event.name }}
{{ custom_data.key }}
```

### Template Location

Create templates in your app's `templates/emails/` directory:

```
myapp/
  templates/
    emails/
      my_template.html
      another_template.html
```

---

## Sending Methods

The `Mailer` class provides several methods for sending emails:

### `send()` - Background Processing (Recommended)

Enqueues the email to be sent asynchronously by RQ workers. This is the recommended method as it doesn't block the request.

```python
mailer = WelcomeMailer(user)
mailer.send()  # Returns immediately, email sent in background
```

**Use when:**
- Sending emails from web requests
- You don't need to wait for the email to be sent
- You want to avoid blocking the request

### `send_now()` - Immediate Synchronous

Sends the email immediately without using background workers. Blocks until the email is sent.

```python
mailer = WelcomeMailer(user)
mailer.send_now()  # Blocks until email is sent
```

**Use when:**
- Sending emails from management commands
- You need to ensure the email is sent before continuing
- Testing or debugging

### `send_async()` - Async Background Processing

Async version of `send()`. Use in async contexts (like async GraphQL mutations).

```python
mailer = WelcomeMailer(user)
await mailer.send_async()  # Returns immediately, email sent in background
```

**Use when:**
- Inside async functions
- Sending from async GraphQL mutations
- You want async/await syntax

### `send_async_now()` - Async Immediate

Async version of `send_now()`. Sends immediately but uses async/await.

```python
mailer = WelcomeMailer(user)
await mailer.send_async_now()  # Blocks until email is sent
```

**Use when:**
- Inside async functions
- You need immediate sending with async syntax

### Comparison Table

| Method | Background | Async | Blocking | Use Case |
|--------|-----------|-------|----------|----------|
| `send()` | ✅ | ❌ | ❌ | Web requests (sync) |
| `send_now()` | ❌ | ❌ | ✅ | Management commands |
| `send_async()` | ✅ | ✅ | ❌ | Async mutations |
| `send_async_now()` | ❌ | ✅ | ✅ | Async immediate sending |

---

## MailChain: Sending Multiple Emails

`MailChain` allows you to send multiple emails in sequence. This is useful when you need to send related emails together (e.g., notify a user and notify admins).

### Creating a MailChain

#### Method 1: Constructor

```python
from utils.mailer import MailChain

mailers = [
    WelcomeMailer(user),
    AdminNotificationMailer(user),
]

chain = MailChain(mailers)
chain.send()
```

#### Method 2: Add Method

```python
chain = MailChain()
chain.add(WelcomeMailer(user))
chain.add(AdminNotificationMailer(user))
chain.send()
```

#### Method 3: Static Methods

```python
from utils.mailer import MailChain

# Send in background
MailChain.send_chain([
    WelcomeMailer(user),
    AdminNotificationMailer(user),
])

# Send immediately
MailChain.send_chain_now([
    WelcomeMailer(user),
    AdminNotificationMailer(user),
])

# Async background
await MailChain.send_chain_async([
    WelcomeMailer(user),
    AdminNotificationMailer(user),
])

# Async immediate
await MailChain.send_chain_async_now([
    WelcomeMailer(user),
    AdminNotificationMailer(user),
])
```

### Example: Application Flow

```python
from utils.mailer import MailChain
from ambassadors.envelopes import (
    AmbassadorEventApplicationMailer,
    NotifyApplicationToClientMailer,
)

# In a GraphQL mutation
async def apply_ambassador_event(self, info, event_id, ambassador_id):
    # ... create application ...
    
    # Send multiple emails
    await MailChain.send_chain_async([
        AmbassadorEventApplicationMailer(application),
        NotifyApplicationToClientMailer(application),
    ])
    
    return response
```

### MailChain Methods

| Method | Background | Async | Blocking |
|--------|-----------|-------|----------|
| `send()` | ✅ | ❌ | ❌ |
| `send_now()` | ❌ | ❌ | ✅ |
| `send_async()` | ✅ | ✅ | ❌ |
| `send_async_now()` | ❌ | ✅ | ✅ |
| `send_chain()` | ✅ | ❌ | ❌ |
| `send_chain_now()` | ❌ | ❌ | ✅ |
| `send_chain_async()` | ✅ | ✅ | ❌ |
| `send_chain_async_now()` | ❌ | ✅ | ✅ |

---

## Examples

### Example 1: Simple Welcome Email

```python
# myapp/envelopes.py
from utils.mailer import Envelope, Mailer
from tenants.models import User


class WelcomeMailer(Mailer):
    def __init__(self, user: User):
        self.user = user
    
    def envelope(self) -> Envelope:
        return Envelope(
            subject="Welcome to Spark!",
            template="myapp.templates.emails.welcome",
            to_emails=[self.user.email],
            context={"user": self.user}
        )


# Usage in a mutation
from myapp.envelopes import WelcomeMailer

mailer = WelcomeMailer(user)
mailer.send()
```

### Example 2: Email with Custom Headers

```python
class CustomHeaderMailer(Mailer):
    def __init__(self, user: User, tenant_id: int):
        self.user = user
        self.tenant_id = tenant_id
    
    def envelope(self) -> Envelope:
        return Envelope(
            subject="Custom Email",
            template="myapp.templates.emails.custom",
            to_emails=[self.user.email],
            context={"user": self.user},
            headers={
                "X-Tenant-ID": str(self.tenant_id),
                "X-Custom-Header": "value",
            }
        )
```

### Example 3: Multiple Recipients

```python
class BulkNotificationMailer(Mailer):
    def __init__(self, event: Event):
        self.event = event
    
    def envelope(self) -> Envelope:
        # Get all recipients
        participants = self.event.participants.all()
        to_emails = [p.user.email for p in participants]
        
        return Envelope(
            subject=f"Event Update: {self.event.name}",
            template="events.templates.emails.event_update",
            to_emails=to_emails,
            context={"event": self.event}
        )
```

### Example 4: Using MailChain in GraphQL Mutation

```python
from strawberry_django.mutations import mutations
from utils.mailer import MailChain
from myapp.envelopes import WelcomeMailer, AdminNotificationMailer


@mutations.mutation
async def register_user(self, info, input):
    # ... create user ...
    
    # Send multiple emails
    await MailChain.send_chain_async([
        WelcomeMailer(user),
        AdminNotificationMailer(user),
    ])
    
    return RegisterUserResponse(success=True, user=user)
```

### Example 5: Conditional Email Sending

```python
class ConditionalMailer(Mailer):
    def __init__(self, user: User, send_welcome: bool = True):
        self.user = user
        self.send_welcome = send_welcome
    
    def envelope(self) -> Envelope:
        if self.send_welcome:
            return Envelope(
                subject="Welcome!",
                template="myapp.templates.emails.welcome",
                to_emails=[self.user.email],
                context={"user": self.user}
            )
        else:
            return Envelope(
                subject="Account Created",
                template="myapp.templates.emails.account_created",
                to_emails=[self.user.email],
                context={"user": self.user}
            )
```

---

## Best Practices

### 1. Organize Mailers by App

Create an `envelopes.py` file in each app to keep mailers organized:

```
myapp/
  envelopes.py  # All mailers for this app
  templates/
    emails/
      welcome.html
      notification.html
```

### 2. Use Descriptive Class Names

```python
# Good
class WelcomeMailer(Mailer):
class EventApplicationMailer(Mailer):
class PasswordResetMailer(Mailer):

# Avoid
class Mailer1(Mailer):
class EmailSender(Mailer):
```

### 3. Document Your Mailers

Always include docstrings explaining what the mailer does and how to use it:

```python
class WelcomeMailer(Mailer):
    """
    Sends a welcome email to new users.
    
    Usage:
        mailer = WelcomeMailer(user)
        mailer.send()
    """
```

### 4. Use Type Hints

Include type hints for better code clarity and IDE support:

```python
def __init__(self, user: User, activation_url: str):
    self.user: User = user
    self.activation_url: str = activation_url
```

### 5. Prefer `send()` Over `send_now()`

Use `send()` (background processing) in web requests to avoid blocking:

```python
# Good - doesn't block
mailer.send()

# Avoid - blocks the request
mailer.send_now()
```

### 6. Use MailChain for Related Emails

When sending multiple related emails, use `MailChain`:

```python
# Good
await MailChain.send_chain_async([
    UserNotificationMailer(user),
    AdminNotificationMailer(user),
])

# Avoid - sends separately
user_mailer.send()
admin_mailer.send()
```

### 7. Validate Email Addresses

Ensure email addresses are valid before sending:

```python
def envelope(self) -> Envelope:
    if not self.user.email:
        raise ValueError("User email is required")
    
    return Envelope(
        to_emails=[self.user.email],
        # ...
    )
```

### 8. Use Context Variables Wisely

Pass only necessary data in the context to avoid template bloat:

```python
# Good - only necessary data
context={
    "user": self.user,
    "action_url": self.action_url,
}

# Avoid - passing entire models
context={
    "user": self.user,
    "user_profile": self.user.profile,
    "user_settings": self.user.settings,
    # ... too much data
}
```

### 9. Test Your Templates

Always test email templates with real data to ensure they render correctly:

```python
# In Django shell
from myapp.envelopes import WelcomeMailer
from tenants.models import User

user = User.objects.first()
mailer = WelcomeMailer(user)
envelope = mailer.envelope()
html = envelope.render_template()
print(html)  # Check the rendered HTML
```

### 10. Handle Errors Gracefully

Email sending can fail. The system automatically retries, but handle errors in your code:

```python
try:
    mailer.send()
except Exception as e:
    logger.error(f"Failed to send email: {e}")
    # Handle error appropriately
```

---

## Troubleshooting

### Email Not Sending

**Problem**: Emails are not being sent.

**Solutions**:
1. Check that RQ workers are running: `uv run python manage.py rqworker default`
2. Verify Redis is running and accessible
3. Check RQ dashboard or logs for failed jobs
4. Ensure `MAIL_DRIVER` is set correctly in settings

### Template Not Found

**Problem**: `TemplateDoesNotExist` error.

**Solutions**:
1. Verify template path format: `app.templates.emails.template_name`
2. Check file exists at: `app/templates/emails/template_name.html`
3. Ensure app is in `INSTALLED_APPS`
4. Run `python manage.py collectstatic` if using static files

### Context Variables Not Available

**Problem**: Template variables are empty or missing.

**Solutions**:
1. Verify context dictionary is passed correctly
2. Check variable names match template usage
3. Ensure objects are serializable (avoid passing complex querysets)

### Mailpit Not Receiving Emails

**Problem**: Emails not appearing in Mailpit.

**Solutions**:
1. Verify Mailpit is running: `mailpit` or `docker run -d -p 8025:8025 -p 1025:1025 axllent/mailpit`
2. Check `EMAIL_HOST` and `EMAIL_PORT` in settings
3. Ensure `MAIL_DRIVER=mailpit` is set

### Resend API Errors

**Problem**: Resend API errors when sending.

**Solutions**:
1. Verify `RESEND_API_KEY` is set correctly
2. Check API key has proper permissions
3. Verify sender email is verified in Resend
4. Check Resend dashboard for error details

### macOS Fork Errors

**Problem**: `objc_initializeAfterForkError` on macOS.

**Solutions**:
```bash
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
uv run python manage.py rqworker default
```

### Async/Await Issues

**Problem**: `TypeError: object NoneType can't be used in 'await' expression`.

**Solutions**:
1. Ensure you're using `await` with async methods
2. Check that the function is marked as `async`
3. Verify you're using `send_async()` not `send()` in async contexts

---

## Additional Resources

- [Django Templates Documentation](https://docs.djangoproject.com/en/stable/topics/templates/)
- [RQ Documentation](https://python-rq.org/)
- [Resend API Documentation](https://resend.com/docs)
- [Mailpit Documentation](https://github.com/axllent/mailpit)

---

## Quick Reference

### Creating a Mailer

```python
from utils.mailer import Envelope, Mailer

class MyMailer(Mailer):
    def __init__(self, data):
        self.data = data
    
    def envelope(self) -> Envelope:
        return Envelope(
            subject="Subject",
            template="app.templates.emails.template",
            to_emails=["user@example.com"],
            context={"data": self.data}
        )
```

### Sending an Email

```python
mailer = MyMailer(data)
mailer.send()  # Background
# or
mailer.send_now()  # Immediate
# or (async)
await mailer.send_async()  # Background
await mailer.send_async_now()  # Immediate
```

### Sending Multiple Emails

```python
from utils.mailer import MailChain

await MailChain.send_chain_async([
    Mailer1(data1),
    Mailer2(data2),
])
```

---

**Last Updated**: 2024

