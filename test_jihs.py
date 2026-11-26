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
raw = json.loads((BASE/"jihs_registry.json").read_text(encoding="utf-8"))
check("registry has exactly 1 journal entry", len(raw) == 1)
need = {"name", "url", "type", "fields", "languages", "api", "notes",
        "verified", "verified_date"}
check("registry entry carries the registry schema",
      all(need <= set(e) for e in raw))
e0 = raw[0]
check("entry is the JIHS journal",
      e0["url"] == "https://jihs.ut.ac.ir" and e0["type"] == "journal")
check("entry verified 2026-11-26",
      e0["verified"] is True and e0["verified_date"] == "2026-11-26")
check("languages are Persian + English",
      e0["languages"] == ["Persian", "English"])
check("history of science is a covered field",
      "history of science" in e0["fields"])
check("notes record the routes tried",
      "sitemap" in e0["notes"] and "RSS" in e0["notes"]
      and "pdf_link" in e0["notes"] and "Ensani" in e0["notes"])

for mod, tag in ((ba, "build_article"), (pt, "propose_topics")):
    di, ui, st = mod.load_source_registry()
    check(f"{tag} loads 21 files / 2188 entries",
          st["files_ok"] == 21 and st["loaded"] == 2188)
    check(f"{tag} jihs indexed by domain", "jihs.ut.ac.ir" in di)

# ------------------------------------------------------------- fixtures
# Trimmed from the REAL journal-site responses probed live
# 2026-11-26: the بیرونی search-results page (21 results) — one
# online-first block with NO PDF anywhere yet (108572, must be
# skipped) and three published blocks carrying direct PDF links.
SEARCH_HTML = "<div class=\"col-md-12\" id=\"dv_ar_count\">تعداد مقالات: <span class=\"badge bg-body-secondary text-dark px-3 mb-2\">21</span></div>\n<div class=''>        <h2 class=\"mb-2 list-article-title text-justify fw-bold \" dir=\"rtl\">\n        <a class=\"tag_a\" href=\"article_108572.html\">از تداوم تا تحول: نقش میراث علمی و نهادی ایران پیش از اسلام در شکوفایی ریاضیات در تمدن اسلامی</a>\n    </h2>\n\n    \n            <p class=\"mb-1\">مقالات آماده انتشار، پذیرفته شده، انتشار آنلاین از <span >10 شهریور 1405</span></p>\n    \n            <p class=\"mb-1 \" dir=\"rtl\" id=\"ar_doi\"><i class=\"ai ai-doi text-warning fa-lg me-1\"></i> <span dir=\"ltr\"><a href=\"https://doi.org/10.22059/jihs.2026.420180.371926\" >10.22059/jihs.2026.420180.371926</a></span></p>\n    \n            <p class=\"mb-1 \" dir=\"rtl\" >علی  پاکدامن</p>\n        \n    <ul class=\"list-inline d-flex align-items-center\">\n\t<li class=\"lis-item me-3\"><a href=\"./article_108572.html\" >مشاهده مقاله</a></li>\n\t    </ul>\n    \n</div>\n<div class=''>    \t<div class=\"col-md-6 pt-1 pb-3\"><div class=\"flex-grow-1 border-top opacity-20\"></div></div>\n\t    <h2 class=\"mb-2 list-article-title text-justify fw-bold \" dir=\"rtl\">\n        <a class=\"tag_a\" href=\"article_106822.html\">سخن سردبیر</a>\n    </h2>\n\n    \n            <p class=\"mb-1\">دوره 23، شماره 2، اسفند 1404</p>\n    \n            <p class=\"mb-1 \" dir=\"rtl\" id=\"ar_doi\"><i class=\"ai ai-doi text-warning fa-lg me-1\"></i> <span dir=\"ltr\"><a href=\"https://doi.org/10.22059/jihs.2026.106822\" >10.22059/jihs.2026.106822</a></span></p>\n    \n            <p class=\"mb-1 \" dir=\"rtl\" >امیرمحمد  گمینی</p>\n        \n    <ul class=\"list-inline d-flex align-items-center\">\n\t<li class=\"lis-item me-3\"><a href=\"./article_106822.html\" >سخن سردبیر</a></li>\n\t        <li class=\"lis-item me-3\"><a href=\"./article_106822_aa3f72012e0e65639db9654b319e0acd.pdf\" target=\"_blank\" class=\"pdf_link\"><i class=\"fa-solid fa-file-pdf text-danger\" ></i> اصل مقاله <span dir=\"ltr\" class=\"badge bg-light text-dark\">281.37 K</span></a></li>\n        </ul>\n    \n</div>\n<div class=''>    \t<div class=\"col-md-6 pt-1 pb-3\"><div class=\"flex-grow-1 border-top opacity-20\"></div></div>\n\t    <h2 class=\"mb-2 list-article-title text-justify fw-bold \" dir=\"rtl\">\n        <a class=\"tag_a\" href=\"article_106655.html\">تاریخ رصدگری تا سدۀ 4 قمری به روایت زیج حاکمی کبیر و متون موازی آن</a>\n    </h2>\n\n    \n            <p class=\"mb-1\">دوره 23، شماره 2، اسفند 1404، صفحه <span >163-198</span></p>\n    \n            <p class=\"mb-1 \" dir=\"rtl\" id=\"ar_doi\"><i class=\"ai ai-doi text-warning fa-lg me-1\"></i> <span dir=\"ltr\"><a href=\"https://doi.org/10.22059/jihs.2026.410032.371873\" >10.22059/jihs.2026.410032.371873</a></span></p>\n    \n            <p class=\"mb-1 \" dir=\"rtl\" >یونس  کرامتی</p>\n        \n    <ul class=\"list-inline d-flex align-items-center\">\n\t<li class=\"lis-item me-3\"><a href=\"./article_106655.html\" >مشاهده مقاله</a></li>\n\t        <li class=\"lis-item me-3\"><a href=\"./article_106655_25420d1f995b4897f263c907e7ad3f66.pdf\" target=\"_blank\" class=\"pdf_link\"><i class=\"fa-solid fa-file-pdf text-danger\" ></i> اصل مقاله <span dir=\"ltr\" class=\"badge bg-light text-dark\">1.1 M</span></a></li>\n        </ul>\n    \n</div>\n<div class=''>    \t<div class=\"col-md-6 pt-1 pb-3\"><div class=\"flex-grow-1 border-top opacity-20\"></div></div>\n\t    <h2 class=\"mb-2 list-article-title text-justify fw-bold \" dir=\"rtl\">\n        <a class=\"tag_a\" href=\"article_85426.html\">بررسی مقومات نظریۀ پزشکی بقراط: سلامتی، بیماری و درمان</a>\n    </h2>\n\n    \n            <p class=\"mb-1\">دوره 19، شماره 1، مرداد 1400، صفحه <span >245-274</span></p>\n    \n            <p class=\"mb-1 \" dir=\"rtl\" id=\"ar_doi\"><i class=\"ai ai-doi text-warning fa-lg me-1\"></i> <span dir=\"ltr\"><a href=\"https://doi.org/10.22059/jihs.2021.332259.371655\" >10.22059/jihs.2021.332259.371655</a></span></p>\n    \n            <p class=\"mb-1 \" dir=\"rtl\" >فرزانه  عامری، غلامحسین  مقدم حیدری، علیرضا  منجمی</p>\n        \n    <ul class=\"list-inline d-flex align-items-center\">\n\t<li class=\"lis-item me-3\"><a href=\"./article_85426.html\" >مشاهده مقاله</a></li>\n\t        <li class=\"lis-item me-3\"><a href=\"./article_85426_4a7d6903e1038e62204acbdf1b8ffc10.pdf\" target=\"_blank\" class=\"pdf_link\"><i class=\"fa-solid fa-file-pdf text-danger\" ></i> اصل مقاله <span dir=\"ltr\" class=\"badge bg-light text-dark\">655.66 K</span></a></li>\n        </ul>\n    \n</div>"

