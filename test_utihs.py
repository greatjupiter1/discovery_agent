import importlib.util, json, pathlib

BASE = pathlib.Path.home()/"workspace/discovery-agent"
def load_mod(name, fname):
    spec = importlib.util.spec_from_file_location(name, BASE/fname)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

ba = load_mod("build_article", "build_article.py")
pt = load_mod("propose_topics", "propose_topics.py")
passed = failed = 0
def check(label, cond):
    global passed, failed
    if cond: passed += 1; print(f"PASS {label}")
    else: failed += 1; print(f"FAIL {label}")

# ------------------------------------------------------------- registry
raw = json.loads((BASE/"utihs_registry.json").read_text(encoding="utf-8"))
check("registry has exactly 1 portal entry", len(raw) == 1)
need = {"name", "url", "type", "fields", "languages", "api", "notes",
        "verified", "verified_date"}
check("registry entry carries the registry schema",
      all(need <= set(e) for e in raw))
e0 = raw[0]
check("entry is the UTIHS portal",
      e0["url"] == "https://utihs.ut.ac.ir" and e0["type"] == "platform")
check("entry verified 2026-11-26",
      e0["verified"] is True and e0["verified_date"] == "2026-11-26")
check("languages are Persian + English",
      e0["languages"] == ["Persian", "English"])
check("history of science is a covered field",
      "history of science" in e0["fields"])
check("entry is registry-only (no wired api route)", e0["api"] == "")
check("notes record the REGISTRY-ONLY verdict",
      "REGISTRY-ONLY" in e0["notes"] and "§39" in e0["notes"])
check("notes record the ArvanCloud bot wall",
      "ArvanCloud" in e0["notes"] and "__arcsjs" in e0["notes"])
check("notes record the routes tried",
      "sitemap" in e0["notes"] and "Wayback" in e0["notes"]
      and "api/jsonws" in e0["notes"] and "cookie" in e0["notes"]
      and "Googlebot" in e0["notes"])
check("notes record the valuable sections",
      "آموزش‌های رایگان" in e0["notes"]
      and "مطالب تاریخ علم" in e0["notes"]
      and "پایان‌نامه‌ها" in e0["notes"])
check("notes keep the §36 journal route unduplicated",
      "§36" in e0["notes"] and "NOT duplicated" in e0["notes"])

for mod, tag in ((ba, "build_article"), (pt, "propose_topics")):
    di, ui, st = mod.load_source_registry()
    check(f"{tag} loads 23 files / 2200 entries",
          st["files_ok"] == 23 and st["loaded"] == 2200)
    check(f"{tag} unique URL count", st["unique_urls"] == 2147)
    check(f"{tag} utihs indexed by domain", "utihs.ut.ac.ir" in di)
    check(f"{tag} REGISTRY_FILES lists utihs_registry.json",
          "utihs_registry.json" in mod.REGISTRY_FILES)

# --------------------------------------------- the §17 signal still works
rec = ba.registry_match_for(
    {"url": "https://utihs.ut.ac.ir/web/guest/home"})
check("a portal URL matches the registry record",
      rec is not None and rec["registry"] == "utihs_registry.json")
cand = {"url": "https://utihs.ut.ac.ir/fa", "title": "x"}
ba.registry_tag(cand)
check("registry_tag annotates a portal candidate (signal only)",
      cand.get("registry_match", "").startswith("پژوهشکدهٔ تاریخ علم"))

# --------------------------------------------- probe evidence (the wall)
# Trimmed from the REAL interstitial probed live 2026-11-26: every
# route on the host (homepage, /web/guest/search, /api/jsonws,
# robots.txt, sitemap.xml) answers this same ArvanCloud challenge
# page instead of content — no records, no article body, only the
# cookie-minting script. That is why §39 wires NO searcher: a
# searcher could only ever harvest this page.
CHALLENGE_HTML = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
    '<link href="/cdn-cgi/assets/css/static-pages-2.6.0.css" '
    'rel="stylesheet"><title></title></head><body>'
    '<section class="error-section error-section--waiting" id="fa">'
    '<h2 class="error-section__subtitle error-section__subtitle'
    '--waiting"> در حال انتقال به سایت مورد نظر هستید...</h2>'
    '</section><script>function values() { return {}; }'
    "document.cookie = '__arcsjs=' + encodeURIComponent(hash_v1) + "
    "'; Max-Age=900; Path=/; Domain=' + "
    "encodeURIComponent('ut.ac.ir');"
    "document.cookie = '__arcsjsc=' + encodeURIComponent(hash);"
    '</script></body></html>')
check("probed interstitial carries the challenge markers",
      "__arcsjs" in CHALLENGE_HTML and "__arcsjsc" in CHALLENGE_HTML
      and "cdn-cgi" in CHALLENGE_HTML)
check("interstitial stripped text is only the transfer notice",
      "در حال انتقال" in ba._arab_strip_tags(CHALLENGE_HTML)
      and "بیرونی" not in ba._arab_strip_tags(CHALLENGE_HTML))
check("no dead searcher is wired for the walled portal",
      not hasattr(ba, "search_utihs")
      and all(s.__name__ != "search_utihs" for s in ba.SEARCHERS)
      and all(s.__name__ != "search_utihs"
              for s in ba.WAVE1_SEARCHERS))

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
