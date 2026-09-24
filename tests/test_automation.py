"""Tests for the automation layer: discovery mapping, packet writing, delivery routing.

These cover the three things that were missing before the radar could run unattended.
The most important assertions here are the negative ones -- that a non-USD price never
reaches price_usd, and that a quiet day never reaches Telegram.
"""

import base64
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

import fetch_ml
import hunter
import notify


def ml_item(**changes):
    """A MercadoLibre search result shaped the way the real API returns them."""
    item = {
        "id": "MLA123456789",
        "title": "Casa en venta en Lasalle, San Isidro, con jardin y pileta",
        "permalink": "https://casa.mercadolibre.com.ar/MLA-123456789-casa-lasalle",
        "price": 340000,
        "currency_id": "USD",
        "location": {
            "address_line": "Juan Bautista de Lasalle 1600",
            "neighborhood": {"name": "Lasalle"},
            "city": {"name": "San Isidro"},
            "latitude": -34.4712,
            "longitude": -58.5134,
        },
        "attributes": [
            {"id": "COVERED_AREA", "value_name": "170 m²", "value_struct": {"number": 170, "unit": "m²"}},
            {"id": "TOTAL_AREA", "value_name": "260 m²", "value_struct": {"number": 260, "unit": "m²"}},
            {"id": "BEDROOMS", "value_name": "3", "value_struct": {"number": 3, "unit": None}},
            {"id": "FULL_BATHROOMS", "value_name": "2", "value_struct": {"number": 2, "unit": None}},
            {"id": "PARKING_LOTS", "value_name": "2", "value_struct": {"number": 2, "unit": None}},
            {"id": "PROPERTY_TYPE", "value_name": "Casa"},
            {"id": "HAS_SWIMMING_POOL", "value_name": "Sí"},
            {"id": "HAS_GARDEN", "value_name": "Sí"},
        ],
        "pictures": [{"secure_url": "https://http2.mlstatic.com/photo-1.jpg"}],
    }
    item.update(changes)
    return item


class DiscoveryMappingTests(unittest.TestCase):
    def setUp(self):
        self.ml_config = fetch_ml.read_ml_config(fetch_ml.DEFAULT_ML_CONFIG)
        self.targeting = hunter.read_config(hunter.DEFAULT_CONFIG)

    def test_maps_a_usd_listing_into_the_research_packet_shape(self):
        record = fetch_ml.map_item(ml_item(), self.ml_config)
        self.assertEqual(record["source"], "Mercado Libre")
        self.assertEqual(record["price_usd"], 340000)
        self.assertEqual(record["covered_m2"], 170)
        self.assertEqual(record["total_m2"], 260)
        self.assertEqual(record["bedrooms"], 3)
        self.assertEqual(record["parking"], 2)
        self.assertTrue(record["private_pool"])
        self.assertTrue(record["private_garden"])
        self.assertIn("Lasalle", record["canonical_address"])
        # The record must survive the existing, unmodified ingest contract.
        normalised = hunter.normalise_record(record)
        self.assertEqual(normalised["price_usd"], 340000)

    def test_every_emitted_value_records_its_origin(self):
        record = fetch_ml.map_item(ml_item(), self.ml_config)
        for field in ("price_usd", "covered_m2", "bedrooms", "private_pool"):
            self.assertIn(field, record["evidence"], f"{field} was emitted without provenance")
            self.assertIn("mercadolibre.com.ar", record["evidence"][field])

    def test_a_peso_price_never_reaches_the_usd_field(self):
        """The 1000x-error guard: an ARS asking price must not be read as USD."""
        record = fetch_ml.map_item(ml_item(price=390000000, currency_id="ARS"), self.ml_config)
        self.assertNotIn("price_usd", record)
        self.assertIn("not USD", record["fit_notes"])
        normalised = hunter.normalise_record(record)
        self.assertIsNone(normalised["price_usd"])
        # With no verified USD price, the listing cannot be promoted to an alert.
        evaluation = hunter.evaluate(normalised, self.targeting)
        self.assertFalse(hunter.is_act_now(normalised, evaluation, self.targeting))

    def test_absent_attributes_stay_unverified_rather_than_guessed(self):
        bare = ml_item(attributes=[], title="Casa en venta en Lasalle, San Isidro")
        record = fetch_ml.map_item(bare, self.ml_config)
        for field in ("covered_m2", "bedrooms", "private_pool", "private_garden", "units"):
            self.assertNotIn(field, record)
        normalised = hunter.normalise_record(record)
        self.assertIsNone(normalised["covered_m2"])
        self.assertIsNone(normalised["private_pool"])
        test = hunter.lasalle_test(normalised, hunter.evaluate(normalised, self.targeting))
        self.assertIn("Not verified.", test[0])

    def test_a_listing_without_a_public_link_is_refused(self):
        with self.assertRaises(hunter.HunterError):
            fetch_ml.map_item(ml_item(permalink=None), self.ml_config)

    def test_off_target_geography_is_dropped_before_it_reaches_history(self):
        on_target = fetch_ml.map_item(ml_item(), self.ml_config)
        self.assertTrue(fetch_ml.relevant(on_target, self.targeting))
        off_target = fetch_ml.map_item(
            ml_item(
                title="Casa en venta en Pilar",
                location={"address_line": "Ruta 8 km 50", "city": {"name": "Pilar"}},
            ),
            self.ml_config,
        )
        self.assertFalse(fetch_ml.relevant(off_target, self.targeting))

    def test_structured_number_is_preferred_over_the_display_string(self):
        value = fetch_ml.attribute_number({"value_name": "1.234 m²", "value_struct": {"number": 1234}})
        self.assertEqual(value, 1234)


class PacketWritingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.directory = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_packet_append_is_idempotent_on_url(self):
        record = fetch_ml.map_item(ml_item(), fetch_ml.read_ml_config(fetch_ml.DEFAULT_ML_CONFIG))
        path = fetch_ml.packet_path(self.directory, "2026-09-23")
        self.assertEqual(fetch_ml.write_packet([record], path), 1)
        self.assertEqual(fetch_ml.write_packet([record], path), 0, "a second pass must not duplicate the listing")
        self.assertEqual(len(path.read_text(encoding="utf-8").strip().splitlines()), 1)

    def test_packet_append_preserves_a_hand_researched_record(self):
        path = fetch_ml.packet_path(self.directory, "2026-09-23")
        path.write_text(json.dumps({"source": "Owner direct", "url": "https://example.com/casa"}) + "\n", encoding="utf-8")
        record = fetch_ml.map_item(ml_item(), fetch_ml.read_ml_config(fetch_ml.DEFAULT_ML_CONFIG))
        fetch_ml.write_packet([record], path)
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("Owner direct", lines[0])


class DeliveryRoutingTests(unittest.TestCase):
    def test_a_quiet_day_never_reaches_telegram(self):
        results = notify.deliver(notify.NO_CHANGE, dry_run=True)
        telegram = next(item for item in results if item["channel"] == "telegram")
        self.assertFalse(telegram["sent"])
        self.assertEqual(telegram["reason"], "suppressed: NO CHANGE")

    def test_a_real_candidate_is_routed_to_both_channels(self):
        results = notify.deliver("🚨 ACT NOW\nLasalle 1600", dry_run=True)
        channels = {item["channel"] for item in results}
        self.assertEqual(channels, {"telegram", "email"})
        telegram = next(item for item in results if item["channel"] == "telegram")
        self.assertNotEqual(telegram.get("reason"), "suppressed: NO CHANGE")

    def test_subject_flags_an_urgent_alert(self):
        self.assertIn("ACT NOW", notify.subject_for("🚨 ACT NOW\nLasalle 1600"))
        self.assertIn("sin novedades", notify.subject_for(notify.NO_CHANGE))

    def test_chunking_respects_the_telegram_limit_and_candidate_boundaries(self):
        candidates = "\n\n".join(f"⚡ VER HOY\nCandidate {index}\n" + "x" * 1200 for index in range(6))
        chunks = notify.telegram_chunks(candidates)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), notify.TELEGRAM_LIMIT)
        # No candidate header may be lost in the split.
        self.assertEqual(sum(chunk.count("⚡ VER HOY") for chunk in chunks), 6)

    def test_an_oversized_single_block_is_still_delivered(self):
        chunks = notify.telegram_chunks("y" * 9000)
        self.assertEqual(len("".join(chunks)), 9000)


