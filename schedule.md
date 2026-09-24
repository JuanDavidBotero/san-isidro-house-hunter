Ejecutá San Isidro House Hunter en el proyecto local, nunca en un worktree aislado:
`data/house_hunter.sqlite3` es el historial acumulado que debe sobrevivir entre
pasadas.

Identificá la hora actual en America/Argentina/Buenos_Aires y elegí el modo:

1. **07:00 — pasada profunda.** Ejecutá `python3 hunter.py research-plan --mode deep`.
   Investigá los portales públicos (Mercado Libre, Zonaprop, Argenprop, Properati si
   es accesible), búsquedas indexadas, sitios de desarrolladores y las fuentes de
   `config/local_sources.json`. Buscá también "próximamente", ingresos nuevos,
   desarrollos y obra nueva. Para redes sociales, mirá sólo perfiles y posts públicos:
   nunca inicies sesión, sigas cuentas, envíes mensajes ni eludas límites.
2. **11:00 y 17:00 — pasada rápida de cambios.** Ejecutá
   `python3 hunter.py research-plan --mode change`. Buscá solamente altas desde la
   pasada anterior, cambios de precio y señales públicas de pre-market. No repitas un
   análisis profundo de todo el inventario.

En ambos modos:

- Abrí sólo la publicación original que sea pública. No hagas scraping, no sorteés
  robots, CAPTCHAs, logins, paywalls ni límites de tasa. Nunca inventes un dato.
- Para cada descubrimiento con posibilidad real, guardá un objeto JSONL con hechos y
  evidencia en `research/YYYY-MM-DD.jsonl`, según `research/README.md`. Importalo con
  `python3 hunter.py ingest --input research/YYYY-MM-DD.jsonl --quiet`.
- Compará primero con la historia. `UNCERTAIN` se revisa con `python3 hunter.py review`
  y sólo se resuelve tras comparar las páginas originales.
- Descartá sin alertar: jardín privado documentado menor a 120 m²; más de 8 unidades;
  expensas de ARS 500k o más; ubicación marcada como rechazada; PHs, dúplex ordinarios,
  condominios grandes, reformas mayores, calles de alto tránsito y jardines compartidos.
- Una casa Priority A extraordinaria hasta USD 410k sólo merece `🟡 INVESTIGAR PRECIO`.
  Verificá comparables, tiempo publicado, recortes previos y motivación antes de marcar
  un camino creíble por debajo de USD 380k.
- Hacé el análisis profundo únicamente de un candidato que pasó el filtro: dirección y
  micro-ubicación exactas, privado vs. común (jardín/pileta), unidades, acceso,
  expensas, estado, antigüedad/publicación, estacionamiento, y fuente/fecha de cada
  dato. Investigá seguridad, riesgo de inundación/sudestada en Bajo, reventa y
  comparables mediante fuentes públicas fiables. Diferenciá hechos, dichos del aviso e
  inferencias.
- Para un candidato de alerta, completá las cuatro salidas de negociación. Si no hay
  comparables, el sistema puede mostrar una inferencia inicial claramente rotulada;
  nunca la presentes como precio de cierre verificado.

Terminá con `python3 hunter.py report` y usá sólo esa salida para comunicar hallazgos.
Si devuelve `NO CHANGE — nothing worth visiting today.`, no envíes mensaje, resumen ni
notificación al usuario. Si devuelve una alerta, enviála de inmediato tal cual: no
mandes listados, planillas ni un resumen de casas descartadas.
