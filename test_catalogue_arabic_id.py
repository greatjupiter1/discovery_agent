"""Mocked offline checks for §20.8 (catalogue confirmation, the
Qanun lesson):

(a) Arabic route — the Latin title searches return nothing, the
    GENERAL Arabic-script search returns the real Qanun item
    (creator field empty, bilingual title) -> confirmed via
    "archive.org (arabic title)" under the item's REAL title.
(b) Identifier route — every title search returns nothing, the
    identifier-pattern search returns the item -> confirmed via
    "archive.org (identifier)".
(c) False-positive guard — the GRIS retest defect: the brain's
    garbled Tahdid title existence-matched al-Biruni's INDIA on
    the single weak token 'tahqiq'. Under §20.8 (>= 2 DISTINCTIVE
    shared tokens; weak tokens and author-name tokens never count)
    it must NOT confirm, even with a fetchable djvu and the author
    corroborated. The two other live false positives ('history',
    'time') are unit-checked the same way.
(d) A work with no copy/record anywhere -> still dropped.
(e) Every catalogue unreachable -> kept unverified (fail-soft).

No network: http_json dispatches on the query form inside the URL,
the Google Books searcher and the djvu fetcher are fakes.
"""
import sys
import urllib.parse

import build_article as ba

PASS = []


def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


# The real IA record for the Qanun (live-checked 2026-11-25/26):
# mixed-script title, NO creator field, a multi-work collection.
QANUN_DOC = {
    "identifier": "al-qanun-al-masudi-biruni-muhammad-ibn-ahmad",
    "title": "Al Qanūn Al Masʿūdī القانون المسعودي "
             "Bīrūnī, Muḥammad Ibn Aḥmad بيروني، محمد بن أحمد",
}
# The India — the book the garbled Tahdid title was wrongly
# existence-confirmed against in the GRIS retest.
INDIA_DOC = {
    "identifier": "TahqiqMaLiAlHind",
    "title": "Tahqiq ma li al-Hind",
    "creator": ["Muhammad ibn Ahmad al-Biruni"],
}
OTHER_DOC = {"identifier": "other", "title": "A Cookbook of Andalusia",
             "creator": ["Someone Else"]}

QANUN_WORK = {"author": "Abu Rayhan al-Biruni",
              "title": "Al-Qanun al-Mas'udi",
              "title_ar": "القانون المسعودي"}
GARBLED_TAHDID = {"author": "Abu Rayhan al-Biruni",
                  "title": "Tahdid al-Amanat li-Tahqiq al-Aqalim"}

STATE = {"title_docs": [], "arabic_docs": [], "id_docs": [],
         "ol_docs": [], "djvu": None, "raise_all": False}


def fake_http_json(url, **kw):
    if STATE["raise_all"]:
        raise RuntimeError("network down")
    u = urllib.parse.unquote(url)
    if "archive.org" in u:
        if "identifier:" in u:
            return {"response": {"docs": STATE["id_docs"]}}
        if "title:(" in u or "creator:(" in u:
            return {"response": {"docs": STATE["title_docs"]}}
        return {"response": {"docs": STATE["arabic_docs"]}}
    if "openlibrary.org" in u:
        return {"docs": STATE["ol_docs"]}
    return {}


def gb_raise(work):
    raise RuntimeError("HTTP 429 from googleapis (daily quota)")


ba.http_json = fake_http_json
ba.search_google_books_primary = gb_raise
ba._ia_djvu_text = lambda ia_id, cap=0: STATE["djvu"]

# ---- (a) Arabic general-search route confirms the Qanun -------------
STATE.update(title_docs=[], arabic_docs=[QANUN_DOC], id_docs=[],
             ol_docs=[], djvu=None)
work = dict(QANUN_WORK)
ok, matched = ba.verify_work_in_catalogue(work)
check("(a1) Latin title searches empty + Arabic general search "
      "returns the real item -> confirmed", ok)
check("(a2) confirmed under the item's REAL catalogue title",
      matched == QANUN_DOC["title"]
      and work.get("catalogue_title") == QANUN_DOC["title"])
check("(a3) route recorded as the arabic-title route",
      work.get("catalogue_via") == "archive.org (arabic title)")
check("(a4) catalogue_id records the IA identifier",
      work.get("catalogue_id") == QANUN_DOC["identifier"])

