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
raw = json.loads((BASE/"tajik_journals_registry.json").read_text(
    encoding="utf-8"))
check("registry has exactly 18 journal entries", len(raw) == 18)
need = {"name", "url", "type", "fields", "languages", "api", "notes",
        "verified", "verified_date"}
check("registry entries carry the registry schema",
      all(need <= set(e) for e in raw))
check("all entries are journals",
      all(e["type"] == "journal" for e in raw))
check("all entries verified 2026-11-26",
      all(e["verified"] is True and e["verified_date"] == "2026-11-26"
          for e in raw))
wired = [e for e in raw if e["api"]]
regonly = [e for e in raw if not e["api"]]
check("11 entries are live-wired via the CyberLeninka API",
      len(wired) == 11
      and all("cyberleninka.ru/api/search" in e["api"] for e in wired))
check("wired entries carry their journal ids in the api field",
      all('"journal": ' in e["api"] for e in wired))
check("7 entries are registry-only",
      len(regonly) == 7)
check("registry-only notes record the routes tried",
      all("CyberLeninka" in e["notes"] and "DOAJ" in e["notes"]
          for e in regonly))
check("the Academy Izvestia series are registry-only",
      any("ofm.php" in e["url"] for e in regonly)
      and any("obmn.php" in e["url"] for e in regonly)
      and any("medv.php" in e["url"] for e in regonly))
check("Doklady is the wired Academy journal",
      any(e["url"].endswith(
          "/doklady-akademii-nauk-respubliki-tadzhikistan")
          for e in wired))
check("no entry registers the cyberleninka.ru root (already in "
      "sources_registry_2)",
      all(e["url"] != "https://cyberleninka.ru" for e in raw))

for mod, tag in ((ba, "build_article"), (pt, "propose_topics")):
    di, ui, st = mod.load_source_registry()
    check(f"{tag} loads 14 files / 2160 entries",
          st["files_ok"] == 14 and st["loaded"] == 2160)
    check(f"{tag} vestnik-tnu.com indexed by domain",
          "vestnik-tnu.com" in di)
    check(f"{tag} cyberleninka.ru indexed by domain",
          "cyberleninka.ru" in di)

# ------------------------------------------------------------- languages
check("Tajik (tg) is NOT in the search matrix (recorded gap)",
      "tg" not in ba.MATRIX_LANGUAGES)
check("Russian (ru) IS in the search matrix",
      "ru" in ba.MATRIX_LANGUAGES)

# ------------------------------------------------------------- fixtures
# Trimmed from the REAL CyberLeninka API response probed live
# 2026-11-26: POST /api/search {"mode":"articles",
# "q":"математика","size":3,"from":0,"journal":28997} ->
# {"found": 527, "articles": [...]}. The first article's name is
# given here WITH the <b> highlight tags the API adds on some
# responses, to prove they are stripped; the third hit has no
# article link and must be skipped.
FIXTURE = {
    "found": 527,
    "articles": [
        {"name": "Производящая функция для числа решений "
                 "представлений тернарной кубической формы в виде "
                 "суммы двух квадратов",
         "annotation": "Для числа решений представлений тернарной "
                       "кубической формы в виде суммы двух "
                       "квадратов, то есть для арифметической "
                       "функции где - число решений диофантова "
                       "уравнения, найдена явная производящего ряда.",
         "link": "/article/n/proizvodyaschaya-funktsiya-dlya-"
                 "chisla-resheniy-predstavleniy-ternarnoy-"
                 "kubicheskoy-formy-v-vide-summy-dvuh-kvadratov",
         "authors": ["Камарадинова З. Н."],
         "year": 2014,
         "journal": "Доклады Академии наук Республики Таджикистан",
         "journal_link": "/journal/n/doklady-akademii-nauk-"
                         "respubliki-tadzhikistan"},
        {"name": "<b>МАТЕМАТИКА</b> И КУЛЬТУРА: УНИКАЛЬНОСТЬ И "
                 "УНИВЕРСАЛЬНОСТЬ <b>МАТЕМАТИКИ</b>",
         "annotation": "В статье обсуждаются вопросы происхождения "
                       "математики и ее природы.",
         "link": "/article/n/matematika-i-kultura",
         "authors": ["Гусева Н. И.", "Петров А. Б."],
         "year": 2015,
         "journal": "Доклады Академии наук Республики Таджикистан",
         "journal_link": "/journal/n/doklady-akademii-nauk-"
                         "respubliki-tadzhikistan"},
        {"name": "Запись без ссылки на статью",
         "annotation": "",
         "link": "",
         "authors": [],
         "year": 2016,
         "journal": "Доклады Академии наук Республики Таджикистан",
         "journal_link": "/journal/n/doklady-akademii-nauk-"
                         "respubliki-tadzhikistan"},
    ],
}

