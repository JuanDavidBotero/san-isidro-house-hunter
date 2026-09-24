"""Tests for the agentic sweep's safety guards.

The sweep is the one place in this project where an LLM produces facts, so these tests
are almost entirely negative: they assert that unevidenced numbers, search URLs, and
malformed output cannot reach the alert path.
"""

import json
import tempfile
import unittest
from pathlib import Path

import fetch_ml
import hunter
import sweep


def record(**changes):
    base = {
        "source": "Zonaprop",
        "url": "https://www.zonaprop.com.ar/propiedades/clasificado/casa-lasalle-123.html",
        "canonical_address": "Juan Bautista de Lasalle 1600, San Isidro",
        "price_usd": 340000,
        "covered_m2": 170,
        "private_pool": True,
        "evidence": {
            "price_usd": "Listing states 'USD 340.000'.",
            "covered_m2": "Listing states '170 m2 cubiertos'.",
            "private_pool": "Listing description mentions 'pileta propia'.",
        },
    }
    base.update(changes)
    return base


class EvidenceEnforcementTests(unittest.TestCase):
    def test_evidenced_facts_survive(self):
        cleaned, dropped = sweep.enforce_evidence(record())
        self.assertEqual(dropped, [])
        self.assertEqual(cleaned["price_usd"], 340000)
        self.assertEqual(cleaned["covered_m2"], 170)
        self.assertTrue(cleaned["private_pool"])

    def test_an_unevidenced_number_is_discarded(self):
        """The core anti-fabrication guarantee: a number the model invented, with no
        supporting quote, must not reach history."""
        cleaned, dropped = sweep.enforce_evidence(
            record(expenses_ars=185000)  # deliberately absent from evidence
        )
        self.assertIn("expenses_ars", dropped)
        self.assertNotIn("expenses_ars", cleaned)

    def test_an_empty_evidence_string_does_not_count_as_evidence(self):
        cleaned, dropped = sweep.enforce_evidence(
            record(units=6, evidence={**record()["evidence"], "units": "   "})
        )
        self.assertIn("units", dropped)
        self.assertNotIn("units", cleaned)

    def test_a_record_with_no_evidence_object_loses_every_fact(self):
        cleaned, dropped = sweep.enforce_evidence(
            {"source": "X", "url": "https://x/y", "canonical_address": "A", "price_usd": 1, "bedrooms": 3}
        )
        self.assertNotIn("price_usd", cleaned)
        self.assertNotIn("bedrooms", cleaned)
        # Identity fields are not claims about the property and must be preserved.
        self.assertEqual(cleaned["url"], "https://x/y")
        self.assertEqual(cleaned["canonical_address"], "A")
        self.assertIn("price_usd", dropped)

    def test_dropped_fields_render_as_not_verified_downstream(self):
        cleaned, _ = sweep.enforce_evidence(record(expenses_ars=185000, units=6))
        normalised = hunter.normalise_record(cleaned)
        self.assertIsNone(normalised["expenses_ars"])
        self.assertIsNone(normalised["units"])
        evaluation = hunter.evaluate(normalised, hunter.read_config(hunter.DEFAULT_CONFIG))
        self.assertFalse(hunter.is_act_now(normalised, evaluation, hunter.read_config(hunter.DEFAULT_CONFIG)))


class ValidationTests(unittest.TestCase):
    def payload(self, listings):
        return {"listings": listings, "skipped": [], "searched": []}

    def test_search_result_urls_are_rejected(self):
        for bad in [
            "https://www.zonaprop.com.ar/casas-en-venta-san-isidro.html",
            "https://www.argenprop.com/casas/venta/san-isidro?q=pileta",
            "https://example.com/listado/inmuebles",
        ]:
            kept, counts = sweep.validate_records(self.payload([record(url=bad)]), verbose=False)
            self.assertEqual(kept, [], f"should have rejected {bad}")
            self.assertEqual(counts["search_url"], 1)

    def test_a_record_without_a_url_is_rejected(self):
        kept, counts = sweep.validate_records(self.payload([record(url=None)]), verbose=False)
        self.assertEqual(kept, [])
        self.assertEqual(counts["no_url"], 1)

    def test_a_record_without_an_address_is_rejected(self):
        kept, counts = sweep.validate_records(self.payload([record(canonical_address=None)]), verbose=False)
        self.assertEqual(kept, [])
        self.assertEqual(counts["no_address"], 1)

    def test_a_good_record_is_kept_and_tagged_with_its_provenance(self):
        kept, counts = sweep.validate_records(self.payload([record()]), verbose=False)
        self.assertEqual(len(kept), 1)
        self.assertIn("agentic web sweep", kept[0]["fit_notes"])
        self.assertTrue(kept[0]["observed_at"])
        self.assertEqual(counts["received"], 1)

    def test_non_dict_entries_are_counted_not_crashed_on(self):
        kept, counts = sweep.validate_records(self.payload(["garbage", 42, None]), verbose=False)
        self.assertEqual(kept, [])
        self.assertEqual(counts["unparseable"], 3)

    def test_a_missing_listings_array_is_an_error(self):
        with self.assertRaises(hunter.HunterError):
            sweep.validate_records({"skipped": []}, verbose=False)

    def test_sweep_output_flows_through_the_existing_ingest_contract(self):
        kept, _ = sweep.validate_records(self.payload([record()]), verbose=False)
        normalised = hunter.normalise_record(kept[0])
        with tempfile.TemporaryDirectory() as directory:
            conn = hunter.connect(Path(directory) / "h.sqlite3")
            hunter.initialise(conn)
            targeting = hunter.read_config(hunter.DEFAULT_CONFIG)
            first = hunter.ingest_record(conn, normalised, targeting)
            self.assertEqual(first["classification"], "NEW")
            self.assertEqual(first["priority"], "A")
            # A repeat sweep of the same listing must go quiet, not re-alert.
            second = hunter.ingest_record(conn, normalised, targeting)
            self.assertEqual(second["classification"], "OLD")
            conn.close()


class JsonExtractionTests(unittest.TestCase):
    def test_bare_json(self):
        self.assertEqual(sweep.extract_json('{"listings": []}')["listings"], [])

    def test_fenced_json(self):
        raw = 'Here you go:\n```json\n{"listings": [], "skipped": []}\n```\nDone.'
        self.assertEqual(sweep.extract_json(raw)["listings"], [])

    def test_json_with_preamble(self):
        raw = 'I searched several sites.\n{"listings": [], "skipped": []}'
        self.assertEqual(sweep.extract_json(raw)["listings"], [])

    def test_unparseable_output_raises_rather_than_returning_empty(self):
        """A silent empty result would read as 'quiet market', which is the most
        dangerous failure this project has."""
        with self.assertRaises(hunter.HunterError) as caught:
            sweep.extract_json("I could not find anything useful today.")
        self.assertIn("parseable JSON", str(caught.exception))

    def test_a_json_array_is_rejected(self):
        with self.assertRaises(hunter.HunterError):
            sweep.extract_json("[1,2,3]")


class PromptTests(unittest.TestCase):
    def test_the_prompt_carries_the_buyer_brief_and_the_evidence_rule(self):
        prompt = sweep.build_prompt(sweep.DEFAULT_PROMPT, max_results=12)
        self.assertIn("Lasalle", prompt)
        self.assertIn("evidence", prompt)
        self.assertIn("WILL BE DISCARDED", prompt)
        # The compliance boundary must be stated in the instruction, not only in code.
        self.assertIn("CAPTCHA", prompt)
        self.assertIn("12", prompt)


if __name__ == "__main__":
    unittest.main()
