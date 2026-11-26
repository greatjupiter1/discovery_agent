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
ireg = json.loads((BASE/"islamweb_registry.json").read_text(encoding="utf-8"))

check("islamweb registry: platform + 3 book entries", len(ireg) == 4)
check("registry carries the registry schema",
      all(need <= set(e) for e in ireg))
check("all entries verified 2026-11-26",
      all(e["verified"] is True and e["verified_date"] == "2026-11-26"
          for e in ireg))
i0 = ireg[0]
check("islamweb platform entry",
      i0["url"] == "https://www.islamweb.net/ar/library/index.php"
      and i0["type"] == "platform"
      and "explibtab.php" in i0["api"]
      and "/ar/library/content/" in i0["api"])
check("platform notes record the routes tried + caveats",
      "expsearch" in i0["notes"] and "bookslist" in i0["notes"]
      and "وري" in i0["notes"] and "light page variant" in i0["notes"]
      and "NOT hosted" in i0["notes"])
check("book entries point at their book ids",
      [e["url"] for e in ireg[1:]]
      == ["https://www.islamweb.net/ar/library/content/60/1",
          "https://www.islamweb.net/ar/library/content/122/1",
          "https://www.islamweb.net/ar/library/content/303/1"])

for mod, tag in ((ba, "build_article"), (pt, "propose_topics")):
    di, ui, st = mod.load_source_registry()
    check(f"{tag} loads 21 files / 2188 entries",
          st["files_ok"] == 21 and st["loaded"] == 2188)
    check(f"{tag} §48 domain indexed", "islamweb.net" in di)

# ------------------------------------------------------------- parsing
# Fixture trimmed from the REAL explibtab response probed live
# 2026-11-26 (query الفلك: books 124/125 are real hits with real
# book paths; book 99999 is synthetic, added to prove rejection).
SEARCH_HTML = """
<div class="global-searchresult">
 <ul class="oneitems search-results w-border">
  <li style="padding:10px 40px 15px 15px;">
   <h5 style="font-weight:bold;margin-bottom: 15px;">
   <a style="color: #002d6a;" target="_blank" href="https://islamweb.net/ar/library/content/124/3293/باب-الفاء-واللام-وما-يثلثهما">
   باب الفاء واللام وما يثلثهما</a></h5>
   <div class="desc" style="line-height: 150%;">( فلك ) الفاء واللام ...</div>
   <div style="margin-top: 15px;" class="book-name">
   <span style="font-size: 17px !important;">معجم مقاييس اللغة &gt; كتاب الفاء &gt; باب الفاء واللام وما يثلثهما </span>
   </div></li>
  <li style="padding:10px 40px 15px 15px;">
   <h5 style="font-weight:bold;margin-bottom: 15px;">
   <a style="color: #002d6a;" target="_blank" href="https://islamweb.net/ar/library/content/124/3294/باب-الفاء-والميم">
   باب الفاء والميم</a></h5>
   <div class="desc" style="line-height: 150%;">( فم ) الفاء والميم ...</div>
   <div style="margin-top: 15px;" class="book-name">
   <span style="font-size: 17px !important;">معجم مقاييس اللغة &gt; كتاب الفاء &gt; باب الفاء والميم </span>
   </div></li>
  <li style="padding:10px 40px 15px 15px;">
   <h5 style="font-weight:bold;margin-bottom: 15px;">
   <a style="color: #002d6a;" target="_blank" href="https://islamweb.net/ar/library/content/99999/1/كتاب-الجداول">
   كتاب الجداول</a></h5>
   <div class="desc" style="line-height: 150%;">...</div>
   <div style="margin-top: 15px;" class="book-name">
   <span style="font-size: 17px !important;">كتاب الجداول </span>
   </div></li>
 </ul>
</div>"""

items = ba._islamweb_parse_results(SEARCH_HTML)
check("parse: 3 hit items extracted", len(items) == 3)
check("parse: book id / node id / url",
      items[0]["book_id"] == "124" and items[0]["node_id"] == "3293"
      and items[0]["url"].startswith(
          "https://islamweb.net/ar/library/content/124/3293/"))
check("parse: book title is the path's first segment",
      items[0]["book_title"] == "معجم مقاييس اللغة"
      and items[2]["book_title"] == "كتاب الجداول")
check("parse: node title from the h5 link",
      items[0]["node_title"] == "باب الفاء واللام وما يثلثهما")
check("parse: empty body parses to nothing",
      ba._islamweb_parse_results("") == [])

