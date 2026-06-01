# Generated for fix/dedupe-timezones
#
# The events_timezone table accumulated DUPLICATE rows because the model had no
# unique constraint. Semantically-identical zones (same name + code + offset)
# appeared multiple times, so the create-request timezone dropdown showed every
# zone twice.
#
# This migration is DEPLOY-SAFE because it dedupes BEFORE constraining:
#   1) RunPython collapses duplicate groups (keep the LOWEST-id survivor),
#      repointing every FK that references a duplicate to the survivor, then
#      deletes the duplicates. The survivor is the SAME semantic zone, so the
#      repoint is lossless.
#   2) AddConstraint then adds a UNIQUE constraint on (name, code, offset). It
#      runs AFTER the RunPython in the same migration, so the constraint can
#      never hit pre-existing duplicates.
#
# The dedupe is deterministic (lowest id survives) and idempotent (running it
# again on already-deduped data is a no-op).

from django.db import migrations, models


# FKs that point at events.TimeZone. (app_label, model_name, field_name).
# Discovered via grep for ForeignKey(TimeZone ...) across the codebase:
#   events.Request.timezone, events.Event.timezone,
#   ambassadors.Attendance.timezone,
#   recaps.Recap.timezone, recaps.CustomRecap.timezone
TIMEZONE_FK_REFERENCES = (
    ("events", "Request", "timezone"),
    ("events", "Event", "timezone"),
    ("ambassadors", "Attendance", "timezone"),
    ("recaps", "Recap", "timezone"),
    ("recaps", "CustomRecap", "timezone"),
)

# The columns that identify a semantically-identical TimeZone.
DUP_KEY_FIELDS = ("name", "code", "offset")


def dedupe_timezones(apps, schema_editor):
    """Collapse duplicate TimeZone rows, repointing FKs to the survivor.

    For each group of rows sharing (name, code, offset) we keep the row with the
    LOWEST id as the survivor and repoint every FK that references a duplicate to
    that survivor, then delete the duplicates. Deterministic + idempotent.
    """
    TimeZone = apps.get_model("events", "TimeZone")

    # Build {survivor_id: [duplicate_id, ...]} for every dup group.
    survivors_by_key: dict[tuple, int] = {}
    duplicates_to_survivor: dict[int, int] = {}

    for tz in TimeZone.objects.all().order_by("id").values("id", *DUP_KEY_FIELDS):
        key = tuple(tz[field] for field in DUP_KEY_FIELDS)
        if key not in survivors_by_key:
            # First (lowest-id) row for this key wins.
            survivors_by_key[key] = tz["id"]
        else:
            duplicates_to_survivor[tz["id"]] = survivors_by_key[key]

    if not duplicates_to_survivor:
        # Nothing to do — table already has no duplicates.
        return

    # Repoint every FK that references a duplicate to its survivor.
    for app_label, model_name, field_name in TIMEZONE_FK_REFERENCES:
        try:
            model = apps.get_model(app_label, model_name)
        except LookupError:
            # Model not present in this historical state — skip defensively.
            continue
        filter_kwargs = {f"{field_name}_id__in": list(duplicates_to_survivor.keys())}
        rows = model.objects.filter(**filter_kwargs).values("id", f"{field_name}_id")
        for row in rows:
            survivor_id = duplicates_to_survivor[row[f"{field_name}_id"]]
            model.objects.filter(id=row["id"]).update(**{f"{field_name}_id": survivor_id})

    # Now that nothing references the duplicates, delete them.
    TimeZone.objects.filter(id__in=list(duplicates_to_survivor.keys())).delete()


def noop_reverse(apps, schema_editor):
    """Reverse is a no-op: deleted duplicates cannot be safely resurrected."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0047_event_ev_event_tenant_date_idx_and_more"),
    ]

    operations = [
        # 1) Dedupe first so the constraint below can never fail on existing dupes.
        migrations.RunPython(dedupe_timezones, noop_reverse),
        # 2) Then constrain to prevent recurrence.
        migrations.AddConstraint(
            model_name="timezone",
            constraint=models.UniqueConstraint(
                fields=["name", "code", "offset"],
                name="uq_timezone_name_code_offset",
            ),
        ),
    ]
