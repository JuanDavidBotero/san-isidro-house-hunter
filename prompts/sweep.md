Run the San Isidro House Hunter discovery pass.

You are stateless. You cannot see previous runs, and you must not skip a listing because
you assume it was already reported. The consuming program owns the history and decides
what is NEW, MATERIAL_CHANGE, OLD or DUPLICATE. Your job is coverage plus evidence.

Spend the record budget in this order: Priority A pre-market, then Priority A listed in
USD at or under the ceiling, then Priority A with a price reduction or other visible
change, then Priority B only when the property is genuinely exceptional.

## Language

Search in Spanish. This is the Buenos Aires market and the listings are in Spanish, so
Spanish queries are the only ones that return real inventory. Try both accented and
unaccented spellings, because listings are inconsistent: jardin/jardín, banos/baños,
Nautico/Náutico, proximamente/próximamente.

## Where to search

Priority A micro-areas, deeply. These are the target:

- alrededores del Club Náutico San Isidro
- Holy Cross / Colegio Holy Cross
- Bajo San Isidro
- Lasalle y Juan Bautista de Lasalle
- San Isidro Chico
- Los Cardos
- calles residenciales hacia Béccar

Priority B only when the property is genuinely exceptional: Lomas de San Isidro,
Béccar (zona de vías), La Horqueta.

Start with the named local agencies, public pages only. They are the channel most
likely to publish a Lasalle Chico type house before the portals do, and their own pages
often keep the unit count and the expenses that portal syndication strips out. Open the
sale inventory section ("Propiedades", "Venta", "Casas") and read the San Isidro and
Béccar house detail pages rather than trusting Google's index of them:

- Rene Martin Propiedades, https://www.renemartinprop.com/ (San Isidro / Lomas)
- Zárate Gestión Inmobiliaria, https://zaratepropiedades.ar/ (San Isidro / Zona Norte)
- Morel Inmobiliaria, https://www.morel.ar/ (San Isidro / Martínez)
- NARVAEZ Inmobiliaria, https://www.narvaez.com.ar/ (San Isidro / Zona Norte)
- Varela Kramer, https://www.varelakramer.com/ (San Isidro / Zona Norte)

If an agency site will not render its inventory without JavaScript, or its detail pages
404, name the agency in "skipped". Do not substitute a portal listing and attribute it
to the agency.

Then the portals, scoped by domain so results are detail pages and not landing pages:

- site:zonaprop.com.ar
- site:argenprop.com
- site:properati.com.ar
- site:inmuebles.mercadolibre.com.ar, site:casa.mercadolibre.com.ar

Then developer and small-builder pages for emprendimientos of few units, and public
owner-direct posts.

## Query patterns that actually work

A Buenos Aires listing never says "small private development". It says: casa en
condominio, complejo de casas, complejo de 6 casas, condominio de 4 unidades, solo 5
viviendas, barrio chico, mini barrio, acceso controlado, portón automático, expensas
bajas. The rest of the pattern reads as jardín propio, jardín privado, parque propio,
pileta propia, apto pileta, con lugar para pileta, quincho, parrilla, galería, lote
propio, 3 dormitorios, 2 dormitorios más playroom, cochera doble, m2 cubiertos, a
estrenar, recién reciclada, impecable.

Combine a micro-area with the product, in Spanish:

- "casa en venta" + San Isidro + "jardín" + "pileta"
- "casa en venta" + Béccar + "jardín propio"
- casa + "Bajo San Isidro" + pileta
- casa + "San Isidro" + "acceso controlado"
- casa + "San Isidro" + "complejo de 6 casas" (also 4, 5, 8 casas)
- casa + "San Isidro" + "casa en condominio" / "barrio chico"
- casa + "San Isidro" + "expensas bajas"
- casa + "San Isidro" + "a estrenar" + jardín
- casa + "San Isidro" + "150 m2 cubiertos" / "3 dormitorios"
- "dueño directo" + casa + San Isidro + jardín

Do not run two near-identical variants of the same query. Spend the budget on coverage.

Pre-market matters most. A property found before it is widely listed is the whole point:

- "próximamente" + casa + San Isidro
- "nuevo ingreso" / "ingreso" / lanzamiento + casa + San Isidro
- preventa / "en pozo" / emprendimiento / "obra nueva" / "en construcción" +
  San Isidro + casas
- nuevos desarrollos de pocas unidades en las micro-zonas Priority A
- the public Instagram and Facebook pages of the agencies listed above, where a
  "nuevo ingreso" post often precedes the portal listing by days

When the source presents the property as coming soon, set market_stage to "pre-market"
and quote the coming-soon wording as its evidence. If a public social post is itself the
publication, use the permanent post permalink as the url, never the profile or feed URL.

