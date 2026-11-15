"""Store names written several ways, shown as one once staff accept it.

Covers: the grouping rules (case, punctuation, "&"/"and", store numbers kept
apart from their chain); the guards against a mega-merge (group size, cities,
invented names, mixed store numbers); that only ACCEPTED aliases change a
chart, through the real schema with the web client's own query; that a brand
user can neither see nor act on suggestions; and that nothing but alias rows
is ever written.

No test reaches OpenAI: ``utils.ai_text._post_json`` is patched.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from unittest import mock

import pytest
from asgiref.sync import sync_to_async
from django.core.management import call_command
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from config.schema_client import schema_clients
from events import models as em
from events import retailer_aliases as ra
from events.models import RetailerAlias
from recaps import models as recap_models

# src/api/queries/StoreNameSuggestionsQuery.ts
FE_LIST_QUERY = """
query StoreNameSuggestionsQuery($tenantId: ID!) {
  storeNameSuggestions(tenantId: $tenantId) {
    maxGroupSize
    groups {
      groupKey
      canonicalName
      kind
      source
      reason
      canonicalRecords
      affectedRecords
      names {
        uuid
        name
        records
        places
      }
    }
    accepted {
      uuid
      name
      canonicalName
      records
    }
  }
}
"""

# src/api/mutations/DecideStoreNameGroupMutation.ts
FE_DECIDE_MUTATION = """
mutation DecideStoreNameGroupMutation(
  $tenantId: ID!
  $groupKey: String!
  $action: String!
  $canonicalName: String
  $aliasUuids: [ID!]
) {
  decideStoreNameGroup(
    tenantId: $tenantId
    groupKey: $groupKey
    action: $action
    canonicalName: $canonicalName
    aliasUuids: $aliasUuids
  ) {
    ok
    message
    changed
    affectedRecords
  }
}
"""

# src/api/mutations/UndoStoreNameAliasMutation.ts
FE_UNDO_MUTATION = """
mutation UndoStoreNameAliasMutation($tenantId: ID!, $aliasUuid: ID!) {
  undoStoreNameAlias(tenantId: $tenantId, aliasUuid: $aliasUuid) {
    ok
    message
    changed
  }
}
"""

# src/api/mutations/CheckStoreNamesMutation.ts
FE_CHECK_MUTATION = """
mutation CheckStoreNamesMutation($tenantId: ID!) {
  checkStoreNames(tenantId: $tenantId) {
    ok
    message
    namesChecked
    groupsFound
    aiUsed
  }
}
"""

# src/api/queries/ConsumerBreakdownQuery.ts
FE_BREAKDOWN_QUERY = """
query ConsumerBreakdownQuery($tenantId: ID!, $startDate: String, $endDate: String) {
  tenantConsumerBreakdown(tenantId: $tenantId, startDate: $startDate, endDate: $endDate) {
    total
    recaps
    storeCount
    stateCount
    byWeek { weekStart consumers recaps }
    byStore { label kind consumers recaps }
    byState { label kind consumers recaps }
  }
}
"""

POST_JSON = "utils.ai_text._post_json"


def _ai_reply(payload: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(payload)}}]}


def _info(name: str, records: int = 1, cities: set[str] | None = None) -> ra.NameInfo:
    return ra.NameInfo(
        key=ra.alias_key(name),
        spellings=Counter({name: records}),
        records=records,
        cities=set(cities or ()),
    )


def _names(*infos: ra.NameInfo) -> dict[str, ra.NameInfo]:
    return {i.key: i for i in infos}


# ─── Pure rules ─────────────────────────────────────────────────────


def test_keys_ignore_case_punctuation_and_ampersand():
    assert ra.alias_key("  H-E-B ") == "h-e-b"
    assert ra.chain_key("H-E-B") == ra.chain_key("heb") == ra.chain_key("H.E.B.") == "heb"
    assert ra.chain_key("Total Wine & More") == ra.chain_key("total wine and more")
    assert ra.chain_key("Trader Joe's") == ra.chain_key("Trader Joes")
    assert ra.chain_key("Total Wine") != ra.chain_key("Total Wine & More")


def test_store_numbers_are_read_off_the_end():
    assert ra.split_store_number("HEB #725") == ("HEB", "725", True)
    assert ra.split_store_number("Kroger Store 412") == ("Kroger", "412", True)
    assert ra.split_store_number("Heb 725") == ("Heb", "725", False)
    assert ra.split_store_number("7-Eleven #3201") == ("7-Eleven", "3201", True)
    assert ra.split_store_number("Total Wine & More") == ("Total Wine & More", "", False)
    # One digit is part of a name ("Pier 1", "Motel 6"), not a store number.
    assert ra.split_store_number("Pier 1") == ("Pier 1", "", False)
    assert ra.split_store_number("Vino 12")[0] == "Vino"


def test_spellings_of_one_chain_group_and_a_numbered_store_stays_a_store():
    names = _names(
        _info("HEB", 40),
        _info("H-E-B", 12),
        _info("Heb 725", 3),
        _info("HEB #725", 2),
        _info("heb 612", 1),
        _info("Total Wine", 9),
        _info("Total Wine & More", 4),
        _info("Forever 21", 2),
    )
    groups, _parsed = ra.deterministic_groups(names)
    by_kind = {(g.kind, g.canonical): sorted(m.display for m in g.members) for g in groups}
    assert by_kind == {
        ("chain", "HEB"): ["H-E-B", "HEB"],
        # The numbered store is its own store, named after its chain.
        ("store", "HEB #725"): ["HEB #725", "Heb 725"],
    }
    # Store 612 is a different store, "Total Wine & More" needs a person (or
    # the model) to say so, and "Forever 21" is a name, not store 21.


def test_one_store_number_in_two_towns_is_not_one_store():
    names = _names(
        _info("HEB"),
        _info("Heb 725", cities={"austin|TX"}),
        _info("HEB #725", cities={"laredo|TX"}),
    )
    groups, _ = ra.deterministic_groups(names)
    assert [g.kind for g in groups] == []


def test_resolving_follows_accepted_names_and_survives_a_loop():
    aliases = {"h-e-b": "HEB", "heb": "H-E-B Grocery", "a": "B", "b": "A"}
    assert ra.resolve_store_name("H-E-B", aliases) == "H-E-B Grocery"
    assert ra.resolve_store_name("Kroger", aliases) == "Kroger"
    # No alias: the name comes back untouched, spacing and all.
    assert ra.resolve_store_name("  Kroger ", {}) == "  Kroger "
    assert ra.resolve_store_name("  Kroger ", aliases) == "  Kroger "
    assert ra.resolve_store_name(None, aliases) == ""
    assert ra.resolve_store_name("a", aliases) in ("A", "B")  # ends, either way


def _vet(raw, sent, **kwargs):
    numbers = {p.info.key: p.ident[1] for p in ra._parse_names(sent)}
    return ra.vet_ai_group(
        raw,
        sent=sent,
        new_keys=kwargs.pop("new_keys", set(sent)),
        numbers=numbers,
        owner_of=kwargs.pop("owner_of", {}),
        accepted={},
        retailer_names=set(),
    )


def test_a_model_group_of_more_than_eight_names_is_refused():
    sent = _names(*[_info(f"Liquor Barn {word}") for word in "abcdefghi"])
    assert len(sent) == ra.MAX_GROUP_NAMES + 1
    raw = {"canonical": "Liquor Barn", "kind": "chain", "names": [i.display for i in sent.values()], "reason": ""}
    assert _vet(raw, sent) is None


def test_the_model_cannot_invent_a_name_or_mix_store_numbers():
    sent = _names(_info("Kroger"), _info("Krogers"), _info("Kroger #12"), _info("Kroger #40"))
    invented = {"canonical": "Kroger", "kind": "chain", "names": ["Kroger", "King Soopers"], "reason": ""}
    assert _vet(invented, sent) is None
    numbered = {"canonical": "Kroger", "kind": "chain", "names": ["Kroger", "Kroger #12"], "reason": ""}
    assert _vet(numbered, sent) is None
    two_stores = {"canonical": "Kroger #12", "kind": "store", "names": ["Kroger #12", "Kroger #40"], "reason": ""}
    assert _vet(two_stores, sent) is None
    fine = {"canonical": "Kroger", "kind": "chain", "names": ["Kroger", "Krogers"], "reason": "A typo."}
    group = _vet(fine, sent)
    assert group is not None and group.canonical == "Kroger" and group.kind == "chain"


def test_different_towns_only_group_as_a_clear_chain_alias():
    plano = _info("Total Wine Plano", cities={"plano|TX"})
    frisco = _info("Total Wine Frisco", cities={"frisco|TX"})
    sent = _names(plano, frisco)
    raw = {"canonical": "Total Wine", "kind": "chain", "names": [plano.display, frisco.display], "reason": ""}
    assert _vet(raw, sent) is None

    short = _info("Total Wine", 9, cities={"tempe|AZ"})
    longer = _info("Total Wine & More", 4, cities={"dallas|TX"})
    sent = _names(short, longer)
    raw = {"canonical": "Total Wine", "kind": "chain", "names": [short.display, longer.display], "reason": "Same chain."}
    group = _vet(raw, sent)
    assert group is not None and group.canonical == "Total Wine"
    # Called one single location, in two towns: refused.
    raw["kind"] = "store"
    assert _vet(raw, sent) is None


def test_the_model_cannot_join_two_groups_that_already_exist():
    sent = _names(_info("HEB"), _info("Kroger"), _info("Grocer"))
    raw = {"canonical": "HEB", "kind": "chain", "names": ["HEB", "Kroger", "Grocer"], "reason": ""}
    group = _vet(
        raw, sent, new_keys={"grocer"}, owner_of={"heb": "HEB", "kroger": "Kroger"}
    )
    assert group is None


# ─── Through the database and the real schema ───────────────────────


@pytest.mark.django_db(transaction=True)
class TestRetailerAliases(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, settings):
        settings.OPENAI_API_KEY = "test-key"
        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.sys = self.get_system_user()
        self.tenant = self.create_tenant(name="Alias Brand")
        self.other = self.create_tenant(name="Alias Other Brand")
        self.admin = self.create_user(
            username="alias-admin",
            email="alias-admin@igniteproductions.co",
            role=self.roles["spark_admin"],
            is_staff=True,
        )
        self.brand_user = self.create_user(
            username="alias-client", email="alias-client@test.com", role=self.roles["client"]
        )
        self.create_tenanted_user(self.brand_user, self.tenant)
        self.et = em.EventType.objects.create(
            name="Retail Sampling", slug="alias-retail", tenant=self.tenant, created_by=self.sys
        )
        self.template = recap_models.CustomRecapTemplate.objects.create(
            name="Alias Retail", event_type=self.et, tenant=self.tenant, created_by=self.sys
        )
        num = recap_models.CustomRecapFieldType.objects.create(name="Number", created_by=self.sys)
        section = recap_models.RecapSection.objects.create(
            name="Sampling", tenant=self.tenant, created_by=self.sys
        )
        self.sampled = recap_models.CustomField.objects.create(
            name="Total number of consumers sampled",
            custom_recap_template=self.template,
            custom_field_type=num,
            recap_section=section,
            created_by=self.sys,
        )
        self.request_type = em.RequestType.objects.create(
            name="Retail", tenant=self.tenant, created_by=self.sys
        )
        self._day = 0

    def _retailer(self, name, tenant=None):
        return em.Retailer.objects.create(
            name=name, tenant=tenant or self.tenant, created_by=self.sys
        )

    def _visit(self, *, retailer=None, typed=None, consumers=10, address="1 Main St, Austin, TX 78701", tenant=None):
        """One approved recap at a store named by a Retailer row or typed."""
        self._day += 1
        tenant = tenant or self.tenant
        when = timezone.make_aware(datetime(2026, 10, 1 + (self._day % 27), 18))
        request = None
        if typed is not None:
            request = em.Request.objects.create(
                name="Gig", address=address, request_type=self.request_type,
                tenant=tenant, created_by=self.sys, date=when, retailer_name=typed,
            )
        event = em.Event.objects.create(
            name=f"Event {self._day}", tenant=tenant, address=address, date=when,
            event_type=self.et, created_by=self.sys, updated_by=self.sys,
            retailer=retailer, request=request,
        )
        recap = recap_models.CustomRecap.objects.create(
            name="r", approved=True, submitted_at=when, event=event, tenant=tenant,
            custom_recap_template=self.template, created_by=self.sys,
        )
        recap_models.CustomFieldValue.objects.create(
            custom_recap=recap, custom_field=self.sampled, value=str(consumers),
            created_by=self.sys,
        )
        return event

    def _seed_heb(self):
        heb = self._retailer("HEB")
        self._visit(retailer=heb, consumers=100)
        self._visit(retailer=heb, consumers=100)
        self._visit(typed="H-E-B", consumers=40)
        self._visit(typed="heb", consumers=5)
        self._visit(typed="Heb 725", consumers=7)
        self._visit(typed="HEB #725", consumers=3)
        self._visit(retailer=self._retailer("Kroger"), consumers=60)

    async def _stores(self, user=None):
        res = await self._execute_mutation(
            FE_BREAKDOWN_QUERY,
            {"tenantId": str(self.tenant.id), "startDate": "2026-10-01", "endDate": "2026-10-31"},
            self.endpoint_path,
            user=user or self.admin,
        )
        assert res.errors is None, res.errors
        data = res.data["tenantConsumerBreakdown"]
        return {r["label"]: r["consumers"] for r in data["byStore"]}, data

    def _snapshot(self):
        """Everything that must never change: retailers and stored names."""
        return (
            sorted(em.Retailer.objects.values_list("id", "name", "tenant_id")),
            sorted(em.Request.objects.values_list("id", "retailer_id", "retailer_name")),
            sorted(em.Event.objects.values_list("id", "retailer_id", "request_id")),
        )

    # ── proposing ─────────────────────────────────────────────────────

    def test_names_are_counted_the_way_the_charts_read_them(self):
        self._seed_heb()
        # A request with no event yet counts; a deleted one does not.
        em.Request.objects.create(
            name="Later", address="2 Oak St, Dallas, TX 75201", request_type=self.request_type,
            tenant=self.tenant, created_by=self.sys, retailer_name="H-E-B",
        )
        em.Request.objects.create(
            name="Gone", address="3 Elm St", request_type=self.request_type,
            tenant=self.tenant, created_by=self.sys, retailer_name="H-E-B",
            deleted_at=timezone.now(),
        )
        self._visit(typed="H-E-B", tenant=self.other)
        names = ra.collect_store_names(self.tenant.id)
        assert {k: v.records for k, v in names.items()} == {
            "heb": 3, "h-e-b": 2, "heb 725": 1, "heb #725": 1, "kroger": 1,
        }
        assert names["heb"].display == "HEB"
        assert names["h-e-b"].cities == {"austin|TX", "dallas|TX"}

    def test_rules_propose_without_the_model_and_change_no_chart(self):
        self._seed_heb()
        before = self._snapshot()
        with mock.patch(POST_JSON) as post:
            result = ra.propose_for_tenant(self.tenant.id, use_ai=False)
            post.assert_not_called()
        assert (result.groups, result.proposed) == (2, 2)
        rows = {r.alias_name: r for r in RetailerAlias.objects.all()}
        assert set(rows) == {"H-E-B", "Heb 725"}
        assert rows["H-E-B"].canonical_name == "HEB" and rows["H-E-B"].kind == "chain"
        assert rows["Heb 725"].canonical_name == "HEB #725" and rows["Heb 725"].kind == "store"
        assert {r.status for r in rows.values()} == {"proposed"}
        assert self._snapshot() == before

    def test_the_model_sees_names_and_counts_only_and_is_asked_once(self):
        self._visit(typed="Total Wine", consumers=10, address="1730 E Warner Rd, Tempe, AZ 85284")
        self._visit(typed="Total Wine", consumers=10, address="1730 E Warner Rd, Tempe, AZ 85284")
        self._visit(typed="Total Wine & More", consumers=10, address="9 Elm Ave, Dallas, TX 75201")
        self._visit(typed="Spec's", consumers=10, address="77 Smith St, Houston, TX 77002")
        reply = _ai_reply(
            {
                "groups": [
                    {
                        "canonical": "Total Wine",
                        "kind": "chain",
                        "names": ["Total Wine", "Total Wine & More"],
                        "reason": "The same chain, with and without “& More”.",
                    },
                    # Not names it was sent: dropped.
                    {"canonical": "Spec's", "kind": "chain", "names": ["Spec's", "Specs Liquor"], "reason": ""},
                ]
            }
        )
        with mock.patch(POST_JSON, return_value=reply) as post:
            result = ra.propose_for_tenant(self.tenant.id)
            assert post.call_count == 1
            sent = json.dumps(post.call_args.args[1])
            for private in ("Warner", "Tempe", "Dallas", "Elm Ave", "85284", "Houston"):
                assert private not in sent
            assert "Total Wine (2)" in sent and "Total Wine & More (1)" in sent
            # Nothing new to ask about: a second run costs no call.
            again = ra.propose_for_tenant(self.tenant.id)
            assert post.call_count == 1
        assert result.ai_used and result.groups == 1 and result.refused == 1
        assert again.groups == 0
        row = RetailerAlias.objects.get(status="proposed")
        assert (row.alias_name, row.canonical_name, row.source) == (
            "Total Wine & More", "Total Wine", "ai",
        )
        assert row.reason.startswith("The same chain")
        # Looked at, nothing found: remembered, and read by nothing.
        assert set(
            RetailerAlias.objects.filter(status="none").values_list("alias_name", flat=True)
        ) == {"Total Wine", "Spec's"}
        assert ra.accepted_alias_map(self.tenant.id) == {}

    def test_no_key_or_a_failed_call_still_proposes_by_rule(self, settings):
        self._seed_heb()
        self._visit(typed="Total Wine & More")
        settings.OPENAI_API_KEY = ""
        with mock.patch(POST_JSON) as post:
            result = ra.propose_for_tenant(self.tenant.id)
            post.assert_not_called()
        assert result.groups == 2 and result.ai_available is False
        # The unseen names are not marked as looked at, so a later run asks.
        assert not RetailerAlias.objects.filter(status="none").exists()

        settings.OPENAI_API_KEY = "test-key"
        from utils.ai_text import AiUnavailable

        with mock.patch(POST_JSON, side_effect=AiUnavailable("AI service timed out.")):
            result = ra.propose_for_tenant(self.tenant.id)
        assert result.ai_available is False
        assert not RetailerAlias.objects.filter(status="none").exists()

    def test_many_spellings_of_one_name_are_split_into_groups_of_eight(self):
        spellings = [
            "Total Wine", "TOTAL WINE.", "Total-Wine", "Total  Wine;", "Total.Wine",
            "TotalWine", "Total Wine,", "Total, Wine", "Total_Wine", "(Total Wine)",
            "Total Wine!", "Total / Wine",
        ]
        for name in spellings:
            self._visit(typed=name)
        result = ra.propose_for_tenant(self.tenant.id, use_ai=False)
        sizes = Counter(RetailerAlias.objects.values_list("group_key", flat=True))
        assert sum(sizes.values()) == 11  # every spelling but the one kept
        assert max(sizes.values()) + 1 <= ra.MAX_GROUP_NAMES
        assert result.groups == len(sizes) == 2
        assert set(RetailerAlias.objects.values_list("canonical_name", flat=True)) == {"Total Wine"}

    def test_the_command_is_safe_to_re_run(self):
        self._seed_heb()
        with mock.patch(POST_JSON) as post:
            call_command("propose_retailer_aliases", "--no-ai")
            first = sorted(RetailerAlias.objects.values_list("alias_key", "status", "group_key"))
            call_command("propose_retailer_aliases", "--no-ai")
            call_command("propose_retailer_aliases", "--no-ai", "--dry-run")
            post.assert_not_called()
        assert sorted(RetailerAlias.objects.values_list("alias_key", "status", "group_key")) == first
        assert len(first) == 2

    # ── what a chart shows ────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_only_an_accepted_alias_changes_the_grouping(self):
        await sync_to_async(self._seed_heb)()
        before = await sync_to_async(self._snapshot)()
        as_typed = {"HEB": 205, "H-E-B": 40, "Heb 725": 7, "HEB #725": 3, "Kroger": 60}
        stores, data = await self._stores()
        assert stores == as_typed and data["total"] == 315 and data["storeCount"] == 5

        # Proposed: nothing moves.
        await sync_to_async(ra.propose_for_tenant)(self.tenant.id, use_ai=False)
        stores, _ = await self._stores()
        assert stores == as_typed

        listed = await self._execute_mutation(
            FE_LIST_QUERY, {"tenantId": str(self.tenant.id)}, user=self.admin
        )
        assert listed.errors is None, listed.errors
        groups = {g["canonicalName"]: g for g in listed.data["storeNameSuggestions"]["groups"]}
        assert set(groups) == {"HEB", "HEB #725"}
        chain = groups["HEB"]
        # Shown before the click: one record would be re-labelled.
        assert (chain["kind"], chain["source"], chain["affectedRecords"], chain["canonicalRecords"]) == (
            "chain", "rule", 1, 3,
        )
        assert [(n["name"], n["records"], n["places"]) for n in chain["names"]] == [
            ("H-E-B", 1, "Austin, TX")
        ]
        assert listed.data["storeNameSuggestions"]["maxGroupSize"] == 8

        # Dismissed: nothing moves, and it is not proposed again.
        store = groups["HEB #725"]
        dismissed = await self._execute_mutation(
            FE_DECIDE_MUTATION,
            {"tenantId": str(self.tenant.id), "groupKey": store["groupKey"], "action": "dismiss"},
            user=self.admin,
        )
        assert dismissed.errors is None, dismissed.errors
        assert dismissed.data["decideStoreNameGroup"] == {
            "ok": True, "message": "", "changed": 1, "affectedRecords": 0,
        }
        stores, _ = await self._stores()
        assert stores == as_typed
        await sync_to_async(ra.propose_for_tenant)(self.tenant.id, use_ai=False)
        assert await sync_to_async(
            RetailerAlias.objects.filter(status="proposed", kind="store").count
        )() == 0

        # Accepted: the two spellings are one row, and the total is the same.
        accepted = await self._execute_mutation(
            FE_DECIDE_MUTATION,
            {"tenantId": str(self.tenant.id), "groupKey": chain["groupKey"], "action": "accept"},
            user=self.admin,
        )
        assert accepted.errors is None, accepted.errors
        assert accepted.data["decideStoreNameGroup"] == {
            "ok": True, "message": "", "changed": 1, "affectedRecords": 1,
        }
        stores, data = await self._stores()
        assert stores == {"HEB": 245, "Heb 725": 7, "HEB #725": 3, "Kroger": 60}
        assert data["total"] == 315 and data["storeCount"] == 4
        # A brand's own user sees the same, tidied chart.
        client_stores, _ = await self._stores(user=self.brand_user)
        assert client_stores == stores

        # Undone: back to exactly what it was.
        listed = await self._execute_mutation(
            FE_LIST_QUERY, {"tenantId": str(self.tenant.id)}, user=self.admin
        )
        held = listed.data["storeNameSuggestions"]["accepted"]
        assert [(a["name"], a["canonicalName"], a["records"]) for a in held] == [("H-E-B", "HEB", 1)]
        undone = await self._execute_mutation(
            FE_UNDO_MUTATION,
            {"tenantId": str(self.tenant.id), "aliasUuid": held[0]["uuid"]},
            user=self.admin,
        )
        assert undone.errors is None, undone.errors
        assert undone.data["undoStoreNameAlias"] == {"ok": True, "message": "", "changed": 1}
        stores, _ = await self._stores()
        assert stores == as_typed

        # Through all of it, no retailer, request or event was touched.
        assert await sync_to_async(self._snapshot)() == before

    @pytest.mark.asyncio
    async def test_staff_can_keep_a_different_spelling(self):
        await sync_to_async(self._seed_heb)()
        await sync_to_async(ra.propose_for_tenant)(self.tenant.id, use_ai=False)
        chain = await sync_to_async(RetailerAlias.objects.get)(alias_key="h-e-b")
        res = await self._execute_mutation(
            FE_DECIDE_MUTATION,
            {
                "tenantId": str(self.tenant.id),
                "groupKey": chain.group_key,
                "action": "accept",
                "canonicalName": "  H-E-B ",
            },
            user=self.admin,
        )
        assert res.errors is None, res.errors
        assert res.data["decideStoreNameGroup"]["ok"] is True
        # 2 events as "HEB" + 1 as "heb" are re-labelled; "H-E-B" is kept.
        assert res.data["decideStoreNameGroup"]["affectedRecords"] == 3
        stores, _ = await self._stores()
        assert stores == {"H-E-B": 245, "Heb 725": 7, "HEB #725": 3, "Kroger": 60}
        # The numbered store still waiting follows the spelling that was kept.
        waiting = await sync_to_async(RetailerAlias.objects.get)(alias_key="heb 725")
        assert waiting.canonical_name == "H-E-B #725" and waiting.status == "proposed"
        retailer = await sync_to_async(em.Retailer.objects.get)(name="HEB", tenant=self.tenant)
        assert retailer.name == "HEB"  # the row itself is not renamed

    @pytest.mark.asyncio
    async def test_a_group_of_more_than_eight_names_cannot_be_accepted(self):
        def _make():
            rows = [
                RetailerAlias(
                    tenant=self.tenant, alias_key=f"big store {i}", alias_name=f"Big Store {i}",
                    canonical_name="Big Store", group_key="toobig", status="proposed",
                )
                for i in range(ra.MAX_GROUP_NAMES)
            ]
            RetailerAlias.objects.bulk_create(rows)

        await sync_to_async(_make)()
        res = await self._execute_mutation(
            FE_DECIDE_MUTATION,
            {"tenantId": str(self.tenant.id), "groupKey": "toobig", "action": "accept"},
            user=self.admin,
        )
        assert res.errors is None, res.errors
        out = res.data["decideStoreNameGroup"]
        assert out["ok"] is False and "at most 8" in out["message"]
        assert await sync_to_async(RetailerAlias.objects.filter(status="accepted").count)() == 0

        # A few at a time is allowed.
        some = await sync_to_async(
            lambda: [str(u) for u in RetailerAlias.objects.values_list("uuid", flat=True)[:3]]
        )()
        res = await self._execute_mutation(
            FE_DECIDE_MUTATION,
            {"tenantId": str(self.tenant.id), "groupKey": "toobig", "action": "accept", "aliasUuids": some},
            user=self.admin,
        )
        assert res.data["decideStoreNameGroup"]["ok"] is True
        assert await sync_to_async(RetailerAlias.objects.filter(status="accepted").count)() == 3

    @pytest.mark.asyncio
    async def test_a_name_already_filed_under_another_cannot_be_the_one_kept(self):
        def _make():
            RetailerAlias.objects.create(
                tenant=self.tenant, alias_key="h-e-b", alias_name="H-E-B",
                canonical_name="HEB", group_key="g1", status="accepted",
            )
            RetailerAlias.objects.create(
                tenant=self.tenant, alias_key="heb grocery", alias_name="HEB Grocery",
                canonical_name="HEB", group_key="g2", status="proposed",
            )

        await sync_to_async(_make)()
        res = await self._execute_mutation(
            FE_DECIDE_MUTATION,
            {"tenantId": str(self.tenant.id), "groupKey": "g2", "action": "accept", "canonicalName": "H-E-B"},
            user=self.admin,
        )
        out = res.data["decideStoreNameGroup"]
        assert out["ok"] is False and "already shown as" in out["message"]

    # ── who may use it ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_a_brand_user_cannot_see_or_act_on_suggestions(self):
        await sync_to_async(self._seed_heb)()
        await sync_to_async(ra.propose_for_tenant)(self.tenant.id, use_ai=False)
        chain = await sync_to_async(RetailerAlias.objects.get)(alias_key="h-e-b")
        tenant_id = str(self.tenant.id)  # their OWN brand

        listed = await self._execute_mutation(
            FE_LIST_QUERY, {"tenantId": tenant_id}, user=self.brand_user
        )
        assert listed.errors is not None and "Staff only" in str(listed.errors[0])

        decided = await self._execute_mutation(
            FE_DECIDE_MUTATION,
            {"tenantId": tenant_id, "groupKey": chain.group_key, "action": "accept"},
            user=self.brand_user,
        )
        assert decided.errors is not None and "Staff only" in str(decided.errors[0])

        undone = await self._execute_mutation(
            FE_UNDO_MUTATION, {"tenantId": tenant_id, "aliasUuid": str(chain.uuid)}, user=self.brand_user
        )
        assert undone.errors is not None and "Staff only" in str(undone.errors[0])

        with mock.patch(POST_JSON) as post:
            checked = await self._execute_mutation(
                FE_CHECK_MUTATION, {"tenantId": tenant_id}, user=self.brand_user
            )
            post.assert_not_called()
        assert checked.errors is not None and "Staff only" in str(checked.errors[0])

        assert await sync_to_async(
            lambda: set(RetailerAlias.objects.values_list("status", flat=True))
        )() == {"proposed"}

    @pytest.mark.asyncio
    async def test_an_ambassador_gets_nothing(self):
        ba = await sync_to_async(self.create_user)(
            username="alias-ba", email="alias-ba@test.com", role=self.roles["ambassador"]
        )
        listed = await self._execute_mutation(
            FE_LIST_QUERY, {"tenantId": str(self.tenant.id)}, user=ba
        )
        assert listed.errors is not None

    @pytest.mark.asyncio
    async def test_staff_can_check_a_brand_now(self):
        await sync_to_async(self._seed_heb)()
        with mock.patch(POST_JSON, return_value=_ai_reply({"groups": []})) as post:
            res = await self._execute_mutation(
                FE_CHECK_MUTATION, {"tenantId": str(self.tenant.id)}, user=self.admin
            )
            assert post.call_count == 1
        assert res.errors is None, res.errors
        assert res.data["checkStoreNames"] == {
            "ok": True, "message": "", "namesChecked": 5, "groupsFound": 2, "aiUsed": True,
        }
