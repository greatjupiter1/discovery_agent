#!/usr/bin/env python3
"""Offline tests for §47: the IMAGE SOURCES (the witness class of
the IMAGE-SOURCE AMENDMENT, Jupiter 2026-11-26). A page image of
the actual content is a source; a passage exists ONLY as a
genuine transcription of an actually fetched page image, cited
by collection + folio/page and tagged image-derived in the
funnel and the §20.7 report. Metadata/descriptions NEVER count.

No network, no keys: the image fetcher, the transcription brain
and the LOC JSON route are replaced by fixtures. Fixtures are
modelled on the REAL probe answers of 2026-11-26: the QDL
Or 1997 IIIF manifest (544 canvases, folio labels, the linear
folio<->file map), an archived treatise-V image response, and
the LOC item JSON for the WDL Tahqiq item 2021666170 (692 page
files, tile.loc.gov IIIF variants, contributor Bīrūnī).
"""
import importlib.util
import json
import os
import pathlib
import sys

BASE = pathlib.Path.home() / "workspace/discovery-agent"


def load_mod(name, fname):
    spec = importlib.util.spec_from_file_location(name, BASE / fname)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ba = load_mod("build_article", "build_article.py")
pt = load_mod("propose_topics", "propose_topics.py")
passed = failed = 0


def check(label, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"PASS {label}")
    else:
        failed += 1
        print(f"FAIL {label}")


# ------------------------------------------------------------- registry
need = {"name", "url", "type", "fields", "languages", "notes",
        "verified", "verified_date"}
ireg = json.loads((BASE / "image_sources_registry.json")
                  .read_text(encoding="utf-8"))
check("image registry: 3 entries (QDL platform, Or 1997 witness, "
      "LOC/WDL)", len(ireg) == 3)
check("image registry carries the registry schema",
      all(need <= set(e) for e in ireg))
check("all entries verified 2026-11-26",
      all(e["verified"] is True and e["verified_date"] == "2026-11-26"
          for e in ireg))
check("QDL platform entry points at the IIIF collection",
      ireg[0]["url"] == "https://www.qdl.qa/en/iiif/collection"
      and "iiif" in ireg[0]["api"].lower())
check("Or 1997 witness entry: item URL + treatise V range recorded",
      ireg[1]["url"] == ("https://www.qdl.qa/en/archive/81055/"
                         "vdc_100022880536.0x000001")
      and "91v" in ireg[1]["notes"] and "195" in ireg[1]["notes"])
check("LOC entry: WDL collection URL + re-probe recorded",
      ireg[2]["url"] == "https://www.loc.gov/collections/"
      "world-digital-library/" and "tile.loc.gov" in ireg[2]["api"])

for mod, tag in ((ba, "build_article"), (pt, "propose_topics")):
    di, ui, st = mod.load_source_registry()
    check(f"{tag} loads 20 files / 2184 entries / 2131 unique",
          st["files_ok"] == 20 and st["loaded"] == 2184
          and st["unique_urls"] == 2131)
    check(f"{tag} §47 witness URL indexed",
          "qdl.qa/en/archive/81055/vdc_100022880536.0x000001" in ui)

# ------------------------------------------------------------- image bar
check("mime: JPEG magic", ba._img47_image_mime(b"\xff\xd8\xff\xe0xx")
      == "image/jpeg")
check("mime: PNG magic", ba._img47_image_mime(b"\x89PNG\r\n") == "image/png")
check("mime: TIFF magic", ba._img47_image_mime(b"II*\x00xx") == "image/tiff")
check("mime: a Cloudflare challenge page is NOT an image",
      ba._img47_image_mime(b"<!DOCTYPE html><title>Just a moment...</title>")
      is None)
check("mime: empty body is not an image", ba._img47_image_mime(b"") is None
      and ba._img47_image_mime(None) is None)

