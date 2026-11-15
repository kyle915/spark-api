"""
Service for generating AI-powered insights from ConsumerFeedback records.
"""
import json
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from django.conf import settings
from django.db.models import QuerySet
from django.utils import timezone

from recaps.models import ConsumerFeedback
from tenants.models import Insights, InsightReport, Tenant
from utils.ai_text import AiUnavailable, generate_json

logger = logging.getLogger(__name__)

_INSIGHTS_SYSTEM_PROMPT = (
    "You analyse consumer feedback from in-store product sampling events. "
    "Use only what the feedback says. Do not invent facts or numbers."
)

# Strict structured-output schema: 1-4 reports, each with a priority. The
# count is checked again in _parse_insights_response.
_INSIGHTS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["insights"],
    "properties": {
        "insights": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "content", "priority"],
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "priority": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                },
            },
        },
    },
}


class InsightsService:
    """Service for generating AI insights from consumer feedback."""

    def __init__(self, tenant: Tenant):
        """
        Initialize the service for a specific tenant.

        Args:
            tenant: The tenant to generate insights for
        """
        self.tenant = tenant
        self._require_openai_key()

    def _require_openai_key(self):
        """Fail early, before any query, when OpenAI is not configured."""
        if not (getattr(settings, "OPENAI_API_KEY", "") or "").strip():
            raise ValueError("OPENAI_API_KEY not configured in settings")

    def _get_feedback_queryset(
        self, from_date: Optional[date] = None, to_date: Optional[date] = None
    ) -> QuerySet:
        """
        Get ConsumerFeedback queryset filtered by tenant and date range.

        Args:
            from_date: Start date for filtering (inclusive)
            to_date: End date for filtering (inclusive)

        Returns:
            QuerySet of ConsumerFeedback records
        """
        queryset = ConsumerFeedback.objects.filter(
            recap__event__tenant_id=self.tenant.id
        ).select_related("recap", "recap__event")

        if from_date:
            # Convert date to datetime for comparison with created_at
            from_datetime = datetime.combine(from_date, datetime.min.time())
            from_datetime = timezone.make_aware(from_datetime)
            queryset = queryset.filter(created_at__gte=from_datetime)

        if to_date:
            # Convert date to datetime for end of day
            to_datetime = datetime.combine(to_date, datetime.max.time())
            to_datetime = timezone.make_aware(to_datetime)
            queryset = queryset.filter(created_at__lte=to_datetime)

        return queryset

    def _build_prompt(self, feedback_records: list[ConsumerFeedback]) -> str:
        """
        Build the prompt that asks for insights from consumer feedback.

        Args:
            feedback_records: List of ConsumerFeedback records

        Returns:
            Formatted prompt string
        """
        # Extract relevant data from feedback records
        feedback_data = []
        for record in feedback_records:
            feedback_item = {
                "feedback": record.feedback or "",
                "quotes": record.quotes or "",
                "positive_stories": record.positive_stories or "",
                "reasons_to_decline": record.reasons_to_decline or "",
            }
            feedback_data.append(feedback_item)

        prompt = f"""You are an AI analyst specializing in customer feedback analysis. Analyze the following consumer feedback data and generate 1-4 prioritized insight reports.

Consumer Feedback Data:
{json.dumps(feedback_data, indent=2)}

Instructions:
1. Analyze customer satisfaction, service quality, and overall experience feedback
2. Identify key patterns, trends, and actionable insights
3. Generate between 1 and 4 insight reports
4. Assign priority to each insight based on:
   - High: Critical issues, strong patterns, highly actionable insights that require immediate attention
   - Medium: Important trends, moderate frequency, actionable but not urgent
   - Low: Minor observations, less frequent patterns, nice-to-know information

For each insight, provide:
- A concise, descriptive title (max 200 characters)
- Detailed content explaining the insight, patterns found, and context
- Priority level (high, medium, or low)

Return your response as a JSON object with the following structure:
{{
  "insights": [
    {{
      "title": "Insight title here",
      "content": "Detailed insight description here",
      "priority": "high" | "medium" | "low"
    }}
  ]
}}

Ensure the JSON is valid and contains between 1 and 4 insights."""

        return prompt

    def _call_ai(self, prompt: str) -> dict:
        """
        Ask OpenAI for the insights, as a JSON object matching
        ``_INSIGHTS_SCHEMA``.

        Raises:
            ValueError: If OpenAI is not configured or returned nothing
                usable. Callers already treat ValueError as "no insights
                this run".
        """
        try:
            result = generate_json(
                _INSIGHTS_SYSTEM_PROMPT,
                prompt,
                schema=_INSIGHTS_SCHEMA,
                max_tokens=4000,
            )
        except AiUnavailable as exc:
            raise ValueError(str(exc)) from exc
        if result is None:
            logger.error("OpenAI returned no usable insights for tenant %s", self.tenant.id)
            raise ValueError("The AI service returned no usable insights.")
        return result

    def _parse_insights_response(self, response: dict) -> list[dict]:
        """
        Parse and validate the insights response.

        Args:
            response: Dictionary response from the AI call

        Returns:
            List of insight dictionaries with title, content, and priority

        Raises:
            ValueError: If response structure is invalid
        """
        if "insights" not in response:
            raise ValueError("Response missing 'insights' key")

        insights = response["insights"]

        if not isinstance(insights, list):
            raise ValueError("'insights' must be a list")

        if len(insights) < 1 or len(insights) > 4:
            raise ValueError(f"Expected 1-4 insights, got {len(insights)}")

        valid_priorities = {"high", "medium", "low"}
        parsed_insights = []

        for idx, insight in enumerate(insights):
            if not isinstance(insight, dict):
                raise ValueError(f"Insight {idx} must be a dictionary")

            # Validate required fields
            for field in ["title", "content", "priority"]:
                if field not in insight:
                    raise ValueError(
                        f"Insight {idx} missing required field: {field}")

            # Validate priority
            priority = insight["priority"].lower()
            if priority not in valid_priorities:
                raise ValueError(
                    f"Insight {idx} has invalid priority: {priority}. Must be one of {valid_priorities}"
                )

            parsed_insights.append(
                {
                    "title": insight["title"],
                    "content": insight["content"],
                    "priority": priority,
                }
            )

        return parsed_insights

    def generate_insights(
        self,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        created_by=None,
    ) -> Insights:
        """
        Generate insights from consumer feedback for the tenant.

        Args:
            from_date: Start date for feedback analysis (defaults to 24 hours ago)
            to_date: End date for feedback analysis (defaults to now)
            created_by: User who triggered the generation (optional)

        Returns:
            Created Insights instance with associated InsightReport instances

        Raises:
            ValueError: If no feedback records found or API call fails
        """
        # Set default date range if not provided (last 24 hours)
        if not to_date:
            to_date = timezone.now().date()
        if not from_date:
            from_date = to_date - timedelta(days=1)

        # Get feedback records
        feedback_queryset = self._get_feedback_queryset(from_date, to_date)
        feedback_records = list(feedback_queryset)

        if not feedback_records:
            raise ValueError(
                f"No ConsumerFeedback records found for tenant {self.tenant.id} "
                f"in date range {from_date} to {to_date}"
            )

        logger.info(
            f"Generating insights for tenant {self.tenant.id} "
            f"from {len(feedback_records)} feedback records "
            f"(date range: {from_date} to {to_date})"
        )

        # Build prompt
        prompt = self._build_prompt(feedback_records)

        response = self._call_ai(prompt)

        # Parse response
        parsed_insights = self._parse_insights_response(response)

        # Create Insights instance
        insights = Insights.objects.create(
            tenant=self.tenant,
            from_date=from_date,
            to_date=to_date,
            total_feedback_count=len(feedback_records),
            created_by=created_by,
        )

        # Create InsightReport instances
        for insight_data in parsed_insights:
            InsightReport.objects.create(
                insights=insights,
                title=insight_data["title"],
                content=insight_data["content"],
                priority=insight_data["priority"],
                created_by=created_by,
            )

        logger.info(
            f"Successfully generated {len(parsed_insights)} insights for tenant {self.tenant.id}"
        )

        return insights
