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

# ---------------------------------------------------------------- registry
raw = json.loads((BASE/"arab_libraries_registry.json").read_text(encoding="utf-8"))
check("registry has exactly 6 library entries", len(raw) == 6)
need = {"name", "url", "type", "fields", "languages", "api", "notes",
        "verified", "verified_date"}
check("registry entries carry the registry schema",
      all(need <= set(e) for e in raw))
check("registry entries verified 2026-11-26",
      all(e["verified"] is True and e["verified_date"] == "2026-11-26"
          for e in raw))
by_url = {e["url"]: e for e in raw}
check("Shamela entry is a digital_library with the wired api",
      by_url["https://shamela.ws"]["type"] == "digital_library"
      and by_url["https://shamela.ws"]["api"]
      == "https://shamela.ws/ajax/search")
check("IJS entry is the wired Iraq alternate route",
      by_url["https://ijs.uobaghdad.edu.iq"]["type"] == "journal"
      and "search" in by_url["https://ijs.uobaghdad.edu.iq"]["api"])
check("registry-only libraries carry no wired api",
      all(by_url[u]["api"] == "" for u in
          ("https://al-mostafa.com", "https://waqfeya.net",
           "https://www.noor-book.com", "https://www.hindawi.org")))

for mod, tag in ((ba, "build_article"), (pt, "propose_topics")):
    di, ui, st = mod.load_source_registry()
    check(f"{tag} loads 11 files / 2122 entries",
          st["files_ok"] == 11 and st["loaded"] == 2122)
    check(f"{tag} arab libraries indexed by domain",
          all(d in di for d in ("shamela.ws", "al-mostafa.com",
                                "waqfeya.net", "noor-book.com",
                                "hindawi.org",
                                "ijs.uobaghdad.edu.iq")))

# dedupe: the same libraries file under two registry names drops duplicates
with tempfile.TemporaryDirectory() as td:
    shutil.copy(BASE/"arab_libraries_registry.json",
                pathlib.Path(td)/"arab_libraries_registry.json")
    shutil.copy(BASE/"arab_libraries_registry.json",
                pathlib.Path(td)/"sources_registry.json")
    di2, ui2, st2 = ba.load_source_registry(base_dir=td)
    check("dedupe by normalised URL across files",
          st2["loaded"] == 12 and st2["duplicates"] == 6
          and st2["unique_urls"] == 6)

# tagging: library-domain candidates get tagged; law unchanged
di, ui, st = ba.load_source_registry()
ba._REGISTRY_CACHE = (di, ui, st)
cand = {"title": "Tahqiq ma li'l-Hind", "authors": [], "year": None,
        "venue": "Al-Shamela Library (book)", "doi": None,
        "url": "https://shamela.ws/book/14100/2886", "abstract": "",
        "arxiv_id": None, "index": "library:shamela"}
rec = ba.registry_tag(cand)
check("shamela candidate tagged with its registry match",
      rec is not None and "Shamela" in cand.get("registry_match", ""))
check("registry tag never marks a candidate verified",
      "verified" not in cand and cand["doi"] is None)
cand2 = {"title": "x", "authors": [], "year": 2020, "venue": "v",
         "doi": None, "url": "https://example.invalid/a",
         "abstract": "", "arxiv_id": None, "index": "library:shamela"}
check("unregistered domain is not tagged",
      ba.registry_tag(cand2) is None
      and "registry_match" not in cand2)
check("quality bar unchanged (MIN_SOURCES still 5)",
      ba.MIN_SOURCES == 5)

# ---------------------------------------------------------------- fixtures
# Trimmed from the REAL probe responses captured 2026-11-26.
SHAMELA_HTML = """<span style="display:none">0</span><div class="alert">النتائج 1 إلى 10</div><article class="srch-result"><div class="srch-result-title"><a target="_blank" href="https://shamela.ws/book/14100/2886"><b><span class="text-primaryy">تاريخ الجزائر الثقافي</span></b> <span class="text-gray">[أبو القاسم سعد الله]</span></a> <span class="text-gray srch-page-ref">(ج 6، ص 320)</span></div><p class="srch-snippet"> <span class="srch-snippet-text">وقد استمرت هذه النظرية سارية إلى العشرينات من هذا القرن. فقد رجع السيد ريمون <em>بيروني</em>، وهو من الضباط مثل لويس رين، إلى نظرية فورنيل وأرنست ميرسييه وفيكتور بيقي وغيرهم، حول البربر وموقفهم من الحضارة</span> <button type="button" class="btn btn-link btn-xs srch-full-toggle" data-cid="14100_2886" data-term="بيروني">عرض المزيد</button> </p><div class="srch-full-snippet softhide" data-loaded="0" data-book-url="https://shamela.ws/book/14100/2886"></div></article><article class="srch-result"><div class="srch-result-title"><a target="_blank" href="https://shamela.ws/book/14100/2761"><b><span class="text-primaryy">تاريخ الجزائر الثقافي</span></b> <span class="text-gray">[أبو القاسم سعد الله]</span></a> <span class="text-gray srch-page-ref">(ج 6، ص 194)</span></div><p class="srch-snippet"> <span class="srch-snippet-text">بالباحثين الآخرين أن يفعلوا ذلك بدلنا. ويذكر <em>بيروني</em> أن الفكون تقاعد سنة ١٨٨١.</span> <button type="button" class="btn btn-link btn-xs srch-full-toggle" data-cid="14100_2761" data-term="بيروني">عرض المزيد</button> </p><div class="srch-full-snippet softhide" data-loaded="0" data-book-url="https://shamela.ws/book/14100/2761"></div></article>"""