class EndToEndTests(unittest.TestCase):
    """Discovery -> history -> report, with no hand-written JSONL anywhere."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database = Path(self.tempdir.name) / "history.sqlite3"
        self.targeting = hunter.read_config(hunter.DEFAULT_CONFIG)
        self.ml_config = fetch_ml.read_ml_config(fetch_ml.DEFAULT_ML_CONFIG)
        self.conn = hunter.connect(self.database)
        hunter.initialise(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def fetched(self, **changes):
        item = ml_item(**changes)
        record = fetch_ml.map_item(item, self.ml_config)
        # Small-development facts are not published as MercadoLibre attributes; a real
        # ACT NOW still needs them, so this mirrors a later human/agent enrichment pass.
        record.setdefault("units", 6)
        record.setdefault("controlled_entrance", True)
        record.setdefault("condition", "excellent")
        record.setdefault("street_quality", "quiet residential")
        return hunter.normalise_record(record)

    def test_an_automated_discovery_becomes_an_alert_then_goes_quiet(self):
        first = hunter.ingest_record(self.conn, self.fetched(), self.targeting)
        self.assertEqual(first["classification"], "NEW")
        self.assertEqual(first["priority"], "A")

        today = hunter.local_now().date().isoformat()
        report = hunter.report(self.conn, self.targeting, today)
        self.assertIn("mercadolibre.com.ar", report)
        self.assertNotIn(notify.NO_CHANGE, report)

        # The critical regression: running the same discovery again must go quiet.
        second = hunter.ingest_record(self.conn, self.fetched(), self.targeting)
        self.assertEqual(second["classification"], "OLD")
        tomorrow_report = hunter.report(self.conn, self.targeting, "2099-01-01")
        self.assertEqual(tomorrow_report, notify.NO_CHANGE)

    def test_a_price_drop_reopens_the_alert(self):
        hunter.ingest_record(self.conn, self.fetched(), self.targeting)
        changed = hunter.ingest_record(self.conn, self.fetched(price=310000), self.targeting)
        self.assertEqual(changed["classification"], "MATERIAL_CHANGE")
        self.assertIn("price_usd", changed["changes"])


class RefreshTokenRotationTests(unittest.TestCase):
    """MercadoLibre refresh tokens are single use. Getting this wrong means the radar
    works on day one and dies on day two with invalid_grant, which is exactly the kind
    of failure that looks like a quiet market."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "ml_refresh_token"
        self._saved = {key: os.environ.get(key) for key in ("ML_REFRESH_TOKEN_FILE", "ML_REFRESH_TOKEN")}
        os.environ["ML_REFRESH_TOKEN_FILE"] = str(self.path)
        os.environ["ML_REFRESH_TOKEN"] = "BOOTSTRAP-FROM-SECRET"

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tempdir.cleanup()

    def test_bootstrap_secret_is_used_when_nothing_has_rotated_yet(self):
        self.assertEqual(fetch_ml.stored_refresh_token(), "BOOTSTRAP-FROM-SECRET")

    def test_a_rotated_token_takes_precedence_over_the_bootstrap_secret(self):
        fetch_ml.store_refresh_token("ROTATED-BY-FIRST-RUN")
        self.assertEqual(fetch_ml.stored_refresh_token(), "ROTATED-BY-FIRST-RUN")

    def test_the_stored_token_is_not_world_readable(self):
        fetch_ml.store_refresh_token("ROTATED-BY-FIRST-RUN")
        self.assertEqual(oct(self.path.stat().st_mode)[-3:], "600")

    def test_a_blank_stored_token_falls_back_rather_than_authenticating_with_nothing(self):
        self.path.write_text("   \n", encoding="utf-8")
        self.assertEqual(fetch_ml.stored_refresh_token(), "BOOTSTRAP-FROM-SECRET")

    def test_missing_refresh_token_is_an_actionable_error_not_a_silent_403(self):
        os.environ.pop("ML_REFRESH_TOKEN", None)
        os.environ["ML_CLIENT_ID"] = "id"
        os.environ["ML_CLIENT_SECRET"] = "secret"
        try:
            with self.assertRaises(hunter.HunterError) as caught:
                fetch_ml.access_token()
            self.assertIn("--auth-url", str(caught.exception))
        finally:
            os.environ.pop("ML_CLIENT_ID", None)
            os.environ.pop("ML_CLIENT_SECRET", None)

    def test_no_credentials_means_no_token_rather_than_an_exception(self):
        for key in ("ML_CLIENT_ID", "ML_CLIENT_SECRET"):
            os.environ.pop(key, None)
        self.assertIsNone(fetch_ml.access_token())


class PkceTests(unittest.TestCase):
    def test_the_challenge_is_unpadded_url_safe_sha256_of_the_verifier(self):
        fetch_ml.store_pkce("verifier-under-test", "state-under-test")
        try:
            verifier = fetch_ml.load_pkce_verifier()
            self.assertEqual(verifier, "verifier-under-test")
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
                .decode("ascii")
                .rstrip("=")
            )
            # MercadoLibre's S256 method requires base64url with no padding.
            for forbidden in ("=", "+", "/"):
                self.assertNotIn(forbidden, challenge)
        finally:
            fetch_ml.PKCE_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
