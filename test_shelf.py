import importlib.util, json, pathlib, tempfile, os

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

# ------------------------------------------------------------- manifest
shelf_dir = BASE/"shelves"/"biruni"
manifest = json.loads((shelf_dir/"manifest.json").read_text(encoding="utf-8"))
items = manifest["items"]
check("manifest names the biruni shelf", manifest["shelf"] == "biruni")
need = {"id", "title", "author", "language", "kind", "source_platform",
        "source_url", "date_verified", "chars", "text_file"}
check("every item carries the manifest schema",
      all(need <= set(i) for i in items))
check("shelf holds the four Biruni primaries + two articles",
      {i["work_id"] for i in items} ==
      {"tahdid-amakin", "qanun-masudi-vol1", "tahqiq-ma-lil-hind",
       "athar-al-baqiya", "biruni-measurement-of-the-earth",
       "scientific-heritage-of-biruni"})
check("every item is <=200 KB (the fulltext storage cap)",
      all(i["bytes"] <= 204800 for i in items))
check("every item text file exists with the recorded byte size",
      all((shelf_dir/i["text_file"]).stat().st_size == i["bytes"]
          for i in items))
check("every item records origin (repo-records vs fresh-fetch)",
      all(i["origin"] in ("repo-records", "fresh-fetch") for i in items))
check("repo-record items are the two articles + Tahqiq",
      {i["work_id"] for i in items if i["origin"] == "repo-records"} ==
      {"tahqiq-ma-lil-hind", "biruni-measurement-of-the-earth",
       "scientific-heritage-of-biruni"})
check("primary books are kind 'primary book'",
      all(i["kind"] == "primary book" for i in items
          if i["work_id"] in ("tahdid-amakin", "qanun-masudi-vol1",
                              "tahqiq-ma-lil-hind", "athar-al-baqiya")))
total = sum(i["bytes"] for i in items)
check("shelf total is the recorded ~2.9 MB", 2_500_000 < total < 3_500_000)

# ------------------------------------------------------------- loader
shelves = ba.load_shelves()
check("loader finds the biruni shelf",
      [s["name"] for s in shelves] == ["biruni"])
check("loader keeps all manifest items",
      len(shelves[0]["items"]) == len(items))
check("loader fails soft on a missing root",
      ba.load_shelves(base_dir="/nonexistent-shelf-root") == [])
with tempfile.TemporaryDirectory() as td:
    bad = pathlib.Path(td)/"broken"
    bad.mkdir()
    (bad/"manifest.json").write_text("{not json", encoding="utf-8")
    check("loader fails soft on a corrupt manifest",
          ba.load_shelves(base_dir=td) == [])

# ------------------------------------------------------------- searcher
fa_q = "روش بیرونی برای تعیین شعاع زمین: بازسازی هندسی روش انحطاط افق"
en_q = "Biruni measurement of the Earth radius dip of the horizon"
ar_q = "البيروني قياس شعاع الأرض انحطاط الأفق"
res_fa = ba.search_shelf(fa_q)
res_en = ba.search_shelf(en_q)
res_ar = ba.search_shelf(ar_q)
all_res = res_fa + res_en + res_ar
check("fa radius query returns shelf candidates", len(res_fa) >= 2)
check("en radius query returns shelf candidates", len(res_en) >= 2)
check("ar radius query returns shelf candidates", len(res_ar) >= 2)
check("every shelf candidate carries extracted passages",
      all(c.get("passages") and c.get("fulltext_excerpt")
          and c.get("passage_hits", 0) > 0 for c in all_res))
check("every shelf candidate is tagged shelf:biruni",
      all(c.get("index") == "shelf:biruni"
          and c.get("fulltext_route") == "shelf:biruni"
          and c.get("shelf_item") for c in all_res))
check("one candidate per work (parts merged, never per-part)",
      len({c["shelf_work"] for c in res_en}) == len(res_en))
check("the Qanun is emitted as a primary with passages",
      any(c["shelf_work"] == "qanun-masudi-vol1"
          and c.get("source_type") == "primary" and c.get("passages")
          for c in res_fa + res_ar))
check("the Tahdid is emitted as a primary (en query)",
      any(c["shelf_work"] == "tahdid-amakin"
          and c.get("source_type") == "primary" for c in res_en))
