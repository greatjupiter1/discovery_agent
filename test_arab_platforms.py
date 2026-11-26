import importlib.util, json, os, pathlib, shutil, sys, tempfile

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

# ---------------------------------------------------------------- registry
raw = json.loads((BASE/"arab_journals_registry.json").read_text(encoding="utf-8"))
check("registry has exactly 8 platform entries", len(raw) == 8)
need = {"name", "url", "type", "fields", "languages", "api", "notes",
        "verified", "verified_date"}
check("registry entries carry the registry schema",
      all(need <= set(e) for e in raw))
check("registry entries verified 2026-11-26",
      all(e["verified"] is True and e["verified_date"] == "2026-11-26"
          for e in raw))
check("registry types are platform",
      all(e["type"] == "platform" for e in raw))
langs = {e["url"]: e["languages"] for e in raw}
check("ASJP + IMIST carry Arabic+French",
      "French" in langs.get("https://www.asjp.cerist.dz", [])
      and "French" in langs.get("https://revues.imist.ma", []))

for mod, tag in ((ba, "build_article"), (pt, "propose_topics")):
    di, ui, st = mod.load_source_registry()
    check(f"{tag} loads 23 files / 2200 entries",
          st["files_ok"] == 23 and st["loaded"] == 2200)
    check(f"{tag} arab platforms indexed by domain",
          all(d in di for d in ("asjp.cerist.dz", "iasj.rdd.edu.iq",
                                "dsr.ju.edu.jo", "journals.najah.edu",
                                "knepublishing.com", "qscience.com",
                                "ekb.eg")))

# dedupe: the same arab file under two registry names drops duplicates
with tempfile.TemporaryDirectory() as td:
    shutil.copy(BASE/"arab_journals_registry.json",
                pathlib.Path(td)/"arab_journals_registry.json")
    shutil.copy(BASE/"arab_journals_registry.json",
                pathlib.Path(td)/"sources_registry.json")
    di2, ui2, st2 = ba.load_source_registry(base_dir=td)
    check("dedupe by normalised URL across files",
          st2["loaded"] == 16 and st2["duplicates"] == 8
          and st2["unique_urls"] == 8)

# tagging: platform-domain candidates get tagged; law unchanged
di, ui, st = ba.load_source_registry()
ba._REGISTRY_CACHE = (di, ui, st)
cand_kne = {"title": "Diabetes study", "authors": [], "year": 2020,
            "venue": "JOVR", "doi": "10.18502/jovr.v15i1.5962",
            "url": "https://knepublishing.com/index.php/JOVR/article/view/5962/11795",
            "abstract": "", "arxiv_id": None, "index": "arab:kne"}
rec = ba.registry_tag(cand_kne)
check("KnE candidate domain-matches the platform entry",
      rec is not None and "KnE" in (cand_kne.get("registry_match") or ""))
cand_dir = dict(cand_kne, url="https://dsr.ju.edu.jo/index.php/Hum/article/view/10971",
                doi=None, index="arab:dirasat")
rec2 = ba.registry_tag(cand_dir)
check("Dirasat candidate domain-matches the platform entry",
      rec2 is not None and "Dirasat" in (cand_dir.get("registry_match") or ""))
ba.page_exists = lambda url: False
cand_no_doi = dict(cand_kne, doi=None)
ba.registry_tag(cand_no_doi)
check("registry match alone does NOT verify (law unchanged)",
      ba.verify(cand_no_doi) is False and "verified" not in cand_no_doi)
ba._REGISTRY_CACHE = None

# ------------------------------------------------------------- searcher wiring
check("search_arab_platforms in SEARCHERS",
      ba.search_arab_platforms in ba.SEARCHERS)
check("search_arab_platforms in WAVE1_SEARCHERS",
      ba.search_arab_platforms in ba.WAVE1_SEARCHERS)
check("searcher label present",
      ba.SEARCHER_LABEL.get("search_arab_platforms") == "Arab platforms")

