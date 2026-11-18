"""What consumers mentioned, tagged once per recap.

Two tables:

* :class:`MentionThemeVocabulary` is a brand's list of themes. It is the
  fixed set every recap is tagged against, so October and November are
  counted on the same terms. Themes are only ever added (a label people keep
  writing under "other" is promoted); a key is never renamed or removed, so a
  month tagged last spring still lines up with this month.
* :class:`RecapMentionTag` is one approved recap's tags: which themes its
  consumer comments touch, which other brands they name, and one comment as
  written per theme. ``source_hash`` is a digest of the comments the tags
  were read from, so a recap is read again only when its comments change.

Nothing here decides what a brand may see. The card reads the recaps a brand
may be shown (approved, not archived, live request, event date) at read time
and only then looks up their tags.
"""

from django.db import models

from tenants.models import Tenant


class MentionThemeVocabulary(models.Model):
    """A brand's themes: ``[{key, label, polarity, source}]``.

    ``polarity`` is ``praise``, ``complaint`` or ``request``. ``source`` is
    where the theme came from: ``default``, ``feedback`` (the brand's
    "What consumers said" themes) or ``promoted`` (written under "other" on
    enough recaps).
    """

    id = models.BigAutoField(primary_key=True)
    tenant = models.OneToOneField(
        Tenant, on_delete=models.CASCADE, related_name="mention_vocabulary"
    )
    themes = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{len(self.themes or [])} themes for tenant {self.tenant_id}"


class RecapMentionTag(models.Model):
    """One recap's tags. ``recap_key`` is ``L<id>`` for a legacy recap and
    ``C<id>`` for a custom one, like :class:`recaps.feedback_themes.Comment`.

    ``themes`` is ``[{theme, label, polarity, quote}]``: ``theme`` is a key
    of the brand's vocabulary, or ``other`` with the model's own short
    ``label``. ``brands`` is ``[{key, name}]``, other brands written in the
    comments, never the brand itself.
    """

    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="recap_mention_tags"
    )
    recap_key = models.CharField(max_length=24)
    source_hash = models.CharField(max_length=64)
    themes = models.JSONField(default=list)
    brands = models.JSONField(default=list)
    tagged_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "recap_key"],
                name="consumer_mentions_tag_tenant_recap_uniq",
            )
        ]

    def __str__(self) -> str:
        return f"{self.recap_key} for tenant {self.tenant_id}"
