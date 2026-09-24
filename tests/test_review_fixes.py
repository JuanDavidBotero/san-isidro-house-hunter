"""Regression tests for the findings from the adversarial review of the sweep.

Each test here corresponds to a confirmed, reproduced defect. They are written as
negative assertions: the fabrication channel must stay closed, and the failure must
stay loud.
"""

import json
import tempfile
import unittest
from pathlib import Path

import hunter
import sweep


def record(**changes):
    base = {
        "source": "Zonaprop",
        "url": "https://www.zonaprop.com.ar/propiedades/clasificado/casa-lasalle-123.html",
        "canonical_address": "Juan Bautista de Lasalle 1600, San Isidro",
        "price_usd": 340000,
        "evidence": {
            "canonical_address": "Listing header reads 'Juan Bautista de Lasalle 1600, San Isidro'.",
            "price_usd": "Listing states 'USD 340.000'.",
        },
    }
    base.update(changes)
    return base


class NegotiationCannotComeFromTheSweep(unittest.TestCase):
    """Finding 1: the negotiation block bypassed the guard entirely, and its invented
    figures printed under a label asserting they were verified."""

    def test_a_supplied_negotiation_block_is_stripped(self):
        cleaned, dropped = sweep.enforce_evidence(record(negotiation={
            "basis": "comparables de la cuadra y 90 dias en el mercado",
            "probable_range_usd": [298000, 312000],
            "realistic_target_usd": 305000,
            "suggested_opening_offer_usd": 292000,
            "maximum_justified_price_usd": 318000,
        }))
        self.assertNotIn("negotiation", cleaned)
        self.assertIn("negotiation", dropped)

    def test_even_with_an_evidence_entry_negotiation_is_still_stripped(self):
        """Evidence for a negotiation estimate is model-authored too, so allowing it
        through on that basis would reopen the same hole."""
        cleaned, _ = sweep.enforce_evidence(record(
            negotiation={"basis": "x" * 40, "realistic_target_usd": 305000},
            evidence={**record()["evidence"], "negotiation": "a" * 40},
        ))
        self.assertNotIn("negotiation", cleaned)

    def test_the_alert_never_claims_a_negotiation_basis_is_verified(self):
        estimate = {
            "basis": "Comparables verificados de septiembre 2026.",
            "probable_range_usd": [350000, 365000],
            "realistic_target_usd": 360000,
            "suggested_opening_offer_usd": 348000,
            "maximum_justified_price_usd": 365000,
        }
        text = hunter.negotiation_report(
            hunter.normalise_record({**record(), "negotiation": estimate}),
            hunter.read_config(hunter.DEFAULT_CONFIG),
        )
        self.assertIn("no verificada", text)
        self.assertNotIn("Base verificada", text)


class LocationClaimsNeedEvidence(unittest.TestCase):
    """Finding 2: canonical_address, zone and description decide Priority A, which is a
    hard requirement for an urgent alert."""

    def test_an_unevidenced_address_is_dropped(self):
        cleaned, dropped = sweep.enforce_evidence(
            record(evidence={"price_usd": "Listing states 'USD 340.000'."})
        )
        self.assertNotIn("canonical_address", cleaned)
        self.assertIn("canonical_address", dropped)

    def test_an_invented_proximity_claim_cannot_create_priority_a(self):
        """The exact reproduction from the review: an unevidenced description sentence
        was enough to make a record Priority A and reach the packet."""
        raw = record(description="A 3 cuadras del Club Nautico San Isidro.",
                     zone="Lasalle Chico",
                     canonical_address="Una calle sin nombre, Zona Norte")
        # No evidence for description, zone, or address.
        raw["evidence"] = {"price_usd": "Listing states 'USD 340.000'."}
        cleaned, _ = sweep.enforce_evidence(raw)
        for field in ("description", "zone", "canonical_address"):
            self.assertNotIn(field, cleaned)
        normalised = hunter.normalise_record({**cleaned, "url": raw["url"], "source": "Zonaprop"})
        priority, _ = hunter.infer_priority(normalised, hunter.read_config(hunter.DEFAULT_CONFIG))
        self.assertIsNone(priority)

    def test_a_record_losing_its_address_is_rejected_entirely(self):
        payload = {"listings": [record(evidence={"price_usd": "Listing states 'USD 340.000'."})],
                   "skipped": [], "searched": []}
        kept, counts = sweep.validate_records(payload, verbose=False)
        self.assertEqual(kept, [])
        self.assertEqual(counts["no_address"], 1)