# ----------------------------------------------------------------- fixtures
IMIST_JSON = '{"total": 1704, "page": 1, "page_size": 5, "results": [{"id": 19249, "identifier": "oai:revues.imist.ma:article/47127", "journal_title": "Revue Marocaine de l\'Évaluation et de la Recherche Educative", "article_title": "تنزيل التناوب اللغوي بسلك التعليم الابتدائي بالأكاديمية الجهوية للتربية والتكوين لجهة بني ملال ــ خنيفرة ـ دراسة تحليلية ـ", "abstract": "لقد شكل اعتماد مبدأ التناوب اللغوي بسلك التعليم الابتدائي تحديا كبيرا لدى تنزيله على مستوى الممارسة الصفية للأساتذة، إذ رغم الاقتناع بأهميته وضرورة تبنيه والعمل به، تنزيلا لمضامين الرؤية الاستراتيجية للإصلاح 2015-2030، والقانون الإطار 51-17 المتعلق بمنظومة التربية والتكوين والبحث العلمي، وكذا المنها", "authors": "ESLIFANI, MOSTAFA de l education nationale du prescolaire; ESLIFANI, MOSTAFA de l education nationale du prescolaire; ESLIFANI, MOSTAFA de l education nationale du prescolaire", "publication_date": "2024-03-07", "article_url": "https://revues.imist.ma/index.php/RMERE/article/view/47127"}, {"id": 29965, "identifier": "oai:journals.imist.ma:article/10917", "journal_title": "مجلة الهيئة العالمية للدراسات واألبحاث; مجلد 9 عدد العدد التاسع : 1446هـ/2024م (2024): محور العدد : \\"المُنجزُ المَعْرفي لأَقسَام اللّغةِ العَربيّة فِي الْجَامِعَات بيْن التّوصِيف الواقِعي والاستحقاق الأكاديمي \\" بعض أعمال المؤتمر العلمي الدولي السابع المحكم ، المنظم حضوريا وعبر التناظر المرئي من طرف الهيئة العالمية للعلماء والباحثين - مكتب دولة ليبيا ؛ ومركز الدراسات الإنسانية التابع للاتحاد العالمي للمثقفين العرب؛  يومي 14 – 15 أكتوبر (تشرين الأول) 2023م وذلك بمناسبة اليوم العالمي للغة العربية  الذي يصادف 18 كانون الأول (ديسمبر) من كل عام.", "article_title": "\\" اللغة العربية والسياسة اللغوية في نيجيريا\\"", "abstract": "الملخص :\\nإن اللغة بصفة عامة ليست أمرا هينا ولايسيرا، بل إنها أمر مقعد يمثل مادة الفكر، وآلة العلم، ووعاء التاريخ، وأداة التواصل، ومزية الإنسان وهويته، وهي من أعظم ما امتن الله به على عباده من رحمة بعد الوحي، ولهذه الأهمية تعمل الدول العالمية جاهدة في تبني سياسة لغوية معينة لحل كثير من مشاكل التعليم ", "authors": "Ahmad Garba                      Department of Arts and Social Science Education.                        Federal University of  Kashere, Nigeria", "publication_date": "2026-05-28", "article_url": "https://journals.imist.ma/index.php/ICSRJ/article/view/10917"}]}'

