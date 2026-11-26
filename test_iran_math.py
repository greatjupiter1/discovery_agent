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
raw = json.loads((BASE/"iran_math_registry.json").read_text(encoding="utf-8"))
check("registry has 19 journal entries", len(raw) == 19)
need = {"name", "url", "type", "fields", "languages", "api", "notes",
        "verified", "verified_date"}
check("registry entries carry the registry schema",
      all(need <= set(e) for e in raw))
check("all entries are journals", all(e["type"] == "journal" for e in raw))
check("all entries verified 2026-11-26",
      all(e["verified"] is True and e["verified_date"] == "2026-11-26"
          for e in raw))
wired = [e for e in raw if e["api"]]
regonly = [e for e in raw if not e["api"]]
check("11 journals live-wired (api route recorded)", len(wired) == 11)
check("8 journals registry-only (no api route)", len(regonly) == 8)
by_url = {e["url"]: e for e in raw}
check("SCMA is wired", by_url["https://scma.maragheh.ac.ir"]["api"] != "")
check("فرهنگ و اندیشه ریاضی is wired",
      by_url["https://mct.iranjournals.ir"]["api"] != "")
check("ریاضی و جامعه is wired",
      by_url["https://math-sci.ui.ac.ir"]["api"] != "")
check("BIMS is registry-only (Springer paywall recorded)",
      by_url["https://link.springer.com/journal/41980"]["api"] == ""
      and "paywall" in
      by_url["https://link.springer.com/journal/41980"]["notes"])
check("JIMS is registry-only (403 wall recorded)",
      by_url["https://jims.ims.ir"]["api"] == ""
      and "403" in by_url["https://jims.ims.ir"]["notes"])
check("Hyperstructures is registry-only (403 wall recorded)",
      by_url["https://jhs.uma.ac.ir"]["api"] == ""
      and "403" in by_url["https://jhs.uma.ac.ir"]["notes"])
check("Mahani is registry-only (host silent recorded)",
      by_url["https://jmmr.uk.ac.ir"]["api"] == ""
      and "did not answer" in by_url["https://jmmr.uk.ac.ir"]["notes"])
check("Khayyam is registry-only (zero-record install recorded)",
      by_url["http://kjm-math.org"]["api"] == ""
      and "ZERO" in by_url["http://kjm-math.org"]["notes"])
check("IJMSI is registry-only (POST search unverified recorded)",
      by_url["https://ijmsi.ir"]["api"] == ""
      and "POST" in by_url["https://ijmsi.ir"]["notes"])
check("every wired entry names its PDF route in the api field",
      all("article_<id>_<hash>.pdf" in e["api"] for e in wired))
check("wired journal list in code matches the registry's wired set",
      {u for u, _v in ba._IRAN_MATH_JOURNALS} ==
      {e["url"] for e in wired})

for mod, tag in ((ba, "build_article"), (pt, "propose_topics")):
    di, ui, st = mod.load_source_registry()
    check(f"{tag} loads 23 files / 2200 entries",
          st["files_ok"] == 23 and st["loaded"] == 2200)
    check(f"{tag} unique URL count", st["unique_urls"] == 2147)
    check(f"{tag} iran-math domains indexed",
          all(d in di for d in ("scma.maragheh.ac.ir",
                                "mct.iranjournals.ir",
                                "math-sci.ui.ac.ir", "toc.ui.ac.ir",
                                "jims.ims.ir", "kjm-math.org")))

