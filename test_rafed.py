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
need = {"name", "url", "type", "fields", "languages", "notes",
        "verified", "verified_date"}
rreg = json.loads((BASE/"rafed_registry.json").read_text(encoding="utf-8"))

check("rafed registry: platform + 6 book entries", len(rreg) == 7)
check("registry carries the registry schema",
      all(need <= set(e) for e in rreg))
check("all entries verified 2026-11-26",
      all(e["verified"] is True and e["verified_date"] == "2026-11-26"
          for e in rreg))
r0 = rreg[0]
check("rafed platform entry",
      r0["url"] == "https://books.rafed.net/"
      and r0["type"] == "platform"
      and "/search?q=" in r0["api"]
      and "page_data" in r0["api"])
check("platform notes record the routes tried + caveats",
      "OAI-PMH" in r0["notes"] and "sitemap" in r0["notes"]
      and "lib.rafed.net" in r0["notes"]
      and "NOT hosted" in r0["notes"] and "3036" in r0["notes"])
check("book entries point at the six Shifa part ids",
      [e["url"] for e in rreg[1:]]
      == ["https://books.rafed.net/view/3037",
          "https://books.rafed.net/view/3066",
          "https://books.rafed.net/view/3068",
          "https://books.rafed.net/view/3070",
          "https://books.rafed.net/view/3072",
          "https://books.rafed.net/view/3075"])
check("registry book entries match the code seed set",
      {e["url"].rsplit("/", 1)[1] for e in rreg[1:]}
      == set(ba._RAFED_SEEDS))

for mod, tag in ((ba, "build_article"), (pt, "propose_topics")):
    di, ui, st = mod.load_source_registry()
    check(f"{tag} loads 22 files / 2195 entries",
          st["files_ok"] == 22 and st["loaded"] == 2195)
    check(f"{tag} §49 domain indexed", "books.rafed.net" in di)

# ------------------------------------------------------------- fixtures
# Fixtures trimmed from the REAL payloads probed live 2026-11-26:
# the search items are books 3064/3559 exactly as the platform
# returned them for البيروني (book 99999 is synthetic, added to
# prove rejection); the reader prose is book 900 p. 10 / p. 100
# verbatim and book 3066 p. 50 verbatim (the Shifa seed).
PROSE = ("بصورة واضحة مع أية آية قرآنية هو مرفوض مهما كانت صحّة سنده "
         "استنادا لقوله تعالى : ( وَلَوْ تَقَوَّلَ عَلَيْنا بَعْضَ "
         "الْأَقاوِيلِ. لَأَخَذْنا مِنْهُ بِالْيَمِينِ. ثُمَّ "
         "لَقَطَعْنا مِنْهُ الْوَتِينَ. فَما مِنْكُمْ مِنْ أَحَدٍ "
         "عَنْهُ حاجِزِينَ ) الكون المغلف كما تصوّره الأقدمون حتى "
         "القرن الرابع عشر الميلادي اعتقد أكثر علماء الفلك بأن "
         "الشمس والقمر والكواكب تدور في مسارات دائرية حول الأرض "
         "الثابتة التي هي مركز الكون كله عندهم")
SHIFA_PROSE = ("الفصل السابع (١) فصل فى حشو الجسم السماوى وما قاله "
               "الناس فى أحوال الأرض وسائر العناصر نقول إن الجرم "
               "المتحرك بالاستدارة حركة وضعية يلزم (٢) ضرورة أن "
               "يكون فيه اختلاف حال (٣) عند الحركة. فإن ثبات "
               "الأحوال كلها مدافع للحركة مقابل لها ؛ إذ هذه "
               "الحركة لا تكون إلا عن اختلاف فى الأحوال والأوضاع "
               "عند كل جزء من أجزاء الجرم المتحرك بها على الدوام")

def push(payload):
    """Wrap payload text the way the real pages carry it: as a
    JSON string inside a self.__next_f.push([1, ...]) script."""
    return ("<html><body><script>self.__next_f.push([1,"
            + json.dumps(payload, ensure_ascii=False)
            + "])</script></body></html>")

def search_payload(items):
    """items: [(book_id, hit_page, title, author)] in the real
    item shape (h2 link to the hit page, author link text)."""
    parts = []
    for i, (bid, page, title, author) in enumerate(items):
        parts.append(
            '["$","div","%d",{"children":[["$","small",null,'
            '{"children":""}],["$","h2",null,{"children":'
            '["$","$L8",null,{"href":"/view/%s/page/%d?search=x",'
            '"children":"%s"}]}]],["$","div",null,{"children":'
            '[["$","$L8",null,{"className":"font-bold",'
            '"href":"author/x","children":"%s"}]]}]]}]'
            % (i, bid, page, title, author))
    return ('["$","div",null,{"children":"عدد النتائج"},'
            + ",".join(parts) + "]")

