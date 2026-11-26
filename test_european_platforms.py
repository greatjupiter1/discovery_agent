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
raw = json.loads((BASE/"european_journals_registry.json").read_text(encoding="utf-8"))
check("registry has exactly 23 platform entries", len(raw) == 23)
need = {"name", "url", "type", "fields", "languages", "api", "notes",
        "verified", "verified_date"}
check("registry entries carry the registry schema",
      all(need <= set(e) for e in raw))
check("registry entries verified 2026-11-26",
      all(e["verified"] is True and e["verified_date"] == "2026-11-26"
          for e in raw))
check("registry types are platform/digital_library/index",
      all(e["type"] in ("platform", "digital_library", "index")
          for e in raw))
check("no entries for the no-portal countries (BE/AT/IE/IT/RO/BG)",
      all(not any(c in e["name"] for c in
                  ("Belgium", "Austria", "Ireland", "Italy",
                   "Romania", "Bulgaria")) for e in raw))

for mod, tag in ((ba, "build_article"), (pt, "propose_topics")):
    di, ui, st = mod.load_source_registry()
    check(f"{tag} loads 16 files / 2162 entries",
          st["files_ok"] == 16 and st["loaded"] == 2162)
    check(f"{tag} european platforms indexed by domain",
          all(d in di for d in ("bibliotekanauki.pl", "persee.fr",
                                "e-periodica.ch", "tidsskrift.dk",
                                "hrcak.srce.hr", "journal.fi",
                                "openjournals.nl", "scindeks.ceon.rs",
                                "dialnet.unirioja.es",
                                "epublishing.ekt.gr")))

# dedupe: the same european file under two registry names drops duplicates
with tempfile.TemporaryDirectory() as td:
    shutil.copy(BASE/"european_journals_registry.json",
                pathlib.Path(td)/"european_journals_registry.json")
    shutil.copy(BASE/"european_journals_registry.json",
                pathlib.Path(td)/"sources_registry.json")
    di2, ui2, st2 = ba.load_source_registry(base_dir=td)
    check("dedupe by normalised URL across files",
          st2["loaded"] == 46 and st2["duplicates"] == 23
          and st2["unique_urls"] == 23)

# tagging: platform-domain candidates get tagged; law unchanged
di, ui, st = ba.load_source_registry()
ba._REGISTRY_CACHE = (di, ui, st)
cand_bn = {"title": "Seismic study", "authors": [], "year": 2024,
           "venue": "Forum Poetyki", "doi": None,
           "url": "https://bibliotekanauki.pl/articles/64160793.pdf",
           "abstract": "", "arxiv_id": None,
           "index": "europe:bibliotekanauki"}
rec = ba.registry_tag(cand_bn)
check("Biblioteka Nauki candidate domain-matches the platform entry",
      rec is not None
      and "Biblioteka Nauki" in (cand_bn.get("registry_match") or ""))
ba.page_exists = lambda url: False
cand_no_doi = dict(cand_bn, doi=None)
ba.registry_tag(cand_no_doi)
check("registry match alone does NOT verify (law unchanged)",
      ba.verify(cand_no_doi) is False and "verified" not in cand_no_doi)
ba._REGISTRY_CACHE = None

# ------------------------------------------------------------- searcher wiring
check("search_european_platforms in SEARCHERS",
      ba.search_european_platforms in ba.SEARCHERS)
check("search_european_platforms in WAVE1_SEARCHERS",
      ba.search_european_platforms in ba.WAVE1_SEARCHERS)
check("searcher label present",
      ba.SEARCHER_LABEL.get("search_european_platforms")
      == "European platforms")
check("only the four full-text-verified platforms are wired",
      {n for n, _ in ba._EURO_PLATFORM_SEARCHERS}
      == {"bibliotekanauki", "persee", "eperiodica", "tidsskrift"})
check("hrcak NOT wired (HTTP 418 robot check, full-text-only law)",
      all(n != "hrcak" for n, _ in ba._EURO_PLATFORM_SEARCHERS))
check("dialnet NOT wired (full text unverified from the sandbox)",
      all(n != "dialnet" for n, _ in ba._EURO_PLATFORM_SEARCHERS))

# ----------------------------------------------------------------- fixtures
# Real Biblioteka Nauki /api/search document captured live on
# 2026-11-26 (the platform's own record; totalResults was 2,760).
BN_DOC = {"publicationId": "64160793", "publicationType": "ARTICLE",
          "mainTitle": "Eco-psychological Analysis of Literary "
          "Descriptions of Catastrophe. The case of the Fukushima "
          "tragedy in 2:46 aftershocks, stories of Japan earthquake",
          "publishedDate": "2024",
          "mainAbstract": "W artykule podjęto próbę zbadania "
          "przecięcia psychoanalizy i ekokrytyki w Japonii.",
          "contributors": ["Shuai Tong"],
          "fullTextFormats": ["PDF"], "accessRestricted": False,
          "journalId": 1334, "journalTitle": "Forum Poetyki",
          "issueYear": "2024", "bookId": 0, "bookTitle": None}
