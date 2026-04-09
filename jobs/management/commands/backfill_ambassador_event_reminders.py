from django.core.management.base import BaseCommand

from jobs.tasks import backfill_ambassador_job_reminders


class Command(BaseCommand):
    help = (
        "Schedule exact 24h and 3h ambassador event reminders for existing "
        "eligible AmbassadorJob records."
    )

    def handle(self, *args, **options):
        result = backfill_ambassador_job_reminders()
        self.stdout.write(
            self.style.SUCCESS(
                "Backfill completed "
                f"(eligible: {result['eligible']}, "
                f"scheduled 24h: {result['scheduled_24h']}, "
                f"scheduled 3h: {result['scheduled_3h']})"
            )
        )