KNE_JSON = '{"success": true, "data": {"msg": "success", "results": {"numFound": 221, "docs": [{"title": "Profound Presentation of Retinopathy in a Patient with Sickle Cell trait and <mark>Diabetes</mark> Mellitus", "abstract": "This is a Photo Essay and does not have an abstract.", "publicationYear": 2020, "journalTitle": "journal of ophthalmic and vision research", "authors": ["A. Rezaei Kasra", "Vangipuarm Gautam", "S. Saraf Steven", "Zhang Qinqin", "Wang Ruikang"], "doi": "10.18502/jovr.v15i1.5962", "fullTextUrl": "https://knepublishing.com/index.php/JOVR/article/view/5962/11795"}, {"title": "Choroidal Thickness and Hemoglobin A1c Levels in Patients with Type 2 <mark>Diabetes</mark> Mellitus", "abstract": "Purpose: The aim of this study was to assess the correlation of hemoglobin A1c (HbA1c) levels with choroidal thickness in patients with type 2 <mark>diabetes</mark> mellitus (DM) using spectral domain optical coherence tomography (SD-OCT).\\r Methods: In this prospective case series, 180 eyes from 90 patients with type 2 DM were classified into three study groups based on HbA1c values: group 1 included patients with good glycemic control (HbA1c ≤ 7%), group 2 included patients with moderate glycemic control (HbA1c between 7% and 8%), and group 3 included patients with poor glycemic control (HbA1c ≥ 8%). Additionally, 50 eyes from 25 non-diabetic subjects were enrolled to group 4 as a control group. Sub-foveal, nasal, and temporal choroidal thickness were measured and compared.\\r Results: Mean central, nasal, and temporal choroidal thicknesses in diabetic patients (247.80, 238.63, and 239.30 μm) were significantly less than non-diabetic healthy subjects (277.56, 262.92, and 266.32 μm). Additionally, mean central, nasal, and temporal choroidal thickness values in group 4 (277.56, 262.92, and 266.32 μm) were significantly greater than the corresponding values in group 2 (248.34, 237.55, and 236.45 μm) and group 3 (239.81, 234.62, and 233.94 μm), but was not significantly different from corresponding values in group 1 (259.46, 246.12, and 251.00 μm).\\r Conclusion: HbA1c values have a significant correlation with choroidal thickness in diabetic patients, and better glycemic control with HbA1c ≤ 7% may prevent choroidal thinning.", "publicationYear": 2019, "journalTitle": "journal of ophthalmic and vision research", "authors": ["Torabi Hamidreza", "Saberi Isfeedvajani Mohsen", "Ramezani Majid", "Daryabari Seyed-Hashem"], "doi": "10.18502/jovr.v14i3.4784", "fullTextUrl": "https://knepublishing.com/index.php/JOVR/article/view/4784/9679"}]}}}'

DIRASAT_HTML = '<html><body><ul class="search_results">\n\t\t\t\t\t<li>\n\t\t\t\t\n\n\t\n<div class="obj_article_summary">\n\t\n\t<h3 class="title">\n\t\t<a id="article-10971" href="https://dsr.ju.edu.jo/index.php/Hum/ar/article/view/10971">\n\t\t\t\t\t\t\t<b>دور الرسوم الكرتونيَّة المُنتَجة بالذكاء الاصطناعي في تعليم العلوم: تعزيز الاستيعاب المفاهيمي لدى طلبة المرحلة الابتدائيَّة </b>\n\t\t\t\t<span class="subtitle">\n\t\t\t\t\tدراسات: العلوم الإنسانية والاجتماعية\n\t\t\t\t</span>\n\t\t\t\t\t</a>\n\t</h3>\n\n\t\t\t\t<div class="meta">\n\t\t\t\t<div class="authors">\n\t\t\tFatma Mohammed Al Muqbali، Mohamed Ali Shahat\n\t\t</div>\n\t\t\n\t\t\t\t\t\t\t<div class="pages">10971</div>\n\t\t\n\t\t\t\t\t<div class="published">\n\t\t\t\t2026-11-16\n\t\t\t</div>\n\t\t\n\t</div>\n\t\n\t\n\t\n</div>\n\t\t\t</li>\n\t\t\t\t\t<li>\n\t\t\t\t\n\n\t\n<div class="obj_article_summary">\n\t\n\t<h3 class="title">\n\t\t<a id="article-10454" href="https://dsr.ju.edu.jo/index.php/Hum/ar/article/view/10454">\n\t\t\t\t\t\t\t<b>\xa0</b> <b>الوظائف الوصفيَّة النصيَّة لأسئلة طلبة الجيل Z في سياق مقرَّرات تعليم اللغة الإندونيسيَّة: مقاربة اللغويات الوظيفيَّة المنهجيَّة </b>\n\t\t\t\t<span class="subtitle">\n\t\t\t\t\tدراسات: العلوم الإنسانية والاجتماعية\n\t\t\t\t</span>\n\t\t\t\t\t</a>\n\t</h3>\n\n\t\t\t\t<div class="meta">\n\t\t\t\t<div class="authors">\n\t\t\tAry Fawzi\n\t\t</div>\n\t\t\n\t\t\t\t\t\t\t<div class="pages">10454</div>\n\t\t\n\t\t\t\t\t<div class="published">\n\t\t\t\t2026-11-16\n\t\t\t</div>\n\t\t\n\t</div>\n\t\n\t\n\t\n</div>\n\t\t\t</li></body></html>'

