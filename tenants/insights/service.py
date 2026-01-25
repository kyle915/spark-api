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

logger = logging.getLogger(__name__)


class InsightsService:
    """Service for generating AI insights from consumer feedback."""

    def __init__(self, tenant: Tenant):
        """
        Initialize the service for a specific tenant.

        Args:
            tenant: The tenant to generate insights for
        """
        self.tenant = tenant
        self._configure_gemini()

    def _configure_gemini(self):
        """Configure Gemini API with API key from settings."""
        import google.generativeai as genai
        api_key = getattr(settings, "GEMINI_API_KEY", None)
        if not api_key:
            raise ValueError("GEMINI_API_KEY not configured in settings")
        genai.configure(api_key=api_key)

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
        Build a prompt for Gemini API to analyze consumer feedback.

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

    def _call_gemini_api(self, prompt: str) -> dict:
        """
        Call Gemini API to generate insights.

        Args:
            prompt: The prompt to send to Gemini

        Returns:
            Parsed JSON response as dictionary

        Raises:
            Exception: If API call fails or response is invalid
        """
        try:
            import google.generativeai as genai

            # Ensure genai is configured (should already be done in __init__, but ensure it)
            api_key = getattr(settings, "GEMINI_API_KEY", None)
            if api_key:
                genai.configure(api_key=api_key)

            # List available models and find one that supports generateContent
            try:
                available_models = genai.list_models()
                model_name = None

                # Try to find a suitable model
                for model in available_models:
                    if "generateContent" in model.supported_generation_methods:
                        # Prefer models with "gemini" in the name
                        if "gemini" in model.name.lower():
                            model_name = model.name
                            break

                # If no gemini model found, use the first available model
                if not model_name:
                    for model in available_models:
                        if "generateContent" in model.supported_generation_methods:
                            model_name = model.name
                            break

                if not model_name:
                    raise ValueError(
                        "No models available that support generateContent. "
                        "Please check your API key and permissions."
                    )

                logger.info(f"Using model: {model_name}")
            except Exception as e:
                # If listing models fails, try common model names as fallback
                logger.warning(
                    f"Could not list available models: {e}. "
                    "Trying common model names as fallback."
                )
                # Try common model name patterns
                for fallback_name in ["gemini-pro", "models/gemini-pro", "gemini-1.5-pro", "models/gemini-1.5-pro"]:
                    try:
                        model = genai.GenerativeModel(fallback_name)
                        # Test if model works by checking if it can be instantiated
                        model_name = fallback_name
                        logger.info(f"Using fallback model: {model_name}")
                        break
                    except Exception:
                        continue

                if not model_name:
                    raise ValueError(
                        f"Could not find an available model. Error: {e}. "
                        "Please check your API key and ensure it has access to Gemini models."
                    )

            model = genai.GenerativeModel(model_name)
            response = model.generate_content(prompt)

            # Extract text from response
            response_text = response.text.strip()

            # Try to extract JSON from response (may have markdown code blocks)
            if "```json" in response_text:
                # Extract JSON from markdown code block
                start = response_text.find("```json") + 7
                end = response_text.find("```", start)
                response_text = response_text[start:end].strip()
            elif "```" in response_text:
                # Extract JSON from generic code block
                start = response_text.find("```") + 3
                end = response_text.find("```", start)
                response_text = response_text[start:end].strip()

            # Parse JSON response
            try:
                result = json.loads(response_text)
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse Gemini JSON response: {e}")
                logger.error(f"Response text: {response_text}")
                raise ValueError(
                    f"Invalid JSON response from Gemini: {e}") from e

            return result

        except Exception as e:
            logger.error(f"Error calling Gemini API: {e}")
            raise

    def _parse_insights_response(self, response: dict) -> list[dict]:
        """
        Parse and validate insights response from Gemini.

        Args:
            response: Dictionary response from Gemini API

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

        # Call Gemini API
        response = self._call_gemini_api(prompt)

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