PROSE = ("قال أبو الريحان في المقالة الخامسة من القانون المسعودي "
         "إن انحطاط الأفق عند الارتفاع عن سطح الأرض يكون بحسب "
         "ارتفاع الناظر عن ذلك السطح فإذا عرف الارتفاع والانحطاط "
         "بالتجربة أمكن استخراج نصف قطر الأرض من نسبة جيب تمام "
         "الانحطاط إلى الفرق بينه وبين الجيب الكلي وذلك أن الخط "
         "المماس لسطح الأرض من رأس الجبل يصنع مع الخط الخارج من "
         "مركز الأرض إلى رأس الجبل زاوية قائمة فيحصل مثلث معلوم "
         "النسبة يستخرج منه مقدار نصف القطر بالأذرع ثم يحول إلى "
         "الأميال والفراسخ على ما هو مقرر في كتب المساحة والهيئة "
         "وقد جرب ذلك في جبل ناندنة من أرض الهند فكان مقدار ما "
         "حصل قريبا مما ذكره المتقدمون من اليونانيين والهنود")
check("transcription bar accepts a real manuscript page",
      ba._img47_transcription_ok(PROSE))
check("transcription bar rejects a short fragment",
      not ba._img47_transcription_ok("القانون المسعودي الباب الأول"))
check("transcription bar rejects a refusal",
      not ba._img47_transcription_ok(
          "I cannot transcribe this image because the handwriting "
          "is not clear enough for me to read it accurately today."))
check("transcription bar rejects a number-only stream",
      not ba._img47_transcription_ok(" ".join(str(1000 + i)
                                              for i in range(120))))

# ------------------------------------------------------------- folio map
check("folio map: file 195 = f. 91v (treatise V opening)",
      ba._qdl_folio_label(195) == "91v")
check("folio map: file 196 = f. 92r", ba._qdl_folio_label(196) == "92r")
check("folio map: file 230 = f. 109r (treatise V end)",
      ba._qdl_folio_label(230) == "109r")
check("folio map: anchors f. 5v / f. 84v / f. 262r",
      ba._qdl_folio_label(23) == "5v"
      and ba._qdl_folio_label(181) == "84v"
      and ba._qdl_folio_label(536) == "262r")

# --------------------------------------------------- fetch discipline
saved = (ba._img47_fetch_bytes, ba._img47_wayback_url)
try:
    ba._img47_wayback_url = lambda url: None
    ba._img47_fetch_bytes = lambda url, timeout=45, cap=0: \
        b"\xff\xd8\xff\xe0" + b"x" * 100
    body, mime = ba._img47_fetch_image("https://example.invalid/a.jpg")
    check("fetch_image: direct JPEG passes", mime == "image/jpeg"
          and body[:3] == b"\xff\xd8\xff")
    ba._img47_fetch_bytes = lambda url, timeout=45, cap=0: \
        b"<!DOCTYPE html>Just a moment..."
    body, mime = ba._img47_fetch_image("https://example.invalid/a.jpg")
    check("fetch_image: HTML is rejected, no fallback snapshot -> none",
          body is None and mime is None)
    calls = []

    def fake_bytes(url, timeout=45, cap=0):
        calls.append(url)
        if "web.archive.org" in url:
            return b"\x89PNG\r\n" + b"y" * 50
        return b"<!DOCTYPE html>Just a moment..."

    ba._img47_fetch_bytes = fake_bytes
    ba._img47_wayback_url = lambda url: \
        "https://web.archive.org/web/20240101000000id_/" + url
    body, mime = ba._img47_fetch_image(
        "https://iiif.qdl.qa/iiif/images/x.jp2/full/800,/0/default.jpg")
    check("fetch_image: Wayback fallback delivers the archived image",
          mime == "image/png" and len(calls) == 2)
finally:
    ba._img47_fetch_bytes, ba._img47_wayback_url = saved

