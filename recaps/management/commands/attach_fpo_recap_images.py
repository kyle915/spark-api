"""Attach FPO ("for position only") placeholder photos to a recap.

For demoing a completed recap to a client before real field photos exist. The
images are generated here rather than shipped as binaries, so there is nothing
to keep in the repo and the labels can follow whatever photo categories the
tenant actually has.

They are deliberately, unmistakably placeholders — flat charcoal cards reading
FOR POSITION ONLY with the category name. A demo that quietly used stock
photography would read as real field work to the client, which is the one thing
a placeholder must never do.

One image per FileRecapCategory on the recap's tenant, so the demo shows the
real grouping the client will see. Falls back to a generic retail-sampling set
when the tenant has no categories configured.

WHICH TABLE THE FILES GO IN
    A CustomRecap and a legacy Recap can share an id — 664 exists as both — and
    they use DIFFERENT file models: CustomRecap -> CustomRecapFile.custom_recap
    (blob field ``url``), Recap -> RecapFile.recap (blob field ``file``).
    Writing an id into the wrong one attaches a file to a stranger's recap and
    the database accepts it, because the row it points at genuinely exists.
    So the file model is derived from the resolved recap's type, never assumed,
    and the resolved type is printed on every run.

DRY-RUN by default: prints the tenant's categories and what would be attached.
--apply writes. Idempotent — a category that already has an FPO image on this
recap is skipped, so re-running never stacks duplicates.

Usage::

    python manage.py attach_fpo_recap_images --recap-id 664
    python manage.py attach_fpo_recap_images --recap-id 664 --apply
    python manage.py attach_fpo_recap_images --recap-id 664 --remove --apply
"""

from __future__ import annotations

import io

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from recaps.models import FileRecapCategory

User = get_user_model()

# Marker in the file row's name so these are findable and removable later. A demo
# asset with no marker is a demo asset nobody can clean up.
FPO_MARKER = "[FPO]"

# Used only when the tenant has no photo categories of its own.
DEFAULT_LABELS = [
    "Table Setup",
    "Sampling in Action",
    "Product Display",
    "Signage & POS",
]

# 4:3, the shape a phone photo lands in.
WIDTH, HEIGHT = 1600, 1200

BACKGROUND = (26, 28, 30)
BORDER = (58, 62, 66)
LABEL = (232, 234, 236)
MUTED = (138, 143, 148)
ACCENT = (196, 216, 46)  # Spark lime, used once


