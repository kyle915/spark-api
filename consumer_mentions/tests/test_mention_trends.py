"""What consumers mention, month over month.

Runs the tagging command and the web client's own query against the real
``schema_clients`` surface. The OpenAI transport
(``utils.ai_text._post_json``) is replaced in every test, so nothing here
reaches the network; the prompts that would have been sent are what the
data rules are asserted on:

  * only approved, unarchived recaps under a live request, no ambassador
    name, no other brand's recaps;
  * a recap is read once, and again only when its comments change;
  * dry run calls nothing and stores nothing; the call cap holds;
  * a theme key not on the list, a brand not written in the comment and the
    brand's own name are all dropped;
  * every count is recaps, counted here; noise is not called a trend;
  * a brand user is held to their own tenant, staff may choose;
  * a recap unapproved after it was tagged drops out of the counts.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time
from io import StringIO
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.core.management import call_command
from django.utils import timezone

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from consumer_mentions.tagging import read_tags, source_hash
from consumer_mentions.trends import (
    DIRECTION_FALLING,
    DIRECTION_RISING,
    DIRECTION_STEADY,
    DIRECTION_TOO_FEW,
    month_keys,
    summarize,
)
from consumer_mentions.vocabulary import (
    OTHER,
    PROMOTE_MIN_RECAPS,
    promotions,
    resolve,
    seed_themes,
)
from events import models as event_models
from recaps import models as recap_models
from recaps.feedback_themes import KIND_GENERAL, Comment
from recaps.weekly_narration import numbers_are_grounded
from utils.ai_text import AiUnavailable

# The field set the web client asks for (src/api/queries/TenantMentionTrendsQuery.ts).
TRENDS = """
query TenantMentionTrendsQuery($tenantId: ID!, $endDate: String, $months: Int) {
  tenantMentionTrends(tenantId: $tenantId, endDate: $endDate, months: $months) {
    status
    months
    recapCounts
    compareMonth
    priorMonth
    minMonthRecaps
    minTrendMentions
    otherCount
    headlines
    taggedAt
    themes {
      key label polarity counts total current previous
      currentShare previousShare direction quote
    }
    brands {
      key label counts total current previous
      currentShare previousShare direction
    }
  }
}
"""

TAG_MARKER = "You tag comments that shoppers made"


def _reply(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _numbered(prompt: str):
    for line in prompt.splitlines():
        match = re.match(r"^(\d+)\.(?: \[(\w+)\])? (.*)$", line)
        if match:
            yield int(match[1]), match[2], match[3].lower()


def _fake_model(url, payload, headers, timeout):
    """Tags by keyword, with the mistakes a model makes: a key that is not on
    the list, a brand the comment never names, the brand's own name as
    another brand, themes on a competitor field, a comment that does not
    exist."""
    user = payload["messages"][1]["content"]
    out = []
    for number, kind, text in _numbered(user):
        themes, other, brands = [], [], []
        if "too sweet" in text:
            themes.append("too_sweet")
        if "pricey" in text:
            themes.append("price_too_high")
        if "caffeine" in text:
            other.append({"label": "wants more caffeine", "polarity": "request"})
        if "smooth" in text:
            other.append({"label": "Smooth, no crash", "polarity": "praise"})
        if kind == "competitor":
            themes.append("great_taste")  # must be ignored
        for written, name in (
            ("celsius", "Celsius"),
            ("redbull", "Red Bull"),
            ("red bull", "Red Bull"),
            ("monster", "Monster Energy"),
        ):
            if written in text:
                brands.append({"as_written": written, "name": name})
        if "famus" in text:
            brands.append({"as_written": "FAMUS", "name": "FAMUS"})
        themes.append("made_up_key")
        brands.append({"as_written": "Rockstar", "name": "Rockstar"})
        out.append({"comment": number, "themes": themes, "other": other, "brands": brands})
    out.append({"comment": 999, "themes": ["too_sweet"], "other": [], "brands": []})
    return _reply(json.dumps({"comments": out, "total": 77}))


def _tag_calls(post) -> list[str]:
    return [
        call.args[1]["messages"][1]["content"]
        for call in post.call_args_list
        if TAG_MARKER in call.args[1]["messages"][0]["content"]
    ]


# September: 6 recaps. October: 7 recaps.
SEPTEMBER = [
    "Too sweet for me",
    "I drink Celsius every day",
    "Smooth and tasty",
    "Nice can",
    "Good stuff",
    "Wish it had more caffeine",
]
OCTOBER = [
    "Way too sweet",
    "Too sweet but I like it",
    "a bit too sweet, prefer celsius",
    "Too sweet, I usually grab a Red Bull",
    "smooth, I switch from celsius",
    "More caffeine please",
    "needs more caffeine than monster",
]


@pytest.mark.django_db(transaction=True)
class TestMentionTrends(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, settings):
        from config.schema_client import schema_clients

        settings.OPENAI_API_KEY = "test-key-not-real"
        settings.OPENAI_MODEL = ""

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"
        self.system_user = self.get_system_user()

        self.tenant = self.create_tenant(name="FAMUS Energy")
        self.other = self.create_tenant(name="Beta Brand")
        self.admin = self.create_user(
            username="mention-admin", email="mention-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="mention-client", email="mention-client@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)
        credited = self.create_user(
            username="jordan-rivera-mention", email="jordan.rivera.mention@ba.test",
            role=self.roles["ambassador"],
        )
        credited.first_name, credited.last_name = "Jordan", "Rivera"
        credited.save()
        self.ba = self.create_ambassador(credited)

        self.request = self._request(self.tenant, "FAMUS Tour")
        self.recaps = []
        for day, text in enumerate(SEPTEMBER, start=1):
            self.recaps.append(self._recap(self._event(self.tenant, date(2026, 9, day)), quotes=text))
        for day, text in enumerate(OCTOBER, start=1):
            self.recaps.append(self._recap(self._event(self.tenant, date(2026, 10, day)), quotes=text))
        # A BA's name in a comment never reaches the model.
        self.ba_recap = self._recap(
            self._event(self.tenant, date(2026, 10, 20)),
            feedback="Jordan Rivera said shoppers found it too sweet",
        )
        # Never read: pending, archived, under a deleted request, another brand.
        self._recap(self._event(self.tenant, date(2026, 10, 9)), quotes="PENDING celsius", approved=False)
        archived = self._recap(self._event(self.tenant, date(2026, 10, 9)), quotes="ARCHIVED celsius")
        recap_models.Recap.objects.filter(id=archived.id).update(archived_at=timezone.now())
        deleted = self._request(self.tenant, "Deleted Tour")
        event_models.Request.objects.filter(id=deleted.id).update(deleted_at=timezone.now())
        self._recap(self._event(self.tenant, date(2026, 10, 9), request=deleted), quotes="DELETED celsius")
        beta_request = self._request(self.other, "Beta Tour")
        self._recap(
            self._event(self.other, date(2026, 10, 9), request=beta_request),
            quotes="BETA SECRET loves monster",
        )
        # A competitor question on a custom template: brands only.
        self.competitor_recap = self._custom_competitor(
            self._event(self.tenant, date(2026, 10, 12)), "Celsius, Monster"
        )

        patcher = patch("utils.ai_text._post_json", side_effect=AiUnavailable("blocked"))
        self.post = patcher.start()
        yield
        patcher.stop()

    # ── fixtures ──────────────────────────────────────────────────────

    def _request(self, tenant, name):
        return event_models.Request.objects.create(
            name=name,
            address="1 Main St",
            tenant=tenant,
            status=self.create_request_status(name="Approved", tenant=tenant, slug="approved"),
            request_type=self.create_request_type(name="Retail", tenant=tenant),
            created_by=self.system_user,
        )

    def _event(self, tenant, day, *, request=None):
        when = timezone.make_aware(datetime.combine(day, time(15)))
        event = self.create_event(
            name=f"{tenant.name} {day}", tenant=tenant, date=when, start_time=when,
            end_time=when,
        )
        event_models.Event.objects.filter(id=event.id).update(request=request or self.request)
        event.refresh_from_db()
        return event

    def _recap(self, event, *, approved=True, **feedback):
        recap = recap_models.Recap.objects.create(
            name=f"recap {event.name}",
            event=event,
            approved=approved,
            submited_at=timezone.now(),
            ambassador=self.ba,
            created_by=self.system_user,
            updated_by=self.system_user,
        )
        recap_models.ConsumerFeedback.objects.create(recap=recap, **feedback)
        return recap

    def _custom_competitor(self, event, value):
        event_type = event_models.EventType.objects.create(
            name="Sampling", slug="mention-sampling", tenant=self.tenant,
            created_by=self.system_user,
        )
        template = recap_models.CustomRecapTemplate.objects.create(
            name="Mention Sampling", event_type=event_type, tenant=self.tenant,
            created_by=self.system_user,
        )
        text = recap_models.CustomRecapFieldType.objects.create(
            name="Text", created_by=self.system_user
        )
        section = recap_models.RecapSection.objects.create(
            name="Consumers", tenant=self.tenant, created_by=self.system_user
        )
        custom_field = recap_models.CustomField.objects.create(
            name="Ask consumers what competitor products they drink. And if so, which one.",
            custom_recap_template=template,
            custom_field_type=text,
            recap_section=section,
            created_by=self.system_user,
        )
        recap = recap_models.CustomRecap.objects.create(
            name="custom competitor",
            approved=True,
            submitted_at=event.date,
            event=event,
            tenant=self.tenant,
            custom_recap_template=template,
            ambassador=self.ba,
            created_by=self.system_user,
        )
        recap_models.CustomFieldValue.objects.create(
            custom_recap=recap, custom_field=custom_field, value=value,
            created_by=self.system_user,
        )
        return recap

    # ── calls ─────────────────────────────────────────────────────────

    def _tag(self, *extra) -> str:
        out = StringIO()
        call_command("tag_consumer_mentions", "--tenant", str(self.tenant.id), *extra,
                     stdout=out, stderr=out)
        return out.getvalue()

    async def _trends(self, user, tenant=None, end="2026-10-31"):
        result = await self._execute_query_authenticated(
            TRENDS,
            {"tenantId": str((tenant or self.tenant).id), "endDate": end, "months": 6},
            user,
            self.endpoint_path,
        )
        assert result.errors is None, result.errors
        return result.data["tenantMentionTrends"]

    def _tags(self):
        from consumer_mentions.models import RecapMentionTag

        return {
            row.recap_key: row for row in RecapMentionTag.objects.all()
        }

    # ── tagging ───────────────────────────────────────────────────────

    def test_dry_run_calls_nothing_and_stores_nothing(self):
        from consumer_mentions.models import MentionThemeVocabulary

        self.post.side_effect = _fake_model
        log = self._tag()
        assert "DRY RUN" in log and "would tag 15" in log
        assert self.post.call_count == 0
        assert self._tags() == {}
        assert not MentionThemeVocabulary.objects.exists()

    def test_prompt_is_approved_only_this_brand_and_no_ba_name(self):
        self.post.side_effect = _fake_model
        self._tag("--apply")
        prompts = "\n".join(_tag_calls(self.post))
        assert "Too sweet for me" in prompts
        for hidden in ("PENDING", "ARCHIVED", "DELETED", "BETA SECRET", "Jordan Rivera"):
            assert hidden not in prompts
        assert "[competitor] Celsius, Monster" in prompts
        # The brand's own name is never another brand, and is said so.
        system = self.post.call_args_list[0].args[1]["messages"][0]["content"]
        assert "FAMUS Energy" in system and "too_sweet: Too sweet" in system
        tags = self._tags()
        expected = {f"L{r.id}" for r in self.recaps} | {
            f"L{self.ba_recap.id}", f"C{self.competitor_recap.id}"
        }
        assert set(tags) == expected
        competitor = tags[f"C{self.competitor_recap.id}"]
        assert competitor.themes == []
        assert {b["name"] for b in competitor.brands} == {"Celsius", "Monster Energy"}
        for row in tags.values():
            assert all(b["name"] not in ("Rockstar", "FAMUS") for b in row.brands)
            assert all(t["theme"] != "made_up_key" for t in row.themes)

    def test_each_recap_is_read_once_and_again_only_when_it_changes(self):
        self.post.side_effect = _fake_model
        self._tag("--apply")
        first = self.post.call_count
        assert first >= 1
        self._tag("--apply")
        assert self.post.call_count == first  # nothing new: no call
        changed = self.recaps[3]
        recap_models.ConsumerFeedback.objects.filter(recap=changed).update(
            quotes="Nice can, but pricey"
        )
        self._tag("--apply")
        assert self.post.call_count == first + 1
        last = _tag_calls(self.post)[-1]
        assert "pricey" in last and "Too sweet for me" not in last
        assert self._tags()[f"L{changed.id}"].themes[0]["theme"] == "price_too_high"

    def test_the_call_cap_holds_and_the_rest_waits(self):
        self.post.side_effect = _fake_model
        with patch("consumer_mentions.tagging.BATCH_MAX_COMMENTS", 2):
            log = self._tag("--apply", "--max-calls", "2")
        assert self.post.call_count == 2
        assert "reached 2 calls" in log
        assert 0 < len(self._tags()) < 15

    def test_a_model_failure_stores_nothing_for_that_batch(self):
        self.post.side_effect = lambda *a, **k: _reply("not json")
        self._tag("--apply")
        assert self._tags() == {}

    def test_other_labels_on_enough_recaps_become_themes(self):
        from consumer_mentions.vocabulary import load_themes

        self.post.side_effect = _fake_model
        log = self._tag("--apply")
        labels = {t["label"]: t for t in load_themes(self.tenant.id)}
        # "caffeine" is on three recaps, "smooth" on two: only one is promoted.
        assert labels["Wants more caffeine"]["source"] == "promoted"
        assert labels["Wants more caffeine"]["polarity"] == "request"
        assert "Smooth, no crash" not in labels
        assert "new themes: Wants more caffeine" in log

    # ── the card ──────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_nothing_tagged_yet_says_so(self):
        data = await self._trends(self.client_user)
        assert data["status"] == "not_tagged"
        assert data["themes"] == [] and data["brands"] == []

    @pytest.mark.asyncio
    async def test_counts_are_recaps_by_month_and_trends_need_enough(self):
        self.post.side_effect = _fake_model
        await sync_to_async(self._tag)("--apply")
        data = await self._trends(self.client_user)
        assert data["status"] == "ready"
        assert data["months"][-2:] == ["2026-09", "2026-10"]
        # October: 7 recaps + the BA-name recap + the competitor recap.
        assert data["recapCounts"][-2:] == [6, 9]
        assert (data["compareMonth"], data["priorMonth"]) == ("2026-10", "2026-09")

        themes = {t["label"]: t for t in data["themes"]}
        sweet = themes["Too sweet"]
        assert sweet["counts"][-2:] == [1, 5]
        assert (sweet["current"], sweet["previous"]) == (5, 1)
        assert sweet["direction"] == DIRECTION_RISING
        assert sweet["polarity"] == "complaint"
        assert "sweet" in sweet["quote"].lower() and "Jordan" not in sweet["quote"]
        caffeine = themes["Wants more caffeine"]
        assert caffeine["counts"][-2:] == [1, 2]
        assert caffeine["direction"] == DIRECTION_STEADY  # 1 → 2 is noise

        brands = {b["label"]: b for b in data["brands"]}
        assert "FAMUS" not in brands and "Rockstar" not in brands
        celsius = brands["Celsius"]
        # Two October comments plus the competitor field: three recaps.
        assert celsius["counts"][-2:] == [1, 3]
        assert celsius["direction"] == DIRECTION_RISING
        assert "Red Bull" not in brands  # one recap is not shown
        assert brands["Monster Energy"]["total"] == 2

        assert data["headlines"]
        facts = {"counts": data["recapCounts"], "s": [[t["current"], t["previous"]] for t in data["themes"] + data["brands"]]}
        for line in data["headlines"]:
            assert numbers_are_grounded(line, facts), line
        assert any("Too sweet" in line and "5 of 9" in line for line in data["headlines"])

    @pytest.mark.asyncio
    async def test_brand_user_is_held_to_their_own_tenant_staff_may_choose(self):
        self.post.side_effect = _fake_model
        await sync_to_async(self._tag)("--apply")
        await sync_to_async(call_command)(
            "tag_consumer_mentions", "--tenant", str(self.other.id), "--apply",
            stdout=StringIO(),
        )
        mine = await self._trends(self.client_user, tenant=self.other)
        assert mine["recapCounts"][-1] == 9  # their own, not Beta's
        staff_beta = await self._trends(self.admin, tenant=self.other)
        assert staff_beta["recapCounts"][-1] == 1
        assert staff_beta["status"] == "too_few"
        staff_famus = await self._trends(self.admin)
        assert staff_famus["recapCounts"][-1] == 9

    @pytest.mark.asyncio
    async def test_a_recap_unapproved_after_tagging_drops_out(self):
        self.post.side_effect = _fake_model
        await sync_to_async(self._tag)("--apply")
        target = self.recaps[-7]  # "Way too sweet"
        await sync_to_async(
            lambda: recap_models.Recap.objects.filter(id=target.id).update(approved=False)
        )()
        data = await self._trends(self.client_user)
        assert data["recapCounts"][-1] == 8
        sweet = next(t for t in data["themes"] if t["label"] == "Too sweet")
        assert sweet["current"] == 4


# ─── pure ───────────────────────────────────────────────────────────


def test_seed_takes_the_brands_own_themes_then_fills_with_defaults():
    rows = [
        {"themes": [
            {"section": "liked", "label": "Loved the smooth taste"},
            {"section": "passed", "label": "Too sweet for them"},
            {"section": "asked", "label": "Wants more caffeine"},
            {"section": "nonsense", "label": "Dropped"},
        ]},
        {"themes": [{"section": "liked", "label": "Loved the smooth taste"}]},
    ]
    themes = seed_themes(rows)
    labels = [t["label"] for t in themes]
    assert labels[:3] == ["Loved the smooth taste", "Too sweet for them", "Wants more caffeine"]
    assert [t["polarity"] for t in themes[:3]] == ["praise", "complaint", "request"]
    assert "Too sweet" not in labels  # the default says the same thing
    assert "Price too high" in labels and "Dropped" not in labels
    assert len({t["key"] for t in themes}) == len(themes)


def test_promotion_needs_enough_recaps_and_old_tags_then_count():
    themes = seed_themes([])
    tags = [[{"theme": OTHER, "label": "Wants more caffeine", "polarity": "request"}]] * (
        PROMOTE_MIN_RECAPS - 1
    )
    assert promotions(themes, tags) == []
    tags = tags + [[
        {"theme": OTHER, "label": "wants more caffeine", "polarity": "request"},
        {"theme": OTHER, "label": "Wants more caffeine", "polarity": "request"},
    ]]
    added = promotions(themes, tags)
    assert [a["label"] for a in added] == ["Wants more caffeine"]
    grown = themes + added
    assert resolve({"theme": OTHER, "label": "wants more caffeine"}, grown)["key"] == added[0]["key"]
    assert resolve({"theme": OTHER, "label": "Something else"}, grown) is None


def test_read_tags_checks_everything_the_model_says():
    themes = seed_themes([])
    numbered = [
        Comment(recap="L1", kind=KIND_GENERAL, text="Too sweet, I prefer RedBull"),
        Comment(recap="L1", kind=KIND_GENERAL, text="also too sweet"),
        Comment(recap="C2", kind="competitor", text="Celsius"),
    ]
    raw = {"comments": [
        {"comment": 1, "themes": ["too_sweet", "nope"], "other": [{"label": "Too sweet", "polarity": "complaint"}],
         "brands": [{"as_written": "RedBull", "name": "Red Bull"}, {"as_written": "Monster", "name": "Monster"},
                    {"as_written": "Acme", "name": "Acme"}]},
        {"comment": 2, "themes": ["too_sweet"], "other": [], "brands": []},
        {"comment": 3, "themes": ["great_taste"], "other": [{"label": "Likes it a lot", "polarity": "praise"}],
         "brands": [{"as_written": "Celsius", "name": "Celsius Holdings Inc"}]},
        {"comment": 7, "themes": ["too_sweet"], "other": [], "brands": []},
        "junk",
    ]}
    out = read_tags(raw, numbered, themes, "Acme Energy", ["Acme Original"])
    assert out["L1"]["themes"] == [{"theme": "too_sweet", "quote": "Too sweet, I prefer RedBull"}]
    assert out["L1"]["brands"] == [{"key": "redbull", "name": "Red Bull"}]
    assert out["C2"] == {"themes": [], "brands": [{"key": "celsius", "name": "Celsius"}]}


def _series(data, label, group="themes"):
    return next(s for s in data[group] if s["label"] == label)


def test_summarize_shares_floors_and_falling():
    themes = seed_themes([])
    months = month_keys(date(2026, 10, 15), 3)
    recaps, tags = {}, {}
    n = 0

    def add(day, theme=None, brand=None):
        nonlocal n
        n += 1
        recaps[f"L{n}"] = day
        tags[f"L{n}"] = {
            "themes": [{"theme": theme, "quote": f"q{n}"}] if theme else [],
            "brands": [{"key": brand.lower(), "name": brand}] if brand else [],
        }

    for i in range(10):
        add(date(2026, 9, i + 1), "too_sweet" if i < 6 else None, "Celsius" if i < 2 else None)
    for i in range(10):
        add(date(2026, 10, i + 1), "too_sweet" if i < 2 else None, "Celsius" if i < 3 else None)
    add(date(2026, 8, 1), "price_too_high")
    recaps["L999"] = date(2026, 10, 3)  # visible but never tagged: not counted
    data = summarize(recaps, tags, themes, months, brand="Acme")
    assert data["recap_counts"] == [1, 10, 10]
    sweet = _series(data, "Too sweet")
    assert sweet["direction"] == DIRECTION_FALLING
    assert (sweet["current_share"], sweet["previous_share"]) == (20, 60)
    assert _series(data, "Celsius", "brands")["direction"] == DIRECTION_STEADY  # 2 → 3
    assert all(s["label"] != "Price too high" for s in data["themes"])  # one mention
    assert len(data["headlines"]) == 1 and "down from 6 of 10 in September" in data["headlines"][0]


def test_a_thin_latest_month_compares_the_two_before_it_or_nothing():
    themes = seed_themes([])
    months = month_keys(date(2026, 11, 3), 3)
    recaps = {f"L{i}": date(2026, 9 + (i % 2), 5) for i in range(16)}
    recaps["L100"] = date(2026, 11, 2)
    tags = {k: {"themes": [], "brands": []} for k in recaps}
    data = summarize(recaps, tags, themes, months)
    assert (data["compare_month"], data["prior_month"]) == ("2026-10", "2026-09")
    few = {k: v for k, v in list(recaps.items())[:6]}
    data = summarize(few, tags, themes, months)
    assert data["compare_month"] is None
    assert all(s["direction"] == DIRECTION_TOO_FEW for s in data["themes"])


def test_source_hash_changes_with_the_comments_only():
    a = [Comment(recap="L1", kind=KIND_GENERAL, text="too sweet")]
    b = [Comment(recap="L9", kind=KIND_GENERAL, text="too sweet")]
    assert source_hash(a) == source_hash(b)
    assert source_hash(a) != source_hash([Comment(recap="L1", kind=KIND_GENERAL, text="too sweet!")])


@pytest.mark.django_db(transaction=True)
def test_cron_needs_the_secret_and_is_a_dry_run_unless_told(client, settings):
    settings.INTERNAL_CRON_SECRET = "s3cret"
    url = "/internal/cron/tag-consumer-mentions"
    assert client.post(url).status_code == 401
    with patch("utils.ai_text._post_json") as post:
        body = client.post(url, HTTP_X_CRON_SECRET="s3cret").json()
        assert body["ok"] and body["dry_run"] is True and "DRY RUN" in body["log"]
        body = client.post(f"{url}?dry_run=false&max_calls=1", HTTP_X_CRON_SECRET="s3cret").json()
        assert body["ok"] and body["dry_run"] is False
    assert post.call_count == 0  # no brand has recaps here