# ------------------------------------------------------------- QDL
saved = (ba._img47_fetch_image, ba._img47_transcribe)
JPEG = (b"\xff\xd8\xff\xe0" + b"z" * 500, "image/jpeg")
try:
    calls = []
    ba._img47_fetch_image = lambda url: (calls.append(url), JPEG)[1]
    ba._img47_transcribe = lambda body, mime: PROSE
    out = ba.search_qdl_images("photosynthesis in plants")
    check("QDL gate: an unrelated query never fetches",
          out == [] and calls == [])
    out = ba.search_qdl_images("روش بیرونی برای تعیین شعاع زمین")
    check("QDL: one witness candidate", len(out) == 1)
    c = out[0]
    check("QDL candidate shape: image-derived + route + primary",
          c["image_derived"] is True
          and c["fulltext_route"] == "qdl:iiif-image"
          and c["index"] == "qdl:iiif-image"
          and c["source_type"] == "primary"
          and c["primary_relation"] == "author")
    check("QDL candidate: folio citation f. 91v, year of the copy",
          "Or 1997, f. 91v" in c["image_citation"]
          and c["image_pages"][0].endswith("f. 91v")
          and c["year"] == 1174 and c["language"] == "ar")
    check("QDL candidate: excerpt stands under its citation header",
          c["fulltext_excerpt"].startswith(
              "[British Library, Or 1997, f. 91v]")
          and PROSE[:60] in c["fulltext_excerpt"]
          and c["passages"] is True and c["passage_hits"] >= 3)
    check("QDL: at most 3 pages transcribed per candidate",
          len(c["image_pages"]) == ba.IMG47_PAGES_PER_CANDIDATE)
    ba._img47_fetch_image = lambda url: (None, None)
    check("QDL: walled/unreachable images -> no candidate (fail-soft)",
          ba.search_qdl_images("القانون المسعودي شعاع الأرض") == [])
    ba._img47_fetch_image = lambda url: JPEG
    ba._img47_transcribe = lambda body, mime: ""
    check("QDL: untranscribable pages -> no candidate (no metadata "
          "passage)", ba.search_qdl_images("biruni qanun") == [])
    n = [0]

    def counting_fetch(url):
        n[0] += 1
        return (None, None)

    ba._img47_fetch_image = counting_fetch
    ba.search_qdl_images("biruni qanun")
    check("QDL: fetch attempts bounded by the per-candidate cap",
          n[0] <= ba.IMG47_MAX_FETCHES_PER_CANDIDATE)
finally:
    ba._img47_fetch_image, ba._img47_transcribe = saved

# ------------------------------------------------------------- LOC
def variants(base):
    return [{"mimetype": "image/jpeg", "width": 440, "height": 717,
             "url": f"https://tile.loc.gov/{base}/pct25.jpg"},
            {"mimetype": "image/jpeg", "width": 881, "height": 1435,
             "url": f"https://tile.loc.gov/{base}/pct50.jpg"},
            {"mimetype": "image/jpeg", "width": 1763, "height": 2871,
             "url": f"https://tile.loc.gov/{base}/full.jpg"},
            {"mimetype": "application/pdf", "width": 0,
             "url": f"https://tile.loc.gov/{base}/doc.pdf"}]


check("LOC variant: widest JPEG <= 1600 px",
      ba._loc_page_variant(variants("x"))["url"].endswith("pct50.jpg"))
check("LOC variant: only oversize -> narrowest",
      ba._loc_page_variant([variants("x")[2]])["url"].endswith("full.jpg"))
check("LOC variant: no JPEG -> None",
      ba._loc_page_variant([{"mimetype": "application/pdf",
                             "url": "u"}]) is None)

ITEM_A = {"item": {"title": "Critical Study of What India Says",
                   "access_restricted": False,
                   "contributor_names": [
                       "Bīrūnī, Muhammad ibn Ahmad, 973?-1048 Author."],
                   "language": ["arabic"], "date": "1957-01-01"},
          "resources": [{"files": [variants(f"p{i}")
                                   for i in range(10)]}]}
ITEM_RESTRICTED = {"item": {"title": "Locked item",
                            "access_restricted": True,
                            "contributor_names": [],
                            "language": ["english"], "date": "1900"},
                   "resources": [{"files": [variants(f"r{i}")
                                            for i in range(10)]}]}
SEARCH = {"results": [
    {"title": "Critical Study of What India Says",
     "url": "https://www.loc.gov/item/2021666170/"},
    {"title": "An LCCN record, not an item",
     "url": "https://lccn.loc.gov/2017322210"},
    {"title": "Locked item",
     "url": "https://www.loc.gov/item/9999999999/"}]}