def reader_payload_ref(prose):
    """A reader payload whose page_data is a Flight
    back-reference ($56) to a raw text row, as probed live."""
    frag = ('<div class="pgcontent">\n<p class=rfdNormal0>'
            + prose + "</p></div>")
    return ('55:[["$","$L55",null,{"page_number":10,"pages":'
            '[null,{"page_data":"$56","page_image":"",'
            '"page_type":-1,"page_number":10}]}]]\n'
            "56:T" + format(len(frag.encode("utf-8")), "x")
            + "," + frag
            + '\n57:["$","div",null,{"children":"نص زائد لاحق"}]')

def reader_payload_inline(prose):
    frag = ('<div class="pgcontent">\n<p class=rfdNormal0>'
            + prose + "</p></div>")
    return ('55:[["$","$L55",null,{"page_number":10,"pages":'
            '[null,{"page_data":' + json.dumps(frag)
            + ',"page_image":"","page_type":-1,'
            '"page_number":10}]}]]')

SEARCH_BODY = push(search_payload([
    ("3064", 245, "تاريخ الأدب الجغرافي العربي",
     "اغناطيوس يوليانوفتش كراتشكوفسكي"),
    ("3559", 313, "تاريخ الإسلام ووفيات المشاهير والأعلام",
     "شمس الدين محمّد بن أحمد بن عثمان الذّهبي"),
    ("99999", 1, "كتاب بلا متن", "مؤلف مجهول")]))

# ------------------------------------------------------------- payload
check("payload: chunks decoded and concatenated",
      ba._rafed_payload(SEARCH_BODY).count("/view/3064/page/245") == 1)
check("payload: absent payload is empty",
      ba._rafed_payload("<html><body>plain</body></html>") == ""
      and ba._rafed_payload("") == "")
check("flight text: row resolved by its hex length",
      "الكون المغلف" in ba._rafed_flight_text(
          ba._rafed_payload(push(reader_payload_ref(PROSE))), "56"))
check("flight text: byte-exact — the following row never leaks in",
      "نص زائد لاحق" not in ba._rafed_flight_text(
          ba._rafed_payload(push(reader_payload_ref(PROSE))), "56"))
check("flight text: missing row is empty",
      ba._rafed_flight_text("no rows here", "56") == "")

# ------------------------------------------------------------- parsing
items = ba._rafed_parse_results(ba._rafed_payload(SEARCH_BODY))
check("parse: 3 hit items extracted", len(items) == 3)
check("parse: book id / hit page / title / author",
      items[0] == {"book_id": "3064", "page": 245,
                   "title": "تاريخ الأدب الجغرافي العربي",
                   "author": "اغناطيوس يوليانوفتش كراتشكوفسكي"}
      and items[1]["book_id"] == "3559"
      and items[1]["page"] == 313)
check("parse: empty payload parses to nothing",
      ba._rafed_parse_results("") == [])

# ------------------------------------------------------------- page text
real_fetch = ba._arab_fetch
try:
    ba._arab_fetch = lambda url, data=None: (
        200, push(reader_payload_ref(PROSE)))
    txt = ba._rafed_page_text("900", 10)
    check("page text: back-referenced prose extracted",
          "علماء الفلك" in txt)
    check("page text: passes the §46 text bar",
          ba._lib46_text_ok(txt))
    ba._arab_fetch = lambda url, data=None: (
        200, push(reader_payload_inline(PROSE)))
    check("page text: inline fragment variant extracted",
          "الكون المغلف" in ba._rafed_page_text("900", 10))
    cover = ('55:[["$","$L55",null,{"page_number":1,"pages":'
             '[null,{"page_data":"<div class=\\"pgcontent\\">١'
             '</div>","page_image":"x.jpg","page_type":-1,'
             '"page_number":1}]}]]')
    ba._arab_fetch = lambda url, data=None: (200, push(cover))
    check("page text: cover page carries no prose",
          not ba._lib46_text_ok(ba._rafed_page_text("900", 1)))
    ba._arab_fetch = lambda url, data=None: (404, "")
    check("page text: HTTP 404 fails soft",
          ba._rafed_page_text("900", 10) == "")
    ba._arab_fetch = lambda url, data=None: (None, "")
    check("page text: transport error fails soft",
          ba._rafed_page_text("900", 10) == "")
finally:
    ba._arab_fetch = real_fetch

