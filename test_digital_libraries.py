"""Offline tests for §27 — world free digital libraries.

Run: python3 test_digital_libraries.py
No network: every fetch is stubbed or fed from fixtures trimmed from
the REAL probe responses of 2026-11-26 (Gutendex catalogue JSON,
Wikisource MediaWiki search JSON, SciELO Books /search/ HTML, MEK
full-search results HTML). The probes themselves (search answers +
full text reachable: PG plain text via the official mirrors, Wikisource
page text, SciELO Books PDF %PDF-1.7, MEK PDF %PDF-1.3) are recorded in
files/world-digital-libraries.md.
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ba = load_module("build_article", "build_article.py")
pt = load_module("propose_topics", "propose_topics.py")

FAILURES = []


def check(label, cond):
    print(("PASS" if cond else "FAIL"), "-", label)
    if not cond:
        FAILURES.append(label)


# ---------------------------------------------------------------- registry
reg_path = os.path.join(HERE, "world_libraries_registry.json")
check("world_libraries_registry.json exists", os.path.exists(reg_path))
reg = json.load(open(reg_path, encoding="utf-8"))
check("registry has 37 entries", len(reg) == 37)
REQUIRED = {"name", "url", "type", "fields", "languages", "api",
            "notes", "verified", "verified_date"}
check("registry schema keys", all(set(e) == REQUIRED for e in reg))
check("all entries are digital_library",
      all(e["type"] == "digital_library" for e in reg))
check("all entries verified 2026-11-26",
      all(e["verified"] is True and e["verified_date"] == "2026-11-26"
          for e in reg))
by_url = {e["url"]: e for e in reg}
WIRED_URLS = {"https://www.gutenberg.org", "https://en.wikisource.org",
              "https://books.scielo.org", "https://mek.oszk.hu",
              "https://www.fadedpage.com",
              "https://theanarchistlibrary.org"}
check("exactly the 6 probed-live libraries carry an api route",
      {e["url"] for e in reg if e["api"]} == WIRED_URLS)
check("the other 31 are registry-only (empty api, honest notes)",
      all((not e["api"]) and len(e["notes"]) > 80
          for e in reg if e["url"] not in WIRED_URLS))
for url in ("https://www.europeana.eu", "https://dp.la",
            "https://trove.nla.gov.au", "https://core.ac.uk",
            "https://ctext.org"):
    check(f"key-walled library registered: {url}", url in by_url)

# ------------------------------------------------------------- registry load
# §23+§24+§25+§26+§27+§32+§34+§36+§37+§38+§39: fifteen registry files; totals updated count-only.
EXPECTED_FILES_OK = 19
EXPECTED_LOADED = 2181
for mod, label in ((ba, "build_article"), (pt, "propose_topics")):
    di, ui, st = mod.load_source_registry()
    check(f"{label}: all registry files load (files_ok)",
          st["files_ok"] == EXPECTED_FILES_OK)
    check(f"{label}: loaded entry count", st["loaded"] == EXPECTED_LOADED)
    check(f"{label}: world libraries indexed by domain",
          all(d in di for d in ("gutenberg.org", "en.wikisource.org",
                                "books.scielo.org", "mek.oszk.hu",
                                "gallica.bnf.fr", "fadedpage.com",
                                "theanarchistlibrary.org")))

with tempfile.TemporaryDirectory() as td:
    for fname in ba.REGISTRY_FILES:
        src = os.path.join(HERE, fname)
        if os.path.exists(src):
            with open(src, encoding="utf-8") as fh:
                data = fh.read()
            with open(os.path.join(td, fname), "w",
                      encoding="utf-8") as fh:
                fh.write(data)
    with open(os.path.join(td, "world_libraries_registry.json"),
              encoding="utf-8") as fh:
        wreg = json.load(fh)
    wreg.append({"name": "Dup Gutenberg",
                 "url": "https://www.gutenberg.org/",
                 "type": "digital_library", "fields": [], "languages": [],
                 "api": "", "notes": "dup", "verified": True,
                 "verified_date": "2026-11-26"})
    with open(os.path.join(td, "world_libraries_registry.json"), "w",
              encoding="utf-8") as fh:
        json.dump(wreg, fh)
    di2, ui2, st2 = ba.load_source_registry(td)
    check("dedupe: trailing-slash gutenberg.org duplicate dropped",
          st2["loaded"] == EXPECTED_LOADED + 1 and
          st2["unique_urls"] == st["unique_urls"] and
          di2["gutenberg.org"]["name"] == "Project Gutenberg")

# ------------------------------------------------------- verification law
cand = {"title": "Astronomy for beginners",
        "url": "https://www.gutenberg.org/ebooks/78112.txt.utf-8",
        "doi": None, "index": "library:gutenberg"}
orig_page_exists = ba.page_exists
ba.page_exists = lambda url: False
try:
    check("verification law unchanged: unreachable URL does not verify",
          ba.verify(dict(cand)) is False and "verified" not in cand)
finally:
    ba.page_exists = orig_page_exists

# ---------------------------------------------------------------- fixtures
GUTENDEX = json.loads('{\n "count": 46,\n "next": null,\n "previous": null,\n "results": [\n  {\n   "id": 78112,\n   "title": "Astronomy for beginners",\n   "authors": [\n    {\n     "name": "Carrington, Hereward",\n     "birth_year": 1880,\n     "death_year": 1959\n    }\n   ],\n   "subjects": [\n    "Astronomy"\n   ],\n   "languages": [\n    "en"\n   ],\n   "formats": {\n    "text/html": "https://www.gutenberg.org/ebooks/78112.html.images",\n    "text/plain; charset=utf-8": "https://www.gutenberg.org/ebooks/78112.txt.utf-8"\n   },\n   "download_count": 24489\n  },\n  {\n   "id": 28247,\n   "title": "A Popular History of Astronomy During the Nineteenth Century: Fourth Edition",\n   "authors": [\n    {\n     "name": "Clerke, Agnes M. (Agnes Mary)",\n     "birth_year": 1842,\n     "death_year": 1907\n    }\n   ],\n   "subjects": [\n    "Astronomy -- History -- 19th century"\n   ],\n   "languages": [\n    "en"\n   ],\n   "formats": {\n    "text/html": "https://www.gutenberg.org/ebooks/28247.html.images",\n    "text/plain; charset=utf-8": "https://www.gutenberg.org/ebooks/28247.txt.utf-8",\n    "text/plain; charset=us-ascii": "https://www.gutenberg.org/files/28247/28247-8.txt"\n   },\n   "download_count": 2606\n  }\n ]\n}')
WIKISOURCE = json.loads('{"batchcomplete":"","continue":{"sroffset":8,"continue":"-||"},"query":{"searchinfo":{"totalhits":462},"search":[{"ns":0,"title":"The Mathematical Principles of Natural Philosophy","pageid":1907,"size":2081,"wordcount":154,"snippet":"(1687) by\\u00a0Isaac <span class=\\"searchmatch\\">Newton</span> 1907Philosophiae Naturalis <span class=\\"searchmatch\\">Principia</span> Mathematica (The Mathematical Principles of Natural Philosophy) \\u2014 1687Isaac <span class=\\"searchmatch\\">Newton</span> English-language","timestamp":"2025-03-27T20:09:23Z"},{"ns":0,"title":"Dictionary of National Biography, 1885-1900/Newton, Isaac","pageid":591075,"size":340,"wordcount":20433,"snippet":"<span class=\\"searchmatch\\">Principia</span>, pp. 20, 23; \\u2018Letter to Leibnitz,\\u2019 24 Oct. 1676, No. lv. in the Commercium Epistolicum; Pemberton, Preface to A View of Sir Isaac <span class=\\"searchmatch\\">Newton&#039;s</span> Philosophy","timestamp":"2023-07-04T09:03:19Z"},{"ns":0,"title":"Principia Mathematica","pageid":425487,"size":256,"wordcount":28,"snippet":"Mathematica may refer to: Philosophiae Naturalis Principia Mathematica. a work by Isaac <span class=\\"searchmatch\\">Newton</span> <span class=\\"searchmatch\\">Principia</span> Mathematica, a work by Russell and Whitehead","timestamp":"2018-06-15T10:50:49Z"},{"ns":0,"title":"The New International Encyclop\\u00e6dia/Principia","pageid":2275519,"size":237,"wordcount":56,"snippet":"The New International Encyclop\\u00e6dia <span class=\\"searchmatch\\">Principia</span> 2275519The New International Encyclop\\u00e6dia \\u2014 <span class=\\"searchmatch\\">Principia</span> \\u200bPRINCIP\\u2032IA (Lat., principles). A famous mathematical","timestamp":"2017-05-20T17:00:20Z"},{"ns":0,"title":"The New Student\'s Reference Work/Newton, Sir Isaac","pageid":114418,"size":255,"wordcount":660,"snippet":"The New Student&#039;s Reference Work <span class=\\"searchmatch\\">Newton</span>, Sir Isaac 114418The New Student&#039;s Reference Work \\u2014 <span class=\\"searchmatch\\">Newton</span>, Sir Isaac \\u200b<span class=\\"searchmatch\\">Newton</span>, Sir Isaac, the foremost English","timestamp":"2024-04-28T18:03:42Z"},{"ns":0,"title":"The Mathematical Principles of Natural Philosophy (1846)/Introduction to the American Edition","pageid":595829,"size":413,"wordcount":951,"snippet":"the American EditionAndrew MotteIsaac <span class=\\"searchmatch\\">Newton</span> \\u200b INTRODUCTION TO THE AMERICAN EDITION. That the <span class=\\"searchmatch\\">Principia</span> of <span class=\\"searchmatch\\">Newton</span> should have remained so generally unknown","timestamp":"2011-03-16T11:05:01Z"},{"ns":0,"title":"A Short Biographical Dictionary of English Literature/Newton, Sir Isaac","pageid":172243,"size":2322,"wordcount":346,"snippet":"William Cousin <span class=\\"searchmatch\\">Newton</span>, Sir Isaac 172243A Short Biographical Dictionary of English Literature \\u2014 <span class=\\"searchmatch\\">Newton</span>, Sir IsaacJohn William Cousin <span class=\\"searchmatch\\">Newton</span>, Sir Isaac (1642-1727)","timestamp":"2009-11-21T21:07:52Z"},{"ns":0,"title":"1911 Encyclop\\u00e6dia Britannica/Newton, Sir Isaac","pageid":261938,"size":509,"wordcount":15498,"snippet":"<span class=\\"searchmatch\\">Newton\\u2019s</span> <span class=\\"searchmatch\\">Principia</span> (1893). A complete bibliography of <span class=\\"searchmatch\\">Newton\\u2019s</span> writings has been given by G.\\u00a0J. Gray (Cambridge, 1880). The collected works of <span class=\\"searchmatch\\">Newton</span>","timestamp":"2024-02-07T19:40:47Z"}]}}')
SCIELO_PAGE = '<div class="resultSet">\n                            \n    <div id="5dczv" class="record">\n\n        <div class="yourSelectionCheck">\n            <label for="select_5dczv" class="hide">Selecionar</label>\n            <input type="checkbox" class="my_selection" value="5dczv"\n             id="select_5dczv">\n        </div>\n\n        <div class="position">1.</div>\n        <div class="data">\n           <div class="thumb">\n                                    <img src="https://books.scielo.org/id/5dczv/cover/cover_thumbnail.jpeg"/>\n                            </div>\n\n            <div class="details">\n                                        <h3 class="title">\n                                                    <a href="https://books.scielo.org/id/5dczv">Objetivos humanísticos, conteúdos científicos: contribuições da história e da filosofia da Ciência para o ensino de Ciências</a>\n                                            </h3>\n                \n    <div class="isbn">\n                    (eISBN:9786586221664)\n            </div>\n            <div>\n            <span>Organizador(es):</span> Silva, Ana Paula Bispo da; Moura, Breno Arsioli\n        </div>\n                <div class="publisher">\n        <span>Editora:</span> EDUEPB\n    </div>\n            <div class="language">\n            <span>Idioma(s):</span> Português\n        </div>\n        <div>\n        <span>Ano:</span> 2019\n    </div>\n            <div>\n            <span>Sinopse:</span> O livro pre\xadtende apresentar novos elementos para a pesquisa em História e Filosofia da Ciência no Ensino de Ciências. Os trabalhos trazem novas questões filosóficas sobre os modelos e narrativas das ciências; os pressupostos culturais dos experimen\xadtos e teorias científicas e sua relação com o ensino; e, de maneira inversa, como  as teorias e pressupostos científicos estão presentes na cultura e na divulgação de conhecimentos.\n        </div>\n                </div>\n\n        </div>\n\n    </div>\n\n    <div id="r76hr" class="record">\n\n        <div class="yourSelectionCheck">\n            <label for="select_r76hr" class="hide">Selecionar</label>\n            <input type="checkbox" class="my_selection" value="r76hr"\n             id="select_r76hr">\n        </div>\n\n        <div class="position">2.</div>\n        <div class="data">\n           <div class="thumb">\n                                    <img src="https://books.scielo.org/id/r76hr/cover/cover_thumbnail.jpeg"/>\n                            </div>\n\n            <div class="details">\n                                        <h3 class="title">\n                                                    <a href="https://books.scielo.org/id/r76hr">Ciência e saúde pela vida: 125 anos de história da Fiocruz</a>\n                                            </h3>\n                \n    <div class="isbn">\n                    (eISBN:9786557082355)\n            </div>\n            <div>\n            <span>Organizador(es):</span> Sá, Dominichi Miranda de; Silva, André Felipe Cândido da; Vieira, Tamara Rangel; Mello, Vanessa Pereira da Silva e; El-Dine, Lorenna Ribeiro Zem\n        </div>\n                <div class="publisher">\n        <span>Editora:</span> Editora FIOCRUZ\n    </div>\n            <div class="language">\n            <span>Idioma(s):</span> Português\n        </div>\n        <div>\n        <span>Ano:</span> 2025\n    </div>\n            <div>\n            <span>Sinopse:</span> Ciência e Saúde pela Vida: 125 anos de história da Fiocruz propõe uma abordagem interdisciplinar e não linear da história da Fiocruz, apresentando-a como protagonista permanente da história da ciência e da saúde pública no Brasil. Da fundação como Instituto Soroterápico Federal em 1900 à atuação internacional durante a pandemia de covid-19, o livro traça um panorama das transformações científicas, políticas e sociais que moldaram o país, e das quais a Fiocruz sempre fez parte. O livro é composto por textos introdutórios e reúne 121 verbetes escritos por 83 autores, em sua maioria historiadores da ciência e da saúde, e oferece ao público uma leitura acessível, crítica e profundamente conectada com os desafios do país.\n        </div>\n                </div>\n\n        </div>\n\n    </div>\n\n    '
MEK_PAGE = '<a class="itemlink" href="https://mek.oszk.hu/09200/09250"><div class="dcauthor">Ponori Thewrewk Aurél</div><div class="dctitle">Csillagok a Bibliában</div><div class="itemlink">https://mek.oszk.hu/09200/09250</div>\n</a><div class="rkdate">2011-03-04</div>        \n        </div> \n            <div class="hit">                         \n            \n                \n        \n                \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                        \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                \n                \n                \n                        \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                \n                        \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                        \n        \n                \n                \n                        \n                \n        \n                        \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n    <a class="itemlink" href="https://mek.oszk.hu/09100/09150"><div class="dcauthor">Ponori Thewrewk Aurél</div><div class="dctitle">Divina astronomia</div><div class="itemlink">https://mek.oszk.hu/09100/09150</div>\n</a><div class="rkdate">2011-02-07</div>        \n        </div> \n            <div class="hit">                         \n            \n                \n        \n                \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n                \n        \n                \n                \n                \n                \n        \n                \n        \n        \n        \n        \n       \n                 \n            \n    '
FADED_JSON = json.loads('{"debug": "", "nrows": 1, "rows": [{"sequence": 3087, "first_publication": 1853, "pid": "20161103", "title": "Bleak House", "detail": "<a href=\\"/showbook.php?pid=20161103\\">detail</a>", "lang": "en", "description": "The story is told partly by the novel\'s heroine, Esther Summerson, and partly by an omniscient narrator. At the centre of Bleak House is the long-running legal case, Jarndyce and Jarndyce, which came about because someone wrote several conflicting wills. This legal case is used by Dickens to satirize the English judicial system, and he makes use of his earlier experiences as a law clerk, and as a litigant seeking to enforce copyright on his earlier books.", "cover": "books/20161103/cover.jpg", "authors": [{"realid": 1363, "pseudoid": 1364, "type": "illustrator", "realname": "Browne, Hablot Knight", "bio": null, "picturepath": null, "pseudoname": "Phiz"}, {"realid": 97, "pseudoid": null, "type": "author", "realname": "Dickens, Charles", "bio": "Charles John Huffam Dickens was an English writer and social critic. He created some of the world\'s best-known fictional characters and is regarded as the greatest novelist of the Victorian era. His works enjoyed unprecedented popularity during his lifetime, and by the twentieth century critics and scholars had recognised him as a literary genius. His novels and short stories enjoy lasting popularity.\\r\\nBorn in Portsmouth, Dickens left school to work in a factory when his father was incarcerated in a debtors\' prison. Despite his lack of formal education, he edited a weekly journal for 20 years, wrote 15 novels, five novellas, hundreds of short stories and non-fiction articles, lectured and performed extensively, was an indefatigable letter writer, and campaigned vigorously for children\'s rights, education, and other social reforms.\\r\\nDickens\'s literary success began with the 1836 serial publication of The Pickwick Papers. Within a few years he had become an international literary celebrity, famous for his humour, satire, and keen observation of character and society. His novels, most published in monthly or weekly instalments, pioneered the serial publication of narrative fiction, which became the dominant Victorian mode for novel publication. The instalment format allowed Dickens to evaluate his audience\'s reaction, and he often modified his plot and character development based on such feedback. For example, when his wife\'s chiropodist expressed distress at the way Miss Mowcher in David Copperfield seemed to reflect her disabilities, Dickens improved the character with positive features. His plots were carefully constructed, and he often wove elements from topical events into his narratives. Masses of the illiterate poor chipped in ha\'pennies to have each new monthly episode read to them, opening up and inspiring a new class of readers.\\r\\nDickens was regarded as the literary colossus of his age. His 1843 novella, A Christmas Carol, remains popular and continues to inspire adaptations in every artistic genre. Oliver Twist and Great Expectations are also frequently adapted, and, like many of his novels, evoke images of early Victorian London. His 1859 novel, A Tale of Two Cities, set in London and Paris, is his best-known work of historical fiction. Dickens\'s creative genius has been praised by fellow writers—from Leo Tolstoy to George Orwell and G. K. Chesterton—for its realism, comedy, prose style, unique characterisations, and social criticism. On the other hand Oscar Wilde, Henry James, and Virginia Woolf complained of a lack of psychological depth, loose writing, and a vein of saccharine sentimentalism. The term Dickensian is used to describe something that is reminiscent of Dickens and his writings, such as poor social conditions or comically repulsive characters.--Wikipedia.", "picturepath": "author/Dickens, Charles.jpg"}], "pages": 794, "downloads": 1603}], "facets": {"tags": {"classics": {"count": 1, "desc": null}, "England": {"count": 1, "desc": null}, "fiction": {"count": 1, "desc": null}, "law": {"count": 1, "desc": null}, "legal thriller": {"count": 1, "desc": null}, "literature": {"count": 1, "desc": null}, "London": {"count": 1, "desc": null}, "satire": {"count": 1, "desc": null}, "film/TV adaptation": {"count": 1, "desc": null}}, "langs": {"English": 1}, "authors": {"Browne, Hablot Knight": 1, "Dickens, Charles": 1}}, "langmap": {"en": "English", "eo": "Esperanto", "fr": "French", "gd": "Gaelic", "de": "German", "el": "Greek", "iw": "Hebrew", "hu": "Hungarian", "it": "Italian", "la": "Latin", "pl": "Polish", "pt": "Portuguese", "es": "Spanish", "cy": "Welsh"}}')
ANARCHIST_PAGE = 'trigger: \'hover\',\n                    container: \'body\',\n                    placement: \'bottom\',\n                });\n            });\n        }\n    });\n});\n</script>\n\n  \n<div class="list-group">\n  \n  <div class="amw-listing-item">\n    \n      \n    \n  \n  <a href="https://theanarchistlibrary.org/library/adreba-solneman-but-which-history-is-this" class="list-group-item clearfix">\n    \n<span class="amw-show-text-type"\n      data-poload="/library/adreba-solneman-but-which-history-is-this/toc"\n      data-title="Table of Contents">\n  <i aria-hidden="true"\n     \n     title="This text is an article"\n     class="awm-show-text-type-icon fa fa-file-text-o"\n     >\n  </i>\n</span>\n    <strong>But Which History Is This?</strong> — Adreba Solneman    \n    <span class="pull-right clearfix amw-list-text-pubdate-locale">\n      <small>Apr 8, 2011</small>\n      \n<span class="amw-show-text-type-and-number-of-pages">\n  <span class="label label-default">\n    19 pp.\n  </span>\n</span>\n    </span>\n  </a>\n  \n  </div>\n\n  <div class="amw-listing-item">\n    \n      \n    \n  \n  <a href="https://theanarchistlibrary.org/library/henry-h-em-nationalism-post-nationalism-and-shin-ch-ae-ho" class="list-group-item clearfix">\n    \n<span class="amw-show-text-type"\n      data-poload="/library/henry-h-em-nationalism-post-nationalism-and-shin-ch-ae-ho/toc"\n      data-title="Table of Contents">\n  <i aria-hidden="true"\n     \n     title="This text is an article"\n     class="awm-show-text-type-icon fa fa-file-text-o"\n     >\n  </i>\n</span>\n    <strong>Nationalism, Post-Nationalism, and Shin Ch’ae-ho</strong> — Henry H. Em    \n    <span class="pull-right clearfix amw-list-text-pubdate-locale">\n      <small>Jun 26, 2021</small>\n      \n<span class="amw-show-text-type-and-number-of-pages">\n  <span class="label label-default">\n    47 pp.\n  </span>\n</span>\n    </span>\n  </a>\n  \n  </div>\n\n  <div class="amw-listing-item">\n    \n      \n    \n  \n  <a href="https://theanarchistlibrary.org/library/leonie-wolters-o-laursen-anarchy-or-chaos" class="list-group-item clearfix">\n    \n<span class="amw-show-text-type"\n      data-poload="/library/leonie-wolters-o-laursen-anarchy-or-chaos/toc"\n      data-title="Table of Contents">\n  <i aria-hidden="true"\n     \n     title="This text is an article"\n     class="awm-show-text-type-icon fa fa-file-text-o"\n     >\n  </i>\n</span>\n    <strong>O. Laursen: Anarchy or Chaos</strong> — Leonie Wolters    \n    <span class="pull-right clearfix amw-list-text-pubdate-locale">\n      <small>Jun 17, 2025</small>\n      \n<span class="amw-show-text-type-and-number-of-pages">\n  <span class="label label-default">\n    6 pp.\n  </span>\n</span>\n    </span>\n  </a>\n  \n  </div>\n\n  <div class="amw-listing-item">\n    \n      \n    \n  \n  <a href="https://theanarchistlibrary.org/library/philip-bonner-jonathan-hyslop-and-lucien-van-der-walt-rethinking-worlds-of-labour" class="list-group-item clearfix">\n    \n<span class="amw-show-text-type"\n      data-poload="/library/philip-bonner-jonathan-hyslop-and-lucien-van-der-walt-rethinking-worlds-of-labour/toc"\n      data-title="Table of Contents">\n  <i aria-hidden="true"\n     \n     title="This text is an article"\n     class="awm-show-text-type-icon fa fa-file-text-o"\n     >\n  </i>\n</span>\n    <strong>Rethinking Worlds of Labour</strong> — Philip Bonner, Jonathan Hyslop and Lucien van der Walt    \n    <span class="pull-right clearfix amw-list-text-pubdate-locale">\n      <small>Sep 12, 2021</small>\n      \n<span class="amw-show-text-type-and-number-of-pages">\n  <span class="label label-default">\n    52 pp.\n  </span>\n</span>\n    </span>\n  </a>\n  \n  </div>\n\n  <div class="amw-listing-item">\n    \n      \n    \n  \n  <a href="https://theanarchistlibrary.org/library/william-godwin-of-history-and-romance" class="list-group-item clearfix">\n    \n<span class="amw-show-text-type"\n      data-poload="/library/william-godwin-of-history-and-romance/toc"\n      data-title="Table of Contents">\n  <i aria-hidden="true"\n     \n     title="This text is an article"\n     class="awm-show-text-type-icon fa fa-file-text-o"\n     >\n  </i>\n</span>\n    <strong>Of History and Romance</strong> — William Godwin    \n    <span class="pull-right clearfix amw-list-text-pubdate-locale">\n      <small>Sep 25, 2020</small>\n      \n<span class="amw-show-text-type-and-number-of-pages">\n  <span class="label label-default">\n    19 pp.\n  </span>\n</span>\n    </span>\n  </a>\n  \n  </div>\n\n  <div class="amw-listing-item">\n    \n      \n    \n  \n  <a href="https://theanarchistlibrary.org/library/ron-tabor-the-dialectics-of-ambiguity" class="list-group-item clearfix">\n    \n<span class="amw-show-text-type"\n      data-poload="/library/ron-tabor-the-dialectics-of-ambiguity/toc"\n      data-title="Table of Contents">\n  <i aria-hidden="true"\n     \n     title="This text is an article"\n     class="awm-show-text-type-icon fa fa-file-text-o"\n     >\n  </i>\n</span>\n    <strong>The Dialectics of Ambiguity</strong> — Ron Tabor    \n    <span class="pull-right clearfix amw-list-text-pubdate-locale">\n      <small>Jul 18, 2020</small>\n      \n<span class="amw-show-text-type-and-number-of-pages">\n  <span class="label label-default">\n    90 pp.\n  </span>\n</span>\n    </span>\n  </a>\n  \n  </div>\n\n  <div class="amw-listing-item">\n    \n      \n    \n  \n  <a href="https://theanarchistlibrary.org/library/agustin-guillamon-the-theorization-of-historical-experiences" class="list-group-item clearfix">\n    \n<span class="amw-show-text-type"\n      data-poload="/library/agustin-guillamon-the-theorization-of-historical-experiences/toc"\n      data-title="Table of Contents">\n  <i aria-hidden="true"\n     \n     title="This text is an article"\n     class="awm-show-text-type-icon fa fa-file-text-o"\n     >\n  </i>\n</span>\n    <strong>The theorization of historical experiences</strong> — Agustin Guillamón    \n    <span class="pull-right clearfix amw-list-text-pubdate-locale">\n      <small>Jul 22, 2020</small>\n      \n<span class="amw-show-text-type-and-number-of-pages">\n  <span class="label label-default">\n    17 pp.\n  </span>\n</span>\n    </span>\n  </a>\n  \n  </div>\n\n  <div class="amw-listing-item">\n    \n      \n    \n  \n  <a href="https://theanarchistlibrary.org/library/cornelius-castoriadis-history-as-creation" class="list-group-item clearfix">\n    \n<span class="amw-show-text-type"\n      data-poload="/library/cornelius-castoriadis-history-as-creation/toc"\n      data-title="Table of Contents">\n  <i aria-hidden="true"\n     \n     title="This text is an article"\n     class="awm-show-text-type-icon fa fa-file-text-o"\n     >\n  </i>\n</span>\n    <strong>History as Creation</strong> — Cornelius Castoriadis    \n    <span class="pull-right clearfix amw-list-text-pubdate-locale">\n      <small>Aug 20, 2020</small>\n      \n<span class="amw-show-text-type-and-number-of-pages">\n  <span class="label label-default">\n    52 pp.\n  </span>\n</span>\n    </span>\n  </a>\n  \n  </div>\n\n  <div class="amw-listing-item">\n    \n      \n    \n  \n  <a href="https://theanarchistlibrary.org/library/beautiful-losers" class="list-group-item clearfix">\n    \n<span class="amw-show-text-type"\n      data-poload="/library/beautiful-losers/toc"\n      data-title="Table of Contents">\n  <i aria-hidden="true"\n     \n     title="This text is an article"\n     class="awm-show-text-type-icon fa fa-file-text-o"\n     >\n  </i>\n</span>\n    <strong>Beautiful Losers</strong> — Bob Black    \n    <span class="pull-right clearfix amw-list-text-pubdate-locale">\n      <small>Aug 25, 2016</small>\n      \n<span class="amw-show-text-type-and-number-of-pages">\n  <span class="label label-default">\n    82 pp.\n  </span>\n</span>\n    </span>\n  </a>\n  \n  </div>\n\n  <div class="amw-listing-item">\n    \n      \n    \n  \n  <a href="https://theanarchistlibrary.org/library/carl-levy-social-histories-of-anarchism" class="list-group-item clearfix">\n    \n<span class="amw-show-text-type"\n      data-poload="/library/carl-levy-social-histories-of-anarchism/toc"\n      data-title="Table of Contents">\n  <i aria-hidden="true"\n     \n     title="This text is an article"\n     class="awm-show-text-type-icon fa fa-file-text-o"\n     >\n  </i>\n</span>\n    <strong>Social Histories of Anarchism</strong> — Carl Levy    \n    <span class="pull-right clearfix amw-list-text-pubdate-locale">\n      <small>Sep 16, 2020</small>\n      \n<span class="amw-show-text-type-and-number-of-pages">\n  <span class="label label-default">\n    64 pp.\n  </span>\n</span>\n    </span>\n  </a>\n  \n  </div>\n\n</div>\n</div>\n\n</div>\n\n          \n\n<ul class="pagination">\n  \n  <li class="active">\n    \n    <a href="https://theanarchistlibrary.org/search?page=1&amp;query=history">1</a>\n    \n  </li>\n  \n  <li >\n    \n    <a href="https://theanarchistlibrary.org/search?query=history&amp;page=2">2</a>\n    \n  </li>\n  \n  <li >\n    \n    <a href="https://theanarchistlibrary.org/search?query=history&amp;page=3">3</a>\n    \n  </li>\n  \n  <li >\n    \n    <a href="#" class="disabled">...</a>\n    \n  </li>\n  \n  <li >\n    \n    <a href="https://theanarchistlibrary.org/search?page=958&amp;query=history">958</a>\n    \n  </li>\n  \n  <li >\n    \n    <a href="https://theanarchistlibrary.org/search?page=959&amp;query=history">959</a>\n    \n  </li>\n  \n  <li >\n    \n    <a href="https://theanarchistlibrary.org/search?query=history&amp;page='

# ---------------------------------------------------------------- parsers
g = ba._parse_gutendex(GUTENDEX)
check("gutendex parser: 2 books from the real fixture", len(g) == 2)
check("gutendex parser: title/authors/text URL",
      g[0]["title"] == "Astronomy for beginners" and
      g[0]["authors"] == ["Carrington, Hereward"] and
      g[0]["url"] == "https://www.gutenberg.org/ebooks/78112.txt.utf-8")
check("gutendex parser: tag + book venue",
      g[0]["index"] == "library:gutenberg" and
      "book" in g[0]["venue"])
no_text = {"results": [{"title": "EPUB only", "authors": [],
                        "formats": {"application/epub+zip":
                                    "https://x.test/b.epub"}}]}
check("gutendex parser: a book with no plain text is skipped",
      ba._parse_gutendex(no_text) == [])
check("gutendex parser: garbage tolerated",
      ba._parse_gutendex(None) == [] and
      ba._parse_gutendex({"results": "junk"}) == [])

w = ba._parse_wikisource_search(WIKISOURCE)
check("wikisource parser: 8 hits from the real fixture", len(w) == 8)
check("wikisource parser: first hit + canonical page URL",
      w[0]["title"] ==
      "The Mathematical Principles of Natural Philosophy" and
      w[0]["url"] == "https://en.wikisource.org/wiki/"
      "The_Mathematical_Principles_of_Natural_Philosophy")
check("wikisource parser: tag + book venue",
      w[0]["index"] == "library:wikisource" and "book" in w[0]["venue"])
check("wikisource parser: garbage tolerated",
      ba._parse_wikisource_search(None) == [] and
      ba._parse_wikisource_search({"query": {}}) == [])

s = ba._parse_scielo_books(SCIELO_PAGE)
check("scielo parser: 2 records from the real fixture", len(s) == 2)
check("scielo parser: title/authors/year/abstract",
      s[0]["title"].startswith("Objetivos humanísticos") and
      s[0]["authors"][:1] == ["Silva, Ana Paula Bispo da"] and
      s[0]["year"] == 2019 and len(s[0]["abstract"]) > 100)
check("scielo parser: book URL + tag",
      s[0]["url"] == "https://books.scielo.org/id/5dczv" and
      s[0]["index"] == "library:scielo-books")
check("scielo parser: garbage tolerated",
      ba._parse_scielo_books("") == [] and
      ba._parse_scielo_books("<html>no records</html>") == [])

m = ba._parse_mek_results(MEK_PAGE)
check("mek parser: 2 records from the real fixture", len(m) == 2)
check("mek parser: title/author/year/url",
      m[0]["title"] == "Csillagok a Bibliában" and
      m[0]["authors"] == ["Ponori Thewrewk Aurél"] and
      m[0]["year"] == 2011 and
      m[0]["url"] == "https://mek.oszk.hu/09200/09250")
check("mek parser: tag", m[0]["index"] == "library:mek")
check("mek parser: garbage tolerated",
      ba._parse_mek_results("") == [] and
      ba._parse_mek_results("<html>nothing</html>") == [])

f = ba._parse_fadedpage(FADED_JSON)
check("fadedpage parser: 1 book from the real fixture", len(f) == 1)
check("fadedpage parser: title/author/year/text URL",
      f[0]["title"] == "Bleak House" and
      f[0]["authors"] == ["Dickens, Charles"] and
      f[0]["year"] == 1853 and
      f[0]["url"] ==
      "https://www.fadedpage.com/books/20161103/20161103.txt")
check("fadedpage parser: tag", f[0]["index"] == "library:fadedpage")
check("fadedpage parser: a row without pid is skipped",
      ba._parse_fadedpage({"rows": [{"title": "No pid"}]}) == [])
check("fadedpage parser: garbage tolerated",
      ba._parse_fadedpage(None) == [])

a = ba._parse_anarchist_results(ANARCHIST_PAGE)
check("anarchist parser: results from the real fixture", len(a) >= 4)
check("anarchist parser: title/author/year/url",
      a[0]["title"] == "But Which History Is This?" and
      a[0]["authors"] == ["Adreba Solneman"] and a[0]["year"] == 2011 and
      a[0]["url"] == "https://theanarchistlibrary.org/library/"
      "adreba-solneman-but-which-history-is-this")
check("anarchist parser: tag",
      a[0]["index"] == "library:anarchist-library")
check("anarchist parser: garbage tolerated",
      ba._parse_anarchist_results("") == [] and
      ba._parse_anarchist_results("<html>nothing</html>") == [])

# -------------------------------------------------------------------- caps
many = {"results": [{"title": f"Book {i}", "authors": [],
                     "formats": {"text/plain; charset=utf-8":
                                 f"https://x.test/{i}.txt"}}
                    for i in range(12)]}
check("gutendex parser: per-source cap 8",
      len(ba._parse_gutendex(many)) == ba.LIB_PER_SOURCE_CAP == 8)

orig_searchers = ba._WLIB_SEARCHERS
ba._WLIB_SEARCHERS = [
    (f"S{i}", (lambda i: lambda q: [
        {"title": f"T{i}-{j}", "url": f"https://x.test/{i}/{j}",
         "index": "library:gutenberg"} for j in range(8)])(i))
    for i in range(5)]
try:
    pooled = ba.search_digital_libraries("anything")
    check("search_digital_libraries: total cap 24",
          len(pooled) == ba.LIB_TOTAL_CAP == 24)
finally:
    ba._WLIB_SEARCHERS = orig_searchers

# --------------------------------------------------------------- fail-soft
orig_fetch = ba._wlib_fetch
ba._wlib_fetch = lambda *a, **k: (None, "")
try:
    check("fail-soft: transport down -> every library returns []",
          ba._lib_gutenberg("x") == [] and ba._lib_wikisource("x") == []
          and ba._lib_scielo_books("x") == [] and ba._lib_mek("x") == []
          and ba._lib_fadedpage("x") == []
          and ba._lib_anarchist("x") == [])
finally:
    ba._wlib_fetch = orig_fetch
ba._wlib_fetch = lambda *a, **k: (500, "server error")
try:
    check("fail-soft: HTTP 500 -> every library returns []",
          ba._lib_gutenberg("x") == [] and ba._lib_wikisource("x") == []
          and ba._lib_scielo_books("x") == [] and ba._lib_mek("x") == []
          and ba._lib_fadedpage("x") == []
          and ba._lib_anarchist("x") == [])
finally:
    ba._wlib_fetch = orig_fetch
ba._wlib_fetch = lambda *a, **k: (200, "this is not json {{{")
try:
    check("fail-soft: garbage body -> JSON libraries return []",
          ba._lib_gutenberg("x") == [] and ba._lib_wikisource("x") == []
          and ba._lib_fadedpage("x") == [])
finally:
    ba._wlib_fetch = orig_fetch


def _boom(query):
    raise RuntimeError("boom")


ba._WLIB_SEARCHERS = [("Boom", _boom), ("OK", lambda q: [
    {"title": "T", "url": "https://x.test/1",
     "index": "library:mek"}])]
try:
    out = ba.search_digital_libraries("q")
    check("fail-soft: one raising source does not sink the pool",
          len(out) == 1 and out[0]["index"] == "library:mek")
finally:
    ba._WLIB_SEARCHERS = orig_searchers

# -------------------------------------------------------------------- tags
tags = set()
for parser, fixture in ((ba._parse_gutendex, GUTENDEX),
                        (ba._parse_wikisource_search, WIKISOURCE),
                        (ba._parse_scielo_books, SCIELO_PAGE),
                        (ba._parse_mek_results, MEK_PAGE),
                        (ba._parse_fadedpage, FADED_JSON),
                        (ba._parse_anarchist_results, ANARCHIST_PAGE)):
    tags |= {c["index"] for c in parser(fixture)}
check("tags: exactly the six §27 library tags",
      tags == {"library:gutenberg", "library:wikisource",
               "library:scielo-books", "library:mek",
               "library:fadedpage", "library:anarchist-library"})
check("tags: no clash with §25 tags",
      not (tags & {"library:shamela", "iraq:ijs",
                   "qatar:qscience-crossref"}))

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURES")
    sys.exit(1)
print("ALL §27 CHECKS PASSED")