## Reading a listing

Open the original listing page before recording any fact. Search-result snippets
truncate and sometimes describe a different unit in the same complex, which is where
wrong m2 and wrong prices come from. If you could not open the page, the listing goes in
"skipped", not into a record built from the snippet.

Watch these specific traps:

- "3 ambientes" counts rooms, not bedrooms. A 3-ambientes is usually 2 bedrooms.
  Record what the listing actually states and do not convert one into the other.
- "$" alone means pesos. "USD" or "U$S" means dollars. Only a dollar-denominated
  asking price goes in price_usd.
- m2 cubiertos, m2 semicubiertos and m2 totales are three different numbers. Never let
  one stand in for another, and never add them yourself.
- jardín común and parque común are not a private garden. Record the shared wording as
  the fact it is; a shared garden is a rejection and worth knowing early.

Give the canonical listing url with its listing id and no query string or fragment
(no ?searchVariation=, no #position=). Tracking parameters break identity matching
between runs.

## The facts that decide whether an alert fires

These are worth a second fetch of the complex or emprendimiento page when the listing
itself is vague:

1. units, the number of homes in the development. The highest-value single fact and the
   one portals bury most often. Look for "complejo de N casas", "N unidades", "solo N
   viviendas", the developer page, or the site-plan caption.
2. controlled_entrance.
3. private_garden, plus garden_m2 when stated.
4. private_pool, or pool_potential when the page says the lot admits a pool.
5. covered_m2, kept separate from total_m2.
6. price_usd and expenses_ars.
7. condition, in the listing's own words.

Also capture when stated: publication_date or the "publicado hace" wording,
listing_state for badges such as "bajó de precio", reservado, vendido, plus
street_quality, orientation, bathrooms, parking.

Record the negatives honestly when the page supports them: high_traffic,
large_development, needs_major_renovation, security_concern, flood_risk. In Bajo San
Isidro, flood and sudestada exposure is a real question, but set flood_risk only from an
actual statement, never from the address alone.

## Make the record survive the downstream filters

- zone decides whether the record is kept at all. The consuming program looks for a
  Priority A or B micro-area term in canonical_address, zone or description, and nothing
  else. A perfect find whose only location wording is "San Isidro" or "Béccar" is
  discarded before anyone sees it. So copy the micro-area wording the page itself
  publishes (breadcrumb, title, bloque de ubicación, map caption, complex name) into
  zone. Copy it, never upgrade it: if the page never names the micro-area, keep the
  generic wording and say in fit_notes that the micro-area is not verified.
- Numbers must be bare JSON numbers. 350000, not "USD 350.000", not "350.000 U$S", not
  "300000-320000". One non-numeric value aborts the import of the whole packet and loses
  every other find in this run. If a price or area is published as a range or as
  "consultar", omit the field and quote the wording in fit_notes.
- Booleans are true, false, or omitted. Never a string, never "probably".
- photo_urls: two or three absolute https:// gallery image URLs. Relative paths and
  data: URIs also abort the whole import.
- description: a short factual extract, two to four sentences, in the listing's own
  words, keeping the location, unit count, garden and pool wording. Do not paste the
  whole page and do not rewrite it as your own marketing prose.
- distinctive_features: two to four specific details such as the complex name, "galería
  con parrilla", "doble altura en el living". Generic words like casa, jardín or pileta
  are ignored by the matcher and wasted here.
- property_type: the listing's own category wording. If it says PH or dúplex, write
  that. Those categories are penalised deliberately; softening the wording smuggles a
  rejected property into the alerts.

## Cross-posting

The same house is routinely posted on Zonaprop, Argenprop and Mercado Libre by
different brokers, at different prices, with the same photos. The program collapses
those into one alert, but only from what you supply: canonical_address, broker,
broker_phone, photo_urls and distinctive_features. Capture those whenever the page shows
them, and report each posting you actually read rather than silently picking one.

## Budget and honesty

Roughly fifteen to twenty distinct searches, then open the most promising detail pages
and read them properly. Depth beats breadth: one fully evidenced Priority A complex of
six houses with a private pool is worth more than ten thin records.

A listing priced only in pesos will almost certainly be filtered out downstream, so
spend the budget on USD-priced or pre-market candidates and record an ARS-only listing
only when it is an exceptional Priority A fit.

Report only what you could actually read. Anything a site refused to serve belongs in
"skipped", named: which site, and why (bot protection, login wall, JavaScript-only
inventory, timeout, no public inventory page). A named gap is useful, because it tells
the family which channel to cover by phone. List the queries and sites you actually ran
in "searched".