# ------------------------------------------------------------- fixtures
# Trimmed from the REAL journal-site responses probed live
# 2026-11-26: the SCMA search page for "topology" (new system
# theme, h2 + mb-1 paragraphs, comma-separated authors), the
# Transactions on Combinatorics page for "graph" (old theme,
# h5 + margin-bottom-3 paragraphs, semicolon-separated
# authors) and the فرهنگ و اندیشه ریاضی page for «جبر»
# (Persian authors, Latin-digit Jalali years).
SCMA_HTML = '<h2 class="mb-2 list-article-title fw-bold text-justify " dir="ltr">\n        <a class="tag_a" href="article_736331.html">Critical Friedmann-Lema\\^{i}tre-Robertson-Walker Metrics</a>\n    </h2>\n\n    \n            <p class="mb-1">Articles in Press, Accepted Manuscript, Available Online from  <span >22 October 2026</span></p>\n    \n            <p class="mb-1 " dir="ltr" id="ar_doi"><i class="ai ai-doi text-warning fa-lg me-1"></i> <span dir="ltr"><a href="https://doi.org/10.22130/scma.2026.2087544.2579" >https://doi.org/10.22130/scma.2026.2087544.2579</a></span></p>\n    \n            <p class="mb-1 " dir="ltr" >Parvane  Atashpeykar, Ali  Haji Badali, Amirhesam  Zaeim</p>\n            <div class="text-container">\n            <p class="text-justify"><strong class="me-2">Abstract </strong> This paper investigates the Friedmann-Lema\\^{i}tre-Robertson-Walker (FLRW) spacetime\'s critical metrics in relation to variational problems. We derive the explicit conditions under which FLRW metric satisfies these critical equations after reviewing the general framework of $\\mathcal{F}_u$-critical and $\\mathcal{S}$-critical metrics. According to our analysis, the critical of these metrics is highly dependent on how the scale factor and spatial curvature behave. The findings contribute to the understanding of variational structures in homogeneous cosmological models by precisely characterizing the point at which FLRW spacetimes become $\\mathcal{F}_u$-critical or $\\mathcal{S}$-critical.</p>\n        </div>\n        <button class="btn btn-link p-0 more_btn">more </button>\n    \n\t<div class="col-md-11 py-2"><div class="flex-grow-1 border-top opacity-20"></div></div>\n    <ul class="list-inline d-flex align-items-center">\n\t<li class="lis-item"><a href="./article_736331.html" >View Article</a></li>\n\t        <li class="lis-item ms-2"><a href="./article_736331_d04dddfce5bfad54715f5c11cc69379c.pdf" target="_blank" class="pdf_link"><i class="fa-solid fa-file-pdf text-danger" ></i> PDF <span dir="ltr" class="badge bg-white px-2 pt-2 text-dark">128.47 K</span></a></li>\n<div class=\'\'>\n<h2 class="mb-2 list-article-title fw-bold text-justify " dir="ltr">\n        <a class="tag_a" href="article_736406.html">Micro Generalized Star Semi Neighborhood in Micro Topological Spaces</a>\n    </h2>\n\n    \n            <p class="mb-1">Articles in Press, Accepted Manuscript, Available Online from  <span >08 November 2026</span></p>\n    \n            <p class="mb-1 " dir="ltr" id="ar_doi"><i class="ai ai-doi text-warning fa-lg me-1"></i> <span dir="ltr"><a href="https://doi.org/10.22130/scma.2026.2076333.2394" >https://doi.org/10.22130/scma.2026.2076333.2394</a></span></p>\n    \n            <p class="mb-1 " dir="ltr" >Sathishmohan  Palanisamy, Mythili  Selvakumar, Rajalakshmi  Kandhasamy, Stanley Roshan  Stephen</p>\n            <div class="text-container">\n            <p class="text-justify"><strong class="me-2">Abstract </strong> The purpose of this paper is to introduce and investigate a new class of micro neighborhood namely micro generalized star semi neighborhoods (briefly Mic-g$^*$s nhbd), micro generalized star semi limit point (briefly Mic-g$^*$s limit point), micro generalized star semi derived set (briefly Mic-g$^*$s DS) in micro topological spaces. The characteristics of micro generalized star semi interior (briefly Mic-g$^*$s Intr.) and micro generalized star semi closure (briefly Mic-g$^*$s Clr.) are also investigated.</p>\n        </div>\n        <button class="btn btn-link p-0 more_btn">more </button>\n    \n\t<div class="col-md-11 py-2"><div class="flex-grow-1 border-top opacity-20"></div></div>\n    <ul class="list-inline d-flex align-items-center">\n\t<li class="lis-item"><a href="./article_736406.html" >View Article</a></li>\n\t        <li class="lis-item ms-2"><a href="./article_736406_bc22996b544914182f2b244c3b03293b.pdf" target="_blank" class="pdf_link"><i class="fa-solid fa-file-pdf text-danger" ></i> PDF <span dir="ltr" class="badge bg-white px-2 pt-2 text-dark">141.16 K</span></a></li>'
TOC_HTML = '<h5 class="margin-bottom-6 list-article-title ltr">\n                <a class="tag_a" href="article_30250.html">On the reflexive edge strength in zigzag graphs</a>\n    </h5>\n\n    \n            <p class="margin-bottom-3">Articles in Press, Corrected Proof, Available Online from  <span >26 May 2026</span></p>\n    \n            <p class="margin-bottom-3 ltr" id="ar_doi"><i class="ai ai-doi size-18 text-orange"></i> <span dir="ltr"><a href="https://doi.org/10.22108/toc.2026.146523.2322" >10.22108/toc.2026.146523.2322</a></span></p>\n    \n            <p class="margin-bottom-3 ltr" >Muhammad  Ibrahim; Muhammad Javed Azhar Khan; Roslan  Hasni; Yoong  Kooi Kuan; Ika Hesti  Agustin</p>\n        \n    <ul class="list-inline size-12 margin-top-10 margin-bottom-3 size-14">\n\t<li style="display: inline;padding:5px"><a href="./article_30250.html" >View Article</a></li>\n\t        <li><a href="./article_30250_8f3c6263e33f345f7e903c32296f9aad.pdf" target="_blank" class="pdf_link"><i class="fa fa-file-pdf-o text-red" ></i> PDF <span dir="ltr" class="badge badge-light">499.9 K</span></a></li>\n<div class=\'\'>\n<h5 class="margin-bottom-6 list-article-title ltr">\n                <a class="tag_a" href="article_30288.html">Some star-critical connected Ramsey numbers</a>\n    </h5>\n\n    \n            <p class="margin-bottom-3">Articles in Press, Corrected Proof, Available Online from  <span >21 February 2026</span></p>\n    \n            <p class="margin-bottom-3 ltr" id="ar_doi"><i class="ai ai-doi size-18 text-orange"></i> <span dir="ltr"><a href="https://doi.org/10.22108/toc.2026.144868.2264" >10.22108/toc.2026.144868.2264</a></span></p>\n    \n            <p class="margin-bottom-3 ltr" >Xiaoqian  Sun; Yanbo  Zhang</p>\n        \n    <ul class="list-inline size-12 margin-top-10 margin-bottom-3 size-14">\n\t<li style="display: inline;padding:5px"><a href="./article_30288.html" >View Article</a></li>\n\t        <li><a href="./article_30288_c429e3f98c703f00f5dc8fba6e91dc62.pdf" target="_blank" class="pdf_link"><i class="fa fa-file-pdf-o text-red" ></i> PDF <span dir="ltr" class="badge badge-light">504.54 K</span></a></li>'
MCT_HTML = '<h2 class="mb-2 list-article-title text-justify fw-bold " dir="rtl">\n        <a class="tag_a" href="article_464.html">نظریه\u200cهای &rlm;همانستگی و تغییر وزن نمایش\u200cهای گالوا</a>\n    </h2>\n\n    \n            <p class="mb-1">دوره 45، شماره 1، خرداد 1405، صفحه <span >1-25</span></p>\n    \n            <p class="mb-1 " dir="rtl" id="ar_doi"><i class="ai ai-doi text-warning fa-lg me-1"></i> <span dir="ltr"><a href="https://doi.org/10.30504/mct.2025.1539.2080" >10.30504/mct.2025.1539.2080</a></span></p>\n    \n            <p class="mb-1 " dir="rtl" >علی  پرتوفرد، سیدمحمدهادی  هدایت\u200cزاده رضوی</p>\n        \n    <ul class="list-inline d-flex align-items-center">\n\t<li class="lis-item me-3"><a href="./article_464.html" >مشاهده مقاله</a></li>\n\t        <li class="lis-item me-3"><a href="./article_464_f1b6f2857fb6d44dd73c7041e0aa0f19.pdf" target="_blank" class="pdf_link"><i class="fa-solid fa-file-pdf text-danger" ></i> اصل مقاله <span dir="ltr" class="badge bg-light text-dark">1.15 M</span></a></li>\n<div class=\'\'>\n<h2 class="mb-2 list-article-title text-justify fw-bold " dir="rtl">\n        <a class="tag_a" href="article_475.html">مروری بر چندجمله\u200cای\u200cهای چبیشف</a>\n    </h2>\n\n    \n            <p class="mb-1">دوره 44، شماره 2، مهر 1404، صفحه <span >87-112</span></p>\n    \n            <p class="mb-1 " dir="rtl" id="ar_doi"><i class="ai ai-doi text-warning fa-lg me-1"></i> <span dir="ltr"><a href="https://doi.org/10.30504/mct.2024.1468.2032" >10.30504/mct.2024.1468.2032</a></span></p>\n    \n            <p class="mb-1 " dir="rtl" >محمد  غلامزاده محمودی، امیر\u200e\u200f\u200cمحمد  کرمی، سید محمد\u200cرضا  موسوی</p>\n        \n    <ul class="list-inline d-flex align-items-center">\n\t<li class="lis-item me-3"><a href="./article_475.html" >مشاهده مقاله</a></li>\n\t        <li class="lis-item me-3"><a href="./article_475_5ef0b4eba35ab2d6180b0bca7e46b6f9.pdf" target="_blank" class="pdf_link"><i class="fa-solid fa-file-pdf text-danger" ></i> اصل مقاله <span dir="ltr" class="badge bg-light text-dark">1.22 M</span></a></li>'

