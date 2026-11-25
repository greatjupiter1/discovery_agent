"""Mocked offline checks for the §20.3 catalogue-confirmation rework:
confirmation must NOT depend on Google Books. Internet Archive (with
multi-form queries and normalised matching) and OpenLibrary each
confirm on their own; Google Books is optional corroboration whose
failure never blocks; a title confirmed by NO catalogue is still
dropped. No network: http_json and the Google Books searcher are
fakes."""
import sys

import build_article as ba

PASS = []


def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


# The real IA record for the known Qanun identifier
# al-qanun-al-masudi-biruni-muhammad-ibn-ahmad (checked live
# 2026-11-25): a mixed-script title with diacritics and NO creator
# field — the author is embedded in the title string itself.
QANUN_IA_DOC = {
    "identifier": "al-qanun-al-masudi-biruni-muhammad-ibn-ahmad",
    "title": "Al Qanūn Al Masʿūdī القانون المسعودي "
             "Bīrūnī, Muḥammad Ibn Aḥmad بيروني، محمد بن أحمد",
}
TAHDID_IA_DOC = {
    "identifier": "Kitab_Tahdid_Nihayat",
    "title": "Kitab Tahdid Nihayat Al-amakin",
    "creator": ["Muhammad ibn Ahmad al-Biruni"],
}
OTHER_DOC = {"identifier": "other", "title": "A Cookbook of Andalusia",
             "creator": ["Someone Else"]}
THEATRE_DOC = {"identifier": "theatre", "title": "Theatre in India",
               "creator": ["A. Playwright"]}

STATE = {"ia_docs": [], "ol_docs": [], "ia_raise": False}


def fake_http_json(url, **kw):
    if "archive.org" in url:
        if STATE["ia_raise"]:
            raise RuntimeError("IA down")
        return {"response": {"docs": STATE["ia_docs"]}}
    if "openlibrary.org" in url:
        return {"docs": STATE["ol_docs"]}
    return {}


def gb_raise(work):
    raise RuntimeError("HTTP 429 from googleapis (daily quota)")


ba.http_json = fake_http_json

# ---- (i) Google Books 429s; IA confirms the Qanun's long title -------
STATE["ia_docs"] = [QANUN_IA_DOC]
STATE["ol_docs"] = []
ba.search_google_books_primary = gb_raise
ok, matched = ba.verify_work_in_catalogue(
    {"author": "Abu Rayhan al-Biruni",
     "title": "Al-Qanun al-Masudi fi al-Hay'a wa al-Nujum"})
check("(i) GB 429 + IA confirms long mapped title vs the real "
      "short mixed-script IA record", ok and "Qan" in matched)

# ---- (ii) IA no match; OpenLibrary confirms; GB also failing ---------
STATE["ia_docs"] = [OTHER_DOC]
STATE["ol_docs"] = [{"title": "The Masudic Canon",
                     "author_name": ["Abu Rayhan al-Biruni"]}]
ba.search_google_books_primary = gb_raise
ok, matched = ba.verify_work_in_catalogue(
    {"author": "al-Biruni", "title": "Masudic Canon"})
check("(ii) IA no match + OpenLibrary confirms (GB raising too)",
      ok and matched == "The Masudic Canon")

# ---- (iii) no catalogue confirms -> still dropped ---------------------
STATE["ia_docs"] = [OTHER_DOC]
STATE["ol_docs"] = []
ba.search_google_books_primary = lambda work: []
ok, _ = ba.verify_work_in_catalogue(
    {"author": "Nobody",
     "title": "The Nonexistent Treatise on Flying Carpets of Ghazna"})
check("(iii) all catalogues answer with no match -> dropped "
      "unconfirmed (law unchanged)", not ok)

# ---- (iv) Arabic-script variant: Farsi yeh vs Arabic yeh --------------
# The work's title_ar ends in Farsi yeh (U+06CC); the IA record's
# title ends in Arabic yeh (U+064A). Normalisation must fold them.
STATE["ia_docs"] = [{
    "identifier": "al-qanun-al-masudi-202610",
    "title": "القانون المسعودي",
    "creator": ["أبو الريحان البيروني"]}]
STATE["ol_docs"] = []
ba.search_google_books_primary = lambda work: []
ok, matched = ba.verify_work_in_catalogue(
    {"author": "al-Biruni", "title": "Al-Qanun al-Masudi",
     "title_ar": "القانون المسعودی"})
check("(iv) Arabic-script title variant (Farsi yeh) matches the IA "
      "record (Arabic yeh)", ok and "القانون" in matched)

# ---- (v) Google Books failure alone never blocks ----------------------
STATE["ia_docs"] = [TAHDID_IA_DOC]
STATE["ol_docs"] = []
ba.search_google_books_primary = gb_raise
ok, matched = ba.verify_work_in_catalogue(
    {"author": "al-Biruni", "title": "Kitab Tahdid Nihayat al-Amakin"})
check("(v-a) GB raising never blocks an IA confirmation",
      ok and "Tahdid" in matched)

# No catalogue answers at all -> fail-soft keep, exactly as before.
STATE["ia_raise"] = True


def ol_raise(url, **kw):
    raise RuntimeError("network down")


ba.http_json = ol_raise
ba.search_google_books_primary = gb_raise
ok, matched = ba.verify_work_in_catalogue(
    {"author": "al-Biruni", "title": "Kitab Tahdid Nihayat al-Amakin"})
check("(v-b) every catalogue unreachable -> kept unverified "
      "(fail-soft)", ok and matched == "")
STATE["ia_raise"] = False
ba.http_json = fake_http_json

# ---- (vi) the 1-token guard still holds -------------------------------
STATE["ia_docs"] = [THEATRE_DOC]
STATE["ol_docs"] = []
ba.search_google_books_primary = lambda work: []
ok, _ = ba.verify_work_in_catalogue(
    {"author": "Abu Rayhan al-Biruni", "title": "India"})
check("(vi) 1-token title matched on that token alone, no author "
      "corroboration -> rejected", not ok)

failed = [n for n, ok in PASS if not ok]
print(f"\n{len(PASS) - len(failed)}/{len(PASS)} checks passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