NAJAH_HTML = '<html><body><article class="journal-article-card">\n    <div class="journal-article-card-head">\n        <div class="journal-article-card-copy">\n            \n            <div class="journal-article-card-flags">\n                \n                <span class="label label-warning">In Press</span>\n                \n                \n                <span class="label label-info">First Online</span>\n                \n            </div>\n            \n        <h3 class="journal-article-card-title"><a href="/journal/anujr-b/first-online/article/2934/">Educational Leadership and Sustainable Development: A Qualitative Study of Educational Leaders&#x27; Perceptions in Palestinian Educational Institutions</a></h3>\n        \n        <div class="journal-article-card-meta">\n            \n            <div class="journal-meta-chip">\n                <strong>Online</strong>\n                <span>2026-10-21</span>\n            </div>\n            \n            \n            \n            <div class="journal-meta-chip">\n                <strong>DOI</strong>\n                <span>10.35552/0247.a2934</span>\n            </div>\n            \n            \n        </div>\n        \n        </div>\n        \n        <a href="/media/journals/full_texts/anujr_b-1099.pdf" alt="Download" class="journal-article-card-pdf" aria-label="Download PDF">\n            <?xml version="1.0" encoding="iso-8859-1"?>\n<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n<svg class="w-6" version="1.1" id="Capa_1" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" x="0px" y="0px"\n\t viewBox="0 0 56 56" style="enable-background:new 0 0 56 56;" xml:space="preserve">\n\t<g>\n\t\t<path style="fill:#E9E9E0;" d="M36.985,0H7.963C7.155,0,6.5,0.655,6.5,1.926V55c0,0.345,0.655,1,1.463,1h40.074\n\t\t\tc0.808,0,1.463-0.655,1.463-1V12.978c0-0.696-0.093-0.92-0.257-1.085L37.607,0.257C37.442,0.093,37.218,0,36.985,0z"/>\n\t\t<polygon style="fill:#D9D7CA;" points="37.5,0.151 37.5,12 49.349,12 \t"/>\n\t\t<path style="fill:#CC4B4C;" d="M19.514,33.324L19.514,33.324c-0.348,0-0.682-0.113-0.967-0.326\n\t\t\tc-1.041-0.781-1.181-1.65-1.115-2.242c0.182-1.628,2.195-3.332,5.985-5.068c1.504-3.296,2.935-7.357,3.788-10.75\n\t\t\tc-0.998-2.172-1.968-4.99-1.261-6.643c0.248-0.579,0.557-1.023,1.134-1.215c0.228-0.076,0.804-0.172,1.016-0.172\n\t\t\tc0.504,0,0.947,0.649,1.261,1.049c0.295,0.376,0.964,1.173-0.373,6.802c1.348,2.784,3.258,5.62,5.088,7.562\n\t\t\tc1.311-0.237,2.439-0.358,3.358-0.358c1.566,0,2.515,0.365,2.902,1.117c0.32,0.622,0.189,1.349-0.39,2.16\n\t\t\tc-0.557,0.779-1.325,1.191-2.22,1.191c-1.216,0-2.632-0.768-4.211-2.285c-2.837,0.593-6.15,1.651-8.828,2.822\n\t\t\tc-0.836,1.774-1.637,3.203-2.383,4.251C21.273,32.654,20.389,33.324,19.514,33.324z M22.176,28.198\n\t\t\tc-2.137,1.201-3.008,2.188-3.071,2.744c-0.01,0.092-0.037,0.334,0.431,0.692C19.685,31.587,20.555,31.19,22.176,28.198z\n\t\t\tM35.813,23.756c0.815,0.627,1.014,0.944,1.547,0.944c0.234,0,0.901-0.01,1.21-0.441c0.149-0.209,0.207-0.343,0.23-0.415\n\t\t\tc-0.123-0.065-0.286-0.197-1.175-0.197C37.12,23.648,36.485,23.67,35.813,23.756z M28.343,17.174\n\t\t\tc-0.715,2.474-1.659,5.145-2.674,7.564c2.09-0.811,4.362-1.519,6.496-2.02C30.815,21.15,29.466,19.192,28.343,17.174z\n\t\t\tM27.736,8.712c-0.098,0.033-1.33,1.757,0.096,3.216C28.781,9.813,27.779,8.698,27.736,8.712z"/>\n\t\t<path style="fill:#CC4B4C;" d="M48.037,56H7.963C7.155,56,6.5,55.345,6.5,54.537V39h43v15.537C49.5,55.345,48.845,56,48.037,56z"/>\n\t\t<g>\n\t\t\t<path style="fill:#FFFFFF;" d="M17.385,53h-1.641V42.924h2.898c0.428,0,0.852,0.068,1.271,0.205\n\t\t\t\tc0.419,0.137,0.795,0.342,1.128,0.615c0.333,0.273,0.602,0.604,0.807,0.991s0.308,0.822,0.308,1.306\n\t\t\t\tc0,0.511-0.087,0.973-0.26,1.388c-0.173,0.415-0.415,0.764-0.725,1.046c-0.31,0.282-0.684,0.501-1.121,0.656\n\t\t\t\ts-0.921,0.232-1.449,0.232h-1.217V53z M17.385,44.168v3.992h1.504c0.2,0,0.398-0.034,0.595-0.103\n\t\t\t\tc0.196-0.068,0.376-0.18,0.54-0.335c0.164-0.155,0.296-0.371,0.396-0.649c0.1-0.278,0.15-0.622,0.15-1.032\n\t\t\t\tc0-0.164-0.023-0.354-0.068-0.567c-0.046-0.214-0.139-0.419-0.28-0.615c-0.142-0.196-0.34-0.36-0.595-0.492\n\t\t\t\tc-0.255-0.132-0.593-0.198-1.012-0.198H17.385z"/>\n\t\t\t<path style="fill:#FFFFFF;" d="M32.219,47.682c0,0.829-0.089,1.538-0.267,2.126s-0.403,1.08-0.677,1.477s-0.581,0.709-0.923,0.937\n\t\t\t\ts-0.672,0.398-0.991,0.513c-0.319,0.114-0.611,0.187-0.875,0.219C28.222,52.984,28.026,53,27.898,53h-3.814V42.924h3.035\n\t\t\t\tc0.848,0,1.593,0.135,2.235,0.403s1.176,0.627,1.6,1.073s0.74,0.955,0.95,1.524C32.114,46.494,32.219,47.08,32.219,47.682z\n\t\t\t\tM27.352,51.797c1.112,0,1.914-0.355,2.406-1.066s0.738-1.741,0.738-3.09c0-0.419-0.05-0.834-0.15-1.244\n\t\t\t\tc-0.101-0.41-0.294-0.781-0.581-1.114s-0.677-0.602-1.169-0.807s-1.13-0.308-1.914-0.308h-0.957v7.629H27.352z"/>\n\t\t\t<path style="fill:#FFFFFF;" d="M36.266,44.168v3.172h4.211v1.121h-4.211V53h-1.668V42.924H40.9v1.244H36.266z"/>\n\t\t</g>\n\t</g>\n</svg>\n\n        </a>\n        \n    </div>\n    <div class="journal-article-card-authors">\n        <span class="journal-article-card-authors-label">Authors</span>\n        \n            <span class="inline">\n                <a href="/authors/4799/">Katyusha Jamil Al-Hawari</a>\n                \n                <span class="text-gray-500">, </span>\n            </span>\n        \n            <span class="inline">\n                <a href="/authors/4800/">Sanaa Ismail Najjar</a>\n                \n                    <a href="https://orcid.org/0009-0007-5709-2910" target="_blank" rel="noopener noreferrer" class="inline align-middle no-underline" aria-label="ORCID 0009-0007-5709-2910" title="0009-0007-5709-2910">\n                        \n<img\n    src="/static/ORCID.png"\n    alt="ORCID"\n    title="ORCID"\n    width="16"\n    height="16"\n    class="inline-block align-baseline"\n/>\n\n                    </a>\n                \n                \n            </span>\n        \n    </div>\n    \n</article>\n\n            \n            \n<article class="journal-article-card">\n    <div class="journal-article-card-head">\n        <div class="journal-article-card-copy">\n            \n        <h3 class="journal-article-card-title"><a href="/journal/anujr-b/issue/anujr-b-v25-i8/article/134/">Education and it&#x27;s Methods in Islamic Law</a></h3>\n        \n        <div class="journal-article-card-meta">\n            \n            <div class="journal-meta-chip">\n                <strong>Online</strong>\n                <span>2011-09-27</span>\n            </div>\n            \n            \n            <div class="journal-meta-chip">\n                <strong>Pages</strong>\n                <span>2023 - 2056</span>\n            </div>\n            \n            \n            <div class="journal-meta-chip">\n                <strong>DOI</strong>\n                <span>10.35552/0247-025-008-003</span>\n            </div>\n            \n            \n            <div class="journal-meta-chip">\n                <strong>Issue</strong>\n                <span>Volume 25, Issue 8, 2011 September</span>\n            </div>\n            \n        </div>\n        \n        </div>\n        \n        <a href="/media/journals/full_texts/education-and-it039s-methods-islamic-law.pdf" alt="Download" class="journal-article-card-pdf" aria-label="Download PDF">\n            <?xml version="1.0" encoding="iso-8859-1"?>\n<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n<svg class="w-6" version="1.1" id="Capa_1" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" x="0px" y="0px"\n\t viewBox="0 0 56 56" style="enable-background:new 0 0 56 56;" xml:space="preserve">\n\t<g>\n\t\t<path style="fill:#E9E9E0;" d="M36.985,0H7.963C7.155,0,6.5,0.655,6.5,1.926V55c0,0.345,0.655,1,1.463,1h40.074\n\t\t\tc0.808,0,1.463-0.655,1.463-1V12.978c0-0.696-0.093-0.92-0.257-1.085L37.607,0.257C37.442,0.093,37.218,0,36.985,0z"/>\n\t\t<polygon style="fill:#D9D7CA;" points="37.5,0.151 37.5,12 49.349,12 \t"/>\n\t\t<path style="fill:#CC4B4C;" d="M19.514,33.324L19.514,33.324c-0.348,0-0.682-0.113-0.967-0.326\n\t\t\tc-1.041-0.781-1.181-1.65-1.115-2.242c0.182-1.628,2.195-3.332,5.985-5.068c1.504-3.296,2.935-7.357,3.788-10.75\n\t\t\tc-0.998-2.172-1.968-4.99-1.261-6.643c0.248-0.579,0.557-1.023,1.134-1.215c0.228-0.076,0.804-0.172,1.016-0.172\n\t\t\tc0.504,0,0.947,0.649,1.261,1.049c0.295,0.376,0.964,1.173-0.373,6.802c1.348,2.784,3.258,5.62,5.088,7.562\n\t\t\tc1.311-0.237,2.439-0.358,3.358-0.358c1.566,0,2.515,0.365,2.902,1.117c0.32,0.622,0.189,1.349-0.39,2.16\n\t\t\tc-0.557,0.779-1.325,1.191-2.22,1.191c-1.216,0-2.632-0.768-4.211-2.285c-2.837,0.593-6.15,1.651-8.828,2.822\n\t\t\tc-0.836,1.774-1.637,3.203-2.383,4.251C21.273,32.654,20.389,33.324,19.514,33.324z M22.176,28.198\n\t\t\tc-2.137,1.201-3.008,2.188-3.071,2.744c-0.01,0.092-0.037,0.334,0.431,0.692C19.685,31.587,20.555,31.19,22.176,28.198z\n\t\t\tM35.813,23.756c0.815,0.627,1.014,0.944,1.547,0.944c0.234,0,0.901-0.01,1.21-0.441c0.149-0.209,0.207-0.343,0.23-0.415\n\t\t\tc-0.123-0.065-0.286-0.197-1.175-0.197C37.12,23.648,36.485,23.67,35.813,23.756z M28.343,17.174\n\t\t\tc-0.715,2.474-1.659,5.145-2.674,7.564c2.09-0.811,4.362-1.519,6.496-2.02C30.815,21.15,29.466,19.192,28.343,17.174z\n\t\t\tM27.736,8.712c-0.098,0.033-1.33,1.757,0.096,3.216C28.781,9.813,27.779,8.698,27.736,8.712z"/>\n\t\t<path style="fill:#CC4B4C;" d="M48.037,56H7.963C7.155,56,6.5,55.345,6.5,54.537V39h43v15.537C49.5,55.345,48.845,56,48.037,56z"/>\n\t\t<g>\n\t\t\t<path style="fill:#FFFFFF;" d="M17.385,53h-1.641V42.924h2.898c0.428,0,0.852,0.068,1.271,0.205\n\t\t\t\tc0.419,0.137,0.795,0.342,1.128,0.615c0.333,0.273,0.602,0.604,0.807,0.991s0.308,0.822,0.308,1.306\n\t\t\t\tc0,0.511-0.087,0.973-0.26,1.388c-0.173,0.415-0.415,0.764-0.725,1.046c-0.31,0.282-0.684,0.501-1.121,0.656\n\t\t\t\ts-0.921,0.232-1.449,0.232h-1.217V53z M17.385,44.168v3.992h1.504c0.2,0,0.398-0.034,0.595-0.103\n\t\t\t\tc0.196-0.068,0.376-0.18,0.54-0.335c0.164-0.155,0.296-0.371,0.396-0.649c0.1-0.278,0.15-0.622,0.15-1.032\n\t\t\t\tc0-0.164-0.023-0.354-0.068-0.567c-0.046-0.214-0.139-0.419-0.28-0.615c-0.142-0.196-0.34-0.36-0.595-0.492\n\t\t\t\tc-0.255-0.132-0.593-0.198-1.012-0.198H17.385z"/>\n\t\t\t<path style="fill:#FFFFFF;" d="M32.219,47.682c0,0.829-0.089,1.538-0.267,2.126s-0.403,1.08-0.677,1.477s-0.581,0.709-0.923,0.937\n\t\t\t\ts-0.672,0.398-0.991,0.513c-0.319,0.114-0.611,0.187-0.875,0.219C28.222,52.984,28.026,53,27.898,53h-3.814V42.924h3.035\n\t\t\t\tc0.848,0,1.593,0.135,2.235,0.403s1.176,0.627,1.6,1.073s0.74,0.955,0.95,1.524C32.114,46.494,32.219,47.08,32.219,47.682z\n\t\t\t\tM27.352,51.797c1.112,0,1.914-0.355,2.406-1.066s0.738-1.741,0.738-3.09c0-0.419-0.05-0.834-0.15-1.244\n\t\t\t\tc-0.101-0.41-0.294-0.781-0.581-1.114s-0.677-0.602-1.169-0.807s-1.13-0.308-1.914-0.308h-0.957v7.629H27.352z"/>\n\t\t\t<path style="fill:#FFFFFF;" d="M36.266,44.168v3.172h4.211v1.121h-4.211V53h-1.668V42.924H40.9v1.244H36.266z"/>\n\t\t</g>\n\t</g>\n</svg>\n\n        </a>\n        \n    </div>\n    <div class="journal-article-card-authors">\n        <span class="journal-article-card-authors-label">Authors</span>\n        \n            <span class="inline">\n                <a href="/authors/209/">Sanaa Hadlah</a>\n                \n                \n            </span>\n        \n    </div>\n    \n</article>\n\n            \n            \n</body></html>'

