"""
Django management command to generate ConsumerFeedback records for testing.

Usage:
    python manage.py generate_consumer_feedback --tenant-id 1 --total-to-create 10

This command will:
1. Create an Event for the tenant
2. Create a Recap that belongs to the Event
3. Create ConsumerFeedback records with random feedback data
"""

import random
from datetime import date, datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model
from django.utils import timezone

from tenants.models import Tenant
from events.models import Event, EventType, EventStatus
from recaps.models import Recap, ConsumerFeedback

User = get_user_model()

# Sample event names
EVENT_NAMES = [
    "Summer Product Launch Event",
    "Holiday Sampling Campaign",
    "Spring Brand Awareness Event",
    "Community Engagement Fair",
    "Retail Store Demo Day",
]

# Sample feedback data (30+ entries)
FEEDBACK_DATA = [
    {
        "feedback": "The product exceeded my expectations. Great taste and quality!",
        "quotes": "I'll definitely buy this again.",
        "positive_stories": "My family loved it, especially the kids.",
        "reasons_to_decline": None,
    },
    {
        "feedback": "Really enjoyed the flavor profile. Very refreshing.",
        "quotes": "This is my new favorite drink.",
        "positive_stories": "I've been recommending it to all my friends.",
        "reasons_to_decline": None,
    },
    {
        "feedback": "The packaging is attractive and the product is delicious.",
        "quotes": "I love the design and the taste.",
        "positive_stories": "I bought three more after trying the sample.",
        "reasons_to_decline": None,
    },
    {
        "feedback": "Good value for money. Will purchase regularly.",
        "quotes": "Affordable and tasty - perfect combination.",
        "positive_stories": "I've already added it to my shopping list.",
        "reasons_to_decline": None,
    },
    {
        "feedback": "The texture is perfect, not too sweet.",
        "quotes": "Just the right amount of sweetness.",
        "positive_stories": "My diabetic friend can enjoy this too.",
        "reasons_to_decline": None,
    },
    {
        "feedback": "Amazing product! Better than I expected.",
        "quotes": "This is going to be a staple in my pantry.",
        "positive_stories": "I've shared it with my coworkers.",
        "reasons_to_decline": None,
    },
    {
        "feedback": "The brand ambassador was very helpful and friendly.",
        "quotes": "Great customer service experience.",
        "positive_stories": "The staff made the experience enjoyable.",
        "reasons_to_decline": None,
    },
    {
        "feedback": "Love the natural ingredients. No artificial flavors.",
        "quotes": "Finally, a product I can trust.",
        "positive_stories": "I feel good about giving this to my family.",
        "reasons_to_decline": None,
    },
    {
        "feedback": "The product is good but a bit pricey for my budget.",
        "quotes": "I like it but wish it was more affordable.",
        "positive_stories": None,
        "reasons_to_decline": "Price is too high for regular purchase.",
    },
    {
        "feedback": "Taste is okay, but I prefer other brands.",
        "quotes": "It's decent but not my favorite.",
        "positive_stories": None,
        "reasons_to_decline": "Personal preference for other products.",
    },
    {
        "feedback": "The flavor is too strong for my taste.",
        "quotes": "A bit overwhelming for me.",
        "positive_stories": None,
        "reasons_to_decline": "Flavor intensity doesn't match my preference.",
    },
    {
        "feedback": "Good product, but I'm not a fan of the packaging.",
        "quotes": "Taste is fine, but design could be better.",
        "positive_stories": None,
        "reasons_to_decline": "Packaging doesn't appeal to me.",
    },
    {
        "feedback": "I'm allergic to one of the ingredients.",
        "quotes": "Can't try it due to allergies.",
        "positive_stories": None,
        "reasons_to_decline": "Contains allergens I need to avoid.",
    },
    {
        "feedback": "The product is excellent! Very high quality.",
        "quotes": "This is premium quality at a great price.",
        "positive_stories": "I've become a loyal customer.",
        "reasons_to_decline": None,
    },
    {
        "feedback": "Love how convenient it is. Perfect for on-the-go.",
        "quotes": "Great for busy lifestyles.",
        "positive_stories": "I take it to work every day.",
        "reasons_to_decline": None,
    },
    {
        "feedback": "The taste reminds me of my childhood favorites.",
        "quotes": "Nostalgic and delicious.",
        "positive_stories": "Brought back great memories.",
        "reasons_to_decline": None,
    },
    {
        "feedback": "I appreciate the health benefits. Good for my diet.",
        "quotes": "Fits perfectly into my healthy lifestyle.",
        "positive_stories": "I've lost weight since switching to this product.",
        "reasons_to_decline": None,
    },
    {
        "feedback": "The product is good, but availability is limited.",
        "quotes": "I like it but can't always find it in stores.",
        "positive_stories": None,
        "reasons_to_decline": "Not available in my local area.",
    },
    {
        "feedback": "Excellent marketing and presentation at the event.",
        "quotes": "The event was well-organized and informative.",
        "positive_stories": "I learned a lot about the product benefits.",
        "reasons_to_decline": None,
    },
    {
        "feedback": "The product is decent, but I need more variety.",
        "quotes": "I'd like to see more flavor options.",
        "positive_stories": None,
        "reasons_to_decline": "Limited flavor selection.",
    },
    {
        "feedback": "Outstanding quality! Best in its category.",
        "quotes": "This is the best product I've tried in years.",
        "positive_stories": "I've converted my entire family to this brand.",
        "reasons_to_decline": None,
    },
    {
        "feedback": "The texture is smooth and creamy. Very satisfying.",
        "quotes": "Perfect consistency and mouthfeel.",
        "positive_stories": "I enjoy it as a treat every day.",
        "reasons_to_decline": None,
    },
    {
        "feedback": "Good product, but the serving size is too small.",
        "quotes": "I wish the portions were larger.",
        "positive_stories": None,
        "reasons_to_decline": "Serving size doesn't meet my needs.",
    },
    {
        "feedback": "I love the eco-friendly packaging approach.",
        "quotes": "Great to see a company caring about the environment.",
        "positive_stories": "I support brands with sustainable practices.",
        "reasons_to_decline": None,
    },
    {
        "feedback": "The product is okay, but I'm loyal to another brand.",
        "quotes": "It's fine, but I prefer my usual brand.",
        "positive_stories": None,
        "reasons_to_decline": "Brand loyalty to competitors.",
    },
    {
        "feedback": "Amazing taste! I'm impressed with the quality.",
        "quotes": "This exceeded all my expectations.",
        "positive_stories": "I've already recommended it to five people.",
        "reasons_to_decline": None,
    },
    {
        "feedback": "The product is good, but I don't like the aftertaste.",
        "quotes": "Initial taste is good, but aftertaste is off-putting.",
        "positive_stories": None,
        "reasons_to_decline": "Unpleasant aftertaste.",
    },
    {
        "feedback": "Perfect for my dietary restrictions. Thank you!",
        "quotes": "Finally, a product I can enjoy without worry.",
        "positive_stories": "This has made my diet so much easier.",
        "reasons_to_decline": None,
    },
    {
        "feedback": "The product is fine, but I expected more based on the marketing.",
        "quotes": "It's good, but the hype was a bit much.",
        "positive_stories": None,
        "reasons_to_decline": "Didn't meet marketing expectations.",
    },
    {
        "feedback": "I love everything about this product!",
        "quotes": "This is perfect in every way.",
        "positive_stories": "I've become a brand ambassador myself.",
        "reasons_to_decline": None,
    },
    {
        "feedback": "The product is good, but I'm concerned about the sugar content.",
        "quotes": "Taste is great, but I'm watching my sugar intake.",
        "positive_stories": None,
        "reasons_to_decline": "Sugar content doesn't fit my diet.",
    },
    {
        "feedback": "Excellent product! The best I've tried.",
        "quotes": "I'm a fan for life now.",
        "positive_stories": "I've bought multiple cases already.",
        "reasons_to_decline": None,
    },
]


