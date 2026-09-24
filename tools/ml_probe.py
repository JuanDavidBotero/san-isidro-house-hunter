"""Map which MercadoLibre endpoints this token can actually reach.

One access token, reused across every probe, so we do not churn the single-use
refresh token more than once.
"""
import json, urllib.error, urllib.parse, urllib.request
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import fetch_ml

token = fetch_ml.access_token()
print("token acquired:", bool(token), "\n")

UA = "san-isidro-house-hunter/1.0 (personal home search)"

def probe(label, path, params=None, auth=True):
    url = f"https://api.mercadolibre.com{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json", "User-Agent": UA}
    if auth:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode())
            note = ""
            if isinstance(body, dict):
                if "paging" in body:
                    note = f"total={body['paging'].get('total')}"
                elif "results" in body:
                    note = f"results={len(body.get('results') or [])}"
                elif "id" in body:
                    note = f"id={body['id']}"
                elif "nickname" in body:
                    note = f"user={body['nickname']}"
            elif isinstance(body, list):
                note = f"list len={len(body)}"
            print(f"  200  {label:44} {note}")
            return body
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(raw).get("message") or json.loads(raw).get("error")
        except Exception:
            msg = raw[:70]
        print(f"  {e.code}  {label:44} {str(msg)[:70]}")
    except Exception as e:
        print(f"  ERR  {label:44} {e}")
    return None

print("== identity / metadata ==")
me = probe("GET /users/me", "/users/me")
# Read the account id rather than hardcoding it, so this tool is portable and no
# personal identifier lives in the repo.
USER_ID = str((me or {}).get("id") or "")
probe("GET /sites/MLA", "/sites/MLA")
probe("GET /categories/MLA1459 (Inmuebles)", "/categories/MLA1459")
probe("GET /categories/MLA1459 (no auth)", "/categories/MLA1459", auth=False)

print("\n== generic item search (the blocked one) ==")
probe("search category=MLA1459", "/sites/MLA/search", {"category": "MLA1459", "limit": 1})
probe("search q=casa only", "/sites/MLA/search", {"q": "casa san isidro", "limit": 1})
probe("search nickname scope", "/sites/MLA/search", {"seller_id": USER_ID, "limit": 1})
probe("search (no auth header)", "/sites/MLA/search", {"q": "casa", "limit": 1}, auth=False)

print("\n== real-estate / VIS specific surfaces ==")
probe("GET /classifieds_promotion_packs/MLA", "/classifieds_promotion_packs/MLA")
probe("GET /users/<self>/items/search", f"/users/{USER_ID}/items/search", {"limit": 1})
probe("GET /sites/MLA/domain_discovery/search", "/sites/MLA/domain_discovery/search", {"q": "casa"})
probe("GET /trends/MLA/MLA1459", "/trends/MLA/MLA1459")

print("\n== single item lookup (for enrichment) ==")
probe("GET /items/MLA1234567890 (nonexistent, shape test)", "/items/MLA1234567890")