BN_JSON = {"documents": [BN_DOC], "totalResults": 2760}

# Real Persée search markup captured live on 2026-11-26 (trimmed
# to the first doc-result block; title/author/bibref are the
# platform's own).
PERSEE_HTML = """<div class="doc-result"> <div> <a href="https://www.persee.fr/doc/rgpso_0035-3221_1968_num_39_4_4848?q=tremblement" class="title title-free">Le tremblement de terre d'Arette</a> [article]</div> <div class="contributors"> <span class="authority-reference"> <a href="https://www.persee.fr/authority/287211" title="Informations sur cette autorité" class="left-picto"><span class="picto"><img src="https://www.persee.fr/persee-theme/images/skin/persee/picto/sem-link-2.png" alt="sem-link" class="hover-base"></span> <span class="name"> Xavier Piolle</span></a></span> </div> <div class="documentBibRef"> <span class="collection"> <a href="https://www.persee.fr/collection/rgpso">Revue géographique des Pyrénées et du Sud-Ouest. Sud-Ouest Européen</a></span> / <span class="documentYear"> Année 1968</span> / <span class="documentIssue"> <a href="https://www.persee.fr/issue/rgpso_0035-3221_1968_num_39_4">39-4</a></span> / <span class="documentPageRange"> pp. 369-396</span> </div> </div> <hr>"""

# Real E-Periodica search markup captured live on 2026-11-26
# (first ep-result item, page snippets shortened).
EP_HTML = """<div class="ep-results__item ep-result ep-result--has-authors" data-pid="sbz-002:1931:97::502" data-movingwall="Die Sperrfrist beträgt 24 Monate.">
  <div class="ep-result__head">
    <p class="ep-result__magazine">
      Schweizerische Bauzeitung&nbsp;&mdash; Band 97/98 (1931)
    </p>
      <h2 class="ep-result__title">
        <a href="/view/sbz-002/1931/97/2883">Ueber die Vertikalbeanspruchung von Bauwerken bei <span class="termhighlight">Erdbeben</span> </a>
      </h2>
      <p class="ep-result__authors">
        Wanner, E. / Schweizerische <span class="termhighlight">Erdbebenwarte</span>
      </p>
  </div>
    <div class="ep-result__page">
      <p class="ep-result__page__text">
        <a href="/view/sbz-002/1931/97/2883">
           Wirkungen im Japanischen <span class="termhighlight">Erdbeben</span>-vom September 1923. Ueber die Vertikalbeanspruchung von Bauwerken bei <span class="termhighlight">Erdbeben</span>. Mitteilung der SCHWEIZER. ERDBEBENWARTE, Zürich.
        </a>
      </p>
    </div>
</div>"""

# Real tidsskrift.dk OJS markup captured live on 2026-11-26
# (first obj_article_summary block, highlighting div trimmed).
TIDSSKRIFT_HTML = """<div class="obj_article_summary">
        <h2 class="title">
    <a id="article-44887" href="https://tidsskrift.dk/geografisktidsskrift/article/view/44887">
        The great Tangshan earthquake of 1976: an anatomy of disaster. Ed. by Chen Yong el al.
            </a>
</h2>
    <div class="meta">
                    <div class="authors">
                Ole Humlum
            </div>
                    <div class="published">
                1992-01-01
            </div>
    </div>
</div>"""

HTML_FIXTURES = {"persee": PERSEE_HTML, "eperiodica": EP_HTML,
                 "tidsskrift": TIDSSKRIFT_HTML}

_real_fetch = ba._arab_fetch
_real_post_json = ba._euro_post_json

def fake_fetch_factory(fail=(), status=200, garbage=False):
    def fake(url, data=None):
        if "persee.fr" in url:
            key = "persee"
        elif "e-periodica.ch" in url:
            key = "eperiodica"
        elif "tidsskrift.dk" in url:
            key = "tidsskrift"
        else:
            return 200, "<html><body></body></html>"
        if key in fail:
            return None, ""
        if garbage:
            return 200, "not html at all {{{"
        return status, HTML_FIXTURES[key]
    return fake