class DedupSignalsNeedEvidence(unittest.TestCase):
    """Findings 3 and 4: the fuzzy-match signals and source_listing_id were outside the
    guard, so a fabricated record could be merged into a real listing's history."""

    def test_dedup_signals_without_evidence_are_dropped(self):
        cleaned, dropped = sweep.enforce_evidence(record(
            broker="Example Brokers",
            broker_phone="+54 11 5555 1234",
            photo_urls=["https://img.example.com/a.jpg"],
            distinctive_features=["galeria cubierta"],
            description="Casa en complejo de 6 unidades.",
        ))
        for field in ("broker", "broker_phone", "photo_urls", "distinctive_features", "description"):
            self.assertNotIn(field, cleaned, f"{field} is a dedup signal and needs evidence")
            self.assertIn(field, dropped)

    def test_source_listing_id_is_never_accepted_from_a_sweep(self):
        cleaned, dropped = sweep.enforce_evidence(record(source_listing_id="58499557"))
        self.assertNotIn("source_listing_id", cleaned)
        self.assertIn("source_listing_id", dropped)

    def test_a_stripped_record_cannot_reach_the_fuzzy_merge_threshold(self):
        """With the dedup signals gone, a fabricated record cannot score high enough to
        be merged into an unrelated listing (hunter merges at >= 0.85)."""
        real = hunter.normalise_record({
            "source": "Zonaprop",
            "url": "https://www.zonaprop.com.ar/propiedades/clasificado/real-house-1.html",
            "canonical_address": "Otra calle 500, San Isidro",
            "broker": "Example Brokers",
            "broker_phone": "+54 11 5555 1234",
            "photo_urls": ["https://img.example.com/a.jpg"],
            "covered_m2": 170,
            "bedrooms": 3,
        })
        forged, _ = sweep.enforce_evidence(record(
            broker="Example Brokers",
            broker_phone="+54 11 5555 1234",
            photo_urls=["https://img.example.com/a.jpg"],
            covered_m2=170,
            bedrooms=3,
        ))
        forged_norm = hunter.normalise_record(forged)
        score, _ = hunter.fuzzy_score(forged_norm, real)
        self.assertLess(score, 0.85, "a zero-evidence record must not reach the merge threshold")


class EvidenceQualityTests(unittest.TestCase):
    """Finding 9: the guard checked that an evidence KEY existed, not that the value was
    plausibly a quote."""

    def test_a_one_character_quote_is_not_evidence(self):
        cleaned, dropped = sweep.enforce_evidence(record(
            covered_m2=170, evidence={**record()["evidence"], "covered_m2": "x"}
        ))
        self.assertNotIn("covered_m2", cleaned)
        self.assertIn("covered_m2", dropped)

    def test_echoing_the_value_back_is_not_evidence(self):
        cleaned, _ = sweep.enforce_evidence(record(
            covered_m2=170, evidence={**record()["evidence"], "covered_m2": "170"}
        ))
        self.assertNotIn("covered_m2", cleaned)

    def test_a_real_quote_passes(self):
        cleaned, _ = sweep.enforce_evidence(record(
            covered_m2=170,
            evidence={**record()["evidence"], "covered_m2": "Aviso dice '170 m2 cubiertos'."},
        ))
        self.assertEqual(cleaned["covered_m2"], 170)


