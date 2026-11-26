import importlib.util, json, pathlib, re

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
need = {"name", "url", "type", "fields", "languages", "notes",
        "verified", "verified_date"}
kreg = json.loads((BASE/"ketabonline_registry.json").read_text(encoding="utf-8"))

check("ketabonline registry: platform + 4 book entries", len(kreg) == 5)
check("registry carries the registry schema",
      all(need <= set(e) for e in kreg))
check("all entries verified 2026-11-26",
      all(e["verified"] is True and e["verified_date"] == "2026-11-26"
          for e in kreg))
k0 = kreg[0]
check("ketabonline platform entry",
      k0["url"] == "https://ketabonline.com/"
      and k0["type"] == "platform"
      and "backend.ketabonline.com/api/v2" in k0["api"]
      and "/books/pages" in k0["api"]
      and "/pages/<page>" in k0["api"])
check("platform notes record the routes tried + caveats",
      "OAI-PMH" in k0["notes"] and "sitemap" in k0["notes"]
      and "NOT HOSTED" in k0["notes"] and "authors=337" in k0["notes"]
      and "29,365" in k0["notes"])
check("book entries point at the four Biruni book ids",
      [e["url"] for e in kreg[1:]]
      == ["https://ketabonline.com/ar/books/992",
          "https://ketabonline.com/ar/books/102678",
          "https://ketabonline.com/ar/books/55071",
          "https://ketabonline.com/ar/books/165"])
check("registry book entries match the code seed set",
      {e["url"].rsplit("/", 1)[1] for e in kreg[1:]}
      == set(ba._KETABONLINE_SEEDS))

for mod, tag in ((ba, "build_article"), (pt, "propose_topics")):
    di, ui, st = mod.load_source_registry()
    check(f"{tag} loads 23 files / 2200 entries",
          st["files_ok"] == 23 and st["loaded"] == 2200)
    check(f"{tag} §50 domain indexed", "ketabonline.com" in di)

# ------------------------------------------------------------- fixtures
# Fixtures trimmed from the REAL API responses probed live
# 2026-11-26: the content-search hit shape (book 16590 p. 487),
# the reader-page shape (book 57086 p. 10 prose verbatim; book
# 165 p. 50 prose verbatim — the Jamahir seed) and the book
# record shape (book 992, author البيروني id 337). Book 99999
# is synthetic, added to prove rejection (heading-only pages).
PROSE = ("بصورة واضحة مع أية آية قرآنية هو مرفوض مهما كانت صحّة سنده "
         "استنادا لقوله تعالى : ( وَلَوْ تَقَوَّلَ عَلَيْنا بَعْضَ "
         "الْأَقاوِيلِ. لَأَخَذْنا مِنْهُ بِالْيَمِينِ. ثُمَّ "
         "لَقَطَعْنا مِنْهُ الْوَتِينَ. فَما مِنْكُمْ مِنْ أَحَدٍ "
         "عَنْهُ حاجِزِينَ ) الكون المغلف كما تصوّره الأقدمون حتى "
         "القرن الرابع عشر الميلادي اعتقد أكثر علماء الفلك بأن "
         "الشمس والقمر والكواكب تدور في مسارات دائرية حول الأرض "
         "الثابتة التي هي مركز الكون كله عندهم")
JAMAHIR_PROSE = ("ورسوب الدر وطفو الغثاء معنى قد تداولته الشعراء "
                 "واكثروا فيه قال ابن الرومي جيف أنتنت فأضحت على "
                 "اللجة والدر تحتها في حجاب وينسب شعر المعالي شعر "
                 "فيه أما ترى البحر يعلو فوقه جيف ويستقر بأقصى "
                 "قعره الدرر فللزوم الدرر ومخبإه القرار وقد قيل "
                 "فيما ورد من الاثار ابتغوا الروق في خبايا الارض "
                 "أيها الغواص في البحار فان الصدف مما خبأته الارض "
                 "عن الاعين كما قيل في انها الجواهر في المعادن")
HEADING = "صفحة رقم: 102 تابع 27 تسمية ملوك القبط"


def page_html(prose):
    return ('<p class="g-paragraph g-rtl p-ctrl " id="p-1-1-1"> '
            + prose + "</p>")