IJS_HTML = """<div class="obj_article_summary">
	<h3 class="title">
		<a id="article-12286" href="https://ijs.uobaghdad.edu.iq/index.php/eijs/article/view/12286">
						Seasonal Effects of Environmental Factors on the Performance Parameters of Three Types of Silicon Solar Modules at NRIAG, Helwan, Egypt
		</a>
	</h3>
				<div class="meta">
				<div class="authors">
			Heba  Zenhom, Ahmed  Ghitas, Ahmed  Abulwfa, Maha  Harhash, M S El-Nawawy El-Nawawy, Mohamed M.  Abdelwahab
		</div>
						<div class="pages">2086-2105</div>
					<div class="published">
				2025-05-30
			</div>
	</div>
</div>
<div class="obj_article_summary">
	<h3 class="title">
		<a id="article-13557" href="https://ijs.uobaghdad.edu.iq/index.php/eijs/article/view/13557">
						Computation of Some Families of Frozen Orbits in the Field of Triaxial Moon
		</a>
	</h3>
				<div class="meta">
				<div class="authors">
			O. M. Ramla, F. A. Abd El-Salam, W. A. Rahoma, E. H. Kattab
		</div>
						<div class="pages">5789-5804</div>
					<div class="published">
				2025-12-30
			</div>
	</div>
</div>"""

# Real Crossref response for filter=issn:2227-0426 (QMJ online ISSN),
# captured 2026-11-26: total-results 1515, publisher HBKU Press.
CROSSREF_QSCIENCE = {"status": "ok", "message-type": "work-list",
  "message": {"total-results": 1515, "items": [
    {"DOI": "10.5339/qmj.2010.1.24",
     "title": ["Abstracts From Other Journals"],
     "publisher": "Hamad bin Khalifa University Press (HBKU Press)",
     "URL": "https://doi.org/10.5339/qmj.2010.1.24",
     "container-title": ["Qatar Medical Journal"]},
    {"DOI": "10.5339/qmj.2012.1.12",
     "title": ["Large Organized Intrapericardial Hematoma Presenting as a Case of Constrictive Pericarditis and Superior Vena Cava Syndrome"],
     "publisher": "Hamad bin Khalifa University Press (HBKU Press)",
     "URL": "https://doi.org/10.5339/qmj.2012.1.12",
     "container-title": ["Qatar Medical Journal"]},
    {"DOI": "10.5339/qmj.2018.1",
     "title": ["Reducing antimicrobial prescribing errors, in the neonatal intensive care unit, Women's Hospital, Hamad Medical Corporation"],
     "publisher": "Hamad bin Khalifa University Press (HBKU Press)",
     "URL": "https://doi.org/10.5339/qmj.2018.1",
     "container-title": ["Qatar Medical Journal"]}]}}

# ---------------------------------------------------------------- parsers
out = ba._parse_shamela_results(SHAMELA_HTML)
check("shamela parser returns the 2 fixture hits", len(out) == 2)
check("shamela candidates are book candidates on shamela.ws",
      all(c["index"] == "library:shamela"
          and c["url"].startswith("https://shamela.ws/book/")
          and "book" in c["venue"] for c in out))
check("shamela title is the book title, author split out",
      out[0]["title"] == "تاريخ الجزائر الثقافي"
      and out[0]["authors"] == ["أبو القاسم سعد الله"])
check("shamela snippet becomes the abstract",
      "بيروني" in out[0]["abstract"])
check("shamela parser on garbage returns []",
      ba._parse_shamela_results("not html at all") == []
      and ba._parse_shamela_results("") == [])
big = "".join(SHAMELA_HTML for _ in range(7))
check("shamela parser caps at LIB_PER_SOURCE_CAP",
      len(ba._parse_shamela_results(big)) == ba.LIB_PER_SOURCE_CAP)

