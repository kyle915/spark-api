"""Territory routing prefers env; falls back to legacy map when unset."""

import os

from django.test import SimpleTestCase, override_settings

from events.routing import (
    extract_state_code,
    ignite_review_cc,
    liquid_death_territory,
    territory_emails_for_state,
    _LEGACY_LIQUID_DEATH_TERRITORY,
    _LEGACY_IGNITE_REVIEW_CC,
)


class ExtractStateCodeTests(SimpleTestCase):
    def test_parses_google_formatted_address(self):
        self.assertEqual(
            extract_state_code("1885 Halite Dr, Sparks, NV 89436, USA"),
            "NV",
        )

    def test_missing_address(self):
        self.assertIsNone(extract_state_code(None))
        self.assertIsNone(extract_state_code(""))


class TerritoryFromEnvTests(SimpleTestCase):
    def setUp(self):
        liquid_death_territory.cache_clear()
        self._had_territory = "LD_RMM_TERRITORY_JSON" in os.environ
        self._had_cc = "IGNITE_REVIEW_CC" in os.environ
        self._prev_territory = os.environ.get("LD_RMM_TERRITORY_JSON")
        self._prev_cc = os.environ.get("IGNITE_REVIEW_CC")
        os.environ.pop("LD_RMM_TERRITORY_JSON", None)
        os.environ.pop("IGNITE_REVIEW_CC", None)
        liquid_death_territory.cache_clear()

    def tearDown(self):
        liquid_death_territory.cache_clear()
        os.environ.pop("LD_RMM_TERRITORY_JSON", None)
        os.environ.pop("IGNITE_REVIEW_CC", None)
        if self._had_territory and self._prev_territory is not None:
            os.environ["LD_RMM_TERRITORY_JSON"] = self._prev_territory
        if self._had_cc and self._prev_cc is not None:
            os.environ["IGNITE_REVIEW_CC"] = self._prev_cc
        liquid_death_territory.cache_clear()

    def test_unset_env_uses_legacy_map(self):
        territory = liquid_death_territory()
        self.assertEqual(len(territory), len(_LEGACY_LIQUID_DEATH_TERRITORY))
        # Pick a known legacy state coverage without hardcoding addresses in asserts
        sample_email, sample_states = next(iter(_LEGACY_LIQUID_DEATH_TERRITORY.items()))
        self.assertIn(sample_states[0], territory[sample_email])

    def test_env_map_overrides_legacy(self):
        os.environ["LD_RMM_TERRITORY_JSON"] = (
            '{"rmm.west@example.com": ["CA", "NV"], '
            '"rmm.east@example.com": ["NY", "NJ"]}'
        )
        liquid_death_territory.cache_clear()
        self.assertEqual(
            territory_emails_for_state("ighn-liquid-death", "NV"),
            ["rmm.west@example.com"],
        )

    def test_unknown_state_fans_out_from_env(self):
        os.environ["LD_RMM_TERRITORY_JSON"] = (
            '{"rmm.west@example.com": ["CA"], '
            '"rmm.east@example.com": ["NY"]}'
        )
        liquid_death_territory.cache_clear()
        emails = territory_emails_for_state("ighn-liquid-death", "TX")
        self.assertCountEqual(
            emails,
            ["rmm.west@example.com", "rmm.east@example.com"],
        )

    def test_other_tenant_slug_ignored(self):
        self.assertEqual(territory_emails_for_state("other-brand", "CA"), [])

    def test_unset_cc_uses_legacy(self):
        self.assertEqual(ignite_review_cc(), list(_LEGACY_IGNITE_REVIEW_CC))

    @override_settings(IGNITE_REVIEW_CC=["ops@example.com", "lead@example.com"])
    def test_env_cc_overrides_legacy(self):
        os.environ["IGNITE_REVIEW_CC"] = "ops@example.com,lead@example.com"
        self.assertEqual(
            ignite_review_cc(),
            ["ops@example.com", "lead@example.com"],
        )
