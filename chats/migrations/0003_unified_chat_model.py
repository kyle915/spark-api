"""Replace the unused JobChatRoom / ChatroomAmbassadorMessage /
ChatroomCompanieMessage scaffolding with the unified ChatThread +
ChatMessage model.

The three old models had migrations + tables but never had a GraphQL
surface and no application code references them — verified with a
repo-wide grep. Dropping the tables in this migration is therefore
safe (zero rows in production).

After this migration:
  - chats_chatthread holds both 'general' BA↔admin DMs and 'job'
    per-shift threads, distinguished by `kind`.
  - chats_chatmessage stores the messages, with two-sided unread
    tracking (read_by_admin_at, read_by_ambassador_at).
"""
import django.db.models.deletion
import uuid6
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("chats", "0002_initial"),
        ("ambassadors", "0001_initial"),
        ("jobs", "0003_alter_company_about_us_alter_company_address_and_more"),
        ("tenants", "0002_alter_role_created_by_alter_role_updated_by_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Drop the old scaffolding. All three tables exist in dev but
        # carry zero rows in production (no resolver wrote to them).
        migrations.DeleteModel(name="ChatroomAmbassadorMessage"),
        migrations.DeleteModel(name="ChatroomCompanieMessage"),
        migrations.DeleteModel(name="JobChatRoom"),
        # Create ChatThread.
        migrations.CreateModel(
            name="ChatThread",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "uuid",
                    models.UUIDField(
                        default=uuid6.uuid7, editable=False, unique=True
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("general", "General BA ↔ admin DM"),
                            ("job", "Per-job thread"),
                        ],
                        default="general",
                        max_length=16,
                    ),
                ),
                (
                    "last_message_at",
                    models.DateTimeField(blank=True, db_index=True, null=True),
                ),
                (
                    "last_message_preview",
                    models.CharField(blank=True, max_length=255, null=True),
                ),
                (
                    "last_message_sender_is_ambassador",
                    models.BooleanField(default=False),
                ),
                ("archived_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "ambassador",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name="chat_threads",
                        to="ambassadors.ambassador",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name="chat_threads_created_by",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "job",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name="chat_threads",
                        to="jobs.job",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name="chat_threads",
                        to="tenants.tenant",
                    ),
                ),
            ],
            options={
                "ordering": ("-last_message_at", "-created_at"),
            },
        ),
        # Create ChatMessage.
        migrations.CreateModel(
            name="ChatMessage",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "uuid",
                    models.UUIDField(
                        default=uuid6.uuid7, editable=False, unique=True
                    ),
                ),
                ("body", models.TextField()),
                ("sender_is_ambassador", models.BooleanField()),
                ("read_by_admin_at", models.DateTimeField(blank=True, null=True)),
                (
                    "read_by_ambassador_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True, db_index=True
                    ),
                ),
                (
                    "sender",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name="chat_messages_sent",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "thread",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name="messages",
                        to="chats.chatthread",
                    ),
                ),
            ],
            options={
                "ordering": ("created_at",),
            },
        ),
        # Unique constraints on ChatThread — both partial so the two
        # "kinds" can coexist.
        migrations.AddConstraint(
            model_name="chatthread",
            constraint=models.UniqueConstraint(
                condition=models.Q(("kind", "general")),
                fields=("tenant", "ambassador"),
                name="chats_thread_one_general_per_pair",
            ),
        ),
        migrations.AddConstraint(
            model_name="chatthread",
            constraint=models.UniqueConstraint(
                condition=models.Q(("kind", "job")),
                fields=("tenant", "ambassador", "job"),
                name="chats_thread_one_job_per_triple",
            ),
        ),
        # Indexes on ChatMessage — powers paginated thread loads + the
        # unread-count badge.
        migrations.AddIndex(
            model_name="chatmessage",
            index=models.Index(
                fields=["thread", "created_at"],
                name="chats_chatm_thread__af1c2c_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="chatmessage",
            index=models.Index(
                fields=["thread", "sender_is_ambassador"],
                name="chats_chatm_thread__7b1f4d_idx",
            ),
        ),
    ]