out = ba._parse_ojs_search_results(
    IJS_HTML, "iraq:ijs",
    "Iraqi Journal of Science (University of Baghdad)")
check("iraq IJS parser returns the 2 fixture articles", len(out) == 2)
check("iraq IJS candidates carry the iraq:ijs tag + view URLs",
      all(c["index"] == "iraq:ijs" and "/article/view/" in c["url"]
          for c in out))
check("iraq IJS authors/year parsed",
      out[0]["year"] == 2025 and "Heba Zenhom" in out[0]["authors"][0])

out = ba._parse_qscience_crossref(CROSSREF_QSCIENCE)
check("qscience crossref parser returns the 3 fixture works",
      len(out) == 3)
check("qscience candidates carry tag, DOI and journal venue",
      all(c["index"] == "qatar:qscience-crossref"
          and (c["doi"] or "").startswith("10.5339/")
          and c["venue"] == "Qatar Medical Journal" for c in out))
# Crossref field shapes for author/published (documented API fields)
one = {"message": {"items": [
    {"DOI": "10.5339/qsc.2020.1", "title": ["A QScience Article"],
     "author": [{"given": "Sara", "family": "Haddad"}],
     "published": {"date-parts": [[2020, 5, 1]]},
     "URL": "https://doi.org/10.5339/qsc.2020.1",
     "container-title": ["QScience Connect"]} ]}}
out1 = ba._parse_qscience_crossref(one)
check("qscience parser reads author + year when present",
      len(out1) == 1 and out1[0]["authors"] == ["Sara Haddad"]
      and out1[0]["year"] == 2020)
check("qscience parser on garbage returns []",
      ba._parse_qscience_crossref({}) == []
      and ba._parse_qscience_crossref({"message": {"items": "junk"}})
      == [])

# ---------------------------------------------------------------- searcher
_real_fetch = ba._arab_fetch
_real_shamela_fetch = ba._shamela_fetch

def fake_fetch_factory(fail=()):
    def fake(url, data=None):
        if "shamela" in url and "shamela" not in fail:
            return 200, SHAMELA_HTML
        if "ijs.uobaghdad" in url and "ijs" not in fail:
            return 200, IJS_HTML
        if "crossref" in url and "crossref" not in fail:
            return 200, json.dumps(CROSSREF_QSCIENCE)
        return None, ""
    return fake

ba._arab_fetch = fake_fetch_factory()
ba._shamela_fetch = fake_fetch_factory()
res = ba.search_arab_libraries("بيروني")
tags = {c["index"] for c in res}
check("searcher merges all three wired routes",
      tags == {"library:shamela", "iraq:ijs",
               "qatar:qscience-crossref"} and len(res) == 10)
check("qscience route queries both ISSNs within its cap",
      sum(1 for c in res if c["index"] == "qatar:qscience-crossref")
      == 6)
check("searcher total stays within LIB_TOTAL_CAP",
      len(res) <= ba.LIB_TOTAL_CAP)

ba._arab_fetch = fake_fetch_factory(fail=("shamela", "crossref"))
ba._shamela_fetch = fake_fetch_factory(fail=("shamela", "crossref"))
res = ba.search_arab_libraries("astronomy")
check("fail-soft: two routes down, IJS still contributes",
      len(res) == 2
      and all(c["index"] == "iraq:ijs" for c in res))

ba._arab_fetch = lambda url, data=None: (500, "server error")
ba._shamela_fetch = ba._arab_fetch
check("fail-soft: HTTP 500 everywhere -> []",
      ba.search_arab_libraries("x") == [])
ba._arab_fetch = lambda url, data=None: (200, "garbage {{{")
ba._shamela_fetch = ba._arab_fetch
check("fail-soft: garbage bodies -> []",
      ba.search_arab_libraries("x") == [])
def boom(url, data=None):
    raise RuntimeError("transport exploded")
ba._arab_fetch = boom
ba._shamela_fetch = boom
check("fail-soft: transport exception -> []",
      ba.search_arab_libraries("x") == [])
ba._arab_fetch = _real_fetch
ba._shamela_fetch = _real_shamela_fetch

# wiring
check("searcher is in SEARCHERS and WAVE1_SEARCHERS",
      ba.search_arab_libraries in ba.SEARCHERS
      and ba.search_arab_libraries in ba.WAVE1_SEARCHERS)
check("searcher has a display label",
      ba.SEARCHER_LABEL.get("search_arab_libraries")
      == "Arab libraries")
check("only the three proven routes are wired",
      [n for n, _ in ba._LIB_SEARCHERS]
      == ["shamela", "iraq-ijs", "qscience-crossref"])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
