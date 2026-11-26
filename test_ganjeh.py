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

# ------------------------------------------------------------- registries
need = {"name", "url", "type", "fields", "languages", "notes",
        "verified", "verified_date"}
greg = json.loads((BASE/"ganjeh_registry.json").read_text(encoding="utf-8"))
areg = json.loads((BASE/"ablibrary_registry.json").read_text(encoding="utf-8"))
nreg = json.loads((BASE/"najaf_registry.json").read_text(encoding="utf-8"))

check("ganjeh registry: platform + 9 seed-book entries", len(greg) == 10)
check("ablibrary registry: platform + 3 volume entries", len(areg) == 4)
check("najaf registry: platform + 4 book entries", len(nreg) == 5)
check("all registries carry the registry schema",
      all(need <= set(e) for e in greg + areg + nreg))
check("all entries verified 2026-11-26",
      all(e["verified"] is True and e["verified_date"] == "2026-11-26"
          for e in greg + areg + nreg))
g0 = greg[0]
check("ganjeh platform entry",
      g0["url"] == "https://reader.najm.ac" and g0["type"] == "platform"
      and "/api/search" in g0["api"] and "/api/books/" in g0["api"])
check("ganjeh platform seed_books match the code seed ids",
      {str(s["book_id"]) for s in g0["seed_books"]}
      == set(ba._GANJEH_SEEDS))
check("ganjeh book entries point at their reader ids",
      all(e["url"] == f"https://reader.najm.ac/book/{bid}"
          for e, bid in zip(greg[1:], ["27050", "27060", "27070",
                                       "27030", "27081", "27040",
                                       "27083", "27080", "27082"])))
a0 = areg[0]
check("ablibrary platform entry + seed ids",
      a0["url"] == "https://ablibrary.net"
      and [str(s["book_id"]) for s in a0["seed_books"]]
      == ["12098", "12099", "12100"]
      and [s["book_id"] for s in ba._ABLIBRARY_SEEDS]
      == ["12098", "12099", "12100"])
check("ablibrary notes record the failed search routes",
      "client-side shell" in a0["notes"] and "11337" in a0["notes"]
      and "الكافي" in a0["notes"])
n0 = nreg[0]
check("najaf platform entry + seed slugs from its sitemap",
      n0["url"] == "https://najafdesertlibrary.com"
      and {s["slug"] for s in n0["seed_books"]}
      == {s["slug"] for s in ba._NAJAF_SEEDS}
      and "sitemap-books.xml" in n0["notes"]
      and "Cloudflare" in n0["notes"])

for mod, tag in ((ba, "build_article"), (pt, "propose_topics")):
    di, ui, st = mod.load_source_registry()
    check(f"{tag} loads 21 files / 2188 entries",
          st["files_ok"] == 21 and st["loaded"] == 2188)
    check(f"{tag} §46 domains indexed",
          "reader.najm.ac" in di and "ablibrary.net" in di
          and "najafdesertlibrary.com" in di)

# ------------------------------------------------------------- the text bar
PROSE = ("مذكور ومدته من الولادة إلى الإيلاد مائة وثلاثون سنة وإنهم "
         "نقصوا من مدد من كان بعد سام بن نوح إلى ناحور من كل واحد "
         "مائة ، ومن مدد ناحور جد إبراهيم خمسين سنة فصارت المدد 292 "
         "ونقصت السامرة مع ذلك من مدة يرخ والد إبراهيم خمسين سنة "
         "فصارت المدة 242 ، وزعم اندرونيقوس أن مدة قينان الساقط مائة "
         "وتسع وثلاثون سنة فصارت السنون عنده 1081 ولم يعده أرسايس "
         "القيساري في الجملة كما لم يعده العبرانيون ، فصارت هذه "
         "الجملة 942 وأما ما بين ولادة إبراهيم إلى الخروج من مصر فإن "
         "التوراة لم تفصح من مدد أشخاصه بمن سوى إبراهيم وإسحاق وموسى")
check("text bar accepts real prose", ba._lib46_text_ok(PROSE))
check("text bar rejects the Ganjeh table placeholder",
      not ba._lib46_text_ok(". . ."))
check("text bar rejects short text",
      not ba._lib46_text_ok("القانون المسعودي"))
check("text bar rejects a Cloudflare challenge page",
      not ba._lib46_text_ok("Just a moment... " + "checking your "
                            "browser challenges.cloudflare.com " * 20))
TABLE = " ".join(str(1000 + i) for i in range(120))
check("text bar rejects a number-only table page",
      not ba._lib46_text_ok(TABLE))
html = ('<html><head><script>var x = "SCRIPT_PAYLOAD";</script></head>'
        '<body><nav>navigation chrome</nav><main id="main-content">'
        f"<p>{PROSE}</p></main></body></html>")
txt = ba._lib46_html_to_text(html)
check("html->text keeps the main prose, drops script payload",
      "مذكور ومدته" in txt and "SCRIPT_PAYLOAD" not in txt
      and "navigation chrome" not in txt)