# ------------------------------------------------------------- page text
PROSE = ("مذكور ومدته من الولادة إلى الإيلاد مائة وثلاثون سنة وإنهم "
         "نقصوا من مدد من كان بعد سام بن نوح إلى ناحور من كل واحد "
         "مائة ، ومن مدد ناحور جد إبراهيم خمسين سنة فصارت المدد 292 "
         "ونقصت السامرة مع ذلك من مدة يرخ والد إبراهيم خمسين سنة "
         "فصارت المدة 242 ، وزعم اندرونيقوس أن مدة قينان الساقط مائة "
         "وتسع وثلاثون سنة فصارت السنون عنده 1081 ولم يعده أرسايس "
         "القيساري في الجملة كما لم يعده العبرانيون")
FALAK_PROSE = ("( فلك ) الفاء واللام والكاف أصل صحيح يدل على استدارة "
               "في شيء . من ذلك فلكة المغزل بفتح الفاء ، سميت "
               "لاستدارتها ; ولذلك قيل : فلك ثدي المرأة ، إذا استدار . "
               + PROSE)
PAGE_HTML = f"""<html><body>
<div class="portalheader"><div class="mx-2">
<h3 class="txt-blue">معجم مقاييس اللغة</h3>
<a target="_blank" href="/ar/library/index.php?page=bookslist&publisher=&author=227">
<h4 class="txt-secondary">ابن فارس - أبو الحسين أحمد بن فارس بن زكريا القزويني الرازي</h4></a>
</div></div>
<div class="bookcontent-dic" id='pagebody_thaskeel' style='display:none'>
<p>THASKEEL_DUPLICATE يجب ألا يظهر</p></div>
<div class="bookcontent-dic" id='pagebody' itemprop="articleBody">
<p>{FALAK_PROSE}</p>
<div class="hashiya"><p>حاشية داخلية في الفلك والنجوم</p></div>
</div>
<div id='pagebody' class="bookcontent-dic">
<p>قطعة ثانية من متن الكتاب في الفلك أيضا ، تتابع الأولى وتكملها
بكلام متصل في أسماء الأفلاك وحركاتها ومقاديرها عند أهل اللغة</p>
</div>
<a href="#" onmouseover="Tip('عرض > التفاصيل WIDTH, 280')">مرجع</a>
</body></html>"""

txt = ba._islamweb_pagebody(PAGE_HTML)
check("pagebody: real prose extracted", "فلكة المغزل" in txt)
check("pagebody: nested div text kept", "حاشية داخلية" in txt)
check("pagebody: second block joined", "قطعة ثانية" in txt)
check("pagebody: thaskeel duplicate never leaks",
      "THASKEEL_DUPLICATE" not in txt)
check("pagebody: passes the §46 text bar", ba._lib46_text_ok(txt))
check("pagebody: absent block is empty",
      ba._islamweb_pagebody("<html><body>light variant</body></html>")
      == "")
check("text: tooltip attribute junk never leaks",
      "WIDTH" not in ba._islamweb_text(
          '<a onmouseover="Tip(\'x > y WIDTH, 280\')">نص</a>')
      and ba._islamweb_text(
          '<a href="/ar/library/index.php?page=treesubj&link=12">'
          'index.php?page=treesubj&link=12</a> نص عربي') == "نص عربي")
title, author = ba._islamweb_book_meta(PAGE_HTML)
check("meta: book title from h3.txt-blue",
      title == "معجم مقاييس اللغة")
check("meta: author keeps the full name after the short name",
      author == "أبو الحسين أحمد بن فارس بن زكريا القزويني الرازي")
check("meta: absent header is empty",
      ba._islamweb_book_meta("<html></html>") == ("", ""))

# ------------------------------------------------------------- searcher
def fake_fetch_factory(pages, light_once=()):
    """pages: {url-substring: html}; light_once: substrings whose
    FIRST fetch returns a light page (no pagebody)."""
    counts = {}
    def fake(url, data=None):
        if "explibtab.php" in url:
            return 200, SEARCH_HTML
        for key, html in pages.items():
            if key in url:
                counts[key] = counts.get(key, 0) + 1
                if key in light_once and counts[key] == 1:
                    return 200, "<html><body>light</body></html>"
                return 200, html
        return 404, ""
    return fake, counts


