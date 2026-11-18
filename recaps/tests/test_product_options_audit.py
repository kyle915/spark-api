"""Read-only audit of recap product-choice options vs each tenant's catalog."""

import json
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, override_settings

from events.models import EventType, Product, ProductType
from recaps.models import (
    CustomField,
    CustomRecapFieldType,
    CustomRecapTemplate,
    RecapSection,
)
from recaps.product_options_audit import audit_product_choice_options
from recaps.products_sampled import PRODUCTS_SAMPLED_TYPES_KEY
from tenants.models import Tenant

User = get_user_model()
URL = "/internal/cron/audit-recap-product-options"
SECRET = "audit-secret"


def _type(name, user):
    return CustomRecapFieldType.objects.get_or_create(
        name=name, defaults={"created_by": user}
    )[0]


def _tenant_with_catalog(user, name, slug, line, products):
    tenant = Tenant.objects.create(name=name, slug=slug, created_by=user)
    ptype = ProductType.objects.create(name=line, tenant=tenant, created_by=user)
    for product in products:
        Product.objects.create(
            name=product, product_type=ptype, tenant=tenant, created_by=user
        )
    return tenant


def _template(user, tenant, fields, layout=None):
    event_type = EventType.objects.create(
        name="Retail Sampling", slug=f"rs-{tenant.slug}", tenant=tenant, created_by=user
    )
    template = CustomRecapTemplate.objects.create(
        name=f"{tenant.name} Recap",
        tenant=tenant,
        event_type=event_type,
        created_by=user,
        layout=layout or {},
    )
    section = RecapSection.objects.create(
        tenant=tenant, name="Sampling", order=0, created_by=user
    )
    for order, (name, ftype, options) in enumerate(fields):
        CustomField.objects.create(
            custom_recap_template=template,
            recap_section=section,
            custom_field_type=_type(ftype, user),
            name=name,
            order=order,
            options=options,
            created_by=user,
        )
    return template


@pytest.fixture
def coho_world(db):
    from tenants.tests.base import ensure_role

    role = ensure_role("Spark Admin", slug="spark-admin")
    user = User.objects.create(username="auditor", email="auditor@test.com", role=role)
    mab = _tenant_with_catalog(
        user, "Mark Anthony Brands", "mark-anthony-brands",
        "The Finnish Long Drink", ["Original", "Cranberry"],
    )
    _template(
        user,
        mab,
        [("Products Sampled", "multiselect", [
            "The Finnish Long Drink — Cranberry",
            "The Finnish Long Drink — Original",
        ])],
    )
    coho = _tenant_with_catalog(
        user, "CoHo Imports", "coho-imports", "Products",
        ["Jinro Chamisul Fresh", "Jinro Plum"],
    )
    _template(
        user,
        coho,
        [
            ("Products Sampled", "multiselect", [
                "The Finnish Long Drink — Original",
                "Products — Jinro Plum",
                "Retired SKU",
            ]),
            ("Sample format", "select", ["Full can", "4oz pour"]),
        ],
        layout={PRODUCTS_SAMPLED_TYPES_KEY: ["The Finnish Long Drink"]},
    )
    return {"mab": mab, "coho": coho, "user": user}


@pytest.mark.django_db
def test_flags_foreign_options_and_writes_nothing(coho_world):
    before = list(CustomField.objects.order_by("id").values_list("options", flat=True))
    result = audit_product_choice_options()
    after = list(CustomField.objects.order_by("id").values_list("options", flat=True))
    assert before == after

    rows = {(r["tenant_slug"], r["field_name"]): r for r in result["fields"]}
    # Ordinary choice fields aren't product pickers.
    assert ("coho-imports", "Sample format") not in rows

    coho = rows[("coho-imports", "Products Sampled")]
    assert coho["foreign"] == [
        {
            "option": "The Finnish Long Drink — Original",
            "owners": [f"[{coho_world['mab'].id}] Mark Anthony Brands"],
        }
    ]
    assert coho["own"] == ["Products — Jinro Plum"]
    assert coho["unknown"] == ["Retired SKU"]
    assert coho["missing"] == ["Jinro Chamisul Fresh"]
    assert coho["stale_type_filter"] == ["The Finnish Long Drink"]
    assert coho["issues"] == ["foreign", "stale_type_filter", "unknown", "missing"]

    mab = rows[("mark-anthony-brands", "Products Sampled")]
    assert mab["issues"] == []
    assert result["summary"]["fields_with_foreign_options"] == 1
    assert result["summary"]["tenants_with_issues"] == [
        f"[{coho_world['coho'].id}] CoHo Imports"
    ]


@pytest.mark.django_db
def test_command_text_and_json(coho_world):
    out = StringIO()
    call_command("audit_recap_product_options", stdout=out)
    text = out.getvalue()
    assert "FOREIGN" in text and "Mark Anthony Brands" in text
    assert "Nothing was changed" in text

    out = StringIO()
    call_command(
        "audit_recap_product_options", "--json", "--tenant-slug", "mark-anthony-brands",
        stdout=out,
    )
    payload = json.loads(out.getvalue())
    assert payload["summary"]["tenants_audited"] == 1
    assert payload["summary"]["fields_with_issues"] == 0


@pytest.mark.django_db(transaction=True)
@override_settings(INTERNAL_CRON_SECRET=SECRET)
def test_cron_endpoint_is_secret_gated_and_read_only(coho_world):
    client = Client()
    assert client.post(URL).status_code == 401
    assert client.post(URL, HTTP_X_CRON_SECRET="nope").status_code == 401

    resp = client.post(URL, HTTP_X_CRON_SECRET=SECRET)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["read_only"] is True
    assert [f["tenant_slug"] for f in body["fields"]] == ["coho-imports"]
    assert "FOREIGN" in body["log"]

    resp = client.post(
        f"{URL}?tenant_slug=mark-anthony-brands&include_ok=true",
        HTTP_X_CRON_SECRET=SECRET,
    )
    body = resp.json()
    assert [f["issues"] for f in body["fields"]] == [[]]