SCMA_BASE, SCMA_VENUE = ba._IRAN_MATH_JOURNALS[0]
cands = ba._parse_iran_math_results(SCMA_HTML, SCMA_BASE, SCMA_VENUE)
check("parser finds the SCMA fixture results", len(cands) == 2)
c0 = cands[0]
check("SCMA first title parsed",
      c0["title"].startswith("Critical Friedmann"))
check("candidate index tag is persian:iran-math",
      c0["index"] == "persian:iran-math")
check("candidate url is the article page",
      c0["url"] ==
      "https://scma.maragheh.ac.ir/article_736331.html")
check("candidate carries the direct PDF file",
      c0["oa_pdf_url"] ==
      "https://scma.maragheh.ac.ir/article_736331_"
      "d04dddfce5bfad54715f5c11cc69379c.pdf")
check("comma-separated authors parsed",
      c0["authors"] == ["Parvane Atashpeykar", "Ali Haji Badali",
                         "Amirhesam Zaeim"])
check("candidate year reads the Gregorian year", c0["year"] == 2026)
check("candidate doi parsed",
      c0["doi"] == "10.22130/scma.2026.2087544.2579")
check("candidate venue names journal + issue line",
      c0["venue"].startswith("Sahand Communications")
      and "Articles in Press" in c0["venue"])