class Command(BaseCommand):
    help = "Generate ConsumerFeedback records for testing"

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant-id",
            type=int,
            required=True,
            help="ID of the tenant to create feedback for",
        )
        parser.add_argument(
            "--total-to-create",
            type=int,
            required=True,
            help="Total number of ConsumerFeedback records to create",
        )

    def handle(self, *args, **options):
        tenant_id = options["tenant_id"]
        total_to_create = options["total_to_create"]

        # Get tenant
        try:
            tenant = Tenant.objects.get(id=tenant_id)
        except Tenant.DoesNotExist:
            raise CommandError(f"Tenant with ID {tenant_id} does not exist.")

        # Get or create a user for created_by
        user = User.objects.filter(is_superuser=True).first()
        if not user:
            user = User.objects.first()
        if not user:
            raise CommandError(
                "No users found. Please create a user first or run create_tenant_and_roles."
            )

        # Get or create default EventType and EventStatus
        event_type = EventType.objects.filter(is_default=True).first()
        if not event_type:
            event_type = EventType.objects.first()
            if not event_type:
                event_type = EventType.objects.create(
                    name="Sampling Event",
                    is_default=True,
                    created_by=user,
                )
                self.stdout.write(
                    self.style.SUCCESS(f"Created EventType: {event_type.name}")
                )

        event_status = EventStatus.objects.filter(is_default=True).first()
        if not event_status:
            event_status = EventStatus.objects.first()
            if not event_status:
                event_status = EventStatus.objects.create(
                    name="Completed",
                    slug="completed",
                    is_default=True,
                    created_by=user,
                )
                self.stdout.write(
                    self.style.SUCCESS(f"Created EventStatus: {event_status.name}")
                )

        # Calculate date range (today to 5 days past)
        today = timezone.now().date()
        from_date = today - timedelta(days=5)
        to_date = today

        # Randomly select an event name
        event_name = random.choice(EVENT_NAMES)

        # Create Event
        event_date = random.choice(
            [
                from_date + timedelta(days=i)
                for i in range((to_date - from_date).days + 1)
            ]
        )
        event_datetime = timezone.make_aware(
            datetime.combine(event_date, datetime.min.time())
        )

        event = Event.objects.create(
            name=event_name,
            tenant=tenant,
            date=event_datetime,
            start_time=event_datetime,
            end_time=event_datetime + timedelta(hours=4),
            address=f"123 Main Street, {tenant.name}",
            event_type=event_type,
            status=event_status,
            created_by=user,
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Created Event: {event.name} (ID: {event.id}) on {event_date}"
            )
        )

        # Create Recap
        recap = Recap.objects.create(
            name=f"Recap for {event.name}",
            event=event,
            created_by=user,
        )

        self.stdout.write(
            self.style.SUCCESS(f"Created Recap: {recap.name} (ID: {recap.id})")
        )

        # Create ConsumerFeedback records
        created_count = 0
        for i in range(total_to_create):
            # Randomly select feedback data
            feedback_data = random.choice(FEEDBACK_DATA)

            # Randomly assign created_at within the date range
            days_offset = random.randint(0, (to_date - from_date).days)
            feedback_date = from_date + timedelta(days=days_offset)
            feedback_datetime = timezone.make_aware(
                datetime.combine(feedback_date, datetime.min.time())
            )
            # Add random time during the day
            feedback_datetime = feedback_datetime + timedelta(
                hours=random.randint(9, 17), minutes=random.randint(0, 59)
            )

            # Create ConsumerFeedback
            consumer_feedback = ConsumerFeedback.objects.create(
                recap=recap,
                feedback=feedback_data["feedback"],
                quotes=feedback_data["quotes"],
                positive_stories=feedback_data["positive_stories"],
                reasons_to_decline=feedback_data["reasons_to_decline"],
                created_by=user,
            )

            # Update created_at to match our date range using update() to bypass auto_now_add
            ConsumerFeedback.objects.filter(id=consumer_feedback.id).update(
                created_at=feedback_datetime
            )

            created_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Created {created_count} ConsumerFeedback records (date range: {from_date} to {to_date})"
            )
        )

        # Summary
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=" * 50))
        self.stdout.write(self.style.SUCCESS("Summary:"))
        self.stdout.write(
            self.style.SUCCESS(f"  Tenant: {tenant.name} (ID: {tenant.id})")
        )
        self.stdout.write(
            self.style.SUCCESS(f"  Event: {event.name} (ID: {event.id})")
        )
        self.stdout.write(
            self.style.SUCCESS(f"  Recap: {recap.name} (ID: {recap.id})")
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"  ConsumerFeedback records: {created_count} (date range: {from_date} to {to_date})"
            )
        )
        self.stdout.write(self.style.SUCCESS("=" * 50))