cands = ba._parse_tajik_cl_results(FIXTURE, "fallback venue")
check("parser keeps the linked fixture hits", len(cands) == 2)
c0 = cands[0]
check("first candidate title parsed",
      c0["title"].startswith("Производящая функция для числа"))
check("candidate index tag is tajik:cyberleninka",
      c0["index"] == "tajik:cyberleninka")
check("candidate url is the article page",
      c0["url"] == "https://cyberleninka.ru/article/n/"
      "proizvodyaschaya-funktsiya-dlya-chisla-resheniy-"
      "predstavleniy-ternarnoy-kubicheskoy-formy-v-vide-summy-"
      "dvuh-kvadratov")
check("candidate carries the article PDF route",
      c0["oa_pdf_url"] == c0["url"] + "/pdf")
check("candidate authors parsed",
      c0["authors"] == ["Камарадинова З. Н."])
check("candidate year parsed", c0["year"] == 2014)
check("candidate venue is the journal name",
      c0["venue"] == "Доклады Академии наук Республики Таджикистан")
check("candidate abstract carried",
      "диофантова уравнения" in c0["abstract"])
c1 = cands[1]
check("highlight tags are stripped from titles",
      "<b>" not in c1["title"] and c1["title"].startswith("МАТЕМАТИКА"))
check("multi-author hit parsed",
      c1["authors"] == ["Гусева Н. И.", "Петров А. Б."])
check("every candidate carries a PDF url",
      all(c["oa_pdf_url"].endswith("/pdf") for c in cands))

check("parser tolerates empty/garbage responses",
      ba._parse_tajik_cl_results(None, "v") == []
      and ba._parse_tajik_cl_results({}, "v") == []
      and ba._parse_tajik_cl_results({"articles": None}, "v") == []
      and ba._parse_tajik_cl_results({"articles": [42, "x"]},
                                     "v") == [])

many = {"found": 99, "articles": [
    {"name": f"Статья номер {i}", "annotation": "",
     "link": f"/article/n/statya-{i}", "authors": [], "year": 2020,
     "journal": "Историк (Муаррих)",
     "journal_link": "/journal/n/istorik-muarrih"}
    for i in range(10)]}
check("parser caps at TAJIK_PER_JOURNAL_CAP",
      len(ba._parse_tajik_cl_results(many, "v"))
      == ba.TAJIK_PER_JOURNAL_CAP == 8)

# ------------------------------------------------------------- searcher
real_fetch = ba._arab_fetch
calls = []
try:
    def fake_fetch(url, data=None):
        calls.append((url, json.loads(data.decode("utf-8"))))
        return 200, json.dumps(FIXTURE)
    ba._arab_fetch = fake_fetch
    res = ba.search_tajik_journals("математика")
    check("searcher returns parsed candidates from every journal",
          len(res) == 22 and len(calls) == 11)
    check("searcher posts to the CyberLeninka API",
          calls[0][0] == "https://cyberleninka.ru/api/search")
    check("payload carries mode articles + the journal filter",
          calls[0][1]["mode"] == "articles"
          and calls[0][1]["journal"] == 28997
          and calls[0][1]["q"] == "математика"
          and calls[0][1]["size"] == ba.TAJIK_PER_JOURNAL_CAP)
    ba._arab_fetch = lambda url, data=None: (200, json.dumps(many))
    res = ba.search_tajik_journals("история")
    check("searcher caps at TAJIK_TOTAL_CAP overall",
          len(res) == ba.TAJIK_TOTAL_CAP == 24)
    attempts = []
    def empty_fetch(url, data=None):
        attempts.append(1)
        return 200, ""
    ba._arab_fetch = empty_fetch
    check("searcher fails soft when the API does not answer",
          ba.search_tajik_journals("математика") == [])
    check("per-journal fetch retries up to three times",
          len(attempts) == 33)
    ba._arab_fetch = lambda url, data=None: (200, "not json")
    check("searcher fails soft on a non-JSON body",
          ba.search_tajik_journals("математика") == [])

    def boom(url, data=None):
        raise RuntimeError("transport down")
    ba._arab_fetch = boom
    check("searcher fails soft on transport exceptions",
          ba.search_tajik_journals("математика") == [])
finally:
    ba._arab_fetch = real_fetch

check("wired journal table has 11 journals",
      len(ba._TAJIK_CL_JOURNALS) == 11)
check("wired journal ids are unique",
      len({j for j, _ in ba._TAJIK_CL_JOURNALS}) == 11)
check("searcher registered in SEARCHERS",
      ba.search_tajik_journals in ba.SEARCHERS)
check("searcher registered in WAVE1",
      ba.search_tajik_journals in ba.WAVE1_SEARCHERS)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