check("html->text of nothing is empty", ba._lib46_html_to_text("") == "")
check("year: Gregorian passes through", ba._lib46_year("2001 م") == 2001)
check("year: Hijri 1422 converts to ~2001",
      ba._lib46_year("1422 ه . ق") == 2001)
check("year: no year is None", ba._lib46_year("دار الكتب العلمية") is None)

# ------------------------------------------------------------- ganjeh
# Fixtures trimmed from the REAL API answers probed live 2026-11-26:
# the /api/search items for انحطاط الأفق (books 27050 p.112 and
# 27060 p.22 are real hits), the /api/books/27050 details metadata,
# and the real p.309 placeholder content shape. Book 99999 is a
# synthetic placeholder-only book added to prove rejection.
PROSE_TERM = ("يذهبون في هذه الأميال إلى قريب من ضعفها والعيان أولى "
              "من الخبر وقد اعتبرت ذلك بأرضهم وحصلت مقدار انحطاط "
              "الافق " + PROSE)
PROSE_TERM2 = ("فليكن ه ا ونصل ط ا فيحصل مثلث ه ط ا قائم زاوية ا "
               "معلوم الزوايا وذلك ان زاوية ا ه ط بمقدار تمام "
               "انحطاط الافق " + PROSE)
SEARCH_FIXTURE = {"results": {"total": 3, "items": [
    {"book_identifier": "27050", "book_name": "القانون المسعودی",
     "main_book_label": "القانون المسعودی", "part_number": "1",
     "page": "112", "snippet": "... انحطاط الافق ..."},
    {"book_identifier": "27060", "book_name": "القانون المسعودی",
     "main_book_label": "القانون المسعودی", "part_number": "2",
     "page": "22", "snippet": "... انحطاط الافق ..."},
    {"book_identifier": "99999", "book_name": "كتاب الجداول",
     "main_book_label": "كتاب الجداول", "part_number": "",
     "page": "5", "snippet": "... الجيوب ..."}]},
    "book_groups": []}
DETAILS_27050 = {"metadata": {
    "اسم الكتاب": "القانون المسعودي",
    "اسم المؤلف": "أبو ريحان البيروني", "جزء": "1",
    "الناشر": "دار الكتب العلمية - بيروت", "سنة الطبع": "1422 ه . ق"},
    "toc": []}
PAGES = {("27050", 112): PROSE_TERM, ("27060", 22): PROSE_TERM2,
         ("99999", 5): ". . ."}


def fake_ganjeh_fetch(url, data=None):
    if "/api/search" in url:
        return 200, json.dumps(SEARCH_FIXTURE, ensure_ascii=False)
    for (bid, pg), content in PAGES.items():
        if url.endswith(f"/api/books/{bid}/pages/{pg}"):
            return 200, json.dumps({"content": content},
                                   ensure_ascii=False)
    if url.endswith("/api/books/27050"):
        return 200, json.dumps(DETAILS_27050, ensure_ascii=False)
    if "/api/books/" in url and "/pages/" not in url:
        return 200, json.dumps({"metadata": {}, "toc": []},
                               ensure_ascii=False)
    return 404, ""


real_fetch = ba._arab_fetch
try:
    ba._GANJEH_META_CACHE.clear()
    ba._arab_fetch = fake_ganjeh_fetch
    res = ba.search_ganjeh("انحطاط الأفق")
    check("ganjeh: passage-carrying books become candidates",
          len(res) == 2)
    check("ganjeh: placeholder-only book yields NO candidate",
          all("99999" not in c["url"] for c in res))
    c0 = res[0]
    check("ganjeh: candidate tag is ganjeh:reader",
          c0["index"] == "ganjeh:reader"
          and c0["fulltext_route"] == "ganjeh:reader")
    check("ganjeh: candidate carries fetched full text + passages",
          c0["fulltext"] is True and c0["passages"] is True
          and "انحطاط" in c0["fulltext_excerpt"]
          and c0["passage_hits"] >= 1)
    check("ganjeh: candidate url is the reader page",
          c0["url"] == "https://reader.najm.ac/book/27050?page=112")
    check("ganjeh: seed book marked primary (author's own work)",
          c0["source_type"] == "primary"
          and c0["primary_relation"] == "author")
    check("ganjeh: author + Hijri year from details metadata",
          c0["authors"] == ["أبو ريحان البيروني"] and c0["year"] == 2001)
    check("ganjeh: title carries the volume",
          "جلد 1" in c0["title"] and "جلد 2" in res[1]["title"])
    check("ganjeh: language from the seed map", c0["language"] == "ar")

    many = {"results": {"total": 10, "items": [
        {"book_identifier": str(30000 + i), "book_name": f"كتاب {i}",
         "main_book_label": f"كتاب {i}", "part_number": "",
         "page": "3", "snippet": "x"} for i in range(10)]},
        "book_groups": []}

    def fake_many(url, data=None):
        if "/api/search" in url:
            return 200, json.dumps(many, ensure_ascii=False)
        if "/pages/" in url:
            return 200, json.dumps({"content": PROSE_TERM},
                                   ensure_ascii=False)
        return 200, json.dumps({"metadata": {}, "toc": []},
                               ensure_ascii=False)

    ba._arab_fetch = fake_many
    check("ganjeh: capped at GANJEH_CAP == 8",
          len(ba.search_ganjeh("انحطاط الأفق")) == ba.GANJEH_CAP == 8)

    ba._arab_fetch = lambda url, data=None: (None, "")
    check("ganjeh: fails soft on transport error",
          ba.search_ganjeh("انحطاط الأفق") == [])
    ba._arab_fetch = lambda url, data=None: (500, "err")
    check("ganjeh: fails soft on HTTP 500",
          ba.search_ganjeh("انحطاط الأفق") == [])
    calls = []
    ba._arab_fetch = lambda url, data=None: calls.append(url) or (200, "{}")
    check("ganjeh: empty query makes no request",
          ba.search_ganjeh("  ") == [] and calls == [])