def search_data(items):
    """items: [(book_id, page, title)] in the real hit shape."""
    return {"data": [{"id": page, "page": page,
                      "book": {"id": bid, "title": title,
                               "lang": "ar"},
                      "part": {"name": "1", "id": 1},
                      "html": page_html(PROSE), "content": ""}
                     for bid, page, title in items],
            "meta": {"total": len(items), "per_page": 25}}


def page_data(bid, page, prose):
    return {"id": page, "page": page,
            "book": {"id": bid, "title": "", "lang": "ar"},
            "part": {"name": "1", "id": 1},
            "content": page_html(prose)}


SEARCH = search_data([
    (16590, 487, "الكشف عن حقيقة الصوفية لأول مرة في التاريخ"),
    (57086, 10, "من علم الفلك القرآني"),
    (99999, 5, "كتاب بلا متن")])

META = {16590: {"data": {"id": 16590, "title": "الكشف عن حقيقة الصوفية",
                         "authors": []}},
        57086: {"data": {"id": 57086,
                         "title": "من علم <em>الفلك</em> القرآني",
                         "authors": [{"id": 1, "name": "د. عدنان شريف"}]}},
        992: {"data": {"id": 992,
                       "title": "تحقيق ما للهند من مقولة مقبولة في العقل أو مرذولة",
                       "authors": [{"id": 337, "name": "البيروني"}]}}}

# ------------------------------------------------------------- parsing
items = ba._ketabonline_parse_hits(SEARCH)
check("parse: 3 hit items extracted", len(items) == 3)
check("parse: book id (str) / title / page",
      items[0] == {"book_id": "16590",
                   "title": "الكشف عن حقيقة الصوفية لأول مرة في التاريخ",
                   "page": 487}
      and items[1]["book_id"] == "57086" and items[1]["page"] == 10)
check("parse: malformed payloads parse to nothing",
      ba._ketabonline_parse_hits(None) == []
      and ba._ketabonline_parse_hits({"data": "x"}) == []
      and ba._ketabonline_parse_hits(
          {"data": [{"book": {"id": "x"}, "page": 1},
                    {"page": 2}, "junk"]}) == [])

# ------------------------------------------------------------- page text / meta
real_json = ba._arab_json
try:
    ba._arab_json = lambda url, data=None: page_data(57086, 10, PROSE)
    txt = ba._ketabonline_page_text("57086", 10)
    check("page text: reader prose extracted from content HTML",
          "علماء الفلك" in txt)
    check("page text: passes the §46 text bar",
          ba._lib46_text_ok(txt))
    ba._arab_json = lambda url, data=None: page_data(102678, 100, HEADING)
    check("page text: heading page carries no prose",
          not ba._lib46_text_ok(ba._ketabonline_page_text("102678", 100)))
    ba._arab_json = lambda url, data=None: {"id": 1, "page": 1}
    check("page text: missing content field is empty",
          ba._ketabonline_page_text("1", 1) == "")
    ba._arab_json = lambda url, data=None: None
    check("page text: transport error fails soft",
          ba._ketabonline_page_text("57086", 10) == "")

    ba._arab_json = lambda url, data=None: META[57086]
    title, authors = ba._ketabonline_book_meta("57086")
    check("meta: title highlights stripped + author names",
          title == "من علم الفلك القرآني" and authors == ["د. عدنان شريف"])
    ba._arab_json = lambda url, data=None: None
    check("meta: transport error fails soft",
          ba._ketabonline_book_meta("57086") == ("", []))
finally:
    ba._arab_json = real_json

# ------------------------------------------------------------- searcher
def fake_json_factory(pages, metas, search=SEARCH):
    """pages: {(book_id_str, page): prose}; dispatch on the URL
    the way the real API routes are shaped."""
    def fake(url, data=None):
        if "/books/pages?" in url:
            return search
        m = re.search(r"/books/(\d+)/pages/(\d+)$", url)
        if m:
            prose = pages.get((m.group(1), int(m.group(2))))
            if prose is None:
                return None
            return page_data(int(m.group(1)), int(m.group(2)), prose)
        m = re.search(r"/books/(\d+)$", url)
        if m:
            return metas.get(int(m.group(1)))
        return None
    return fake


