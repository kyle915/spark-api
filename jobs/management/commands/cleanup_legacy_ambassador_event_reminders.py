from django.core.management.base import BaseCommand

from jobs.tasks import cancel_legacy_ambassador_event_reminder_schedules


class Command(BaseCommand):
    help = "Cancel legacy hourly ambassador reminder schedules left in rq-scheduler."

    def handle(self, *args, **options):
        canceled = cancel_legacy_ambassador_event_reminder_schedules()
        self.stdout.write(
            self.style.SUCCESS(
                f"Legacy ambassador reminder schedules canceled: {canceled}"
            )
        )