TOC_BASE, TOC_VENUE = ba._IRAN_MATH_JOURNALS[4]
tcands = ba._parse_iran_math_results(TOC_HTML, TOC_BASE, TOC_VENUE)
check("parser finds the TOC (old theme) fixture results",
      len(tcands) == 2)
t0 = tcands[0]
check("TOC first title parsed",
      t0["title"] == "On the reflexive edge strength in zigzag graphs")
check("semicolon-separated authors parsed",
      t0["authors"] == ["Muhammad Ibrahim",
                         "Muhammad Javed Azhar Khan", "Roslan Hasni",
                         "Yoong Kooi Kuan", "Ika Hesti Agustin"])
check("TOC candidate carries the direct PDF file",
      t0["oa_pdf_url"] ==
      "https://toc.ui.ac.ir/article_30250_"
      "8f3c6263e33f345f7e903c32296f9aad.pdf")
check("TOC candidate doi parsed",
      t0["doi"] == "10.22108/toc.2026.146523.2322")
check("TOC candidate year", t0["year"] == 2026)

MCT_BASE, MCT_VENUE = ba._IRAN_MATH_JOURNALS[1]
mcands = ba._parse_iran_math_results(MCT_HTML, MCT_BASE, MCT_VENUE)
check("parser finds the MCT fixture results", len(mcands) == 2)
m0 = mcands[0]
check("MCT first title parsed (Persian)",
      "همانستگی" in m0["title"])
