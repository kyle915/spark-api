from django.core.management.base import BaseCommand

from jobs.tasks import schedule_hourly_ambassador_event_reminders


class Command(BaseCommand):
    help = "Register hourly rq-scheduler job for ambassador event reminders"

    def handle(self, *args, **options):
        job_ids = schedule_hourly_ambassador_event_reminders()
        self.stdout.write(
            self.style.SUCCESS(
                "Hourly ambassador event reminder schedules registered "
                f"(24h job id: {job_ids['24h']}, 3h job id: {job_ids['3h']})"
            )
        )
