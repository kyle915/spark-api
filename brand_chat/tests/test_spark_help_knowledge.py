"""Unit tests for Spark Help Knowledge Base, OpenAI embeddings, Chat AI assist,
and strict Client vs. Admin access boundaries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from brand_chat.help_knowledge import (
    BUILTIN_HELP_DOCS,
    RESTRICTED_INTERNAL_REFUSAL,
    answer_spark_help_question,
    is_ignite_admin_user,
    is_restricted_internal_topic,
    normalize_gap_key,
    scrub_gap_question,
    search_help_knowledge,
    upsert_help_article,
)
from brand_chat.models import SparkHelpArticle, SparkHelpGap
from brand_chat.schema import (
    BrandChatMutations,
    BrandChatQueries,
    ResolveSparkHelpGapInput,
    UpsertSparkHelpArticleInput,
)
from chats.mutations import ChatMutations
from utils.ai_text import cosine_similarity, embed_texts


def _fake_info(user):
    return SimpleNamespace(context=SimpleNamespace(request=SimpleNamespace(user=user)))


def _client_user():
    return SimpleNamespace(
        pk=None,
        is_authenticated=True,
        is_active=True,
        is_staff=False,
        is_superuser=False,
        email="founder@brand.example",
        role=SimpleNamespace(slug="client"),
    )


def _admin_user():
    return SimpleNamespace(
        pk=None,
        is_authenticated=True,
        is_active=True,
        is_staff=True,
        is_superuser=False,
        email="ops@igniteproductions.co",
        role=SimpleNamespace(slug="spark-admin"),
    )


def test_audience_isolation_and_route_aware_boost():
    # Admin searching admin-only Spark UI screens (Sandbox / Missing Recaps) gets admin docs.
    admin_hits, _ = search_help_knowledge(
        "How do I preview the admin sample brand sandbox?",
        is_admin=True,
        use_embeddings=False,
    )
    assert admin_hits
    assert any(h.audience == "admin" for h in admin_hits)
    assert any("/sandbox" in h.route_path for h in admin_hits)

    # Brand client searching the exact same admin-only UI screen NEVER gets admin docs!
    client_hits, _ = search_help_knowledge(
        "How do I preview the admin sample brand sandbox?",
        is_admin=False,
        use_embeddings=False,
    )
    assert all(h.audience != "admin" for h in client_hits)

    # Route-aware boost: asking "how does this page work?" on /demo-costs ranks Demo Costs #1.
    route_hits, _ = search_help_knowledge(
        "How does this page work?",
        is_admin=False,
        current_path="/demo-costs",
        use_embeddings=False,
    )
    assert route_hits
    assert route_hits[0].route_path == "/demo-costs"


def test_blocks_internal_ignite_financials_and_processes_for_everyone():
    restricted_prompts = (
        "How do Wingspan payments and BA payroll hours work?",
        "What is Ignite's profit margin and P&L breakdown?",
        "What is Ignite's internal billing markup and fee structure?",
        "How do Cloud Scheduler X-Cron-Secret jobs run internally?",
        "Walk me through Ignite's internal staffing process and hiring policy.",
        "What are BA hourly pay rates and contractor reimbursement policies?",
    )
    for prompt in restricted_prompts:
        assert is_restricted_internal_topic(prompt) is True
        for is_adm in (False, True):
            hits, mode = search_help_knowledge(prompt, is_admin=is_adm, use_embeddings=False)
            assert hits == []
            assert mode == "blocked"

            res = answer_spark_help_question(prompt, is_admin=is_adm, log_gaps=True)
            assert res["ok"] is True
            assert res["answer"] == RESTRICTED_INTERNAL_REFUSAL
            assert res["matched_via"] == "policy"
            assert res["sources"] == []
            assert res["gap_logged"] is False

    # Confirm normal brand workspace product questions are NOT falsely blocked!
    for safe_prompt in (
        "How do I use the Demo Costs page?",
        "How does Budget Pacing calculate projected spend?",
        "How do I upload Retailer POS CSVs for velocity lift?",
        "How do I export a Buyer QBR PowerPoint deck?",
        "How do I configure outbound webhooks for Zapier?",
    ):
        assert is_restricted_internal_topic(safe_prompt) is False

    # Confirm no built-in doc leaks internal Ignite financials/processes
    for doc in BUILTIN_HELP_DOCS:
        assert not is_restricted_internal_topic(f"{doc['title']} {doc['body']}"), (
            f"Built-in doc trips internal policy filter: {doc['id']}"
        )

    # Confirm even admins cannot save an article about internal Ignite financials/processes
    with pytest.raises(ValueError, match="internal Ignite financials"):
        upsert_help_article(
            user=_admin_user(),
            title="Ignite P&L and BA Payroll Rates",
            body="Wingspan payout schedule and Ignite profit margin details.",
            audience="admin",
        )


def test_openai_embeddings_and_hybrid_semantic_retrieval(settings):
    settings.OPENAI_API_KEY = "test-key-for-embeddings"
    settings.OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"

    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def _fake_post(url, payload, headers, *, timeout):
        inputs = payload["input"]
        data = []
        for idx, text in enumerate(inputs):
            low = text.lower()
            if "demo costs" in low or "unit economics" in low or "spend per sample" in low:
                vec = [0.98, 0.02, 0.0]
            elif "webhook" in low or "zapier" in low:
                vec = [0.02, 0.98, 0.0]
            else:
                vec = [0.0, 0.0, 1.0]
            data.append({"index": idx, "embedding": vec})
        return {"model": "text-embedding-3-small", "data": data}

    with patch("utils.ai_text._post_json", side_effect=_fake_post):
        vecs, model = embed_texts(["spend per sample", "zapier catch hook"])
        assert model == "text-embedding-3-small"
        assert len(vecs) == 2
        assert cosine_similarity(vecs[0], [0.98, 0.02, 0.0]) > 0.99

        # Paraphrased question ("spend per sample") with zero exact title words still matches Demo Costs via hybrid!
        hits, mode = search_help_knowledge(
            "how do I track spend per sample",
            is_admin=False,
            use_embeddings=True,
        )
        assert mode == "hybrid"
        assert hits
        assert hits[0].route_path == "/demo-costs"
        assert all(h.audience != "admin" for h in hits)


def test_brand_clients_cannot_view_or_add_knowledge_or_use_chat_ai():
    client = _client_user()
    admin = _admin_user()
    assert is_ignite_admin_user(client) is False
    assert is_ignite_admin_user(admin) is True

    info = _fake_info(client)
    queries = BrandChatQueries()
    mutations = BrandChatMutations()

    # Client cannot read Knowledge Base / Gaps admin panel
    kb_res = asyncio.run(queries.spark_help_knowledge(info))
    assert kb_res.ok is False
    assert kb_res.is_admin is False
    assert kb_res.articles == []
    assert kb_res.gaps == []

    # Client cannot add or edit knowledge
    upsert_res = asyncio.run(
        mutations.upsert_spark_help_article(
            info,
            UpsertSparkHelpArticleInput(
                title="Client injection attempt",
                body="Should never be saved.",
            ),
        )
    )
    assert upsert_res.ok is False
    assert "admin" in (upsert_res.reason or "").lower()

    # Client cannot delete knowledge, resolve gaps, dismiss gaps, or sync embeddings
    del_res = asyncio.run(mutations.delete_spark_help_article(info, "00000000-0000-0000-0000-000000000001"))
    assert del_res.ok is False

    res_gap = asyncio.run(
        mutations.resolve_spark_help_gap(
            info,
            ResolveSparkHelpGapInput(
                gap_uuid="00000000-0000-0000-0000-000000000001",
                title="Nope",
                body="Nope",
            ),
        )
    )
    assert res_gap.ok is False

    dis_gap = asyncio.run(mutations.dismiss_spark_help_gap(info, "00000000-0000-0000-0000-000000000001"))
    assert dis_gap.ok is False

    sync_res = asyncio.run(mutations.sync_spark_help_embeddings(info))
    assert sync_res.ok is False

    # Client cannot call admin Chat AI draft or Teach Spark distill mutations
    chat_muts = ChatMutations()
    with patch(
        "chats.services.resolve_caller_context",
        return_value=(client, None, False, False, client.email),
    ):
        draft_res = asyncio.run(
            chat_muts.draft_chat_reply_with_ai(info, "00000000-0000-0000-0000-000000000001")
        )
        assert draft_res.ok is False
        assert "admin" in (draft_res.reason or "").lower()

        distill_res = asyncio.run(
            chat_muts.distill_chat_to_help_knowledge(
                info,
                "00000000-0000-0000-0000-000000000001",
                "00000000-0000-0000-0000-000000000002",
            )
        )
        assert distill_res.ok is False
        assert "admin" in (distill_res.reason or "").lower()


def test_gap_scrubbing_models_sql_and_graphql_schema_builds():
    scrubbed = scrub_gap_question("Email bob@acme.com or call 513-555-0199 about POS")
    assert "bob@acme.com" not in scrubbed
    assert "513-555-0199" not in scrubbed
    assert "[email]" in scrubbed and "[phone]" in scrubbed
    assert normalize_gap_key("How do demo costs work?") == "demo cost"

    assert len(BUILTIN_HELP_DOCS) >= 15
    assert any(d["audience"] == "admin" for d in BUILTIN_HELP_DOCS)
    assert any(d["audience"] == "all" for d in BUILTIN_HELP_DOCS)

    # Verify ORM query compilation for both new models
    for qs in (
        SparkHelpArticle.objects.filter(is_active=True, audience__in=("all", "client")),
        SparkHelpGap.objects.filter(status="open", caller_role="client").select_related("resolved_article"),
    ):
        sql, _params = qs.query.sql_with_params()
        assert "SELECT" in sql.upper()

    # Verify Strawberry client schema compiles cleanly with all new fields/mutations
    from config.schema_client import schema_clients

    sdl = schema_clients.as_str()
    for field_name in (
        "sparkHelpKnowledge",
        "askSparkHelp",
        "reportBrandChatUnhelpful",
        "upsertSparkHelpArticle",
        "deleteSparkHelpArticle",
        "resolveSparkHelpGap",
        "dismissSparkHelpGap",
        "syncSparkHelpEmbeddings",
        "draftChatReplyWithAi",
        "distillChatToHelpKnowledge",
    ):
        assert field_name in sdl, f"Missing {field_name} in schema_client SDL"
