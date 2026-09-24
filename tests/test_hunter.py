import tempfile
import unittest
from pathlib import Path

import hunter


class HouseHunterHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "history.sqlite3"
        self.config = hunter.read_config(hunter.DEFAULT_CONFIG)
        self.conn = hunter.connect(self.database)
        hunter.initialise(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def listing(self, **changes):
        record = {
            "source": "Zonaprop",
            "url": "https://www.zonaprop.com.ar/propiedades/clasificado/lasalle-chico-1.html?utm=ignored",
            "source_listing_id": "lasalle-chico-1",
            "canonical_address": "Juan Bautista de Lasalle 1600, San Isidro",
            "price_usd": 380000,
            "covered_m2": 170,
            "total_m2": 260,
            "bedrooms": 2,
            "bathrooms": 2,
            "parking": 2,
            "units": 6,
            "private_garden": True,
            "private_pool": True,
            "controlled_entrance": True,
            "condition": "Excellent",
            "property_type": "House in small condominium",
            "street_quality": "quiet residential",
            "broker": "Example Brokers",
            "broker_phone": "+54 11 5555 1234",
            "photo_urls": ["https://images.example.com/lasalle-pool.jpg"],
            "distinctive_features": ["covered gallery", "two car garage"],
            "observed_at": "2026-09-20T10:00:00-03:00",
            "evidence": {"units": "The listing says six units."},
        }
        record.update(changes)
        return hunter.normalise_record(record)

    def test_classifies_new_old_change_and_cross_post(self):
        first = hunter.ingest_record(self.conn, self.listing(), self.config)
        self.assertEqual(first["classification"], "NEW")
        self.assertEqual(first["priority"], "A")
        self.assertGreaterEqual(first["fit_score"], 70)

        old = hunter.ingest_record(self.conn, self.listing(observed_at="2026-09-21T10:00:00-03:00"), self.config)
        self.assertEqual(old["classification"], "OLD")

        changed = hunter.ingest_record(
            self.conn,
            self.listing(price_usd=365000, observed_at="2026-09-22T10:00:00-03:00"),
            self.config,
        )
        self.assertEqual(changed["classification"], "MATERIAL_CHANGE")
        self.assertIn("price_usd", changed["changes"])

        cross_post = hunter.ingest_record(
            self.conn,
            self.listing(
                source="Argenprop",
                url="https://www.argenprop.com/casa-en-venta-lasalle-chico-1",
                source_listing_id="argen-999",
                observed_at="2026-09-22T12:00:00-03:00",
            ),
            self.config,
        )
        self.assertEqual(cross_post["classification"], "DUPLICATE")

    def test_ambiguous_same_address_is_not_promoted(self):
        hunter.ingest_record(self.conn, self.listing(), self.config)
        ambiguous = hunter.ingest_record(
            self.conn,
            self.listing(
                url="https://www.zonaprop.com.ar/propiedades/clasificado/other-unit.html",
                source_listing_id="other-unit",
                broker_phone=None,
                photo_urls=[],
                description=None,
                distinctive_features=[],
                observed_at="2026-09-21T10:00:00-03:00",
            ),
            self.config,
        )
        self.assertEqual(ambiguous["classification"], "UNCERTAIN")
        hunter.command_resolve(type("Args", (), {"database": str(self.database), "observation": ambiguous["observation_id"], "decision": "separate"})())
        classification = self.conn.execute(
            "SELECT classification FROM observations WHERE id = ?", (ambiguous["observation_id"],)
        ).fetchone()["classification"]
        self.assertEqual(classification, "NEW")

    def test_report_only_emits_meaningful_new_or_changed_listing(self):
        hunter.ingest_record(self.conn, self.listing(), self.config)
        report = hunter.report(self.conn, self.config, "2026-09-20")
        self.assertIn("🚨 ACT NOW", report)
        self.assertIn("Publicación original: https://www.zonaprop.com.ar/propiedades/clasificado/lasalle-chico-1.html", report)
        self.assertIn("Precio estimado de cierre: USD 359,000–USD 370,000", report)
        self.assertNotIn("search", report.lower())

        empty = hunter.report(self.conn, self.config, "2026-09-25")
        self.assertEqual(empty, "NO CHANGE — nothing worth visiting today.")

    def test_act_now_requires_the_small_controlled_development_pattern(self):
        record = self.listing(units=None, controlled_entrance=None)
        evaluation = hunter.evaluate(record, self.config)
        self.assertFalse(hunter.is_act_now(record, evaluation, self.config))

    def test_negotiation_numbers_need_evidence_and_render_separately(self):
        with self.assertRaises(hunter.HunterError):
            hunter.normalise_record({
                "source": "Zonaprop",
                "url": "https://www.zonaprop.com.ar/example",
                "negotiation": {"realistic_target_usd": 350000},
            })
        record = self.listing(negotiation={
            "basis": "Verified comparable sales dated September 2026.",
            "probable_range_usd": [350000, 365000],
            "realistic_target_usd": 360000,
            "suggested_opening_offer_usd": 348000,
            "maximum_justified_price_usd": 365000,
        })
        negotiation = hunter.negotiation_report(record, self.config)
        self.assertIn("USD 350,000–USD 365,000", negotiation)
        self.assertIn("USD 348,000", negotiation)

    def test_high_fit_price_above_ceiling_is_investigated_not_promoted(self):
        record = self.listing(price_usd=390000, bedrooms=3, expenses_ars=180000)
        evaluation = hunter.evaluate(record, self.config)
        self.assertTrue(hunter.is_meaningful(record, evaluation, self.config))
        self.assertTrue(hunter.requires_price_path_investigation(record, self.config))
        self.assertFalse(hunter.is_act_now(record, evaluation, self.config))

    def test_explicit_hard_stops_block_an_alert(self):
        record = self.listing(garden_m2=100, units=20, expenses_ars=500000)
        evaluation = hunter.evaluate(record, self.config)
        self.assertTrue(evaluation["exclusions"])
        self.assertFalse(hunter.is_meaningful(record, evaluation, self.config))

    def test_baseline_negotiation_inference_matches_the_phone_alert_shape(self):
        record = self.listing(price_usd=365000)
        negotiation = hunter.negotiation_report(record, self.config)
        self.assertIn("USD 345,000–USD 355,000", negotiation)
        self.assertIn("USD 330,000–USD 335,000", negotiation)
        self.assertIn("Máximo justificado: USD 355,000", negotiation)


if __name__ == "__main__":
    unittest.main()