class Command(BaseCommand):
    help = (
        "Generate and attach FPO placeholder photos to a recap. "
        "Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--recap-id",
            dest="recap_id",
            type=int,
            required=True,
            help="CustomRecap / Recap id to attach to.",
        )
        parser.add_argument(
            "--owner-email",
            dest="owner_email",
            default=None,
            help="created_by for the files. Defaults to the recap's creator.",
        )
        parser.add_argument(
            "--remove",
            action="store_true",
            help=f"Delete this recap's {FPO_MARKER} files instead of adding any.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually write (omit for a dry run that changes nothing).",
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        recap, kind = self._resolve_recap(opts["recap_id"])
        tenant = self._tenant_of(recap)
        file_model, fk_field, blob_field = self._file_model(kind)

        self.stdout.write("=" * 72)
        self.stdout.write(
            f"RECAP : id={recap.id} uuid={recap.uuid}  "
            f"({'CustomRecap' if kind == 'custom' else 'Recap (legacy)'})\n"
            f"FILES : {file_model.__name__}.{fk_field} -> blob in .{blob_field}\n"
            f"TENANT: [{getattr(tenant, 'id', '?')}] "
            f"{getattr(tenant, 'name', '(unknown)')!r}\n"
            f"MODE  : {'APPLY (writing)' if apply else 'DRY-RUN (no writes)'}"
        )
        self.stdout.write("=" * 72)

        existing = list(
            file_model.objects.filter(
                **{fk_field: recap.id}, name__startswith=FPO_MARKER
            ).order_by("id")
        )

        if opts["remove"]:
            self._remove(existing, blob_field, apply)
            return

        categories = list(
            FileRecapCategory.objects.filter(tenant_id=tenant.id).order_by("id")
        ) if tenant else []

        if categories:
            plan = [(c.name, c) for c in categories]
            self.stdout.write(
                f"\n  Tenant photo categories ({len(categories)}) — one FPO each:"
            )
        else:
            plan = [(label, None) for label in DEFAULT_LABELS]
            self.stdout.write(
                "\n  Tenant has no photo categories; using the default set:"
            )

        have = {f.name for f in existing}
        todo = []
        for label, category in plan:
            name = f"{FPO_MARKER} {label}"
            if name in have:
                self.stdout.write(f"    = {label}  (already attached, skip)")
            else:
                todo.append((name, label, category))
                self.stdout.write(f"    + {label}")

        if not todo:
            self.stdout.write(
                "\n  Nothing to do — every category already has an FPO image."
            )
            return

        if not apply:
            self.stdout.write(
                f"\nDRY-RUN — would generate and attach {len(todo)} "
                f"{WIDTH}x{HEIGHT} placeholder(s). Re-run with --apply to write."
            )
            return

        owner = self._resolve_owner(opts["owner_email"], recap)
        file_type = self._resolve_file_type()

        self.stdout.write("")
        made = 0
        for name, label, category in todo:
            png = self._render(label)
            with transaction.atomic():
                rf = file_model(
                    name=name,
                    file_type=file_type,
                    file_recap_category=category,
                    approved=True,
                    created_by=owner,
                    **{fk_field: recap.id},
                )
                slug = label.lower().replace(" ", "-").replace("&", "and")
                getattr(rf, blob_field).save(
                    f"fpo-{kind}-{recap.id}-{slug}.png",
                    ContentFile(png),
                    save=False,
                )
                rf.save()
            made += 1
            cat_txt = f"  category={category.name!r}" if category else "  (no category)"
            self.stdout.write(
                f"  + {file_model.__name__} id={rf.id} {name!r}  "
                f"{len(png) // 1024}KB{cat_txt}"
            )

        self.stdout.write("")
        self.stdout.write("=" * 72)
        self.stdout.write(
            self.style.SUCCESS(f"Attached {made} FPO image(s) to recap {recap.id}.")
        )
        self.stdout.write(
            f"Remove them later with:  --recap-id {recap.id} --remove --apply"
        )
        self.stdout.write("=" * 72)

    # ------------------------------------------------------------------

    def _remove(self, existing, blob_field: str, apply: bool) -> None:
        if not existing:
            self.stdout.write(f"\n  No {FPO_MARKER} files on this recap.")
            return
        self.stdout.write(f"\n  {len(existing)} {FPO_MARKER} file(s):")
        for f in existing:
            self.stdout.write(f"    - id={f.id} {f.name!r}")
        if not apply:
            self.stdout.write(
                "\nDRY-RUN — would delete the files above. Add --apply to write."
            )
            return
        n = 0
        for f in existing:
            # Drop the stored blob too, so removing the demo doesn't leave
            # orphaned objects paying rent in the bucket.
            try:
                blob = getattr(f, blob_field, None)
                if blob:
                    blob.delete(save=False)
            except Exception as exc:  # noqa: BLE001 — blob may already be gone
                self.stdout.write(
                    self.style.WARNING(f"    blob delete failed for {f.id}: {exc}")
                )
            f.delete()
            n += 1
        self.stdout.write(self.style.SUCCESS(f"\nDeleted {n} FPO file(s)."))

    def _resolve_recap(self, recap_id: int):
        """Return (recap, kind). Prefers CustomRecap; ids collide across both."""
        from recaps.models import CustomRecap, Recap

        row = CustomRecap.objects.filter(id=recap_id).first()
        if row is not None:
            return row, "custom"
        row = Recap.objects.filter(id=recap_id).first()
        if row is not None:
            return row, "legacy"
        raise CommandError(f"No recap with id={recap_id}.")

    def _file_model(self, kind: str):
        """(model, fk field, blob field) for the resolved recap type."""
        from recaps.models import CustomRecapFile, RecapFile

        if kind == "custom":
            return CustomRecapFile, "custom_recap_id", "url"
        return RecapFile, "recap_id", "file"

    def _tenant_of(self, recap):
        for path in ("tenant", "event.tenant", "custom_recap_template.tenant"):
            obj = recap
            try:
                for part in path.split("."):
                    obj = getattr(obj, part, None)
                    if obj is None:
                        break
                if obj is not None:
                    return obj
            except Exception:  # noqa: BLE001
                continue
        return None

    def _resolve_owner(self, owner_email: str | None, recap):
        if owner_email:
            owner = User.objects.filter(email__iexact=owner_email).first()
            if owner is None:
                raise CommandError(f"No user with email {owner_email!r}.")
            return owner
        owner = getattr(recap, "created_by", None)
        if owner is None:
            raise CommandError(
                "Recap has no created_by; pass --owner-email explicitly."
            )
        return owner

    def _resolve_file_type(self):
        from recaps.models import FileType

        ft = (
            FileType.objects.filter(name__icontains="image").first()
            or FileType.objects.first()
        )
        if ft is None:
            raise CommandError("No FileType rows exist; cannot create a file row.")
        return ft

    # ------------------------------------------------------------------

    def _font(self, size: int):
        """A scalable font, whatever the container has.

        Pillow's bitmap default doesn't scale, so a placeholder rendered with it
        is unreadable at 1600px. load_default(size=) is scalable on Pillow 10+;
        real TTFs are tried first because they simply look better.
        """
        from PIL import ImageFont

        for path in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/Library/Fonts/Arial.ttf",
        ):
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001 — try the next candidate
                continue
        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()

    def _render(self, label: str) -> bytes:
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
        d = ImageDraw.Draw(img)

        margin = 56
        d.rectangle(
            [margin, margin, WIDTH - margin, HEIGHT - margin],
            outline=BORDER,
            width=2,
        )

        eyebrow_font = self._font(30)
        label_font = self._font(76)
        meta_font = self._font(26)

        eyebrow = "F O R   P O S I T I O N   O N L Y"
        self._centered(d, eyebrow, eyebrow_font, HEIGHT // 2 - 150, MUTED)

        # One accent, used once.
        rule_w = 96
        rule_y = HEIGHT // 2 - 92
        d.rectangle(
            [WIDTH // 2 - rule_w // 2, rule_y, WIDTH // 2 + rule_w // 2, rule_y + 4],
            fill=ACCENT,
        )

        self._centered(d, label, label_font, HEIGHT // 2 - 42, LABEL)
        self._centered(
            d,
            "Placeholder — replace with field photo",
            meta_font,
            HEIGHT // 2 + 70,
            MUTED,
        )
        self._centered(
            d, f"{WIDTH} x {HEIGHT}", meta_font, HEIGHT - margin - 62, BORDER
        )

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    def _centered(self, draw, text: str, font, y: int, fill) -> None:
        box = draw.textbbox((0, 0), text, font=font)
        draw.text(((WIDTH - (box[2] - box[0])) // 2, y), text, font=font, fill=fill)