def fake_post_factory(mode="ok", many=False):
    def fake(url, payload):
        if mode == "down":
            return None
        if mode == "garbage":
            return {"unexpected": True}
        docs = [dict(BN_DOC)]
        if many:
            docs = []
            for i in range(12):
                d = dict(BN_DOC)
                d["publicationId"] = str(70000000 + i)
                docs.append(d)
        return {"documents": docs, "totalResults": 2760}
    return fake

CAND_KEYS = {"title", "authors", "year", "venue", "doi", "url",
             "abstract", "arxiv_id", "index"}

ba._arab_fetch = fake_fetch_factory()
ba._euro_post_json = fake_post_factory()
try:
    out = ba.search_european_platforms("earthquake")
finally:
    ba._arab_fetch = _real_fetch
    ba._euro_post_json = _real_post_json
by_index = {}
for c in out:
    by_index.setdefault(c["index"], []).append(c)

b = by_index.get("europe:bibliotekanauki", [])
check("bibliotekanauki candidates parsed", len(b) == 1)
check("bibliotekanauki candidate shape + fields",
      bool(b) and CAND_KEYS <= set(b[0])
      and b[0]["url"] == "https://bibliotekanauki.pl/articles/64160793.pdf"
      and b[0]["year"] == 2024 and b[0]["venue"] == "Forum Poetyki"
      and b[0]["authors"] == ["Shuai Tong"]
      and "Eco-psychological" in b[0]["title"]
      and "psychoanalizy" in b[0]["abstract"])
p = by_index.get("europe:persee", [])
check("persee candidates parsed", len(p) == 1)
check("persee candidate fields",
      bool(p) and p[0]["title"] == "Le tremblement de terre d'Arette"
      and p[0]["authors"] == ["Xavier Piolle"] and p[0]["year"] == 1968
      and "Revue géographique des Pyrénées" in p[0]["venue"]
      and p[0]["url"] == "https://www.persee.fr/doc/rgpso_0035-3221_1968_num_39_4_4848")
e = by_index.get("europe:eperiodica", [])
check("eperiodica candidates parsed", len(e) == 1)
check("eperiodica candidate fields",
      bool(e) and "Vertikalbeanspruchung" in e[0]["title"]
      and e[0]["venue"] == "Schweizerische Bauzeitung"
      and e[0]["year"] == 1931
      and "Wanner, E." in (e[0]["authors"] or [""])[0]
      and e[0]["url"] == "https://www.e-periodica.ch/view/sbz-002/1931/97/2883"
      and "Japanischen" in e[0]["abstract"])
t = by_index.get("europe:tidsskrift", [])
check("tidsskrift OJS candidates parsed", len(t) == 1)
check("tidsskrift candidate fields",
      bool(t) and "Tangshan earthquake" in t[0]["title"]
      and t[0]["year"] == 1992
      and t[0]["url"].endswith("/article/view/44887"))

# full-text-only filter inside the BN parser: restricted or
# PDF-less documents are never emitted
mixed = {"documents": [
    dict(BN_DOC),
    dict(BN_DOC, publicationId="111", accessRestricted=True),
    dict(BN_DOC, publicationId="222", fullTextFormats=["HTML"]),
    dict(BN_DOC, publicationId="333", fullTextFormats=[]),
    dict(BN_DOC, publicationId="444", mainTitle="")],
    "totalResults": 5}
filtered = ba._parse_bibliotekanauki(mixed)
check("BN parser drops restricted / PDF-less / title-less documents",
      len(filtered) == 1
      and filtered[0]["url"].endswith("/articles/64160793.pdf"))

# fail-soft: persee down, the others still answer
ba._arab_fetch = fake_fetch_factory(fail=("persee",))
ba._euro_post_json = fake_post_factory()
try:
    out2 = ba.search_european_platforms("earthquake")
finally:
    ba._arab_fetch = _real_fetch
    ba._euro_post_json = _real_post_json
check("fail-soft per platform",
      all(c["index"] != "europe:persee" for c in out2)
      and any(c["index"] == "europe:bibliotekanauki" for c in out2))

# fail-soft: BN API down (None), HTML platforms still answer
ba._arab_fetch = fake_fetch_factory()
ba._euro_post_json = fake_post_factory(mode="down")
try:
    out3 = ba.search_european_platforms("earthquake")
finally:
    ba._arab_fetch = _real_fetch
    ba._euro_post_json = _real_post_json
check("BN API down contributes nothing, no crash",
      all(c["index"] != "europe:bibliotekanauki" for c in out3)
      and any(c["index"] == "europe:persee" for c in out3))

# fail-soft: HTTP 500 everywhere + garbage BN JSON -> only tidsskrift
# (also 500) -> nothing, and no crash
ba._arab_fetch = fake_fetch_factory(status=500)
ba._euro_post_json = fake_post_factory(mode="garbage")
try:
    out4 = ba.search_european_platforms("earthquake")
