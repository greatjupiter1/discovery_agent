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
raw = json.loads((BASE/"asian_journals_registry.json").read_text(encoding="utf-8"))
check("registry has exactly 22 platform entries", len(raw) == 22)
need = {"name", "url", "type", "fields", "languages", "api", "notes",
        "verified", "verified_date"}
check("registry entries carry the registry schema",
      all(need <= set(e) for e in raw))
check("registry entries verified 2026-11-26",
      all(e["verified"] is True and e["verified_date"] == "2026-11-26"
          for e in raw))
check("registry types are platform",
      all(e["type"] == "platform" for e in raw))
by_url = {e["url"]: e for e in raw}
check("wired platforms carry their search route in api",
      by_url["https://garuda.kemdiktisaintek.go.id"]["api"].endswith("/documents")
      and "search" in by_url["https://www.nepjol.info"]["api"]
      and "search" in by_url["https://vjol.info.vn"]["api"])
check("DergiPark + CyberLeninka api fields name the OAI endpoints",
      "oai" in by_url["https://dergipark.org.tr"]["api"]
      and "oai" in by_url["https://cyberleninka.ru"]["api"])
check("no invented URLs for the no-platform countries",
      not any("singapore" in e["url"] or "taiwan" in e["url"]
              for e in raw))

for mod, tag in ((ba, "build_article"), (pt, "propose_topics")):
    di, ui, st = mod.load_source_registry()
    check(f"{tag} loads 22 files / 2195 entries",
          st["files_ok"] == 22 and st["loaded"] == 2195)
    check(f"{tag} asian platforms indexed by domain",
          all(d in di for d in ("garuda.kemdiktisaintek.go.id",
                                "nepjol.info", "banglajol.info",
                                "mongoliajol.info", "vjol.info.vn",
                                "journals.nauka-nanrk.kz",
                                "jstage.jst.go.jp", "dergipark.org.tr",
                                "sid.ir", "cyberleninka.ru",
                                "koreascience.kr", "sciopen.com")))

# dedupe: the same asian file under two registry names drops duplicates
with tempfile.TemporaryDirectory() as td:
    shutil.copy(BASE/"asian_journals_registry.json",
                pathlib.Path(td)/"asian_journals_registry.json")
    shutil.copy(BASE/"asian_journals_registry.json",
                pathlib.Path(td)/"sources_registry.json")
    di2, ui2, st2 = ba.load_source_registry(base_dir=td)
    check("dedupe by normalised URL across files",
          st2["loaded"] == 44 and st2["duplicates"] == 22
          and st2["unique_urls"] == 22)

# tagging: platform-domain candidates get tagged; law unchanged
di, ui, st = ba.load_source_registry()
ba._REGISTRY_CACHE = (di, ui, st)
cand_garuda = {"title": "Seismic study", "authors": [], "year": 2024,
               "venue": "Jurnal Fisika", "doi": None,
               "url": "https://garuda.kemdiktisaintek.go.id/documents/detail/4266513",
               "abstract": "", "arxiv_id": None, "index": "asia:garuda"}
rec = ba.registry_tag(cand_garuda)
check("GARUDA candidate domain-matches the platform entry",
      rec is not None and "GARUDA" in (cand_garuda.get("registry_match") or ""))
cand_nep = dict(cand_garuda,
                url="https://www.nepjol.info/index.php/JNPhysSoc/article/view/101578",
                index="asia:nepjol")
rec2 = ba.registry_tag(cand_nep)
# nepjol.info already carries a DOAJ journal record in the 500_more
# registry, and the §17 domain index keeps the first record per
# domain — the candidate is still registry-tagged, with that name.
check("NepJOL candidate is registry-tagged by domain",
      rec2 is not None and bool(cand_nep.get("registry_match")))
ba.page_exists = lambda url: False
cand_no_doi = dict(cand_garuda, doi=None)
ba.registry_tag(cand_no_doi)
check("registry match alone does NOT verify (law unchanged)",
      ba.verify(cand_no_doi) is False and "verified" not in cand_no_doi)
ba._REGISTRY_CACHE = None

# ------------------------------------------------------------- searcher wiring
check("search_asian_platforms in SEARCHERS",
      ba.search_asian_platforms in ba.SEARCHERS)
check("search_asian_platforms in WAVE1_SEARCHERS",
      ba.search_asian_platforms in ba.WAVE1_SEARCHERS)
check("searcher label present",
      ba.SEARCHER_LABEL.get("search_asian_platforms") == "Asian platforms")
check("only the five full-text-verified platforms are wired",
      {n for n, _ in ba._ASIA_PLATFORM_SEARCHERS}
      == {"garuda", "nepjol", "mongoliajol", "vjol", "kaznas"})
check("banglajol NOT wired (full text unverified, full-text-only law)",
      all(n != "banglajol" for n, _ in ba._ASIA_PLATFORM_SEARCHERS))