FIXTURES = {
    "imist": IMIST_JSON,
    "kne": KNE_JSON,
    "dirasat": DIRASAT_HTML,
    "najah": NAJAH_HTML,
}

_real_fetch = ba._arab_fetch
def fake_fetch_factory(fail=()):
    def fake(url, data=None):
        if "prsm.imist.ma" in url:
            key = "imist"
        elif "kneopen.com" in url:
            key = "kne"
        elif "dsr.ju.edu.jo" in url:
            key = "dirasat"
        elif "journals.najah.edu" in url:
            key = "najah"
        else:
            return None, ""
        if key in fail:
            return None, ""
        return 200, FIXTURES[key]
    return fake

CAND_KEYS = {"title", "authors", "year", "venue", "doi", "url",
             "abstract", "arxiv_id", "index"}

ba._arab_fetch = fake_fetch_factory()
res = ba.search_arab_platforms("education")
by_index = {}
for c in res:
    by_index.setdefault(c["index"], []).append(c)
check("all four wired platforms contribute",
      all(k in by_index for k in ("arab:imist", "arab:kne",
                                  "arab:dirasat", "arab:najah")))
check("candidate shape matches the other searchers",
      all(CAND_KEYS <= set(c) and c["title"] and c["url"] for c in res))
check("total cap respected", len(res) <= ba.ARAB_TOTAL_CAP)
im = by_index["arab:imist"][0]
check("imist candidate fields",
      im["url"].startswith("https://revues.imist.ma/")
      and im["year"] == 2024 and bool(im["abstract"])
      and im["venue"] and im["authors"])
