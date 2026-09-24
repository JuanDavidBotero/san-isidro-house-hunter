# Research packet format

The radar deliberately does **not** scrape portals. A Codex run researches publicly
accessible original listing pages, then saves one JSON object per discovery in a
`research/YYYY-MM-DD.jsonl` packet. Import the packet with:

```sh
python3 hunter.py ingest --input research/YYYY-MM-DD.jsonl
```

The source URL must be the original public listing page, never a search-result URL.
Unknown values are omitted or set to `null`; do not guess.

```json
{
  "source": "Zonaprop",
  "url": "https://www.zonaprop.com.ar/propiedades/clasificado/...html",
  "source_listing_id": "58499557",
  "canonical_address": "Juan Bautista de Lasalle 1600, San Isidro",
  "price_usd": 380000,
  "covered_m2": 170,
  "total_m2": 240,
  "bedrooms": 2,
  "bathrooms": 2,
  "parking": 2,
  "units": 6,
  "expenses": "ARS 185000",
  "expenses_ars": 185000,
  "private_garden": true,
  "private_pool": true,
  "controlled_entrance": true,
  "condition": "excellent",
  "property_type": "house in small condominium",
  "street_quality": "quiet residential",
  "description": "Short factual extraction from the public listing.",
  "broker": "Agency or agent name",
  "broker_phone": null,
  "photo_urls": ["https://.../photo.jpg"],
  "distinctive_features": ["two levels", "covered gallery"],
  "publication_date": null,
  "market_stage": "listed",
  "observed_at": "2026-09-20T10:30:00-03:00",
  "evidence": {
    "private_pool": "Listing description says 'jardín con pileta'.",
    "units": "Listing describes a complex of six units."
  },
  "risks": ["Monthly expenses need confirmation."],
  "negotiation_notes": null,
  "negotiation": {
    "basis": "Verified broker-provided comparable sales and days-on-market evidence.",
    "probable_range_usd": [350000, 365000],
    "realistic_target_usd": 360000,
    "suggested_opening_offer_usd": 348000,
    "maximum_justified_price_usd": 365000
  }
}
```

Additional supported facts are `pool_potential`, `garden_m2`, `orientation`,
`owner_direct`, `listing_state`, `exceptional_layout`, `credible_price_path`,
`high_traffic`, `large_development`, `needs_major_renovation`,
`security_concern`, `flood_risk`, and `micro_location_rejected`. Use
`market_stage: "pre-market"` only for a source explicitly presenting the property as
coming soon, upcoming, or equivalent. `credible_price_path` must only be set when
the packet includes factual support in `evidence` or `negotiation_notes`.

`negotiation` is optional. When supplied, every amount needs a factual `basis`—for
example, dated comparable-sale evidence or verified days on market. The radar will
otherwise show all four negotiation values as not estimated rather than invent a
discount from the asking price.