try:
    pages = {("16590", 487): PROSE, ("16590", 488): PROSE,
             ("57086", 10): PROSE,
             ("99999", 5): HEADING, ("99999", 6): HEADING,
             ("99999", 7): HEADING}
    ba._arab_json = fake_json_factory(pages, META)
    res = ba.search_ketabonline("الفلك")
    check("ketabonline: passage-carrying books become candidates",
          len(res) == 2)
    c0 = res[0]
    check("ketabonline: candidate tag is ketabonline:library",
          c0["index"] == "ketabonline:library"
          and c0["fulltext_route"] == "ketabonline:library")
    check("ketabonline: candidate carries fetched full text + passages",
          c0["fulltext"] is True and c0["passages"] is True
          and "الفلك" in c0["fulltext_excerpt"]
          and c0["passage_hits"] >= 1)
    check("ketabonline: title + author from the book record",
          res[1]["title"] == "من علم الفلك القرآني"
          and res[1]["authors"] == ["د. عدنان شريف"])
    check("ketabonline: candidate URL is the first hit page",
          c0["url"] == "https://ketabonline.com/ar/books/16590/read?page=487")
    check("ketabonline: language is ar, NOT marked primary",
          c0["language"] == "ar" and "source_type" not in c0)
    check("ketabonline: heading-only book yields NO candidate",
          all("99999" not in c["url"] for c in res))

    # Seed: a Tahqiq (992) hit is marked primary (the author's
    # own work, §50 seeds — the Ganjeh/Rafed pattern).
    SEED_SEARCH = search_data([(992, 8, "تحقيق ما للهند")])
    ba._arab_json = fake_json_factory(
        {("992", 8): JAMAHIR_PROSE, ("992", 9): JAMAHIR_PROSE}, META,
        search=SEED_SEARCH)
    res_s = ba.search_ketabonline("الهند")
    check("ketabonline: seed candidate emitted",
          len(res_s) == 1 and res_s[0]["passages"] is True)
    check("ketabonline: seed marked primary / author",
          res_s[0].get("source_type") == "primary"
          and res_s[0].get("primary_relation") == "author"
          and res_s[0]["authors"] == ["البيروني"])

    # Seed fallback: the book record fetch fails — the seed's
    # attested title/author stand in.
    ba._arab_json = fake_json_factory(
        {("992", 8): JAMAHIR_PROSE}, {}, search=SEED_SEARCH)
    res_f = ba.search_ketabonline("الهند")
    check("ketabonline: seed title/author survive a meta failure",
          len(res_f) == 1
          and res_f[0]["title"].startswith("تحقيق ما للهند")
          and res_f[0]["authors"] == ["البيروني"]
          and res_f[0].get("source_type") == "primary")

    # Fallback: the platform index matched but the fetched
    # prose carries none of the query terms — the real prose
    # stands as the passage.
    ba._arab_json = fake_json_factory(pages, META)
    res_b = ba.search_ketabonline("البيروني")
    check("ketabonline: index-matched book still yields a candidate "
          "(platform index matched, real prose fetched)",
          len(res_b) == 2 and res_b[0]["passages"] is True
          and res_b[0]["passage_hits"] == 2)

    many = search_data([(2000 + i, 5, f"كتاب {i}") for i in range(10)])
    ba._arab_json = fake_json_factory(
        {(str(2000 + i), 5): PROSE for i in range(10)}, {},
        search=many)
    check("ketabonline: capped at KETABONLINE_CAP == 8",
          len(ba.search_ketabonline("الفلك")) == ba.KETABONLINE_CAP == 8)

    ba._arab_json = lambda url, data=None: None
    check("ketabonline: fails soft on transport error",
          ba.search_ketabonline("الفلك") == [])
    calls = []
    ba._arab_json = lambda url, data=None: calls.append(url) or None
    check("ketabonline: empty query makes no request",
          ba.search_ketabonline("  ") == [] and calls == [])
finally:
    ba._arab_json = real_json

# ------------------------------------------------------------- registration
check("search_ketabonline registered in SEARCHERS",
      ba.search_ketabonline in ba.SEARCHERS)
check("search_ketabonline registered in WAVE1",
      ba.search_ketabonline in ba.WAVE1_SEARCHERS)
check("search_ketabonline registered in the §43 new-only set",
      ba.search_ketabonline in ba.NEW_ONLY_SEARCHERS)
check("searcher label present",
      ba.SEARCHER_LABEL["search_ketabonline"] == "KetabOnline library (§50)")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
