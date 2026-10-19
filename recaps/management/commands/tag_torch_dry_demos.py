"""Torch THC: tag reviewed historical recaps as dry demos.

For each ``--ids`` CustomRecap whose OWN notes say no product was tasted,
writes ``Dry demo? = Yes`` and copies the current consumers-sampled count
into ``People engaged``. Consumers sampled and every other value is left
untouched; conversion then pairs the recap's purchases with People engaged.

Guards, per recap:
  * must be a Torch recap on the template that has both new fields
    (run ``add_torch_dry_demo_fields --apply`` first);
  * its notes must match a no-tasting phrase — the matched text is logged
    as the evidence; no match, no write;
  * an existing "Dry demo?" answer is never overwritten;
  * People engaged is only written when consumers sampled is > 0 (a 0 or
    blank count is not evidence of how many people the BA talked with);
  * ids in PROTECTED_IDS are never written.

DRY-RUN by default; ``--apply`` writes. Logs before/after per recap.

    python manage.py tag_torch_dry_demos --ids 1152,1156
    python manage.py tag_torch_dry_demos --ids 1152,1156 --apply
"""

from __future__ import annotations

import re

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from recaps.management.commands.add_torch_dry_demo_fields import (
    DEFAULT_TENANT,
    DRY_DEMO,
    PEOPLE_ENGAGED,
    Command as AddFieldsCommand,
)

# Kyle: #1577 stays as ops edited it.
PROTECTED_IDS = frozenset({1577})

_NO_TASTING_RE = re.compile(
    r"\bdry[- ]?(demo|sampl\w*|tasting|educational)"
    r"|didn.?t have (any )?samples|did not have (any )?samples"
    r"|no samples were available|no product to sample"
    r"|could not sample anything|no items to actually physically sample"
    r"|did not receive my samples|wasn.?t able to do sampling"
    r"|couldn.?t sample or even smell",
    re.IGNORECASE,
)


def _parse_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for part in re.split(r"[,\s]+", raw or ""):
        if not part:
            continue
        if not part.isdigit():
            raise CommandError(f"--ids: {part!r} is not a recap id.")
        ids.append(int(part))
    if not ids:
        raise CommandError("--ids is required (comma-separated CustomRecap ids).")
    return list(dict.fromkeys(ids))


def _snippet(text: str, match: re.Match, pad: int = 90) -> str:
    start = max(0, match.start() - pad)
    end = min(len(text), match.end() + pad)
    out = text[start:end].replace("\n", " ").strip()
    return ("…" if start else "") + out + ("…" if end < len(text) else "")