kn = by_index["arab:kne"][0]
check("kne candidate fields (mark tags stripped, DOI kept)",
      "<mark>" not in kn["title"] and kn["doi"] == "10.18502/jovr.v15i1.5962"
      and kn["url"].startswith("https://knepublishing.com/")
      and kn["year"] == 2020)
dr = by_index["arab:dirasat"][0]
check("dirasat candidate fields (OJS parse)",
      "/article/view/" in dr["url"] and dr["authors"]
      and dr["year"] is not None and bool(dr["title"]))
nj = by_index["arab:najah"][0]
check("najah candidate fields (card parse)",
      nj["url"].startswith("https://journals.najah.edu/journal/")
      and "/article/" in nj["url"] and bool(nj["title"]))
check("per-platform cap respected",
      all(len(v) <= ba.ARAB_PER_PLATFORM_CAP for v in by_index.values()))

# truncated KnE body: complete docs salvaged from a cut JSON stream
full_kne = FIXTURES["kne"]
cut = full_kne[:len(full_kne)//3]
docs = ba._kne_docs_from_body(cut)
check("kne truncated body salvages complete docs",
      len(docs) >= 1 and docs[0].get("doi") == "10.18502/jovr.v15i1.5962")
check("kne garbage body yields no docs", ba._kne_docs_from_body("}{") == [])

# fail-soft: every platform down -> empty list, no exception
ba._arab_fetch = fake_fetch_factory(fail=("imist", "kne", "dirasat", "najah"))
check("all platforms down -> [] without raising",
      ba.search_arab_platforms("education") == [])
# fail-soft: transport returns (None, "") via exception inside a platform fn
def boom(query):
    raise RuntimeError("simulated platform crash")
saved = ba._ARAB_PLATFORM_SEARCHERS
ba._ARAB_PLATFORM_SEARCHERS = [("boom", boom)] + list(saved[1:])
ba._arab_fetch = fake_fetch_factory()
res2 = ba.search_arab_platforms("education")
check("one platform crashing leaves the others working",
      len(res2) > 0 and all(c["index"] != "arab:imist" for c in res2))
ba._ARAB_PLATFORM_SEARCHERS = saved
# non-200 / empty bodies contribute nothing
ba._arab_fetch = lambda url, data=None: (500, "server error")
check("HTTP 500 everywhere -> []", ba.search_arab_platforms("x") == [])
ba._arab_fetch = _real_fetch

print(f"\nTOTAL: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
