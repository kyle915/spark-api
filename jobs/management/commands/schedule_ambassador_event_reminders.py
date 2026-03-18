from django.core.management.base import BaseCommand

from jobs.tasks import schedule_hourly_ambassador_event_reminders


class Command(BaseCommand):
    help = "Register hourly rq-scheduler job for ambassador event reminders"

    def handle(self, *args, **options):
        job_id = schedule_hourly_ambassador_event_reminders()
        self.stdout.write(
            self.style.SUCCESS(
                f"Hourly ambassador event reminder schedule registered (job id: {job_id})"
            )
        )
