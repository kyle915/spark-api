"""
Django management command to generate AI insights from ConsumerFeedback records.

Usage:
    # Generate insights for a specific tenant (synchronously)
    python manage.py generate_insights --tenant-id 1

    # Generate insights for all tenants (synchronously)
    python manage.py generate_insights --all-tenants

    # Generate insights with custom date range
    python manage.py generate_insights --tenant-id 1 --from-date 2025-01-01 --to-date 2025-01-31

    # Enqueue the task to RQ instead of running synchronously
    python manage.py generate_insights --tenant-id 1 --enqueue
"""

from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tenants.models import Tenant
from tenants.insights.tasks import (
    generate_insights_for_tenant,
    generate_insights_for_all_tenants,
)
from tenants.insights.service import InsightsService
from utils.queues import Queues


class Command(BaseCommand):
    help = "Generate AI insights from ConsumerFeedback records"

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            type=int,
            help="ID of the tenant to generate insights for (required if --all-tenants not set)",
        )
        parser.add_argument(
            "--all-tenants",
            action="store_true",
            help="Generate insights for all tenants",
        )
        parser.add_argument(
            "--from-date",
            type=str,
            help="Start date for feedback analysis (YYYY-MM-DD). Defaults to 24 hours ago.",
        )
        parser.add_argument(
            "--to-date",
            type=str,
            help="End date for feedback analysis (YYYY-MM-DD). Defaults to today.",
        )
        parser.add_argument(
            "--enqueue",
            action="store_true",
            help="Enqueue the task to RQ instead of running synchronously",
        )

    def handle(self, *args, **options):
        tenant_id = options.get("tenant_id")
        all_tenants = options.get("all_tenants", False)
        from_date_str = options.get("from_date")
        to_date_str = options.get("to_date")
        enqueue = options.get("enqueue", False)

        # Validate arguments
        if not all_tenants and not tenant_id:
            raise CommandError(
                "Either --tenant-id or --all-tenants must be provided."
            )

        if all_tenants and tenant_id:
            raise CommandError(
                "Cannot specify both --tenant-id and --all-tenants."
            )

        # Parse date arguments
        from_date = None
        to_date = None

        if from_date_str:
            try:
                from_date = date.fromisoformat(from_date_str)
            except ValueError:
                raise CommandError(
                    f"Invalid --from-date format: {from_date_str}. Use YYYY-MM-DD."
                )

        if to_date_str:
            try:
                to_date = date.fromisoformat(to_date_str)
            except ValueError:
                raise CommandError(
                    f"Invalid --to-date format: {to_date_str}. Use YYYY-MM-DD."
                )

        # Set default date range if not provided (last 24 hours)
        if not to_date:
            to_date = timezone.now().date()
        if not from_date:
            from_date = to_date - timedelta(days=1)

        if from_date > to_date:
            raise CommandError("--from-date must be before or equal to --to-date.")

        # Execute insights generation
        if enqueue:
            # Enqueue to RQ
            queues = Queues()

            if all_tenants:
                self.stdout.write(
                    self.style.WARNING(
                        f"Enqueuing insights generation for all tenants "
                        f"(date range: {from_date} to {to_date})"
                    )
                )
                queues.default.add(
                    generate_insights_for_all_tenants, from_date, to_date
                )
                self.stdout.write(
                    self.style.SUCCESS(
                        "✓ Task enqueued. Check RQ dashboard for progress."
                    )
                )
            else:
                # Verify tenant exists
                try:
                    tenant = Tenant.objects.get(id=tenant_id)
                except Tenant.DoesNotExist:
                    raise CommandError(f"Tenant with ID {tenant_id} does not exist.")

                self.stdout.write(
                    self.style.WARNING(
                        f"Enqueuing insights generation for tenant {tenant.name} (ID: {tenant_id}) "
                        f"(date range: {from_date} to {to_date})"
                    )
                )
                queues.default.add(
                    generate_insights_for_tenant, tenant_id, from_date, to_date
                )
                self.stdout.write(
                    self.style.SUCCESS(
                        "✓ Task enqueued. Check RQ dashboard for progress."
                    )
                )
        else:
            # Run synchronously
            if all_tenants:
                self.stdout.write(
                    self.style.WARNING(
                        f"Generating insights for all tenants "
                        f"(date range: {from_date} to {to_date})"
                    )
                )
                self.stdout.write(
                    self.style.WARNING(
                        "This may take a while. Consider using --enqueue for background processing."
                    )
                )

                # Get all tenants and process them
                tenants = Tenant.objects.all()
                total_tenants = tenants.count()
                processed = 0
                successful = 0
                failed = 0

                for tenant in tenants:
                    processed += 1
                    self.stdout.write(
                        f"Processing tenant {processed}/{total_tenants}: {tenant.name} (ID: {tenant.id})..."
                    )
                    try:
                        service = InsightsService(tenant)
                        insights = service.generate_insights(
                            from_date=from_date, to_date=to_date
                        )
                        successful += 1
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"  ✓ Generated insights {insights.id} "
                                f"({insights.total_feedback_count} feedback records)"
                            )
                        )
                    except ValueError as e:
                        failed += 1
                        self.stdout.write(
                            self.style.WARNING(
                                f"  ⚠ Skipped: {e}"
                            )
                        )
                    except Exception as e:
                        failed += 1
                        self.stdout.write(
                            self.style.ERROR(
                                f"  ✗ Error: {e}"
                            )
                        )

                # Summary
                self.stdout.write("")
                self.stdout.write(self.style.SUCCESS("=" * 50))
                self.stdout.write(self.style.SUCCESS("Summary:"))
                self.stdout.write(
                    self.style.SUCCESS(f"  Total tenants: {total_tenants}")
                )
                self.stdout.write(
                    self.style.SUCCESS(f"  Successful: {successful}")
                )
                self.stdout.write(self.style.WARNING(f"  Skipped/Failed: {failed}"))
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  Date range: {from_date} to {to_date}"
                    )
                )
                self.stdout.write(self.style.SUCCESS("=" * 50))

            else:
                # Single tenant
                try:
                    tenant = Tenant.objects.get(id=tenant_id)
                except Tenant.DoesNotExist:
                    raise CommandError(f"Tenant with ID {tenant_id} does not exist.")

                self.stdout.write(
                    self.style.WARNING(
                        f"Generating insights for tenant {tenant.name} (ID: {tenant_id}) "
                        f"(date range: {from_date} to {to_date})"
                    )
                )

                try:
                    service = InsightsService(tenant)
                    insights = service.generate_insights(
                        from_date=from_date, to_date=to_date
                    )

                    self.stdout.write("")
                    self.stdout.write(self.style.SUCCESS("=" * 50))
                    self.stdout.write(self.style.SUCCESS("Success!"))
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  Insights ID: {insights.id}"
                        )
                    )
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  Tenant: {tenant.name} (ID: {tenant.id})"
                        )
                    )
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  Date range: {from_date} to {to_date}"
                        )
                    )
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  Feedback records analyzed: {insights.total_feedback_count}"
                        )
                    )
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  Insight reports generated: {insights.reports.count()}"
                        )
                    )
                    self.stdout.write(self.style.SUCCESS("=" * 50))

                    # Show insight reports
                    if insights.reports.exists():
                        self.stdout.write("")
                        self.stdout.write(
                            self.style.SUCCESS("Generated Insight Reports:")
                        )
                        for report in insights.reports.all().order_by(
                            "-priority", "created_at"
                        ):
                            priority_color = {
                                "high": self.style.ERROR,
                                "medium": self.style.WARNING,
                                "low": self.style.SUCCESS,
                            }.get(report.priority, self.style.SUCCESS)

                            self.stdout.write(
                                priority_color(
                                    f"  [{report.priority.upper()}] {report.title}"
                                )
                            )
                            self.stdout.write(
                                f"      {report.content[:100]}..."
                                if len(report.content) > 100
                                else f"      {report.content}"
                            )

                except ValueError as e:
                    raise CommandError(
                        f"Cannot generate insights: {e}. "
                        "This may be expected if no feedback records exist in the date range."
                    )
                except Exception as e:
                    raise CommandError(f"Error generating insights: {e}")
