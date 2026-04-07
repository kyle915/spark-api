"""
Django management command to sync existing approved upcoming events to Google Calendar.

Usage:
    # Sync approved upcoming events (default behavior - no parameters required)
    python manage.py sync_events_to_google_calendar

    # Sync all events for a specific tenant
    python manage.py sync_events_to_google_calendar --tenant-id 1

    # Sync a specific event
    python manage.py sync_events_to_google_calendar --event-id 16

    # Sync multiple events
    python manage.py sync_events_to_google_calendar --event-ids 16,17,18

    # Sync events in a date range
    python manage.py sync_events_to_google_calendar --tenant-id 1 --from-date 2025-01-01 --to-date 2025-01-31

    # Include events without requests (not recommended)
    python manage.py sync_events_to_google_calendar --no-request

    # Enqueue to RQ instead of running synchronously
    python manage.py sync_events_to_google_calendar --enqueue

    # Dry run to see what would be synced
    python manage.py sync_events_to_google_calendar --dry-run
"""

from datetime import date, datetime
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from events.models import Event
from events.jobs.google_calendar_jobs import EventGoogleCalendarJob
from events.tasks import sync_event_to_all_connected_users
from tenants.models import Tenant
from utils.queues import Queues


class Command(BaseCommand):
    help = 'Sync existing events to Google Calendar for all connected users'

    def add_arguments(self, parser):
        parser.add_argument(
            '--tenant-id',
            type=int,
            help='ID of the tenant to sync events for',
        )
        parser.add_argument(
            '--event-id',
            type=int,
            help='ID of a specific event to sync',
        )
        parser.add_argument(
            '--event-ids',
            type=str,
            help='Comma-separated list of event IDs to sync (e.g., "16,17,18")',
        )
        parser.add_argument(
            '--from-date',
            type=str,
            help='Start date for event filtering (YYYY-MM-DD). Filters by request.date if event has a request.',
        )
        parser.add_argument(
            '--to-date',
            type=str,
            help='End date for event filtering (YYYY-MM-DD). Filters by request.date if event has a request.',
        )
        parser.add_argument(
            '--no-request',
            action='store_true',
            help='Include events without requests (not recommended - Google Calendar sync requires a request)',
        )
        parser.add_argument(
            '--enqueue',
            action='store_true',
            help='Enqueue sync jobs to RQ instead of running synchronously',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be synced without actually syncing',
        )

    def handle(self, *args, **options):
        tenant_id = options.get('tenant_id')
        event_id = options.get('event_id')
        event_ids_str = options.get('event_ids')
        from_date_str = options.get('from_date')
        to_date_str = options.get('to_date')
        no_request = options.get('no_request', False)
        has_request = not no_request  # Default to True (only sync events with requests)
        enqueue = options.get('enqueue', False)
        dry_run = options.get('dry_run', False)

        # Validate arguments
        if event_id and event_ids_str:
            raise CommandError('Cannot specify both --event-id and --event-ids')

        # Parse event IDs if provided
        event_ids = None
        if event_ids_str:
            try:
                event_ids = [int(eid.strip()) for eid in event_ids_str.split(',')]
            except ValueError:
                raise CommandError(f'Invalid --event-ids format: {event_ids_str}. Use comma-separated integers.')

        # Parse date arguments
        from_date = None
        to_date = None

        if from_date_str:
            try:
                from_date = date.fromisoformat(from_date_str)
            except ValueError:
                raise CommandError(f'Invalid --from-date format: {from_date_str}. Use YYYY-MM-DD.')

        if to_date_str:
            try:
                to_date = date.fromisoformat(to_date_str)
            except ValueError:
                raise CommandError(f'Invalid --to-date format: {to_date_str}. Use YYYY-MM-DD.')

        if from_date and to_date and from_date > to_date:
            raise CommandError('--from-date must be before or equal to --to-date.')

        today = timezone.localdate()
        today_start = timezone.make_aware(datetime.combine(today, datetime.min.time()))

        # Build queryset
        queryset = Event.objects.all()

        # Default behavior: only approved upcoming events
        queryset = queryset.filter(
            status__slug="approved",
        ).filter(
            date__gte=today_start,
        )

        # Filter by tenant
        if tenant_id:
            try:
                tenant = Tenant.objects.get(id=tenant_id)
                queryset = queryset.filter(tenant_id=tenant_id)
                self.stdout.write(
                    self.style.SUCCESS(f'Filtering events for tenant: {tenant.name} (ID: {tenant_id})')
                )
            except Tenant.DoesNotExist:
                raise CommandError(f'Tenant with ID {tenant_id} does not exist.')

        # Filter by event ID(s)
        if event_id:
            queryset = queryset.filter(id=event_id)
        elif event_ids:
            queryset = queryset.filter(id__in=event_ids)

        # Filter by request requirement (default: True)
        if has_request:
            queryset = queryset.filter(request__isnull=False)

        # Filter by date range (using request.date)
        if from_date or to_date:
            if from_date:
                # Convert date to datetime for comparison
                from_datetime = timezone.make_aware(datetime.combine(from_date, datetime.min.time()))
                queryset = queryset.filter(date__gte=from_datetime)
            if to_date:
                # Convert date to datetime for comparison (end of day)
                to_datetime = timezone.make_aware(datetime.combine(to_date, datetime.max.time()))
                queryset = queryset.filter(date__lte=to_datetime)

        # Get events
        events = queryset.select_related('request', 'tenant').all()
        total_events = events.count()

        if total_events == 0:
            self.stdout.write(
                self.style.WARNING('No events found matching the specified criteria.')
            )
            return

        # Show what will be synced
        if dry_run:
            self.stdout.write(
                self.style.WARNING(f'DRY RUN: Would sync {total_events} event(s) to Google Calendar')
            )
            self.stdout.write('')
            self.stdout.write('Events that would be synced:')
            for event in events[:20]:  # Show first 20
                request_info = f'Request ID: {event.request.id}' if event.request else 'No request'
                self.stdout.write(f'  - Event ID: {event.id}, Name: {event.name}, Tenant: {event.tenant.name}, {request_info}')
            if total_events > 20:
                self.stdout.write(f'  ... and {total_events - 20} more event(s)')
            return

        # Execute sync
        if enqueue:
            queues = Queues()
            self.stdout.write(
                self.style.WARNING(f'Enqueuing {total_events} event(s) to RQ for Google Calendar sync...')
            )

            enqueued = 0
            for event in events:
                try:
                    queues.default.add(sync_event_to_all_connected_users, event.id)
                    enqueued += 1
                except Exception as e:
                    self.stdout.write(
                        self.style.ERROR(f'  ✗ Failed to enqueue event {event.id}: {e}')
                    )

            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS('=' * 60))
            self.stdout.write(self.style.SUCCESS('Summary:'))
            self.stdout.write(self.style.SUCCESS(f'  Total events: {total_events}'))
            self.stdout.write(self.style.SUCCESS(f'  Enqueued: {enqueued}'))
            self.stdout.write(self.style.WARNING(f'  Failed: {total_events - enqueued}'))
            self.stdout.write(self.style.SUCCESS('=' * 60))
            self.stdout.write(
                self.style.SUCCESS('✓ Tasks enqueued. Check RQ dashboard for progress.')
            )
        else:
            # Run synchronously
            self.stdout.write(
                self.style.WARNING(f'Syncing {total_events} event(s) to Google Calendar synchronously...')
            )
            self.stdout.write(
                self.style.WARNING('This may take a while. Consider using --enqueue for background processing.')
            )
            self.stdout.write('')

            synced = 0
            skipped = 0
            failed = 0
            skipped_events = []
            failed_events = []

            for idx, event in enumerate(events, 1):
                try:
                    # Check if event has request (required for Google Calendar sync)
                    if not event.request:
                        skipped += 1
                        skipped_events.append((event.id, event.name, 'No request'))
                        self.stdout.write(
                            self.style.WARNING(f'  [{idx}/{total_events}] ⚠ Skipping event {event.id}: No request')
                        )
                        continue

                    # Check if request has start_time (required)
                    if not event.request.start_time:
                        skipped += 1
                        skipped_events.append((event.id, event.name, 'Request has no start_time'))
                        self.stdout.write(
                            self.style.WARNING(f'  [{idx}/{total_events}] ⚠ Skipping event {event.id}: Request has no start_time')
                        )
                        continue

                    # Sync the event
                    self.stdout.write(
                        f'  [{idx}/{total_events}] Processing event {event.id}: {event.name}...'
                    )
                    job = EventGoogleCalendarJob(event.id)
                    job.handle()
                    synced += 1
                    self.stdout.write(
                        self.style.SUCCESS(f'    ✓ Synced event {event.id}')
                    )
                except Exception as e:
                    failed += 1
                    failed_events.append((event.id, event.name, str(e)))
                    self.stdout.write(
                        self.style.ERROR(f'    ✗ Failed to sync event {event.id}: {e}')
                    )

            # Summary
            self.stdout.write('')
            self.stdout.write(self.style.SUCCESS('=' * 60))
            self.stdout.write(self.style.SUCCESS('Summary:'))
            self.stdout.write(self.style.SUCCESS(f'  Total events: {total_events}'))
            self.stdout.write(self.style.SUCCESS(f'  Synced: {synced}'))
            self.stdout.write(self.style.WARNING(f'  Skipped: {skipped}'))
            self.stdout.write(self.style.ERROR(f'  Failed: {failed}'))
            self.stdout.write(self.style.SUCCESS('=' * 60))

            # Show skipped events if any
            if skipped_events:
                self.stdout.write('')
                self.stdout.write(self.style.WARNING('Skipped events:'))
                for event_id, event_name, reason in skipped_events[:10]:
                    self.stdout.write(f'  - Event {event_id} ({event_name}): {reason}')
                if len(skipped_events) > 10:
                    self.stdout.write(f'  ... and {len(skipped_events) - 10} more skipped event(s)')

            # Show failed events if any
            if failed_events:
                self.stdout.write('')
                self.stdout.write(self.style.ERROR('Failed events:'))
                for event_id, event_name, error in failed_events[:10]:
                    self.stdout.write(f'  - Event {event_id} ({event_name}): {error}')
                if len(failed_events) > 10:
                    self.stdout.write(f'  ... and {len(failed_events) - 10} more failed event(s)')