# ------------------------------------------------------------- searcher
def fake_fetch_factory(pages):
    """pages: {url-substring: body}; the search URL is fixed."""
    def fake(url, data=None):
        if "/search?" in url:
            return 200, SEARCH_BODY
        for key, body in pages.items():
            if key in url:
                return 200, body
        return 404, ""
    return fake


try:
    pages = {"/view/3064/page/": push(reader_payload_ref(PROSE)),
             "/view/3559/page/": push(reader_payload_ref(PROSE)),
             "/view/99999/page/": push(cover)}
    ba._arab_fetch = fake_fetch_factory(pages)
    res = ba.search_rafed("الفلك")
    check("rafed: passage-carrying books become candidates",
          len(res) == 2)
    c0 = res[0]
    check("rafed: candidate tag is rafed:books",
          c0["index"] == "rafed:books"
          and c0["fulltext_route"] == "rafed:books")
    check("rafed: candidate carries fetched full text + passages",
          c0["fulltext"] is True and c0["passages"] is True
          and "الفلك" in c0["fulltext_excerpt"]
          and c0["passage_hits"] >= 1)
    check("rafed: title + author from the search item",
          c0["title"] == "تاريخ الأدب الجغرافي العربي"
          and c0["authors"] == ["اغناطيوس يوليانوفتش كراتشكوفسكي"])
    check("rafed: candidate URL is the first hit page",
          c0["url"] == "https://books.rafed.net/view/3064/page/245")
    check("rafed: language is ar, NOT marked primary",
          c0["language"] == "ar" and "source_type" not in c0)
    check("rafed: textless book yields NO candidate",
          all("99999" not in c["url"] for c in res))

    # Seed: a Shifa part hit is marked primary (the author's
    # own work, §49 seeds — the Ganjeh/shelf pattern).
    SEED_BODY = push(search_payload([
        ("3037", 100, "الشفاء ـ طبيعيّات",
         "أبو علي حسين بن عبد الله بن سينا [ شيخ الرئيس ابن سينا ]")]))
    def fake_seed(url, data=None):
        if "/search?" in url:
            return 200, SEED_BODY
        if "/view/3037/page/" in url:
            return 200, push(reader_payload_ref(SHIFA_PROSE))
        return 404, ""
    ba._arab_fetch = fake_seed
    res_s = ba.search_rafed("الجسم السماوي")
    check("rafed: Shifa seed candidate emitted",
          len(res_s) == 1 and res_s[0]["passages"] is True)
    check("rafed: Shifa seed marked primary / author",
          res_s[0].get("source_type") == "primary"
          and res_s[0].get("primary_relation") == "author")

    # Fallback: the platform index matched but the fetched
    # prose carries none of the query terms (the ى/ي variant
    # case) — the real prose stands as the passage.
    ba._arab_fetch = fake_fetch_factory(pages)
    res_b = ba.search_rafed("البيروني")
    check("rafed: index-matched book still yields a candidate "
          "(platform index matched, real prose fetched)",
          len(res_b) == 2 and res_b[0]["passages"] is True
          and res_b[0]["passage_hits"] == 3)

    many = push(search_payload([
        (str(2000 + i), 5, f"كتاب {i}", "مؤلف") for i in range(10)]))
    def fake_many(url, data=None):
        if "/search?" in url:
            return 200, many
        if "/page/" in url:
            return 200, push(reader_payload_ref(PROSE))
        return 404, ""
    ba._arab_fetch = fake_many
    check("rafed: capped at RAFED_CAP == 8",
          len(ba.search_rafed("الفلك")) == ba.RAFED_CAP == 8)

    ba._arab_fetch = lambda url, data=None: (None, "")
    check("rafed: fails soft on transport error",
          ba.search_rafed("الفلك") == [])
    ba._arab_fetch = lambda url, data=None: (500, "err")
    check("rafed: fails soft on HTTP 500",
          ba.search_rafed("الفلك") == [])
    calls = []
    ba._arab_fetch = lambda url, data=None: calls.append(url) or (200, "")
    check("rafed: empty query makes no request",
          ba.search_rafed("  ") == [] and calls == [])
finally:
    ba._arab_fetch = real_fetch

# ------------------------------------------------------------- registration
check("search_rafed registered in SEARCHERS",
      ba.search_rafed in ba.SEARCHERS)
check("search_rafed registered in WAVE1",
      ba.search_rafed in ba.WAVE1_SEARCHERS)
check("search_rafed registered in the §43 new-only set",
      ba.search_rafed in ba.NEW_ONLY_SEARCHERS)
check("searcher label present",
      ba.SEARCHER_LABEL["search_rafed"] == "Rafed library (§49)")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
