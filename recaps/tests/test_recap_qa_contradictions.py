from recaps.recap_quality import _RecapFacts, _check_audience_exceeds_sampled
from recaps.recap_review_checks import _Facts, _check_photo_buckets


class TestAudienceExceedsSampled:
    def test_flags_when_willing_plus_not_willing_exceeds_consumers_sampled(self):
        facts = _RecapFacts()
        facts.exists = True
        facts.consumers_reached = 100
        facts.max_audience_subcount = 120
        facts.audience_pair_max = 130

        flag = _check_audience_exceeds_sampled(facts)
        assert flag is not None
        assert flag["code"] == "audience_exceeds_sampled"
        assert flag["severity"] == "medium"
        assert "130" in flag["label"] and "100" in flag["label"]

    def test_passes_when_subcounts_fit_within_consumers_sampled(self):
        facts = _RecapFacts()
        facts.exists = True
        facts.consumers_reached = 100
        facts.max_audience_subcount = 80
        facts.audience_pair_max = 95

        assert _check_audience_exceeds_sampled(facts) is None


class TestPhotoBucketsTemplateFallback:
    def test_counts_template_image_fields_matching_bucket_name(self):
        facts = _Facts()
        facts.exists = True
        facts.files = []
        facts.field_pairs = [
            (
                "Consumer Sampling Pictures",
                "https://storage.googleapis.com/bucket/a.jpg, https://storage.googleapis.com/bucket/b.png",
                "Image",
            )
        ]
        res = _check_photo_buckets(
            facts,
            [{"name": "Consumer Sampling Pictures", "min": 1}],
        )
        assert res["status"] == "pass"

    def test_still_flags_genuinely_missing_required_bucket(self):
        facts = _Facts()
        facts.exists = True
        facts.files = []
        facts.field_pairs = []
        res = _check_photo_buckets(
            facts,
            [{"name": "Consumer Sampling Pictures", "min": 1}],
        )
        assert res["status"] == "flag"
        assert "Consumer Sampling Pictures" in res["label"]