finally:
    ba._arab_fetch = real_fetch

# ------------------------------------------------------------- seed libraries
def ab_page(n):
    return ("<html><head><script>var p = 'SCRIPT_PAYLOAD';</script>"
            "</head><body><main><p>القانون المسعودي للبيروني ، وفيه "
            "الكلام على انحطاط الأفق واستخراج قطر الأرض وأبعادها ، "
            + PROSE + f" صفحة {n}</p></main></body></html>")


def fake_ab_fetch(url, data=None):
    if "/book_content/b/" in url:
        return 200, ab_page(int(url.rsplit("/", 1)[1]))
    return 404, ""


try:
    ba._SEED_SCAN.clear()
    ba._arab_fetch = fake_ab_fetch
    res = ba.search_ablibrary("انحطاط الأفق")
    check("ablibrary: seed volumes become candidates", len(res) == 3)
    c0 = res[0]
    check("ablibrary: candidate tag is ablibrary:books",
          c0["index"] == "ablibrary:books"
          and c0["fulltext_route"] == "ablibrary:books")
    check("ablibrary: passages come from fetched pages",
          c0["passages"] is True and "انحطاط" in c0["fulltext_excerpt"]
          and "SCRIPT_PAYLOAD" not in c0["fulltext_excerpt"])
    check("ablibrary: seed marked primary",
          c0["source_type"] == "primary")
    st = ba._SEED_SCAN[("ablibrary", "12098")]
    check("ablibrary: scan is bounded per call",
          st["next"] == 1 + ba.SEED_SCAN_PAGES_PER_CALL)
    ba.search_ablibrary("انحطاط الأفق")
    check("ablibrary: scan frontier advances across calls",
          ba._SEED_SCAN[("ablibrary", "12098")]["next"]
          == 1 + 2 * ba.SEED_SCAN_PAGES_PER_CALL)
    fetched = []
    ba._arab_fetch = lambda url, data=None: fetched.append(url) or (200, "")
    check("ablibrary: unrelated query passes no metadata gate",
          ba.search_ablibrary("quantum chromodynamics lattice") == []
          and fetched == [])
    ba._SEED_SCAN.clear()
    ba._arab_fetch = lambda url, data=None: (
        403, "<html><title>Just a moment...</title></html>")
    check("ablibrary: fails soft behind a wall",
          ba.search_ablibrary("انحطاط الأفق") == [])

    ba._SEED_SCAN.clear()
    ba._arab_fetch = lambda url, data=None: (
        403, "<html><title>Just a moment...</title>"
             "challenges.cloudflare.com</html>")
    check("najaf: fails soft behind the Cloudflare wall",
          ba.search_najaf("التفهيم") == [])

    def fake_najaf_fetch(url, data=None):
        if "najafdesertlibrary.com/book/" in url:
            return 200, ("<html><body><div>" + PROSE
                         + " التفهيم لاوائل صناعة التنجيم للبيروني"
                         "</div></body></html>")
        return 404, ""

    ba._SEED_SCAN.clear()
    ba._arab_fetch = fake_najaf_fetch
    res = ba.search_najaf("التفهيم")
    check("najaf: un-walled seed yields a candidate", len(res) == 1)
    check("najaf: candidate tag is najaf:desert",
          res[0]["index"] == "najaf:desert"
          and res[0]["fulltext_route"] == "najaf:desert")
    check("najaf: candidate url follows the verified pattern",
          res[0]["url"].startswith(
              "https://najafdesertlibrary.com/book/")
          and res[0]["url"].endswith("/v/1/p/1"))
    ba._arab_fetch = lambda url, data=None: (None, "")
    ba._SEED_SCAN.clear()
    check("najaf: fails soft on transport error",
          ba.search_najaf("التفهيم") == [])
finally:
    ba._arab_fetch = real_fetch
    ba._SEED_SCAN.clear()

# ------------------------------------------------------------- registration
for fn in (ba.search_ganjeh, ba.search_ablibrary, ba.search_najaf):
    check(f"{fn.__name__} registered in SEARCHERS",
          fn in ba.SEARCHERS)
    check(f"{fn.__name__} registered in WAVE1",
          fn in ba.WAVE1_SEARCHERS)
    check(f"{fn.__name__} registered in the §43 new-only set",
          fn in ba.NEW_ONLY_SEARCHERS)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
