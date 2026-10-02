"""Torch recap titles read "Store Name #1234" (client ask, Sep 2026).

Walk-in titles were "M/D/YYYY - <address> (<store, if typed>)" and scheduled
events carried whatever the request was called, so the PDF Summary "Name"
rarely said which store or its number. The recap form now asks for both on
store-identity brands and the recap is named from them.
"""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import Client as DjangoClient
from django.urls import reverse
from django.utils import timezone

from ambassadors import checkin_web
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.checkin_tokens import make_checkin_session_token
from recaps.models import CustomRecap, CustomRecapTemplate, FileType


def test_compose_appends_number_once():
    assert checkin_web.compose_store_recap_name("Total Wine & More", "1234") == (
        "Total Wine & More #1234"
    )
    assert checkin_web.compose_store_recap_name("Total Wine & More", "#1234") == (
        "Total Wine & More #1234"
    )
    assert checkin_web.compose_store_recap_name("Total Wine #1234", "1234") == (
        "Total Wine #1234"
    )
    assert checkin_web.compose_store_recap_name("  Big Bend   Liquor ", "") == (
        "Big Bend Liquor"
    )


@pytest.mark.django_db(transaction=True)
class TestStoreRecapName(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.actor = self.create_user(
            username="actor-srn@test.com",
            email="actor-srn@test.com",
            role=self.roles["spark_admin"],
        )
        ba_user = self.create_user(
            username="ba-srn@test.com",
            email="ba-srn@test.com",
            role=self.roles["ambassador"],
        )
        self.ba = self.create_ambassador(ba_user)
        FileType.objects.get_or_create(name="image", defaults={"created_by": self.actor})
        self.http = DjangoClient()

    def _event(self, tenant, *, name, address, code):
        etype = self.create_event_type("Retail Sampling", tenant)
        CustomRecapTemplate.objects.create(
            tenant=tenant, name="Retail", event_type=etype, created_by=self.actor
        )
        event = self.create_event(
            name=name, tenant=tenant, address=address, event_type=etype, date=timezone.now()
        )
        event.walkup_code = code
        event.save(update_fields=["walkup_code"])
        return event

    def _post(self, event, **extra):
        token = make_checkin_session_token(event.id, self.ba.id)
        return self.http.post(
            reverse("events.public_checkin_recap", kwargs={"code": event.walkup_code}),
            data={
                "session": token,
                "fieldValues": [],
                "files": [{"blobName": f"recap_files/checkin/{event.uuid}/a.jpg"}],
                **extra,
            },
            content_type="application/json",
        )

    def test_torch_recap_is_named_store_and_number(self):
        torch = self.create_tenant(name="Torch THC", slug="keee-torch-thc")
        event = self._event(
            torch,
            name="9/12/2026 - 123 Main St, Sarasota, FL 34231",
            address="123 Main St, Sarasota, FL 34231",
            code="TH-SRN1",
        )
        res = self._post(event, storeName="Total Wine & More", storeNumber="1234")
        assert res.status_code == 200, res.content
        assert CustomRecap.objects.get(event=event).name == "Total Wine & More #1234"

    def test_torch_refile_takes_the_corrected_store(self):
        torch = self.create_tenant(name="Torch THC", slug="keee-torch-thc")
        event = self._event(torch, name="Big Bend", address="1 A St, X, MO 63143", code="TH-SRN2")
        self._post(event, storeName="Big Bend Liquor")
        self._post(event, storeName="Big Bend Liquor", storeNumber="7")
        assert list(CustomRecap.objects.filter(event=event).values_list("name", flat=True)) == [
            "Big Bend Liquor #7"
        ]

    def test_torch_without_store_fields_keeps_event_title(self):
        torch = self.create_tenant(name="Torch THC", slug="keee-torch-thc")
        event = self._event(torch, name="Queued offline", address="1 A St, X, MO", code="TH-SRN3")
        assert self._post(event).status_code == 200
        assert CustomRecap.objects.get(event=event).name == "Queued offline"

    def test_other_brands_ignore_store_fields(self):
        other = self.create_tenant(name="Liquid Death", slug="liquid-death")
        event = self._event(other, name="HEB Congress", address="1 Congress Ave", code="LD-SRN")
        self._post(event, storeName="Something Else", storeNumber="9")
        assert CustomRecap.objects.get(event=event).name == "HEB Congress"

    def test_context_prefills_store_only_for_torch(self):
        torch = self.create_tenant(name="Torch THC", slug="keee-torch-thc")
        walkin = self._event(
            torch,
            name="9/12/2026 - 123 Main St, Sarasota, FL (Total Wine & More (Sarasota))",
            address="123 Main St, Sarasota, FL",
            code="TH-SRN4",
        )
        assert checkin_web.build_public_context(walkin)["storeIdentity"] == {
            "name": "Total Wine & More (Sarasota)",
            "number": "",
        }
        bare = self._event(
            torch,
            name="9/12/2026 - 9 Elm St, Tampa, FL",
            address="9 Elm St, Tampa, FL",
            code="TH-SRN5",
        )
        assert checkin_web.build_public_context(bare)["storeIdentity"]["name"] == ""
        other = self.create_tenant(name="Liquid Death", slug="liquid-death")
        ld = self._event(other, name="HEB", address="1 Congress Ave", code="LD-SRN2")
        assert "storeIdentity" not in checkin_web.build_public_context(ld)

    def _filed(self, event, name):
        return CustomRecap.objects.create(
            name=name,
            event=event,
            tenant_id=event.tenant_id,
            ambassador=self.ba,
            created_by=self.actor,
            submitted_at=timezone.now(),
            custom_recap_template=CustomRecapTemplate.objects.filter(
                event_type=event.event_type
            ).first(),
        )

    def _backfill(self, **opts):
        out = io.StringIO()
        call_command("backfill_store_recap_names", stdout=out, **opts)
        return out.getvalue()

    def test_backfill_retitles_filed_torch_recaps(self):
        torch = self.create_tenant(name="Torch THC", slug="keee-torch-thc")
        walkin_title = "9/12/2026 - 123 Main St, Sarasota, FL (Torch Sampling - Total Wine & More (Sarasota))"
        walkin = self._event(torch, name=walkin_title, address="123 Main St, Sarasota, FL", code="TH-B1")
        numbered = self._filed(walkin, walkin_title)
        second = self._filed(walkin, f"{walkin_title} · Second shift")
        scheduled = self._event(torch, name="Big Bend Liquor", address="1 A St, X, MO", code="TH-B2")
        unnumbered = self._filed(scheduled, "Big Bend Liquor")
        hand_typed = self._filed(scheduled, "Ops renamed this one")

        dry = self._backfill(numbers_json='{"Total Wine & More (Sarasota)": "#1234"}')
        assert "DRY-RUN" in dry
        assert "Big Bend Liquor | 1 A St, X, MO | 1 recap(s)" in dry
        assert CustomRecap.objects.get(id=numbered.id).name == walkin_title

        self._backfill(numbers_json='{"Total Wine & More (Sarasota)": "1234"}', apply=True)
        names = dict(CustomRecap.objects.values_list("id", "name"))
        assert names[numbered.id] == "Total Wine & More (Sarasota) #1234"
        assert names[second.id] == "Total Wine & More (Sarasota) #1234 · Second shift"
        assert names[unnumbered.id] == "Big Bend Liquor"
        assert names[hand_typed.id] == "Ops renamed this one"

        rerun = self._backfill(apply=True)
        assert "rename         #" not in rerun
        assert CustomRecap.objects.get(id=numbered.id).name == "Total Wine & More (Sarasota) #1234"

    def test_backfill_uses_store_number_on_file_for_the_address(self):
        torch = self.create_tenant(name="Torch THC", slug="keee-torch-thc")
        event = self._event(torch, name="Big Bend Liquor", address="1 A St, X, MO", code="TH-B3")
        recap = self._filed(event, "Big Bend Liquor")
        with patch(
            "recaps.management.commands.backfill_store_recap_names.known_store_number",
            return_value="77",
        ):
            self._backfill(apply=True)
        assert CustomRecap.objects.get(id=recap.id).name == "Big Bend Liquor #77"

    def test_backfill_finds_torch_by_public_form_slug(self):
        torch = self.create_tenant(name="Torch THC", slug="torch-prod")
        torch.request_url_name = "keee-torch-thc"
        torch.save(update_fields=["request_url_name"])
        event = self._event(torch, name="Big Bend Liquor", address="1 A St, X, MO", code="TH-B5")
        recap = self._filed(event, "Big Bend Liquor")
        self._backfill(numbers_json='{"Big Bend Liquor": "9"}', apply=True)
        assert CustomRecap.objects.get(id=recap.id).name == "Big Bend Liquor #9"

    def test_backfill_recovers_store_from_title_or_same_address(self):
        torch = self.create_tenant(name="Torch THC", slug="keee-torch-thc")
        dated = self._event(torch, name="8/27/2026 - Big Bend Liquor", address="3620 S Big Bend Blvd, Maplewood, MO 63143", code="TH-B6")
        dated_recap = self._filed(dated, dated.name)
        self._event(torch, name="Torch Sampling - Arena Liquors", address="1217 Hampton Avenue, Saint Louis, MO 63139", code="TH-B7")
        bare = self._event(torch, name="9/4/2026 - 1217 Hampton Ave, Saint Louis, MO 63139", address="1217 Hampton Ave, Saint Louis, MO 63139", code="TH-B8")
        bare_recap = self._filed(bare, bare.name)
        nowhere = self._event(torch, name="9/4/2026 - Bryan Road, O'Fallon, Missouri 63368", address="Bryan Rd, O'Fallon, MO 63368", code="TH-B9")
        nowhere_recap = self._filed(nowhere, nowhere.name)

        self._backfill(apply=True)
        assert CustomRecap.objects.get(id=dated_recap.id).name == dated.name

        self._backfill(include_unnumbered=True, apply=True)
        names = dict(CustomRecap.objects.values_list("id", "name"))
        assert names[dated_recap.id] == "Big Bend Liquor"
        assert names[bare_recap.id] == "Arena Liquors"
        assert names[nowhere_recap.id] == nowhere.name

    def test_backfill_matches_total_wine_list_despite_geocoder_noise(self):
        torch = self.create_tenant(name="Torch THC", slug="keee-torch-thc")
        near = self._event(
            torch,
            name="9/23/2026 - 3310 South Glenstone Avenue, Springfield, MO 65804 (Total Wine)",
            address="3310 South Glenstone Avenue, Springfield, MO 65804",
            code="TH-B10",
        )
        near_recap = self._filed(near, near.name)
        typed = self._event(
            torch,
            name="9/17/2026 - Total Wine, Lee's Summit, Missouri 64065",
            address="Total Wine, Lee's Summit, Missouri 64065",
            code="TH-B11",
        )
        typed_recap = self._filed(typed, typed.name)
        kc = self._event(
            torch,
            name="9/20/2026 - Kansas City, Missouri (Total Wine)",
            address="Kansas City, Missouri",
            code="TH-B12",
        )
        kc_recap = self._filed(kc, kc.name)
        spaced = self._event(
            torch,
            name="9/24/2026 - 3954 A Peachtree Rd Ne (Total Wine And More)",
            address="3954 A Peachtree Rd Ne",
            code="TH-B13",
        )
        spaced_recap = self._filed(spaced, spaced.name)

        self._backfill(directory="torch_total_wine_stores", apply=True)
        names = dict(CustomRecap.objects.values_list("id", "name"))
        assert names[near_recap.id] == "Total Wine & More (Springfield) #1809"
        assert names[typed_recap.id] == "Total Wine & More (Lee's Summit) #1807"
        assert names[spaced_recap.id] == "Total Wine & More (Brookhaven) #804"
        assert names[kc_recap.id] == kc.name

    def test_backfill_matches_binnys_list_however_the_ba_wrote_it(self):
        torch = self.create_tenant(name="Torch THC", slug="keee-torch-thc")
        cases = {
            "Binny's (Chicago - Lincoln Park) #24": ("9/20/2026 - 1725 n marcey (Binny's)", "1725 n marcey"),
            "Binny's (Lake Zurich) #20": ("9/20/2026 - lake zurich Illinois (Binny's)", "lake zurich Illinois"),
            "Binny's (Chicago - Logan Square) #35": (
                "9/21/2026 - 3934 W Diversey Ave, Chicago, IL 60647 (Binnys)",
                "3934 W Diversey Ave, Chicago, IL 60647",
            ),
            "Binny's (Chicago - Hyde Park) #7": (
                "Retail Sampling - BINNY'S - HYDE PARK",
                "1240 E. 47th St. Chicago, IL 60653",
            ),
            "Binny's (Joliet) #40": (
                "9/11/2026 - Tonti Drive, Plainfield Township, Illinois 60431",
                "Tonti Drive, Plainfield Township, Illinois 60431",
            ),
            "Binny's (Elmwood Park) #5": (
                "9/17/2026 - 7330 North Avenue, Elmwood Park, IL 60707",
                "7330 North Avenue, Elmwood Park, IL 60707",
            ),
        }
        recaps = {}
        for i, (expected, (name, address)) in enumerate(cases.items()):
            event = self._event(torch, name=name, address=address, code=f"TH-BN{i}")
            recaps[expected] = self._filed(event, name)
        typed_event = self._event(
            torch, name="10/1/2026 - Milwaukee Avenue, Niles, Illinois", address="Milwaukee Ave, Niles, IL", code="TH-BN9"
        )
        typed = self._filed(typed_event, "Binnys Niles")
        elsewhere = self._event(
            torch,
            name="9/4/2026 - Bryan Road, O'Fallon, Missouri 63368",
            address="Bryan Rd, O'Fallon, MO 63368",
            code="TH-BN10",
        )
        elsewhere_recap = self._filed(elsewhere, elsewhere.name)
        promenade = self._event(
            torch,
            name="9/23/2026 - Brentwood Promenade Court, Brentwood, Missouri 63144",
            address="Brentwood Promenade Court, Brentwood, Missouri 63144",
            code="TH-BN11",
        )
        promenade_recap = self._filed(promenade, promenade.name)

        self._backfill(directory="torch_total_wine_stores,torch_binnys_stores", apply=True)
        names = dict(CustomRecap.objects.values_list("id", "name"))
        assert {expected: names[r.id] for expected, r in recaps.items()} == {e: e for e in cases}
        assert names[typed.id] == "Binny's (Niles) #18"
        assert names[elsewhere_recap.id] == elsewhere.name
        assert names[promenade_recap.id] == "Total Wine & More (Brentwood) #1802"

    def test_backfill_numbers_ba_typed_chain_titles_but_not_conflicts(self):
        torch = self.create_tenant(name="Torch THC", slug="keee-torch-thc")
        sunset = self._event(
            torch, name="9/26/2026 - 5601 Brodie Lane, Sunset Valley, TX 78745", address="5601 Brodie Lane, Sunset Valley, TX 78745", code="TH-BC1"
        )
        missing = self._filed(sunset, "Total wine #Sunset valley")
        right = self._filed(sunset, "Total Wine #509")
        akers = self._event(
            torch, name="9/27/2026 - 2955 Cobb Pkwy Atlanta, GA 30339", address="2955 Cobb Pkwy, Atlanta, GA 30339", code="TH-BC2"
        )
        conflict = self._filed(akers, "Total wine Akers mill #803")
        other_chain = self._filed(akers, "Sip & Smoke")

        out = self._backfill(directory="torch_total_wine_stores,torch_binnys_stores", apply=True)
        names = dict(CustomRecap.objects.values_list("id", "name"))
        assert names[missing.id] == "Total Wine & More (Sunset Valley) #509"
        assert names[right.id] == "Total Wine #509"
        assert names[conflict.id] == "Total wine Akers mill #803"
        assert f"conflict       #{conflict.id}  'Total wine Akers mill #803'  (list: Total Wine & More (Atlanta) #805)" in out
        assert names[other_chain.id] == "Sip & Smoke"

    def test_street_key_folds_directionals_but_keeps_unit_letters(self):
        from recaps.management.commands.backfill_store_recap_names import _street

        assert _street("7330 W. North Ave") == _street("7330 North Avenue") == "7330 north"
        assert _street("2712 east colonial drive") == _street("2712 E Colonial Dr") == "2712 colonial"
        assert _street("3954 A Peachtree Rd Ne") == _street("3954A PEACHTREE ROAD NE") == "3954a peachtree"

    def test_placeholder_store_numbers_are_ignored(self):
        assert checkin_web.real_store_number("BINNY-60202") == ""
        assert checkin_web.real_store_number("#1805") == "1805"

    def test_backfill_leaves_other_brands_alone(self):
        torch = self.create_tenant(name="Torch THC", slug="keee-torch-thc")
        other = self.create_tenant(name="Liquid Death", slug="liquid-death")
        self._event(torch, name="Unused", address="2 B St", code="TH-B4")
        ld = self._event(other, name="HEB Congress", address="1 Congress Ave", code="LD-B1")
        recap = self._filed(ld, "HEB Congress")
        self._backfill(numbers_json='{"HEB Congress": "5"}', apply=True)
        assert CustomRecap.objects.get(id=recap.id).name == "HEB Congress"