# ---- (b) identifier-pattern route confirms the Qanun ----------------
STATE.update(title_docs=[], arabic_docs=[], id_docs=[QANUN_DOC],
             ol_docs=[], djvu=None)
work = {"author": "Abu Rayhan al-Biruni",
        "title": "Al-Qanun al-Mas'udi"}  # no title_ar at all
ok, matched = ba.verify_work_in_catalogue(work)
check("(b1) all title searches empty + identifier search returns "
      "the item -> confirmed", ok and matched == QANUN_DOC["title"])
check("(b2) route recorded as the identifier route",
      work.get("catalogue_via") == "archive.org (identifier)")
slugs = ba._ia_slug_candidates(work)
check("(b3) slug derivation yields the item's slug prefixes",
      "al-qanun-al-masudi" in slugs
      and "al-qanun-al-masudi-biruni" in slugs)

# ---- (c) the false-positive guard ------------------------------------
# Unit level: distinctive-shared-token accounting.
shared = ba._distinctive_shared_tokens(
    GARBLED_TAHDID, INDIA_DOC["title"], INDIA_DOC["identifier"])
check("(c1) garbled Tahdid vs India: NO distinctive shared tokens "
      "('tahqiq' is weak)", shared == set())
check("(c2) existence match refuses the India for the garbled "
      "Tahdid title", not ba._catalogue_existence_match(
          GARBLED_TAHDID, INDIA_DOC["title"],
          INDIA_DOC["creator"], INDIA_DOC["identifier"]))
check("(c3) 'history' alone does not existence-confirm",
      not ba._catalogue_existence_match(
          {"author": "al-Biruni",
           "title": "The History of the Exact Sciences in Islam"},
          "Studies in Tudor History", ["al-Biruni"]))
check("(c4) 'time'/'earth' do not existence-confirm",
      not ba._catalogue_existence_match(
          {"author": "al-Biruni",
           "title": "The Measurement of the Earth in the Time of "
                    "al-Biruni"},
          "In Time of Earthquake", ["al-Biruni"]))
qshared = ba._distinctive_shared_tokens(
    QANUN_WORK, QANUN_DOC["title"], QANUN_DOC["identifier"])
check("(c5) Qanun vs its real item: >= 2 distinctive shared tokens "
      "(qanun/masudi and/or القانون/المسعودي)",
      len(qshared) >= 2 and "tahqiq" not in qshared)
check("(c6) existence match accepts the Qanun item",
      ba._catalogue_existence_match(
          QANUN_WORK, QANUN_DOC["title"], [],
          QANUN_DOC["identifier"]))

# End to end: the India is the ONLY doc anywhere and its djvu even
# fetches — the garbled Tahdid must still be dropped.
STATE.update(title_docs=[INDIA_DOC], arabic_docs=[INDIA_DOC],
             id_docs=[INDIA_DOC],
             ol_docs=[{"title": INDIA_DOC["title"],
                       "author_name": ["al-Biruni"]}],
             djvu="x" * 500)
ok, _ = ba.verify_work_in_catalogue(dict(GARBLED_TAHDID))
check("(c7) end-to-end: garbled Tahdid + retrievable India copy "
      "everywhere -> still dropped unconfirmed", not ok)

# ---- (d) no copy anywhere -> dropped ---------------------------------
STATE.update(title_docs=[OTHER_DOC], arabic_docs=[], id_docs=[],
             ol_docs=[], djvu=None)
ba.search_google_books_primary = lambda work: []
ok, _ = ba.verify_work_in_catalogue(
    {"author": "Nobody",
     "title": "The Nonexistent Treatise on Flying Carpets of Ghazna"})
check("(d) no catalogue holds the work -> dropped (law unchanged)",
      not ok)

# ---- (e) all catalogues down -> kept unverified (fail-soft) ----------
STATE.update(raise_all=True)
ba.search_google_books_primary = gb_raise
ok, matched = ba.verify_work_in_catalogue(
    {"author": "al-Biruni", "title": "Al-Qanun al-Mas'udi"})
check("(e) every catalogue unreachable -> kept unverified "
      "(fail-soft)", ok and matched == "")
STATE.update(raise_all=False)

failed = [n for n, ok in PASS if not ok]
print(f"\n{len(PASS) - len(failed)}/{len(PASS)} checks passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