cands = ba._parse_jihs_results(SEARCH_HTML)
check("parser finds the published fixture results", len(cands) == 3)
check("online-first hit without a PDF is skipped",
      all("108572" not in c["url"] for c in cands))
c0 = cands[0]
check("first candidate is سخن سردبیر",
      c0["title"] == "سخن سردبیر")
check("candidate index tag is persian:jihs",
      c0["index"] == "persian:jihs")
check("candidate url is the article page",
      c0["url"] == "https://jihs.ut.ac.ir/article_106822.html")
check("candidate carries the direct PDF file",
      c0["oa_pdf_url"] ==
      "https://jihs.ut.ac.ir/article_106822_"
      "aa3f72012e0e65639db9654b319e0acd.pdf")
check("candidate authors parsed", c0["authors"] == ["امیرمحمد گمینی"])
check("candidate year reads the Latin-digit Jalali year",
      c0["year"] == 1404)
check("candidate doi parsed",
      c0["doi"] == "10.22059/jihs.2026.106822")
check("candidate venue names journal + issue",
      c0["venue"].startswith("تاریخ علم") and "دوره 23" in c0["venue"])
c2 = cands[2]
check("multi-author block splits on the Persian comma",
      c2["authors"] == ["فرزانه عامری", "غلامحسین مقدم حیدری",
                         "علیرضا منجمی"])
check("multi-author block year", c2["year"] == 1400)
check("every candidate carries a PDF url",
      all(c["oa_pdf_url"].endswith(".pdf") for c in cands))

check("year helper: Latin Jalali", ba._jihs_year("اسفند 1404") == 1404)
check("year helper: Persian digits", ba._jihs_year("مرداد ۱۴۰۰") == 1400)
check("year helper: Gregorian", ba._jihs_year("2026") == 2026)
check("year helper: none", ba._jihs_year("بدون سال") is None)

check("empty/garbage pages parse to nothing",
      ba._parse_jihs_results("") == []
      and ba._parse_jihs_results("<html></html>") == [])

# ------------------------------------------------------------- searcher
real_fetch = ba._arab_fetch
try:
    ba._arab_fetch = lambda url, data=None: (200, SEARCH_HTML)
    res = ba.search_jihs("بیرونی")
    check("searcher returns parsed candidates", len(res) == 3)
    check("searcher candidates carry PDF urls",
          all(c["oa_pdf_url"].startswith(
              "https://jihs.ut.ac.ir/article_") for c in res))
    ba._arab_fetch = lambda url, data=None: (None, "")
    check("searcher fails soft on transport error",
          ba.search_jihs("بیرونی") == [])
    ba._arab_fetch = lambda url, data=None: (500, "err")
    check("searcher fails soft on HTTP 500",
          ba.search_jihs("بیرونی") == [])
    ba._arab_fetch = lambda url, data=None: (200, SEARCH_HTML * 10)
    check("searcher caps at JIHS_CAP",
          len(ba.search_jihs("بیرونی")) == ba.JIHS_CAP == 8)
finally:
    ba._arab_fetch = real_fetch

check("searcher registered in SEARCHERS",
      ba.search_jihs in ba.SEARCHERS)
check("searcher registered in WAVE1",
      ba.search_jihs in ba.WAVE1_SEARCHERS)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