finally:
    ba._arab_fetch = _real_fetch
    ba._euro_post_json = _real_post_json
check("HTTP 500 / garbage bodies return [] without raising",
      out4 == [])

# fail-soft: transport raises inside every platform -> []
def boom(url, data=None):
    raise RuntimeError("network exploded")
def boom_post(url, payload):
    raise RuntimeError("network exploded")
ba._arab_fetch = boom
ba._euro_post_json = boom_post
try:
    out5 = ba.search_european_platforms("earthquake")
finally:
    ba._arab_fetch = _real_fetch
    ba._euro_post_json = _real_post_json
check("all transports raising returns [] without raising", out5 == [])

# garbage HTML bodies parse to nothing
ba._arab_fetch = fake_fetch_factory(garbage=True)
ba._euro_post_json = fake_post_factory(mode="down")
try:
    out6 = ba.search_european_platforms("earthquake")
finally:
    ba._arab_fetch = _real_fetch
    ba._euro_post_json = _real_post_json
check("garbage HTML returns [] without raising", out6 == [])

# caps: 12 BN documents -> per-platform cap 8; total cap 24
ba._arab_fetch = fake_fetch_factory()
ba._euro_post_json = fake_post_factory(many=True)
try:
    out7 = ba.search_european_platforms("earthquake")
finally:
    ba._arab_fetch = _real_fetch
    ba._euro_post_json = _real_post_json
check("per-platform cap respected",
      len([c for c in out7 if c["index"] == "europe:bibliotekanauki"])
      == ba.EURO_PER_PLATFORM_CAP == 8)
check("total cap respected", len(out7) <= ba.EURO_TOTAL_CAP == 24)

# ------------------------------------------------------- language matrix (§26)
check("fi/nl/pl/pt/hr/sr/uk/el in MATRIX_LANGUAGES",
      all(l in ba.MATRIX_LANGUAGES
          for l in ("fi", "nl", "pl", "pt", "hr", "sr", "uk", "el")))
check("european languages appended after ko (order preserved)",
      list(ba.MATRIX_LANGUAGES[-8:])
      == ["fi", "nl", "pl", "pt", "hr", "sr", "uk", "el"]
      and ba.MATRIX_LANGUAGES[0] == "en")
check("matrix caps unchanged",
      ba.MATRIX_MAX_QUERIES == 28 and ba.MATRIX_PER_LANG == 6)
check("LANGUAGE_NAMES covers the european languages",
      ba.LANGUAGE_NAMES.get("fi") == "Finnish"
      and ba.LANGUAGE_NAMES.get("nl") == "Dutch"
      and ba.LANGUAGE_NAMES.get("pl") == "Polish"
      and ba.LANGUAGE_NAMES.get("pt") == "Portuguese"
      and ba.LANGUAGE_NAMES.get("hr") == "Croatian"
      and ba.LANGUAGE_NAMES.get("sr") == "Serbian"
      and ba.LANGUAGE_NAMES.get("uk") == "Ukrainian"
      and ba.LANGUAGE_NAMES.get("el") == "Greek")
check("LANGUAGE_NAMES covers every matrix language",
      all(l in ba.LANGUAGE_NAMES for l in ba.MATRIX_LANGUAGES))

captured = {}
def fake_llm(prompt, *a, **k):
    captured["prompt"] = prompt
    return json.dumps({
        "core_keywords": ["Earth radius", "Biruni"],
        "name_variants": ["Biruni"],
        "queries": {
            "en": ["Biruni Earth radius measurement",
                   "Biruni Earth circumference method"],
            "fi": [f"Biruni maan säde mittaus {i}" for i in range(8)],
            "pl": ["Biruni pomiar promienia Ziemi",
                   "Biruni obwód Ziemi metoda"],
            "uk": ["Біруні радіус Землі вимірювання"],
        }})
_real_llm = ba.call_llm
ba.call_llm = fake_llm
try:
    matrix = ba.multilingual_query_matrix("History of astronomy",
                                          "Biruni Earth radius")
finally:
    ba.call_llm = _real_llm
check("matrix prompt lists the european codes as allowed",
      "fi nl pl pt hr sr uk el" in captured.get("prompt", ""))
check("matrix keeps fi/pl/uk languages",
      all(l in matrix["languages"] for l in ("fi", "pl", "uk")))
fi_qs = [q for q in matrix["queries"] if "maan säde" in q]
check("per-language cap 6 applied to fi", len(fi_qs) == 6)
check("total matrix cap 28 respected", len(matrix["queries"]) <= 28)
check("English queries come first",
      matrix["queries"][0].startswith("Biruni Earth radius"))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
