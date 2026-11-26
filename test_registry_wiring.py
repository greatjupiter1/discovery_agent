import importlib.util, json, os, pathlib, shutil, sys, tempfile

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

# (a) all 6 files load (both scripts) — §23 added
# arab_journals_registry.json (8 platform entries) to the original 4,
# §24 added asian_journals_registry.json (22 platform entries)
# §25 added arab_libraries_registry.json (6 library entries)
# §26 added european_journals_registry.json (23 platform entries)
for mod, tag in ((ba, "build_article"), (pt, "propose_topics")):
    di, ui, st = mod.load_source_registry()
    check(f"{tag} (a) 16 files load", st["files_ok"] == 16 and st["loaded"] == 2162)
    check(f"{tag} (c) dedupe drops duplicates", st["duplicates"] > 0 and st["unique_urls"] == 2162 - st["duplicates"])
    print(f"  {tag} stats: {st}")
    globals()[f"stats_{tag}"] = st

# (b) 500_more compact schema normalises
di, ui, st = ba.load_source_registry()
sample = None
for rec in ui.values():
    if rec["registry"] == "sources_registry_500_more.json":
        sample = rec; break
check("(b) 500_more record normalised", sample is not None and sample["domain"] and sample["name"] and isinstance(sample["languages"], list))
raw = json.loads((BASE/"sources_registry_500_more.json").read_text())[0]
check("(b) raw 500_more has no verified/api keys", "verified" not in raw and "api" not in raw)
rec0 = ui[ba._registry_norm_url(raw["url"])]
check("(b) first 500_more entry found by normalised URL", rec0["name"] == raw["name"] and rec0["languages"] == raw["languages"])

# normalisation specifics
check("norm www/trailing slash/default port", ba._registry_norm_url("https://www.Example.COM:443/a/b/") == "example.com/a/b")
check("norm keeps non-default port", ba._registry_norm_url("http://example.com:8080/x") == "example.com:8080/x")

# (e) missing registry fails soft
with tempfile.TemporaryDirectory() as td:
    di2, ui2, st2 = ba.load_source_registry(base_dir=td)
    check("(e) empty dir fails soft", di2 == {} and ui2 == {} and st2["files_ok"] == 0)
    shutil.copy(BASE/"thesis_registry.json", pathlib.Path(td)/"thesis_registry.json")
    di3, ui3, st3 = ba.load_source_registry(base_dir=td)
    check("(e) partial dir loads the rest", st3["files_ok"] == 1 and st3["loaded"] == 500)

# pick a real registry journal domain for candidate tests
journal_rec = next(r for r in ui.values() if "journal" in r["type"] and r["registry"] != "sources_registry.json")
jdom = journal_rec["domain"]
print("  journal domain used:", jdom, journal_rec["name"])

# (d) tagging + boost in screen_sources
abstract = ("This study presents a detailed method and full numerical "
            "results with error analysis and comparison of values.")
def cand(url):
    return {"title": "Biruni Earth radius measurement reconstruction",
            "authors": ["A Scholar"], "year": 2020, "venue": "Journal",
            "doi": None, "url": url, "abstract": abstract,
            "index": "test"}
nonmatched = cand("https://unlisted-random-journal-xyz.example/article/1")
matched = cand(f"https://{jdom}/article/2")
counted, stats = ba.screen_sources("History of astronomy",
                                   "Biruni Earth radius measurement",
                                   [nonmatched, matched], "", "")
check("(d) both counted", len(counted) == 2)
check("(d) matched tagged", bool(matched.get("registry_match")))
check("(d) non-matched untagged", not nonmatched.get("registry_match"))
check("(d) matched boosted first", counted[0] is matched)
check("(d) funnel stats registry_matched == 1", stats.get("registry_matched") == 1)
cards = ba.source_cards_text(counted)
check("(d) source card carries registry tag", "Registry match" in cards and journal_rec["name"] in cards)
ft = ba.build_funnel_text("t", {"candidates": 9, "verified": 4, "with_content": 3,
                                "relevant": 2, "counted": 2, "registry_matched": 1}, True, word_count=100, word_target=(5000,7000))
check("(d) funnel text has registry-matched count", "رجیستری" in ft and "1" in ft)

# (f) verification law untouched
ba.page_exists = lambda url: False
victim = cand(f"https://{jdom}/article/99")
ba.registry_tag(victim)
check("(f) tag present before verify", bool(victim.get("registry_match")))
check("(f) registry match alone does NOT verify", ba.verify(victim) is False)
check("(f) tagging adds no verified flag", "verified" not in victim)

# propose_topics grounding preference: openalex (registry domain) results tagged
pt.search_openalex = lambda q, per_page=25: [{"title": "Existing work on Biruni Earth radius", "year": 2019}]
pt.search_crossref = lambda q, rows=25: [{"title": "Another existing Biruni study", "year": 2018}]
pt.search_openaire = lambda q: []
pt.search_europe_pmc = lambda q: []
digest, n = pt.grounding_digest("History of astronomy", "Biruni Earth radius", "", "")
check("propose grounding returns works", n >= 2 and "Existing work" in digest)

print(f"\nTOTAL: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