real_fetch = ba._arab_fetch
try:
    pages = {"/content/124/3293/": PAGE_HTML,
             "/content/124/3294/": PAGE_HTML,
             "/content/99999/1/": "<html><body><div id='pagebody'>"
                                  "جدول أرقام فقط</div></body></html>"}
    fake, counts = fake_fetch_factory(pages, light_once=("/content/124/3293/",))
    ba._arab_fetch = fake
    res = ba.search_islamweb("الفلك")
    check("islamweb: passage-carrying book becomes a candidate",
          len(res) == 1)
    c0 = res[0]
    check("islamweb: candidate tag is islamweb:library",
          c0["index"] == "islamweb:library"
          and c0["fulltext_route"] == "islamweb:library")
    check("islamweb: candidate carries fetched full text + passages",
          c0["fulltext"] is True and c0["passages"] is True
          and "فلك" in c0["fulltext_excerpt"]
          and c0["passage_hits"] >= 1)
    check("islamweb: title + author from the content page header",
          c0["title"] == "معجم مقاييس اللغة"
          and c0["authors"] == ["أبو الحسين أحمد بن فارس بن زكريا "
                                 "القزويني الرازي"])
    check("islamweb: language is ar, NOT marked primary",
          c0["language"] == "ar" and "source_type" not in c0)
    check("islamweb: light-variant first fetch was retried",
          counts["/content/124/3293/"] == 2)
    check("islamweb: textless book yields NO candidate",
          all("99999" not in c["url"] for c in res))

    # Fallback: the platform index matched but the fetched prose
    # carries none of the query terms (the stemmer case, probed
    # live for البيروني) — the real prose stands as the passage.
    LISAN_HTML = ("<html><body><h3 class=\"txt-blue\">لسان العرب</h3>"
                  "<h4 class=\"txt-secondary\">ابن منظور - أبو الفضل "
                  "جمال الدين محمد بن مكرم الأنصاري</h4>"
                  "<div class=\"bookcontent-dic\" id='pagebody'><p>"
                  + PROSE + "</p></div></body></html>")
    SEARCH_B = SEARCH_HTML.replace("/124/3293/", "/122/9007/") \
                          .replace("/124/3294/", "/122/9008/") \
                          .replace("معجم مقاييس اللغة", "لسان العرب")
    def fake_b(url, data=None):
        if "explibtab.php" in url:
            return 200, SEARCH_B
        if "/content/122/" in url:
            return 200, LISAN_HTML
        return 404, ""
    ba._arab_fetch = fake_b
    res_b = ba.search_islamweb("البيروني")
    check("islamweb: stemmer-matched book still yields a candidate "
          "(platform index matched, real prose fetched)",
          len(res_b) == 1 and res_b[0]["title"] == "لسان العرب"
          and res_b[0]["passages"] is True
          and res_b[0]["passage_hits"] == 2)

    many = "".join(
        f'<li><h5><a href="https://islamweb.net/ar/library/content/'
        f'{2000 + i}/1/كتاب">كتاب {i}</a></h5>'
        f'<div class="book-name"><span>كتاب {i} </span></div></li>'
        for i in range(10))
    def fake_many(url, data=None):
        if "explibtab.php" in url:
            return 200, f'<ul class="oneitems">{many}</ul>'
        if "/content/" in url:
            return 200, PAGE_HTML
        return 404, ""
    ba._arab_fetch = fake_many
    check("islamweb: capped at ISLAMWEB_CAP == 8",
          len(ba.search_islamweb("الفلك")) == ba.ISLAMWEB_CAP == 8)

    ba._arab_fetch = lambda url, data=None: (None, "")
    check("islamweb: fails soft on transport error",
          ba.search_islamweb("الفلك") == [])
    ba._arab_fetch = lambda url, data=None: (500, "err")
    check("islamweb: fails soft on HTTP 500",
          ba.search_islamweb("الفلك") == [])
    calls = []
    ba._arab_fetch = lambda url, data=None: calls.append(url) or (200, "")
    check("islamweb: empty query makes no request",
          ba.search_islamweb("  ") == [] and calls == [])
finally:
    ba._arab_fetch = real_fetch

# ------------------------------------------------------------- registration
check("search_islamweb registered in SEARCHERS",
      ba.search_islamweb in ba.SEARCHERS)
check("search_islamweb registered in WAVE1",
      ba.search_islamweb in ba.WAVE1_SEARCHERS)
check("search_islamweb registered in the §43 new-only set",
      ba.search_islamweb in ba.NEW_ONLY_SEARCHERS)
check("searcher label present",
      ba.SEARCHER_LABEL["search_islamweb"] == "IslamWeb library (§48)")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