check("Persian-comma authors parsed",
      m0["authors"][0] == "علی پرتوفرد" and len(m0["authors"]) == 2)
check("MCT candidate year reads the Latin-digit Jalali year",
      m0["year"] == 1405)
check("MCT candidate doi parsed",
      m0["doi"] == "10.30504/mct.2025.1539.2080")
check("MCT candidate venue names journal + issue",
      m0["venue"].startswith("فرهنگ و اندیشه ریاضی")
      and "دوره 45" in m0["venue"])
check("MCT candidate carries the direct PDF file",
      m0["oa_pdf_url"] ==
      "https://mct.iranjournals.ir/article_464_"
      "f1b6f2857fb6d44dd73c7041e0aa0f19.pdf")

# A hit without a PDF link (nothing published yet) is skipped:
# the real SCMA block with its pdf_link anchor removed.
NOPDF_HTML = SCMA_HTML.replace(
    'href="./article_736331_d04dddfce5bfad54715f5c11cc69379c.pdf"',
    'href="./article_736331.html"')
ncands = ba._parse_iran_math_results(NOPDF_HTML, SCMA_BASE, SCMA_VENUE)
check("hit whose PDF link is gone is skipped",
      len(ncands) == 1
      and all("736331" not in c["url"] for c in ncands))
check("every parsed candidate carries a PDF url",
      all(c["oa_pdf_url"].endswith(".pdf")
          for c in cands + tcands + mcands))
check("empty/garbage pages parse to nothing",
      ba._parse_iran_math_results("", SCMA_BASE, SCMA_VENUE) == []
      and ba._parse_iran_math_results(
          "<html></html>", SCMA_BASE, SCMA_VENUE) == [])

# ------------------------------------------------------------- searcher
real_fetch = ba._arab_fetch
try:
    ba._arab_fetch = lambda url, data=None: (200, SCMA_HTML)
    one = ba._iran_math_journal(SCMA_BASE, SCMA_VENUE, "topology")
    check("one-journal search returns parsed candidates",
          len(one) == 2)
    check("one-journal candidates carry PDF urls",
          all(c["oa_pdf_url"].startswith(
              "https://scma.maragheh.ac.ir/article_") for c in one))
    ba._arab_fetch = lambda url, data=None: (200, SCMA_HTML * 10)
    check("per-journal cap is IRAN_MATH_PER_JOURNAL_CAP",
          len(ba._iran_math_journal(SCMA_BASE, SCMA_VENUE,
                                    "topology"))
          == ba.IRAN_MATH_PER_JOURNAL_CAP == 8)
    check("total cap is IRAN_MATH_TOTAL_CAP",
          len(ba.search_iran_math("topology"))
          == ba.IRAN_MATH_TOTAL_CAP == 24)
    ba._arab_fetch = lambda url, data=None: (None, "")
    check("searcher fails soft on transport error",
          ba.search_iran_math("topology") == [])
    ba._arab_fetch = lambda url, data=None: (500, "err")
    check("searcher fails soft on HTTP 500",
          ba.search_iran_math("topology") == [])
finally:
    ba._arab_fetch = real_fetch

check("searcher registered in SEARCHERS",
      ba.search_iran_math in ba.SEARCHERS)
check("searcher registered in WAVE1",
      ba.search_iran_math in ba.WAVE1_SEARCHERS)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