class Command(BaseCommand):
    help = (
        "Torch THC: tag reviewed recaps as dry demos (Dry demo? = Yes, People "
        "engaged = consumers sampled). Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--ids", required=True, help="Comma-separated CustomRecap ids.")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument(
            "--tenant",
            default=DEFAULT_TENANT,
            help="Tenant slug (exact), else request_url_name.",
        )

    def handle(self, *args, **opts):
        from recaps.models import CustomField, CustomFieldValue, CustomRecap
        from recaps.types import _consumers_sampled_from_fields

        apply = bool(opts["apply"])
        ids = _parse_ids(opts["ids"])
        tenant = AddFieldsCommand._resolve_tenant((opts["tenant"] or DEFAULT_TENANT).strip())

        self.stdout.write("=" * 68)
        self.stdout.write(f"Tenant : [{tenant.id}] {tenant.name!r} slug={tenant.slug!r}")
        self.stdout.write(f"Mode   : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}")
        self.stdout.write(f"Ids    : {len(ids)} requested")
        self.stdout.write("=" * 68)

        recaps = {
            r.id: r
            for r in CustomRecap.objects.filter(id__in=ids)
            .select_related("custom_recap_template")
            .prefetch_related("custom_field_value__custom_field")
        }

        fields_by_template: dict[int, tuple] = {}
        tagged: list[int] = []
        skipped: list[tuple[int, str]] = []

        for rid in ids:
            if rid in PROTECTED_IDS:
                skipped.append((rid, "protected"))
                self.stdout.write(f"#{rid}: SKIP — protected, left as is")
                continue
            recap = recaps.get(rid)
            if recap is None:
                skipped.append((rid, "not found"))
                self.stdout.write(f"#{rid}: SKIP — not found")
                continue
            if recap.tenant_id != tenant.id:
                skipped.append((rid, f"tenant {recap.tenant_id}"))
                self.stdout.write(f"#{rid}: SKIP — belongs to tenant {recap.tenant_id}")
                continue

            tpl_id = recap.custom_recap_template_id
            if tpl_id not in fields_by_template:
                fields_by_template[tpl_id] = (
                    CustomField.objects.filter(
                        custom_recap_template_id=tpl_id, name=DRY_DEMO["name"]
                    ).first(),
                    CustomField.objects.filter(
                        custom_recap_template_id=tpl_id, name=PEOPLE_ENGAGED["name"]
                    ).first(),
                )
            dry_field, people_field = fields_by_template[tpl_id]
            if dry_field is None or people_field is None:
                skipped.append((rid, "template missing fields"))
                self.stdout.write(
                    f"#{rid}: SKIP — template [{tpl_id}] lacks the dry-demo fields "
                    "(run add_torch_dry_demo_fields --apply)"
                )
                continue

            values = list(recap.custom_field_value.all())
            pairs = [(v.custom_field.name, v.value) for v in values]
            sampled = _consumers_sampled_from_fields(pairs)
            dry_existing = next((v for v in values if v.custom_field_id == dry_field.id), None)
            people_existing = next(
                (v for v in values if v.custom_field_id == people_field.id), None
            )
            notes = " | ".join(
                f"{v.custom_field.name}: {v.value}"
                for v in values
                if v.custom_field_id not in (dry_field.id, people_field.id) and (v.value or "").strip()
            )
            match = _NO_TASTING_RE.search(notes)

            before = (
                f"dry_demo={dry_existing.value if dry_existing else '(blank)'!r} "
                f"people_engaged={people_existing.value if people_existing else '(blank)'!r} "
                f"consumers_sampled={sampled!r}"
            )
            if dry_existing is not None:
                skipped.append((rid, f"already answered {dry_existing.value!r}"))
                self.stdout.write(f"#{rid}: SKIP — Dry demo? already answered. {before}")
                continue
            if match is None:
                skipped.append((rid, "no notes evidence"))
                self.stdout.write(f"#{rid}: SKIP — no no-tasting phrase in its notes. {before}")
                continue

            write_people = (
                people_existing is None and sampled is not None and sampled > 0
            )
            after_people = (
                str(sampled)
                if write_people
                else (people_existing.value if people_existing else "(blank)")
            )
            after = (
                f"dry_demo='Yes' people_engaged={after_people!r} "
                f"consumers_sampled={sampled!r}"
            )
            self.stdout.write(f"#{rid}: {'TAG' if apply else 'WOULD TAG'}")
            self.stdout.write(f"    evidence: {_snippet(notes, match)}")
            self.stdout.write(f"    before  : {before}")
            self.stdout.write(f"    after   : {after}")
            if not write_people and people_existing is None:
                self.stdout.write(
                    "    note    : People engaged left blank (consumers sampled is 0/blank)"
                )

            if apply:
                with transaction.atomic():
                    CustomFieldValue.objects.create(
                        custom_recap=recap,
                        custom_field=dry_field,
                        value="Yes",
                        created_by_id=recap.created_by_id,
                    )
                    if write_people:
                        CustomFieldValue.objects.create(
                            custom_recap=recap,
                            custom_field=people_field,
                            value=str(sampled),
                            created_by_id=recap.created_by_id,
                        )
            tagged.append(rid)

        self.stdout.write("=" * 68)
        verb = "Tagged" if apply else "Would tag"
        self.stdout.write(f"{verb} {len(tagged)}: {', '.join(f'#{i}' for i in tagged) or 'none'}")
        if skipped:
            self.stdout.write(
                f"Skipped {len(skipped)}: "
                + "; ".join(f"#{i} ({why})" for i, why in skipped)
            )
        if not apply:
            self.stdout.write(
                self.style.WARNING("DRY-RUN complete — re-run with --apply to write.")
            )