# ----------------------------------------------------------------- fixtures
# Real OJS markup captured live from NepJOL on 2026-11-26 (titles,
# authors and dates are the platform's own).
NEPJOL_HTML = """<html><body><ul class="search_results">
<li><div class="obj_article_summary">
<h3 class="title">
<a id="article-101578" href="https://www.nepjol.info/index.php/JNPhysSoc/article/view/101578">
Decay Rates of Aftershocks of Gorkha Earthquake and Jajarkot Earthquake Using Omori Law
<span class="subtitle"> Journal of Nepal Physical Society </span>
</a>
</h3>
<div class="meta">
<div class="authors"> Sushiksha Kandangwa, Ram Krishna Tiwari, Harihar Paudyal </div>
<div class="pages">58-63</div>
<div class="published"> 2026-11-16 </div>
</div>
</div></li>
<li><div class="obj_article_summary">
<h3 class="title">
<a id="article-1361" href="https://www.nepjol.info/index.php/MGS/article/view/777">
Seismic Hazard Assessment of the Kathmandu Valley
<span class="subtitle"> Journal of Nepal Geological Society </span>
</a>
</h3>
<div class="meta">
<div class="authors"> A. Sharma, B. Karki </div>
<div class="pages">1-12</div>
<div class="published"> 2021-06-23 </div>
</div>
</div></li>
</ul></body></html>"""

# Real OJS markup captured live from the Kazakhstan NAS platform.
KAZNAS_HTML = """<html><body><ul class="search_results">
<li><div class="obj_article_summary">
<h3 class="title">
<a id="article-483" href="https://journals.nauka-nanrk.kz/physics-mathematics/article/view/483">
RESEARCH OF VARIATIONS OF SEISMIC ACTIVITY IN CONNECTION WITH VARIATIONS OF STRUCTURE AND DYNAMICS OF THE EARTH'S RADIATION BELT
</a>
</h3>
<div class="meta">
<div class="authors"> Zhu. Sha. Zhantayev, G. Ya. Khachikyan, S.A. Pulinets </div>
<div class="pages"> 169-176 </div>
<div class="published"> 2020-06-08 </div>
</div>
</div></li>
</ul></body></html>"""

# GARUDA markup as served live on 2026-11-26 (structure verbatim,
# second item shortened).
GARUDA_HTML = """<html><body>
<div class="article-item">
<a class="title-article" href="/documents/detail/4266513">
<xmp>ANALISIS KEAKURATAN HASIL CENTROID MOMENT TENSOR (CMT) UNTUK GEMPA BUMI SEDANG DAN BESAR DI INDONESIA</xmp>
</a>
<a class="author-article" href="/author/view/7287493"><xmp>Oktaviani, Rachma Dwi</xmp></a>;
<a class="author-article" href="/author/view/311499"><xmp>Madlazim, Madlazim</xmp></a>
<br>
<xmp class="subtitle-article"> Inovasi Fisika Indonesia Vol. 13 No. 3 (2024): Vol 13 No 3 </xmp>
<br>
<i class="subtitle-article">Publisher : </i><xmp class="subtitle-article">Prodi Fisika FMIPA Universitas Negeri Surabaya </xmp>
</div>
<div class="article-item">
<a class="title-article" href="/documents/detail/4944917">
<xmp>STUDI SEISMISITAS WILAYAH JAWA TIMUR</xmp>
</a>
<a class="author-article" href="/author/view/555"><xmp>Santoso, Budi</xmp></a>
<br>
<xmp class="subtitle-article"> Jurnal Geofisika Vol. 5 No. 1 (2021) </xmp>
</div>
</body></html>"""

FIXTURES = {"garuda": GARUDA_HTML, "nepjol": NEPJOL_HTML,
            "kaznas": KAZNAS_HTML}

_real_fetch = ba._arab_fetch
def fake_fetch_factory(fail=(), many=False):
    def fake(url, data=None):
        if "garuda.kemdiktisaintek.go.id" in url:
            key = "garuda"
        elif "nepjol.info" in url:
            key = "nepjol"
        elif "nauka-nanrk.kz" in url:
            key = "kaznas"
        else:
            return 200, "<html><body></body></html>"
        if key in fail:
            return None, ""
        body = FIXTURES[key]
        if many and key == "garuda":
            body = body.replace("/documents/detail/4266513",
                                "/documents/detail/ID")
            body = "".join(body.replace("ID", str(1000 + i))
                           for i in range(12))
        return 200, body
    return fake

CAND_KEYS = {"title", "authors", "year", "venue", "doi", "url",
             "abstract", "arxiv_id", "index"}

ba._arab_fetch = fake_fetch_factory()
try:
    out = ba.search_asian_platforms("earthquake")
