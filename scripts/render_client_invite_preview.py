"""Render the client-invite email templates to static HTML for review.

Run from the spark-api worktree with the repo venv:
    SECRET_KEY=preview DEBUG=1 ALLOWED_HOSTS=localhost \
        /Users/kylechristiansen/spark-api/.venv/bin/python scripts/render_client_invite_preview.py <outdir>

No DB, no network, no email sent — pure template rendering.
"""

import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.template.loader import render_to_string  # noqa: E402


class FakeUser:
    first_name = "Ross"
    email = "ross@liquid-death.com"


TOK = "eyJ1Ijo0MiwiZSI6InJvc3NAbGlxdWlkLWRlYXRoLmNvbSIsImsiOiJpbnZpdGUifQ:1tAbCd:xK9mQ2vR8sL4nP7wY3jH6gF0dS5aZ1bN2cV8xM4qW7eR"
BASE = "https://admin.igniteproductions.co"

invite_ctx = {
    "user": FakeUser(),
    "first_name": "Ross",
    "tenant_name": "Liquid Death",
    "set_password_link": f"{BASE}/reset-password/{TOK}",
    "magic_link": f"{BASE}/magic/{TOK}",
    "login_url": f"{BASE}/login",
    # Local preview: served by the http.server rooted at the output dir.
    "brand_mark_url": "ignite-wavy-mark.jpg",
    "expires_days": 7,
}

magic_ctx = {
    "user": FakeUser(),
    "first_name": "Ross",
    "link": f"{BASE}/magic/{TOK}",
    "mobile_link": f"spark://magic/{TOK}",
    "app_primary": False,
    "expires_minutes": 30,
}


def main() -> None:
    outdir = sys.argv[1]
    os.makedirs(outdir, exist_ok=True)

    proposed = render_to_string("emails/client_invite.html", invite_ctx)
    current = render_to_string("emails/magic_link.html", magic_ctx)

    for name, html in (
        ("01-PROPOSED-invite-email.html", proposed),
        ("00-CURRENT-email-magic-link.html", current),
    ):
        path = os.path.join(outdir, name)
        with open(path, "w") as f:
            f.write(html)
        print(f"wrote {path} ({len(html)} bytes)")


if __name__ == "__main__":
    main()