saved = (ba._loc_json, ba._img47_fetch_image, ba._img47_transcribe)
try:
    def fake_loc_json(url):
        if "/search/" in url:
            return SEARCH
        if "2021666170" in url:
            return ITEM_A
        if "9999999999" in url:
            return ITEM_RESTRICTED
        return None

    ba._loc_json = fake_loc_json
    ba._img47_fetch_image = lambda url: JPEG
    ba._img47_transcribe = lambda body, mime: PROSE
    out = ba.search_loc_images("biruni")
    check("LOC: one candidate (restricted + non-item skipped)",
          len(out) == 1)
    c = out[0]
    check("LOC candidate shape: image-derived + route + primary "
          "(contributor names Biruni)",
          c["image_derived"] is True
          and c["fulltext_route"] == "loc:iiif-image"
          and c["source_type"] == "primary"
          and c["authors"] == ["Bīrūnī, Muhammad ibn Ahmad, 973?-1048"])
    check("LOC candidate: cited by item + digital page",
          "Library of Congress, item 2021666170, digital page"
          in c["image_citation"] and "of 10" in c["image_citation"])
    check("LOC candidate: year/language from the item record",
          c["year"] == 1957 and c["language"] == "ar")
    ba._loc_json = lambda url: None
    check("LOC: API down -> [] (fail-soft)",
          ba.search_loc_images("biruni") == [])
    ba._loc_json = fake_loc_json
    ba._img47_transcribe = lambda body, mime: ""
    check("LOC: untranscribable pages -> no candidate",
          ba.search_loc_images("biruni") == [])
finally:
    ba._loc_json, ba._img47_fetch_image, ba._img47_transcribe = saved

# ------------------------------------------------- vision brain guard
saved_env = os.environ.pop("GEMINI_API_KEY", None)
try:
    check("vision brain without a key returns '' (searchers stay "
          "dormant, no request)", ba._gemini_vision(b"\xff\xd8\xff", "image/jpeg") == "")
finally:
    if saved_env is not None:
        os.environ["GEMINI_API_KEY"] = saved_env

# ------------------------------------------------------- registration
check("both searchers registered in SEARCHERS + WAVE1 + NEW_ONLY",
      ba.search_qdl_images in ba.SEARCHERS
      and ba.search_loc_images in ba.SEARCHERS
      and ba.search_qdl_images in ba.WAVE1_SEARCHERS
      and ba.search_loc_images in ba.WAVE1_SEARCHERS
      and ba.search_qdl_images in ba.NEW_ONLY_SEARCHERS
      and ba.search_loc_images in ba.NEW_ONLY_SEARCHERS)
check("searcher labels present",
      ba.SEARCHER_LABEL.get("search_qdl_images", "").startswith("QDL")
      and ba.SEARCHER_LABEL.get("search_loc_images", "")
      .startswith("LOC"))

# ------------------------------------------- tagging: funnel + report
saved2 = (ba._img47_fetch_image, ba._img47_transcribe)
c = None
try:
    ba._img47_fetch_image = lambda url: JPEG
    ba._img47_transcribe = lambda body, mime: PROSE
    c = ba.search_qdl_images("biruni qanun شعاع الأرض")[0]
finally:
    ba._img47_fetch_image, ba._img47_transcribe = saved2
rep = ba.build_detailed_source_report("موضوع", [c])
check("§20.7 report tags the image source with its folio citation",
      "منبع تصویری" in rep and "Or 1997, f. 91v" in rep
      and "qdl:iiif-image" in rep)
FUNNEL = {"candidates": 9, "verified": 7, "with_content": 6,
          "relevant": 5, "counted": 5, "registry_matched": 2,
          "primary_counted": 1, "fulltext_counted": 3,
          "image_derived_counted": 1}
ft = ba.build_funnel_text("t", FUNNEL, True, word_count=100,
                          word_target=(90, 110))
check("funnel carries the image-derived counted line",
      "منابع تصویریِ شمرده‌شده" in ft and "رونویسی" in ft)
ft0 = ba.build_funnel_text("t", {k: v for k, v in FUNNEL.items()
                                 if k != "image_derived_counted"},
                           True, word_count=100, word_target=(90, 110))
check("funnel without image sources carries NO image line",
      "منابع تصویریِ شمرده‌شده" not in ft0)
f1, f2 = {}, {}
ba._merge_funnel(f1, {"image_derived_counted": 2, "counted": 3})
ba._merge_funnel(f1, {"image_derived_counted": 1})
check("_merge_funnel accumulates image_derived_counted",
      f1.get("image_derived_counted") == 3)
check("checkpoint source keys carry the image fields",
      all(k in ba.CHECKPOINT_SOURCE_KEYS for k in
          ("image_derived", "image_citation", "image_pages")))
stored = ba._checkpoint_source(c, 400)
check("checkpoint keeps the image tag + citation",
      stored.get("image_derived") is True
      and "f. 91v" in (stored.get("image_citation") or ""))

print(f"\nTOTAL: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