finally:
    ba._arab_fetch = _real_fetch
by_index = {}
for c in out:
    by_index.setdefault(c["index"], []).append(c)
check("garuda candidates parsed", len(by_index.get("asia:garuda", [])) == 2)
g = by_index.get("asia:garuda", [{}])[0]
check("garuda candidate shape + fields",
      CAND_KEYS <= set(g) and g["url"].endswith("/documents/detail/4266513")
      and g["year"] == 2024 and "Oktaviani" in (g["authors"] or [""])[0]
      and "Inovasi Fisika Indonesia" in g["venue"]
      and "CENTROID MOMENT TENSOR" in g["title"])
n = by_index.get("asia:nepjol", [])
check("nepjol OJS candidates parsed", len(n) == 2)
check("nepjol candidate fields",
      bool(n) and n[0]["year"] == 2026
      and "Omori Law" in n[0]["title"]
      and n[0]["venue"] == "Journal of Nepal Physical Society"
      and len(n[0]["authors"]) == 3)
k = by_index.get("asia:kaznas", [])
check("kaznas OJS candidate parsed",
      len(k) == 1 and k[0]["year"] == 2020
      and "SEISMIC ACTIVITY" in k[0]["title"])
check("empty platforms contribute nothing, no crash",
      "asia:banglajol" not in by_index and "asia:vjol" not in by_index
      and "asia:mongoliajol" not in by_index)

# fail-soft: garuda down, the OJS platforms still answer
ba._arab_fetch = fake_fetch_factory(fail=("garuda",))
try:
    out2 = ba.search_asian_platforms("earthquake")
finally:
    ba._arab_fetch = _real_fetch
check("fail-soft per platform",
      all(c["index"] != "asia:garuda" for c in out2)
      and any(c["index"] == "asia:nepjol" for c in out2))

# fail-soft: transport raises inside one platform fn -> still no crash
def boom(url, data=None):
    raise RuntimeError("network exploded")
ba._arab_fetch = boom
try:
    out3 = ba.search_asian_platforms("earthquake")
finally:
    ba._arab_fetch = _real_fetch
check("all platforms failing returns [] without raising", out3 == [])

# caps: 12 garuda items in the page -> per-platform cap 8
ba._arab_fetch = fake_fetch_factory(many=True)
try:
    out4 = ba.search_asian_platforms("earthquake")
finally:
    ba._arab_fetch = _real_fetch
check("per-platform cap respected",
      len([c for c in out4 if c["index"] == "asia:garuda"])
      == ba.ASIA_PER_PLATFORM_CAP == 8)
check("total cap respected", len(out4) <= ba.ASIA_TOTAL_CAP == 24)

# ------------------------------------------------------- language matrix (§24)
check("zh/ja/ko in MATRIX_LANGUAGES",
      all(l in ba.MATRIX_LANGUAGES for l in ("zh", "ja", "ko")))
check("matrix caps unchanged",
      ba.MATRIX_MAX_QUERIES == 28 and ba.MATRIX_PER_LANG == 6)
check("English still first in the matrix",
      ba.MATRIX_LANGUAGES[0] == "en")
check("LANGUAGE_NAMES covers zh/ja/ko",
      ba.LANGUAGE_NAMES.get("zh") == "Chinese"
      and ba.LANGUAGE_NAMES.get("ja") == "Japanese"
      and ba.LANGUAGE_NAMES.get("ko") == "Korean")

captured = {}
def fake_llm(prompt, *a, **k):
    captured["prompt"] = prompt
    return json.dumps({
        "core_keywords": ["Earth radius", "Biruni"],
        "name_variants": ["Biruni"],
        "queries": {
            "en": ["Biruni Earth radius measurement",
                   "Biruni Earth circumference method"],
            "zh": [f"白鲁尼 地球半径 测量 方法{i}" for i in range(8)],
            "ja": ["ビールーニー 地球 半径 測定", "ビールーニー 地球 円周"],
            "ko": ["비루니 지구 반지름 측정"],
        }})
_real_llm = ba.call_llm
ba.call_llm = fake_llm
try:
    matrix = ba.multilingual_query_matrix("History of astronomy",
                                          "Biruni Earth radius")
finally:
    ba.call_llm = _real_llm
check("matrix prompt lists zh ja ko as allowed codes",
      "zh ja ko" in captured.get("prompt", ""))
check("matrix keeps zh/ja/ko languages",
      all(l in matrix["languages"] for l in ("zh", "ja", "ko")))
zh_qs = [q for q in matrix["queries"] if "白鲁尼" in q]
check("per-language cap trims zh to 6", len(zh_qs) == 6)
check("total cap respected in matrix", len(matrix["queries"]) <= 28)
check("English queries come first",
      matrix["queries"][0].startswith("Biruni Earth radius"))

print(f"\nTOTAL: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