class MalformedValuesCannotPoisonThePacket(unittest.TestCase):
    """Finding 5: one malformed field aborted the entire ingest, and because the packet
    is appended to, the poison persisted across every later run."""

    def test_a_non_numeric_price_is_dropped_not_propagated(self):
        cleaned, dropped = sweep.coerce_types({"price_usd": "300000-320000", "bedrooms": 3})
        self.assertNotIn("price_usd", cleaned)
        self.assertIn("price_usd", dropped)
        self.assertEqual(cleaned["bedrooms"], 3)

    def test_a_non_boolean_flag_is_dropped(self):
        cleaned, dropped = sweep.coerce_types({"private_pool": "maybe"})
        self.assertNotIn("private_pool", cleaned)
        self.assertIn("private_pool", dropped)

    def test_a_malformed_list_is_dropped(self):
        cleaned, dropped = sweep.coerce_types({"photo_urls": "not-a-list"})
        self.assertNotIn("photo_urls", cleaned)
        self.assertIn("photo_urls", dropped)

    def test_a_surviving_record_always_normalises(self):
        payload = {"listings": [record(price_usd="300000-320000", covered_m2="unos 170")],
                   "skipped": [], "searched": []}
        kept, counts = sweep.validate_records(payload, verbose=False)
        self.assertEqual(len(kept), 1)
        self.assertGreaterEqual(counts["bad_types_dropped"], 1)
        # The whole point: this must not raise.
        hunter.normalise_record(kept[0])

    def test_ingest_failure_names_the_offending_packet_line(self):
        with tempfile.TemporaryDirectory() as directory:
            packet = Path(directory) / "packet.jsonl"
            packet.write_text(
                json.dumps({"source": "A", "url": "https://x/a", "canonical_address": "A"}) + "\n"
                + json.dumps({"source": "B", "url": "https://x/b", "price_usd": "no-es-numero"}) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(hunter.HunterError) as caught:
                list(hunter.load_packet(packet))
            self.assertIn(":2:", str(caught.exception), "the error must name the bad line")


class UrlClassificationTests(unittest.TestCase):
    """Finding 11: the marker list missed the real search-URL shapes, and a naive
    blocklist risks discarding genuine listing pages."""

    def test_real_original_listing_urls_are_kept(self):
        for good in [
            "https://www.zonaprop.com.ar/propiedades/clasificado/casa-lasalle-123.html",
            "https://www.argenprop.com/casa-en-venta-en-san-isidro--12345678",
            "https://casa.mercadolibre.com.ar/MLA-1234567890-casa-lasalle",
            "https://inmuebles.mercadolibre.com.ar/MLA-987654321-casa",
        ]:
            self.assertFalse(sweep.is_search_url(good), f"wrongly rejected {good}")

    def test_search_and_index_urls_are_rejected(self):
        for bad in [
            "https://www.zonaprop.com.ar/casas-venta-san-isidro.html",
            "https://www.argenprop.com/casas/venta/san-isidro",
            "https://www.zonaprop.com.ar/casas-venta-beccar-orden-publicado-descendente.html",
            "https://inmuebles.mercadolibre.com.ar/casas/venta/san-isidro/",
            "https://example.com/buscar?q=casa+san+isidro",
            "https://example.com/listado/inmuebles",
        ]:
            self.assertTrue(sweep.is_search_url(bad), f"wrongly accepted {bad}")


class TimestampTests(unittest.TestCase):
    """Finding 10: observed_at was setdefault, so a model-supplied past date hid the
    record from every report."""

    def test_a_supplied_past_date_is_overwritten(self):
        payload = {"listings": [record(observed_at="2020-01-01T00:00:00-03:00")],
                   "skipped": [], "searched": []}
        kept, _ = sweep.validate_records(payload, verbose=False)
        self.assertNotIn("2020", kept[0]["observed_at"])
        self.assertTrue(kept[0]["observed_at"].startswith(hunter.local_now().date().isoformat()[:4]))


class ToolRestrictionTests(unittest.TestCase):
    """Finding 8: --permission-mode bypassPermissions approves everything, which made the
    --allowedTools list decorative and granted Bash/Write to an unattended agent."""

    def test_the_command_never_bypasses_permissions(self):
        command = sweep.build_command("/usr/bin/claude", "prompt text")
        self.assertNotIn("bypassPermissions", command)
        self.assertNotIn("--permission-mode", command)
        self.assertNotIn("--dangerously-skip-permissions", command)

    def test_only_the_two_web_tools_are_allowed(self):
        command = sweep.build_command("/usr/bin/claude", "prompt text")
        allowed = command[command.index("--allowedTools") + 1]
        self.assertEqual(sorted(allowed.split(",")), ["WebFetch", "WebSearch"])

    def test_the_dangerous_tools_are_explicitly_denied(self):
        command = sweep.build_command("/usr/bin/claude", "prompt text")
        denied = command[command.index("--disallowedTools") + 1].split(",")
        for tool in ("Bash", "Write", "Edit", "NotebookEdit"):
            self.assertIn(tool, denied, f"{tool} must be denied to an unattended agent")

    def test_the_diagnostic_shares_the_same_policy(self):
        """The --check-tools path once carried its own weaker flags, which is exactly how
        a permission policy drifts apart from the code that documents it."""
        source = Path(sweep.__file__).read_text(encoding="utf-8")
        # Strip comments: the fix is deliberately explained in a comment that names the
        # flag it removed, and that explanation should not trip this check.
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotIn("--permission-mode", code, "no invocation may set a permission mode")
        self.assertGreaterEqual(
            code.count("build_command("), 3,
            "both the sweep and the diagnostic should build their command through the "
            "shared helper",
        )


class PromptScopeTests(unittest.TestCase):
    """Finding 6: the prompt advertised `negotiation` without its mandatory `basis`, so
    the model could emit a shape that aborted the whole packet."""

    def test_the_prompt_tells_the_model_not_to_price(self):
        prompt = sweep.build_prompt(sweep.DEFAULT_PROMPT, max_results=12)
        self.assertIn("Do NOT estimate these", prompt)
        self.assertIn('DO NOT emit "negotiation"', prompt)

    def test_forbidden_fields_are_not_advertised_in_the_optional_list(self):
        prompt = sweep.build_prompt(sweep.DEFAULT_PROMPT, max_results=12)
        # They may appear in the buyer brief and in the prohibition, but never as a
        # bare quoted schema entry inviting the model to fill them.
        self.assertNotIn('"source_listing_id",', prompt)


if __name__ == "__main__":
    unittest.main()