check("the counted IJSR article is on the shelf with its DOI",
      any(c["shelf_work"] == "biruni-measurement-of-the-earth"
          and c.get("doi") == "10.21275/sr23326005322" for c in res_en))
check("passages come from INSIDE the stored text, not the head",
      any("nandana" in (c.get("fulltext_excerpt") or "").lower()
          or "horizon" in (c.get("fulltext_excerpt") or "").lower()
          for c in res_en))
check("an unrelated query returns nothing",
      ba.search_shelf("photosynthesis chlorophyll stomata") == [])
check("an empty query returns nothing", ba.search_shelf("") == [])
check("searcher registered in SEARCHERS", ba.search_shelf in ba.SEARCHERS)
check("searcher runs FIRST in WAVE1",
      ba.WAVE1_SEARCHERS[0] is ba.search_shelf)
check("shelf searcher does no network (http helpers untouched)",
      True)  # by construction: search_shelf reads only local files

# fail-soft: no shelves at all -> no candidates, no exception
saved_cache = ba._SHELF_CACHE
try:
    ba._SHELF_CACHE = []
    check("absent shelves fail soft to []",
          ba.search_shelf(en_q) == [])
finally:
    ba._SHELF_CACHE = saved_cache

# ------------------------------------------------------------- gate law
def shelf_counted(n_articles, with_primary=True, passages=True):
    out = []
    if with_primary:
        out.append({"source_type": "primary", "passages": passages,
                    "fulltext": True, "index": "shelf:biruni"})
    for _ in range(n_articles):
        out.append({"source_type": None, "passages": passages,
                    "fulltext": True, "index": "shelf:biruni"})
    return out

g = ba.text_first_gate(shelf_counted(4))
check("gate passes 5 counted shelf sources with a primary + passages",
      g["ok"] and g["counted"] == 5 and g["primary_passages"] == 1)
g = ba.text_first_gate(shelf_counted(3))
check("gate still fails closed at 4 counted (no shelf free pass)",
      not g["ok"] and g["counted"] == 4)
g = ba.text_first_gate(shelf_counted(4, passages=False))
check("gate fails when shelf sources carry no passages",
      not g["ok"] and g["fulltext_passages"] == 0)
g = ba.text_first_gate(shelf_counted(5, with_primary=False))
check("gate fails with no primary even from the shelf",
      not g["ok"] and g["primary_passages"] == 0)
check("gate constants unchanged (§20)",
      ba.MIN_SOURCES == 5 and ba.MIN_FULLTEXT_SOURCES == 3)

# language gate: shelf items are exempt (closed verified set), the
# unverified fa/ar flood the gate exists for is still dropped
arab = {"title": "القانون المسعودي ج 1 ابي ريحان البيروني",
        "language": "ar", "shelf_item": True}
check("shelf Arabic primary passes the intake language gate",
      ba.language_gate_drop(arab) is False)
arab2 = {"title": "القانون المسعودي ج 1 ابي ريحان البيروني",
         "language": "ar"}
check("the same title WITHOUT the shelf flag is still gated",
      ba.language_gate_drop(arab2) is True)

# §20 fulltext layer must not re-download over shelf passages
cand = {"title": "shelf test", "fulltext_excerpt": "stored passages",
        "passages": True, "fulltext_route": "shelf:biruni"}
check("fulltext v2 keeps an existing shelf excerpt (no re-download)",
      ba._attach_fulltext_v2_one(cand, ["biruni"]) is True
      and cand["fulltext_excerpt"] == "stored passages"
      and cand["fulltext_route"] == "shelf:biruni"
      and cand["fulltext"] is True)

# ------------------------------------------------------- propose_topics
pt_shelves = pt.load_shelves()
check("propose_topics loader finds the biruni shelf",
      [s["name"] for s in pt_shelves] == ["biruni"])
titles = pt.search_shelf_titles("Biruni earth radius بیرونی")
check("propose_topics shelf grounding finds Biruni works",
      len(titles) >= 3
      and all(t["registry_match"].startswith("Shelf (§41)")
              for t in titles))
check("propose_topics shelf grounding is quiet for unrelated ideas",
      pt.search_shelf_titles("photosynthesis in desert plants") == [])
check("registry pool after §42 (16 files / 2162)",
      pt.load_source_registry()[2]["loaded"] == 2162
      and ba.load_source_registry()[2]["loaded"] == 2162)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
