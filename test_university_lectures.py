"""Offline tests for §32 — open-text university lecture platforms.

Run: python3 test_university_lectures.py
No network: every fetch is stubbed or fed from fixtures trimmed from
the REAL probe responses of 2026-11-26 (MIT OCW sitemap index +
course sitemap + video resource page, Yale catalogue + ASTR 160
session list, SEE course list + CS229 page, CS50x 2025 index nav,
TU Delft search RSS, METU Moodle search/course/resource pages,
Wikiversity API JSON, OpenStax site config + Astronomy 2e tree,
NYCU search + course page, Hokkaido search + lecture page, TÜBA
homepage + course page, BOU directory listings). The probes
themselves (search answers + full text reachable for the 12 wired
platforms; routes tried for the 13 registry-only ones) are recorded
in lectures_registry.json and in files/university-lectures*.md.
"""
import importlib.util
import json
import os
import sys

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


FIX = {
    'mit_index.xml': '<?xml version="1.0" encoding="utf-8"?>\n<sitemapindex>\n  <sitemap><loc>https://ocw.mit.edu/sitemap-www.xml</loc></sitemap>\n  <sitemap><loc>https://ocw.mit.edu/courses/5-111-principles-of-chemical-science-fall-2008/sitemap.xml</loc></sitemap>\n  <sitemap><loc>https://ocw.mit.edu/courses/5-111sc-principles-of-chemical-science-fall-2014/sitemap.xml</loc></sitemap>\n  <sitemap><loc>https://ocw.mit.edu/courses/8-282j-introduction-to-astronomy-spring-2006/sitemap.xml</loc></sitemap>\n</sitemapindex>',
    'mit_course_sitemap.xml': '<urlset>\n  <url><loc>https://ocw.mit.edu/courses/5-111-principles-of-chemical-science-fall-2008/resources/lecture-1/</loc></url>\n  <url><loc>https://ocw.mit.edu/courses/5-111-principles-of-chemical-science-fall-2008/resources/lecture-10/</loc></url>\n  <url><loc>https://ocw.mit.edu/courses/5-111-principles-of-chemical-science-fall-2008/resources/lecture-11/</loc></url>\n  <url><loc>https://ocw.mit.edu/courses/5-111-principles-of-chemical-science-fall-2008/resources/lecture-12/</loc></url>\n</urlset>',
    'mit_resource.html': '93caa4a3f"\n    class="video-js vjs-default-skin vjs-big-play-centered vjs-ocw"\n    controls\n    data-downloadlink = "http://www.archive.org/download/MIT5.111F08/ocw-5.111-f08-lec01_300k.mp4"\n    data-transcriptlink = "/courses/5-111-principles-of-chemical-science-fall-2008/f562084c51ab8bcc3ff67beed05e5e4c_-c-X8zk0ywo.pdf"\n    data-setup=\'{\n    "fluid": true,\n    "techOrder": ["youtube"],\n    "inactivityTimeout": 0,\n    "sources": [{"type": "video/youtube", "src": "https://www.youtube.com/embed/-c-X8zk0ywo"}]\n    , &#34;youtube&#34;: {&#34;ytControls&#34;:2} }\'\n  >\n      \n        \n        <trac\n<!-- segment 2 -->\n\n\t<div class="video-player-wrapper">\n\t\t\n\n\n\n\n<div class="video-container embedded-video-container youtube-container">\n  <video\n    id="video-player--c-X8zk0ywo-8909d19c54ef401c9f562cf93caa4a3f"\n    class="video-js vjs-default-skin vjs-big-play-centered vjs-ocw"\n    controls\n    data-downloadlink = "http://www.archive.org/download/MIT5.111F08/ocw-5.111-f08-lec01_300k.mp4"\n    data-transcriptlink = "/courses/5-111-principles-of-chemical-science-fall-2008/f562084c51ab8bcc3ff67beed05e5e4c_-c-X8zk0ywo.pdf"\n    data-setup=\'{\n    "fluid": true,\n    "techOrder": ["youtube"],\n    "inactivityTimeout": 0,\n    "sources": [{"type": "video/youtube", "src": "https://www.youtube.com/embed/-c-X8zk0ywo"}]\n    , &#34;youtube&#34;: {&#34;ytControls&#34;:2} }\'\n  >\n      \n        \n        <track kind="captions" ',
    'yale_courses.html': '                                             <td headers="view-field-semester-table-column" class="views-field views-field-field-semester views-align-left">Fall 2011          </td>\n              </tr>\n          <tr>\n                                                                                        <td headers="view-title-table-column" class="views-field views-field-title is-active views-align-left"><a href="/astronomy" hreflang="en">Astronomy</a>          </td>\n                                                                                        <td headers="view-field-course-number-table-column" class="views-field views-field-field-course-number views-align-left"><a href="/astronomy/astr-160" hreflang="en">ASTR 160</a>          </td>\n                                                                                        <td headers="view-title-1-table-column" class="views-field views-field-title-1 views-align-left"><a href="/astronomy/astr-160" hreflang="en">Frontiers and Controversies in Astrophysics</a>          </td>\n                                                                                        <td headers="view-field-course-professor-last-name-table-column" class="views-field views-field-field-course-professor-last-name views-align-left">Charles Bailyn          </td>\n                                                                                        <td headers="view-field-semester-table-column" class="views-field views-field-field-semester views-align-left">Spring 2007          </td>\n              </tr>\n          <tr>\n                              ',
    'yale_course.html': '3>\n    </div>\n      \n      <div class="view-content">\n      <table class="views-table views-view-table cols-0">\n      <tbody>\n          <tr>\n                                                                                        <td class="views-field views-field-field-session-display-number">Lecture 1          </td>\n                                                                                        <td class="views-field views-field-field-session-display-title"><a href="/astronomy/astr-160/lecture-1" hreflang="en">Introduction</a>          </td>\n              </tr>\n          <tr>\n                                                                                        <td class="views-field views-field-field-session-display-number">Lecture 2          </td>\n                                                                                        <td class="views-field views-field-field-session-display-title"><a href="/astronomy/astr-160/lecture-2" hreflang="en">Planetary Orbits</a>          </td>\n              </tr>\n          <tr>\n                                                                                        <td class="views-field views-field-field-session-display-number">Lecture 3          </td>\n                                                                                        <td class="views-field views-field-field-session-display-title"><a href="/astronomy/astr-160/lecture-3" hreflang="en">Our Solar System and the Pluto Problem</a>          </td>\n              </tr>\n          <tr>\n                                                                                        <td class="views-field views-field-field-session-display-number">Lecture 4          </td>\n                                                                                        <td class="views-field views-field-field-session-display-title"><a href="/astronomy/astr-160/lecture-4" hrefla',
    'see_list.html': 'ligence">Artificial Intelligence</h2>\n</div>\n<ul class="channels-overview-list">\n<li>\n<a href="/Course/CS223A">\n<div class="channel-overview">\n<div class="channel-overview-img">\n<img alt="Introduction to Robotics" height="200" src="/Content/Images/Courses/CS223A.jpg" width="356"/>\n</div>\n</div>\n</a>\n<h2 class="channel-overview-title"><a href="/Course/CS223A">Introduction to Robotics</a></h2>\n<ul class="channel-overview-details">\n<li>CS223A</li>\n<li>Khatib, Oussama</li>\n</ul>\n</li>\n<li>\n<a href="/Course/CS229">\n<div class="channel-overview">\n<div class="channel-overview-img">\n<img alt="Machine Learning" height="200" src="/Content/Images/Courses/CS229.jpg" width="356"/>\n</div>\n</div>\n</a>\n<h2 class="channel-overview-title"><a href="/Course/CS229">Machine Learning</a></h2>\n<ul class="channel-overview-details">\n<li>CS229</li>\n<li>Ng, Andrew</li>\n</ul>\n</li>\n</ul>\n<div class="heading">\n<h2 id="Linear Systems and Optimization">Linear Systems and Optimization</h2>\n</div>\n<ul class="channels-overview-list">\n<li>\n<a href="/Course/EE261">\n<div class="channel-overview">\n<div class="channel-overview-img">\n<img alt="The Fourier Transform and its Applications" height="200" src="/Content/Images/C',
    'see_course.html': '>\n</td>\n<td>\n\t\t\t\t\t\t\t\t\t\t\t1 hr 9 min\n\t\t\t\t\t\t\t\t\t\t</td>\n</tr>\n<tr class="row-secondary">\n<td colspan="4">\n<strong>Topics:</strong> The Motivation &amp; Applications of Machine Learning, The Logistics of the Class, The Definition of Machine Learning, The Overview of Supervised Learning, The Overview of Learning Theory, The Overview of Unsupervised Learning, The Overview of Reinforcement Learning\n\t\t\t\t\t\t\t\t\t\t</td>\n</tr>\n</tbody>\n</table>\n<div class="heading" style="margin-top: 0">\n<h3>Transcripts</h3>\n</div>\n<ul class="list-inline">\n<li><a class="btn btn-small btn-highlight" href="/materials/aimlcs229/transcripts/MachineLearning-Lecture01.html" target="_blank">HTML</a></li>\n<li><a class="btn btn-small btn-highlight" href="/materials/aimlcs229/transcripts/MachineLearning-Lecture01.pdf" target="_blank">PDF</a></li>\n</ul>\n</div>\n</div>\n<div class="panel panel-collapsable">\n<div class="panel-heading">\n<h2>Lecture 2</h2>\n</div>\n<div class="panel-content">\n<table class="table">\n<thead>\n<tr>\n<td width="33%">Watch Online:</td>\n<td>\n\t\t\t\t\t\t\t\t\t\t\tDownload:\n\t\t\t\t\t\t\t\t\t\t\t<div class="down-hints">Right Click, and Save As</div>\n</td>\n<td>Duration:</td>\n</tr>\n</thead>\n<tbody>\n<tr>\n<td>\n<a class="btn" href="/Course/CS229/54" rel="player">\n<i class="icon icon-play"></i> Watch Now\n\t\t\t\t\t\t\t\t\t\t\t</a>\n</td>\n<td>\n<a class="btn btn-link" download="" href="/videos/courses/see/CS229/CS229-lecture02.mp4">\n<i class="icon icon-download"></i> Download\n\t\t\t\t\t\t\t\t\t\t\t</a>\n</td>\n<td>\n\t\t\t\t\t\t\t\t\t\t\t1 hr 16 min\n\t\t\t\t\t\t\t\t\t\t</td>\n</t',
    'cs50_index.html': '-hidden="true" class="ps-2 text-decoration-none">🖼️ </span></a></li>\n  <li data-marker="*"><a href="new/">What’s new for 2025?</a></li>\n</ul>\n\n<!-- Course Nav -->\n<hr>\n\n<ul>\n  <li data-marker="*">\n<a href="weeks/0/">Week 0</a> Scratch</li>\n  <li data-marker="*">\n<a href="weeks/1/">Week 1</a> C</li>\n  <li data-marker="*">\n<a href="weeks/2/">Week 2</a> Arrays</li>\n  <li data-marker="*">\n<a href="weeks/3/">Week 3</a> Algorithms</li>\n  <li data-marker="*">\n<a href="weeks/4/">Week 4</a> Memory</li>\n  <li data-marker="*">\n<a href="weeks/5/">Week 5</a> Data Structures</li>\n  <li data-marker="*">\n<a href="weeks/6/">Week 6</a> Python</li>\n  <li data-marker="*">\n<a href="weeks/7/">Week 7</a> SQL</li>\n  <li data-marker="*">\n<a href="weeks/8/">Week 8</a> HTML, CSS, JavaScript</li>\n  <li data-marker="*">\n<a href="weeks/9/">Week 9</a> Flask</li>\n  <li data-marker="*">\n<a href="weeks/10/">Week 10</a> The End</li>\n</ul>\n<ul>\n  <li data-marker="*"><a href="weeks/ai/">Artificial Intelligence</a></li>\n</ul>\n<ul class="small">\n  <li data-marker="*"><a href="practice/"><i class="fa-solid fa-rocket pe-2 text-decoration-none"></i>Additional Practice</a></li>\n</ul>\n\n<hr>\n\n<!-- Final project spec, seminars, shorts -->\n<ul>\n  <li data-marker="*"><a href="project/">Final Project</a></li>\n  <li data-marker="*"><a href="gallery/">Gallery of Final Projects<span aria-hidden="true" class="ps-2 text-decoration-none">🖼️ </span></a></li>\n  <li data-marker="*"><a href="seminars/">Seminars</a></li>\n  <li data-marker="*">\n</li></ul>\n\n<!-- Items that might usually appear in an academic syllabus -->\n<hr>\n\n<ul>\n  <li data-marker="*"><a href="honesty/">Academic Honesty</a></li>\n  <li data-marker="*"><a href="cert',
    'tudelft_feed.xml': '<rss><channel>\n<item>\n\t\t<title>5.3.2 Program Management Components</title>\n\t\t<link>https://ocw.tudelft.nl/course-readings/5-3-2-program-management-components/#utm_source=rss&#038;utm_medium=rss&#038;utm_campaign=5-3-2-program-management-components</link></item>\n<item>\n\t\t<title>Martine Rutten</title>\n\t\t<link>https://ocw.tudelft.nl/teachers/martine-rutten/#utm_source=rss&#038;utm_medium=rss&#038;utm_campaign=martine-rutten</link></item>\n</channel></rss>',
    'metu_search.html': 'mathematics"\n        >\n        <div class="input-group-append">\n            <button type="submit" class="btn  btn-primary search-icon">\n                <i class="icon fa fa-search fa-fw " aria-hidden="true"  ></i>\n                <span class="sr-only">Search courses</span>\n            </button>\n        </div>\n\n    </div>\n    </form>\n</div><h2>Search results: 1</h2><div class="courses course-search-result course-search-result-search"><div class="coursebox clearfix odd first last" data-courseid="83" data-type="1"><div class="info"><h3 class="coursename"><a class="" href="https://ocw.metu.edu.tr/course/view.php?id=83">Elements of Probability and Statistics</a></h3><div class="moreinfo"></div><div class="enrolmenticons"><i class="icon fa fa-unlock-alt fa-fw "  title="Guest access" role="img" aria-label="Guest access"></i></div></div><div class="content content-block"><div class="summary"><div class="no-overflow"><br />\n <p>The goal of this course is to introduce students to the basic probability theory and mathematical statistics and help them in establishing a good theoretical background for their future professions. This course provides a comprehensive introduction to probability, st',
    'metu_course.html': 'span></span></strong></p>\n<table border="0">\n<tbody>\n<tr>\n<td width="175" valign="top">\n<p align="right"><img src="https://ocw.metu.edu.tr/pluginfile.php/2265/course/section/810/ceylan2.jpg" width="150" height="198" alt="Photo of Ceylan Yozgatligil" /></p>\n</td>\n<td width="250" valign="top">\n<p><br /><br /><br /></p>\n<p>Instructor: Ceylan YOZGATLIGİL</p>\n<p>Added: 15 March 2010</p>\n</td>\n</tr>\n</tbody>\n</table></div></div><ul class="section img-text"><li class="activity resource modtype_resource  " id="module-1273"><div><div class="mod-indent-outer w-100"><div class="mod-indent"></div><div><div class="activityinstance"><a class="aalink" onclick="" href="https://ocw.metu.edu.tr/mod/resource/view.php?id=1273"><img src="https://ocw.metu.edu.tr/theme/image.php/eguru/core/1793185538/f/pdf-24" class="iconlarge activityicon" alt="" role="presentation" aria-hidden="true" /><span class="instancename">Syllabus<span class="accesshide " > File</span></span></a></div></div></div></div></li></ul></div></li><li id="section-1" class="section main clearfix" role="region" aria-labelledby="sectionid-811-title" data-sectionid="1" data-sectionreturnid="0"><div class="left side"></div><div class="right side"><img class="icon spacer" width="1" height="1" alt="" aria-hidden="true" src="https://ocw.metu.edu.tr/theme/image.php/eguru/core/1793185538/spacer" /></div><div class="content"><h3 id="sectionid-811-title" class="sectionname"><span><a href="https://ocw.metu.edu.tr/course/view.php?id=83#section-1">Topic 1</a></span></h3><div class="section_availability"></div><div class="summary"',
    'metu_resource.html': '                    \n                    </div>\n                </div>\n            </div>\n        </div>\n    </div>\n</header>\n\n        <div id="page-content" class="row pb-3">\n            <div id="region-main-box" class="col-12">\n                <section id="region-main" >\n                    <span class="notifications" id="user-notifications"></span>\n                    <div role="main"><span id="maincontent"></span><h2>Syllabus</h2><div class="resourcecontent resourcepdf">\n  <object id="resourceobject" data="https://ocw.metu.edu.tr/pluginfile.php/2275/mod_resource/content/0/ocw_iam530/outline.pdf" type="application/pdf" width="800" height="600">\n    <param name="src" value="https://ocw.metu.edu.tr/pluginfile.php/2275/mod_resource/content/0/ocw_iam530/outline.pdf" />\n    Click <a href="https://ocw.metu.edu.tr/pluginfile.php/2275/mod_resource/content/0/ocw_iam530/outline.pdf" >outline.pdf</a> link to view the file.\n  </object>\n</div></div>\n                    <div class="mt-5 mb',
    'wikiversity.json': '{"batchcomplete":"","continue":{"sroffset":3,"continue":"-||"},"query":{"searchinfo":{"totalhits":764},"search":[{"ns":0,"title":"Astronomy","pageid":237287,"size":3072,"wordcount":384,"snippet":"file) <span class=\\"searchmatch\\">Astronomy</span> learning projects (6 cats, 32 pgs) <span class=\\"searchmatch\\">Astronomy</span> quizzes (4 cats, 5 pgs) <span class=\\"searchmatch\\">Astronomy</span>/Laboratories (2 pgs) <span class=\\"searchmatch\\">Astronomy</span>/Lectures (5 pgs) <span class=\\"searchmatch\\">Astronomy</span>/Lessons","timestamp":"2024-02-26T19:00:37Z"},{"ns":0,"title":"Introduction to astronomy","pageid":126155,"size":6592,"wordcount":609,"snippet":"see Directions for use for more information. Welcome to Introduction to <span class=\\"searchmatch\\">Astronomy</span> Deep space remains one of the greatest mysteries, and our ever-present","timestamp":"2021-01-02T04:46:27Z"},{"ns":0,"title":"Amateur astronomy","pageid":71968,"size":8294,"wordcount":1239,"snippet":"Amateur <span class=\\"searchmatch\\">Astronomy</span> is <span class=\\"searchmatch\\">astronomy</span> done by non-professionals who either recreationally observe or hope to contribute to current scientific research. Amateur","timestamp":"2022-05-13T14:41:33Z"}]}}',
    'openstax_anchor.html': '"loadOptions":{"booksConfig":{"archiveUrl":"/apps/archive/20260604.144757","books":{"13ac107a-f15f-49d2-97e8-60ab2e3b519c":{"defaultVersion":"ebc5beb" ... 6-528c-4101-ad26-19c612c5a2fa":{"defaultVersion":"52021fd"},"4c29f9e5-a53d-42c0-bdb5-091990527d79":{"defaultVersion":"dff6acf"},"4eaa8f03-88a8-485a-a777-dd3602f6c13e":{"defaultVersion":"',
    'openstax_tree.json': '{"title": "Astronomy 2e", "tree": {"id": "4c29f9e5-a53d-42c0-bdb5-091990527d79@dff6acf", "title": "Astronomy 2e", "slug": "astronomy-2e", "contents": [{"id": "b795b4f7-3318-4419-a338-8f5eeb0874ee@", "title": "<span data-type=\\"\\" itemprop=\\"\\" class=\\"os-text\\">Preface</span>", "toc_type": "book-content", "toc_target_type": "preface", "slug": "preface"}, {"id": "7a834c01-a9a2-5697-a1cf-0f59df8b56ca@dff6acf", "title": "<span class=\\"os-number\\"><span class=\\"os-part-text\\">Chapter </span>1</span>\\n<span class=\\"os-divider\\"> </span>\\n<span class=\\"os-text\\" data-type=\\"\\" itemprop=\\"\\">Science and the Universe: A Brief Tour</span>", "slug": "1-science-and-the-universe-a-brief-tour", "contents": [{"id": "ff8e6ee8-8a50-4756-b95e-c3b8e803622b@", "title": "<span data-type=\\"\\" itemprop=\\"\\" class=\\"os-text\\">Introduction</span>", "toc_type": "book-content", "toc_target_type": "intro", "slug": "1-introduction"}, {"id": "7f11b6c9-3774-45f2-89e7-65c0196f6c29@", "title": "<span class=\\"os-number\\">1.1</span><span class=\\"os-divider\\"> </span><span data-type=\\"\\" itemprop=\\"\\" class=\\"os-text\\">The Nature of Astronomy</span>", "toc_type": "book-content", "toc_target_type": "numbered-section", "slug": "1-1-the-nature-of-astronomy"}, {"id": "c2150b0b-4881-42c7-83b2-ad766c79d36a@", "title": "<span class=\\"os-number\\">1.2</span><span class=\\"os-divider\\"> </span><span data-type=\\"\\" itemprop=\\"\\" class=\\"os-text\\">The Nature of Science</span>", "toc_type": "book-content", "toc_target_type": "numbered-section", "slug": "1-2-the-nature-of-science"}]}]}}',
    'nycu_search.html': 'p_mobile&quot;:{&quot;unit&quot;:&quot;px&quot;,&quot;size&quot;:&quot;&quot;,&quot;sizes&quot;:[]}}" data-widget_type="archive-posts.archive_classic">\n\t\t\t\t<div class="elementor-widget-container">\n\t\t\t<link rel="stylesheet" href="https://ocw.nycu.edu.tw/wp-content/plugins/elementor-pro/assets/css/widget-posts.min.css">\t\t<div class="elementor-posts-container elementor-posts elementor-posts--skin-classic elementor-grid">\n\t\t\t\t<article class="elementor-post elementor-grid-item post-129529 course_page type-course_page status-publish format-standard has-post-thumbnail hentry">\n\t\t\t\t<a class="elementor-post__thumbnail__link" href="https://ocw.nycu.edu.tw/?course_page=all-course/college-of-science/am/calculus-a-ii-academic-109" >\n\t\t\t<div class="elementor-post__thumbnail"><img width="1920" height="1080"   alt="微積分甲二_蘇承芳老師" data-src="https://ocw.nycu.edu.tw/wp-content/uploads/2024/10/微積分甲二_蘇承芳老師.jpg" class="attachment-full size-full wp-image-129532 lazyload" src="data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==" /><noscript><img width="1920" height="1080" src="https://ocw.nycu.edu.tw/wp-content/uploads/2024/10/微積分甲二_蘇承芳老師.jpg" class="attachment-full size-full wp-image-129532" alt="微積分甲二_蘇承芳老師" /></noscript></div>\n\t\t</a>\n\t\t\t\t<div class="elementor-post__text">\n\t\t\t\t<h3 class="elementor-post__title">\n\t\t\t<a href="https://ocw.nycu.edu.tw/?course_page=all-course/college-of-science/am/calculus-a-ii-academic-109" >\n\t\t\t\t微積分甲(二)109學年度 &#8211; Calculus I (B)  Academic Year 109 | 應用數學系 蘇承芳老師\t\t\t</a>\n\t\t</h3>\n\t\t\t\t<div class="elementor-post__meta-data">\n\t\t\t\t\t<span class="eleme',
    'nycu_course.html': '      <p><strong class="title">學習筆記 Learning Notes</strong></p><p>\xa0</p><table border="1" width="98% cellspacing="><tbody><tr class="stitle"><td colspan="2" bgcolor="#e4d8b0"><strong> 學習筆記-由光電工程學系陳啟馼同學提供</strong></td></tr><tr class="stitle"><td bgcolor="#e4d8b0"><strong> 章節</strong></td><td bgcolor="#e4d8b0"><strong> 下載連結</strong></td></tr><tr><td style="padding: 5px;"><span class="text">1. 單變數純量函數—極限與連續</span></td><td style="padding: 5px; text-align: center;"><span class="text"><a href="https://video.ocw.nycu.edu.tw/pub/ca091/ca091ca092_studynotes01.pdf" target="_blank" rel="noopener noreferrer">PDF</a></span></td></tr><tr bgcolor="#EEEEEE"><td style="padding: 5px;"><span class="text">2. 單變數純量函數—導數（概念篇）</span></td><td style="padding: 5px; text-align: center;"><span class="text"><a href="https://video.ocw.nycu.edu.tw/pub/ca091/ca091ca092_studynotes01.pdf" target="_blank" rel="noopener noreferrer">PDF</a></span></td></tr><tr><td style="padding: 5px;"><span class="text">3. 單變數純量函數—導數（應用篇）</span></td><td style="padding: 5px; text-align: center;"><span class="text"><a href="https://video.ocw.nycu.edu.tw/pub/ca091/ca091ca092_studynotes03.pdf" target="_blank" rel="noopener noreferrer">PDF</a></span></td></tr><tr bgcolor="#EEEEEE"><td style="padding: 5px;"><span class="text">4.\xa0單變數純量函數—積分（概念篇）</span></td><td style="padding: 5px; text-align: center;"><span class="text"><a href="https://video.ocw.nycu.edu.tw/pub/ca091/ca091ca092_studynotes04.pdf" target="_blank" rel="noopener noreferrer">PDF</a></span></td></tr><tr><td style="padding: 5px;"><span class="text">5. 單變數純量函數—積分（技巧篇）</span',
    'hokudai_search.html': 'ps://ocw.hokudai.ac.jp/lecture/english1-robb" class="truncate_more_link">もっと見る</a></div>\n                                </span>\n                            </td>\n                        </tr>\n                    <!-- 投稿タイプを判定 -->\n                                    <!-- 投稿タイプを判定：講義 -->\n                        <tr style="opacity: 1;" class="clearfix search_result_item">\n                            <td class="item_image">\n                                <a href="https://ocw.hokudai.ac.jp/lecture/internationalfaculty10omics-2007" class="thumb">\n                                    <img src="/commons/images/noimage.jpg"/>\n                                </a>\n                            </td>\n                            <td class="item_title">\n                                <div class="item_title1">\n                                    <a href="https://ocw.hokudai.ac.jp/lecture/internationalfaculty10omics-2007"><p>国際経済学</p>\n</a>\n                                                                    </div>\n                                <div class="item_title2">\n                                                                    </div>\n                            </td>\n                    ',
    'hokudai_lecture.html': '）</a>\n<a href="/lecture/internationaleconomics-2005" target="_blank" rel="noopener noreferrer">国際経済学（2005年度） </a></div><div class=\'cnt_left\'>Course Materials</div><div class=\'cnt_right\'><p><span style="text-decoration: underline;"><a href="https://ocw.hokudai.ac.jp/wp-content/uploads/2016/01/InternationalEconomics-2007-Note-all.pdf" target="_blank" rel="noopener">全講義ノート</a></span></p>\n<p>目次\u3000 <span style="text-decoration: underline;"><a href="https://ocw.hokudai.ac.jp/wp-content/uploads/2016/01/InternationalEconomics-2007-Note-00.pdf" target="_blank" rel="noopener">目次、参考資料</a></span></p>\n<p>第1章\u3000<span style="text-decoration: underline;"><a href="https://ocw.hokudai.ac.jp/wp-content/uploads/2016/01/InternationalEconomics-2007-Note-01.pdf" target="_blank" rel="noopener">グローバリゼーションと国際経済学</a></span></p>\n<p>第2章\u3000<span style="text-decoration: underline;"><a href="https://ocw.hokudai.ac.jp/wp-content/uploads/2016/01/InternationalEconomics-2007-Note-02.pdf" target="_blank" rel="noopener">国民経済と世界市場</a></span></p>\n<p>第3章\u3000<span style="text-decoration: underline;"><a href="https://ocw.hokudai.ac.jp/wp-content/uploads/2016/01/InternationalEconomics-2007-Note-03.pdf" target="_blank" rel="noopener">国際貿易</a></span></p>\n<p>第4章\u3000<span style="text-decoration: underline;"><a href="https://ocw.hokudai.ac.jp/wp-content/uploads/2016/01/InternationalEconomics-2007-Note-04.pdf" target="_blank" rel="noopener">国際資本移動</a></span></p>\n<p>第5章\u3000<span style="text-decoration: underline;"><a href="https://ocw.hokudai.ac.jp/wp-content/uploads/2016/01/InternationalEconomics-2007-Note-05.pdf" target="_blank" rel="n',
    'tuba_home.html': '80</span> ders bulundu\n                    </p>\n                </div>\n            </div>\n\n            <!-- Course grid -->\n                <div class="course-grid" id="course-grid">\n\n\n<article class="course-card group" data-institution="MIT" data-language="TR">\n    <a class="block h-full" href="/ders/algoritmalara-giris-mit-prof-charles-leiserson-ve-prof-erik-demaine">\n        <!-- Gold gradient top border (visible on hover via CSS) -->\n        <div class="card-top-border" aria-hidden="true"></div>\n\n        <div class="p-5 flex flex-col h-full">\n            <!-- Header row: code + institution badge -->\n            <div class="flex items-start justify-between gap-2 mb-3">\n                    <span class="font-mono text-xs text-text-secondary bg-white/5 px-2 py-0.5 rounded border border-border-dark">\n                        6.046J / 18.410J\n                    </span>\n                    <span class="inst-pill text-xs font-medium px-2.5 py-0.5 rounded-full flex-shrink-0\n                        mit-badge">\n                        MIT\n                    </span>\n            </div>\n\n            <!-- Course title -->\n            <h3 class="font-serif font-semibold text-base text-text-primary leading-snug mb-2 group-hover:text-gold transition-colors duration-200 line-clamp-2 flex-1">\n                Algoritmalara Giri&#x15F; (MIT) (Prof. Charles Leiserson &amp; Prof. Erik Demaine)\n            </h3>\n\n            <!-- Professors -->\n\n            <!-- Semester -->\n                <p class="text-xs text-text-secondary/70 mb-4">Sonbahar 2005</p>\n\n            <!-- Available materials row -->\n            <div class="flex items-center gap-3 pt-3 border-t border-border-dark/60">\n                <span class="material-badge unavailable" title="İzlence">\n                    <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>\n                    <span>İzlence</span>\n                </span>\n                <span class="material-badge available" title="Ders Notları">\n                    <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"/></svg>\n                    <span>Notlar</span>\n                </span>\n                <span class="material-badge available" title="Ödevler">\n                    <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/></svg>\n                    <span>Ödevler</span>\n                </span>\n                <span class="material-badge available" title="Sınavlar">\n                    <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 20H5a2 2 0 01-2-2V6a2 2 0 012-2h10a2 2 0 012 2v1m2 13a2 2 0 01-2-2V7m2 13a2 2 0 002-2V9a2 2 0 00-2-2h-2m-4-3H9M7 16h6M7 8h6v4H7V8z"/></svg>\n                    <span>Sınavlar</span>\n                </span>\n            </div>\n        </div>\n    </a>\n</article>\n\n\n<article class="course-card group" data-institution="University of Notre Dame" data-language="TR">\n    <a class="block h-full" href="/ders/antik-roma-tarihi-university-of-notre-dame-prof-elizabeth-mazurek">\n        <!-- Gold gradient top border (visible on hover via CSS) -->\n        <div class="card-top-border" aria-hidden="true"></div>\n\n        <div class="p-5 flex flex-col h-full">\n            <!-- Header row: code + institution badge -->\n            <div class="flex items-start justify-between gap-2 mb-3">\n                    <span class="font-mono text-xs text-text-secondary bg-white/5 px-2 py-0.5 rounded border border-border-dark">\n                        UND-CLAS 30205\n                    </span>\n                    <span class="inst-pill text-xs font-medium px-2.5 py-0.5 rounded-full flex-shrink-0\n                        other-badge">\n                        University of Notre Dame\n                  ',
    'tuba_course.html': 'rses/algoritmalara-giris-mit-prof-charles-leiserson-ve-prof-erik-demaine/lectures/1/video/6046_Algoritma_d01.mp4" class="lecture-resource-link" target="_blank" rel="noopener">\n                                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>\n                            Video\n                        </a>\n                        <a href="/ADUploads/courses/algoritmalara-giris-mit-prof-charles-leiserson-ve-prof-erik-demaine/lectures/1/notes/Ders01notlari.pdf" class="lecture-resource-link" target="_blank" rel="noopener">\n                                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 21h10a2 2 0 002-2V9.414a1 1 0 00-.293-.707l-5.414-5.414A1 1 0 0012.586 3H7a2 2 0 00-2 2v14a2 2 0 002 2z"/></svg>\n                            Ders Notlar&#x131;\n                        </a>\n                </div>\n            </div>\n        </article>\n        <article class="lecture-block"\n                 id="ders-2"\n                 >\n                <h3 class="lecture-title">\n                    <span class="lecture-num">Ders 2</span>\n                    <span class="lecture-topic">Ders 2: Asimptotik Simgelem; Yinelemeler; Yerine koyma, Ana Metod</span>\n                </h3>\n\n            <div class="lecture-content">\n\n\n\n    <div class="video-wrapper">\n        <video controls preload="none" playsinline class="lecture-video">\n            <source src="/ADUploads/courses/algoritmalara-giris-mit-prof-charles-leiserson-ve-prof-erik-demaine/lectures/2/video/6046_Algoritma_d02.mp4" type="video/mp4; codecs=&quot;avc1.42E01E, mp4a.40.2&quot;" />\n            Tarayıcınız video oynatmayı desteklemiyor.\n       ',
    'bou_root.html': 'id="table-content"><thead class="t-header"><tr><th class="colname" aria-sort="ascending"><a class="name" href="?ND"  onclick="return false"">Name</a></th><th class="colname" data-sort-method="number"><a href="?MA"  onclick="return false"">Last Modified</a></th><th class="colname" data-sort-method="number"><a href="?SA"  onclick="return false"">Size</a></th></tr></thead>\n<tr data-sort-method="none"><td><a href="/Books/"><img class="icon" src="/_autoindex/assets/icons/corner-left-up.svg" alt="Up">Parent Directory</a></td><td></td><td></td></tr>\n<tr><td data-sort="*os"><a href="/Books/Text/OS/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">OS</a></td><td data-sort="122096608">2023-11-02 03:43</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*sard"><a href="/Books/Text/SARD/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">SARD</a></td><td data-sort="122104273">2023-11-02 05:51</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*sob"><a href="/Books/Text/SOB/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">SOB</a></td><td data-sort="122104334">2023-11-02 05:52</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*soe"><a href="/Books/Text/SOE/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">SOE</a></td><td data-sort="122104405">2023-11-02 05:53</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*sshl"><a href="/Books/Text/SSHL/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">SSHL</a></td><td data-sort="122104454">2023-11-02 05:54</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*sst"><a href="/Books/Text/SST/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">SST</a></td><td data-sort="122104493">2023-11-02 05:54</td><td data-sort="-1">-</td></tr>\n</table></div>\n<address>Proudly Served by LiteSpeed Web Server at ebookbou.edu.bd Port 443</address></div><script>\n\tnew Tablesort(document.getElementById("table-content"));\n\tvar keywordInput = document.getElementById(\'filter-keyword\');\n\tdocument.addEventListener(\'keyup\', filterTable);\n\n\tfunction filterTable(e',
    'bou_school.html': 'id="table-content"><thead class="t-header"><tr><th class="colname" aria-sort="ascending"><a class="name" href="?ND"  onclick="return false"">Name</a></th><th class="colname" data-sort-method="number"><a href="?MA"  onclick="return false"">Last Modified</a></th><th class="colname" data-sort-method="number"><a href="?SA"  onclick="return false"">Size</a></th></tr></thead>\n<tr data-sort-method="none"><td><a href="/Books/Text/"><img class="icon" src="/_autoindex/assets/icons/corner-left-up.svg" alt="Up">Parent Directory</a></td><td></td><td></td></tr>\n<tr><td data-sort="*bba"><a href="/Books/Text/SOB/BBA/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">BBA</a></td><td data-sort="208958884">2026-08-03 12:08</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*cemba-cempa"><a href="/Books/Text/SOB/CEMBA-CEMPA/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">CEMBA-CEMPA</a></td><td data-sort="122104368">2023-11-02 05:52</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*mba"><a href="/Books/Text/SOB/MBA/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">MBA</a></td><td data-sort="208614779">2026-07-30 12:32</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*pgdm"><a href="/Books/Text/SOB/pgdm/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">pgdm</a></td><td data-sort="122104393">2023-11-02 05:53</td><td data-sort="-1">-</td></tr>\n</table></div>\n<address>Proudly Served by LiteSpeed Web Server at ebookbou.edu.bd Port 443</address></div><script>new Tablesort(document.getElementById("table-content"));</script></body></html>\n',
    'bou_prog.html': 'id="table-content"><thead class="t-header"><tr><th class="colname" aria-sort="ascending"><a class="name" href="?ND"  onclick="return false"">Name</a></th><th class="colname" data-sort-method="number"><a href="?MA"  onclick="return false"">Last Modified</a></th><th class="colname" data-sort-method="number"><a href="?SA"  onclick="return false"">Size</a></th></tr></thead>\n<tr data-sort-method="none"><td><a href="/Books/Text/SOB/"><img class="icon" src="/_autoindex/assets/icons/corner-left-up.svg" alt="Up">Parent Directory</a></td><td></td><td></td></tr>\n<tr><td data-sort="*cemba_1602"><a href="/Books/Text/SOB/CEMBA-CEMPA/cemba_1602/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">cemba_1602</a></td><td data-sort="122113415">2023-11-02 08:23</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*cemba_1603"><a href="/Books/Text/SOB/CEMBA-CEMPA/cemba_1603/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">cemba_1603</a></td><td data-sort="122113423">2023-11-02 08:23</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*cemba_1604"><a href="/Books/Text/SOB/CEMBA-CEMPA/cemba_1604/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">cemba_1604</a></td><td data-sort="122113430">2023-11-02 08:23</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*cemba_2606"><a href="/Books/Text/SOB/CEMBA-CEMPA/cemba_2606/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">cemba_2606</a></td><td data-sort="122113446">2023-11-02 08:24</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*cemba_3619"><a href="/Books/Text/SOB/CEMBA-CEMPA/cemba_3619/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">cemba_3619</a></td><td data-sort="122113463">2023-11-02 08:24</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*core_1601"><a href="/Books/Text/SOB/CEMBA-CEMPA/core_1601/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">core_1601</a></td><td data-sort="122113453">2023-11-02 08:24</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*core_1602"><a href="/Books/Text/SOB/CEMBA-CEMPA/core_1602/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">core_1602</a></td><td data-sort="122113471">2023-11-02 08:24</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*core_1604"><a href="/Books/Text/SOB/CEMBA-CEMPA/core_1604/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">core_1604</a></td><td data-sort="122113506">2023-11-02 08:25</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*core_2605"><a href="/Books/Text/SOB/CEMBA-CEMPA/core_2605/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">core_2605</a></td><td data-sort="122113517">2023-11-02 08:25</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*core_2606"><a href="/Books/Text/SOB/CEMBA-CEMPA/core_2606/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">core_2606</a></td><td data-sort="122113527">2023-11-02 08:25</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*core_2607"><a href="/Books/Text/SOB/CEMBA-CEMPA/core_2607/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">core_2607</a></td><td data-sort="122113534">2023-11-02 08:25</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*core_2608"><a href="/Books/Text/SOB/CEMBA-CEMPA/core_2608/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">core_2608</a></td><td data-sort="122113543">2023-11-02 08:25</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*core_2609"><a href="/Books/Text/SOB/CEMBA-CEMPA/core_2609/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">core_2609</a></td><td data-sort="122113551">2023-11-02 08:25</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*elec_3614"><a href="/Books/Text/SOB/CEMBA-CEMPA/elec_3614/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">elec_3614</a></td><td data-sort="122113560">2023-11-02 08:26</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*elec_3615"><a href="/Books/Text/SOB/CEMBA-CEMPA/elec_3615/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">elec_3615</a></td><td data-sort="122113571">2023-11-02 08:26</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*elec_3619"><a href="/Books/Text/SOB/CEMBA-CEMPA/elec_3619/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">elec_3619</a></td><td data-sort="122113581">2023-11-02 08:26</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*elec_3620"><a href="/Books/Text/SOB/CEMBA-CEMPA/elec_3620/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">elec_3620</a></td><td data-sort="122113589">2023-11-02 08:26</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*elec_4616"><a href="/Books/Text/SOB/CEMBA-CEMPA/elec_4616/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">elec_4616</a></td><td data-sort="122113598">2023-11-02 08:26</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*elec_4617"><a href="/Books/Text/SOB/CEMBA-CEMPA/elec_4617/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">elec_4617</a></td><td data-sort="122113609">2023-11-02 08:26</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*elec_4618"><a href="/Books/Text/SOB/CEMBA-CEMPA/elec_4618/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">elec_4618</a></td><td data-sort="122113619">2023-11-02 08:26</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*elec_4621"><a href="/Books/Text/SOB/CEMBA-CEMPA/elec_4621/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">elec_4621</a></td><td data-sort="122113629">2023-11-02 08:27</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*scom_3610"><a href="/Books/Text/SOB/CEMBA-CEMPA/scom_3610/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">scom_3610</a></td><td data-sort="122113639">2023-11-02 08:27</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*scom_3612"><a href="/Books/Text/SOB/CEMBA-CEMPA/scom_3612/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">scom_3612</a></td><td data-sort="122113650">2023-11-02 08:27</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="*scom_4613"><a href="/Books/Text/SOB/CEMBA-CEMPA/scom_4613/"><img class="icon" src="/_autoindex/assets/icons/folder-fill.svg" alt="Directory">scom_4613</a></td><td data-sort="122113660">2023-11-02 08:27</td><td data-sort="-1">-</td></tr>\n<tr><td data-sort="economic_environment_of_business.pdf"><a href="/Books/Text/SOB/CEMBA-CEMPA/Economic_Environment_of_Business.pdf"><img class="icon" src="/_autoindex/assets/icons/file.svg" alt="File">Economic_Environment_of_Business.pdf</a></td><td data-sort="1372693786">2013-07-01 15:49</td><td data-sort="2296946">   2244k</td></tr>\n<tr><td data-sort="human_resource_management.pdf"><a href="/Books/Text/SOB/CEMBA-CEMPA/Human_Resource_Management.pdf"><img class="icon" src="/_autoindex/assets/icons/file.svg" alt="File">Human_Resource_Management.pdf</a></td><td data-sort="1372693346">2013-07-01 15:42</td><td data-sort="3070198">   2999k</td></tr>\n<tr><td data-sort="management_and_organisations.pdf"><a href="/Books/Text/SOB/CEMBA-CEMPA/Management_and_Organisations.pdf"><img class="icon" src="/_autoindex/assets/icons/file.svg" alt="File">Management_and_Organisations.pdf</a></td><td data-sort="1372604312">2013-06-30 14:58</td><td data-sort="6590592">   6437k</td></tr>\n<tr><td data-sort="public_policy.pdf"><a href="/Books/Text/SOB/CEMBA-CEMPA/Public_Policy.pdf"><img class="icon" src="/_autoindex/assets/icons/file.svg" alt="File">Public_Policy.pdf</a></td><td data-sort="1372695320">2013-07-01 16:15</td><td data-sort="2349604">   2295k</td></tr>\n<tr><td data-sort="quality_management.pdf"><a href="/Books/Text/SOB/CEMBA-CEMPA/Quality_Management.pdf"><img class="icon" src="/_autoindex/assets/icons/file.svg" alt="File">Quality_Management.pdf</a></td><td data-sort="1372695908">2013-07-01 16:25</td><td data-sort="1272052">   1243k</td></tr>\n<tr><td data-sort="strategic_management.pdf"><a href="/Books/Text/SOB/CEMBA-CEMPA/Strategic_Management.pdf"><img class="icon" src="/_autoindex/assets/icons/file.svg" alt="File">Strategic_Management.pdf</a></td><td data-sort="1372694764">2013-07-01 16:06</td><td data-sort="1377432">   1346k</td></tr>\n',
    'bou_course.html': 'id="table-content"><thead class="t-header"><tr><th class="colname" aria-sort="ascending"><a class="name" href="?ND"  onclick="return false"">Name</a></th><th class="colname" data-sort-method="number"><a href="?MA"  onclick="return false"">Last Modified</a></th><th class="colname" data-sort-method="number"><a href="?SA"  onclick="return false"">Size</a></th></tr></thead>\n<tr data-sort-method="none"><td><a href="/Books/Text/SOB/CEMBA-CEMPA/"><img class="icon" src="/_autoindex/assets/icons/corner-left-up.svg" alt="Up">Parent Directory</a></td><td></td><td></td></tr>\n<tr><td data-sort="index.pdf"><a href="/Books/Text/SOB/CEMBA-CEMPA/core_1601/Index.pdf"><img class="icon" src="/_autoindex/assets/icons/file.svg" alt="File">Index.pdf</a></td><td data-sort="1488427618">2017-03-02 04:06</td><td data-sort="118294">    116k</td></tr>\n<tr><td data-sort="module-1.pdf"><a href="/Books/Text/SOB/CEMBA-CEMPA/core_1601/Module-1.pdf"><img class="icon" src="/_autoindex/assets/icons/file.svg" alt="File">Module-1.pdf</a></td><td data-sort="1488427640">2017-03-02 04:07</td><td data-sort="108385">    106k</td></tr>\n<tr><td data-sort="module-10.pdf"><a href="/Books/Text/SOB/CEMBA-CEMPA/core_1601/Module-10.pdf"><img class="icon" src="/_autoindex/assets/icons/file.svg" alt="File">Module-10.pdf</a></td><td data-sort="1488427884">2017-03-02 04:11</td><td data-sort="160556">    157k</td></tr>\n<tr><td data-sort="module-11.pdf"><a href="/Books/Text/SOB/CEMBA-CEMPA/core_1601/Module-11.pdf"><img class="icon" src="/_autoindex/assets/icons/file.svg" alt="File">Module-11.pdf</a></td><td data-sort="1488427904">2017-03-02 04:11</td><td data-sort="110020">    108k</td></tr>\n<tr><td data-sort="module-12.pdf"><a href="/Books/Text/SOB/CEMBA-CEMPA/core_1601/Module-12.pdf"><img class="icon" src="/_autoindex/assets/icons/file.svg" alt="File">Module-12.pdf</a></td><td data-sort="1488427928">2017-03-02 04:12</td><td data-sort="103549">    102k</td></tr>\n<tr><td data-sort="module-2.pdf"><a href="/Books/Text/SOB/CEMBA-CEMPA/core_1601/Module-2.pdf"><img class="icon" src="/_autoindex/assets/icons/file.svg" alt="File">Module-2.pdf</a></td><td data-sort="1488427656">2017-03-02 04:07</td><td data-sort="141038">    1',
}

# ---------------------------------------------------------------- registry
reg_path = os.path.join(HERE, "lectures_registry.json")
check("lectures_registry.json exists", os.path.exists(reg_path))
reg = json.load(open(reg_path, encoding="utf-8"))
check("registry has 25 entries", len(reg) == 25)
REQUIRED = {"name", "url", "type", "fields", "languages", "api",
            "notes", "verified", "verified_date"}
check("registry schema keys", all(set(e) == REQUIRED for e in reg))
check("all entries verified 2026-11-26",
      all(e["verified"] is True and e["verified_date"] == "2026-11-26"
          for e in reg))
by_url = {e["url"]: e for e in reg}
WIRED_URLS = {"https://ocw.mit.edu", "https://oyc.yale.edu",
              "https://see.stanford.edu", "https://cs50.harvard.edu",
              "https://ocw.tudelft.nl", "https://ocw.metu.edu.tr",
              "https://en.wikiversity.org", "https://openstax.org",
              "https://ocw.nycu.edu.tw", "https://ocw.hokudai.ac.jp",
              "https://acikders.tuba.gov.tr", "https://ebookbou.edu.bd"}
check("exactly the 12 probed-live platforms carry an api route",
      {e["url"] for e in reg if e["api"]} == WIRED_URLS)
check("the other 13 are registry-only (empty api, honest notes)",
      all((not e["api"]) and len(e["notes"]) > 80
          for e in reg if e["url"] not in WIRED_URLS))
check("OpenStax is flagged type textbook",
      by_url["https://openstax.org"]["type"] == "textbook")
check("all other entries are lecture_platform",
      all(e["type"] == "lecture_platform"
          for e in reg if e["url"] != "https://openstax.org"))
check("Wikiversity is flagged low-credibility community",
      "LOW-CREDIBILITY" in
      by_url["https://en.wikiversity.org"]["notes"])
check("Saylor is registry-only with the probe contradiction recorded",
      by_url["https://learn.saylor.org"]["api"] == "" and
      "could not be" in by_url["https://learn.saylor.org"]["notes"])

# ------------------------------------------------------------- registry load
# §23+§24+§25+§26+§27+§32+§34+§36+§37: thirteen registry files; totals updated count-only.
EXPECTED_FILES_OK = 14
EXPECTED_LOADED = 2160
EXPECTED_UNIQUE = 2108
for mod, label in ((ba, "build_article"), (pt, "propose_topics")):
    di, ui, st = mod.load_source_registry()
    check(f"{label}: all registry files load (files_ok)",
          st["files_ok"] == EXPECTED_FILES_OK)
    check(f"{label}: loaded entry count", st["loaded"] == EXPECTED_LOADED)
    check(f"{label}: unique URL count",
          st["unique_urls"] == EXPECTED_UNIQUE)
    check(f"{label}: lecture platforms indexed by domain",
          all(d in di for d in ("ocw.mit.edu", "oyc.yale.edu",
                                "see.stanford.edu", "cs50.harvard.edu",
                                "ocw.tudelft.nl", "ocw.metu.edu.tr",
                                "en.wikiversity.org", "openstax.org",
                                "ocw.nycu.edu.tw", "ocw.hokudai.ac.jp",
                                "acikders.tuba.gov.tr",
                                "ebookbou.edu.bd")))

# ------------------------------------------------------------------ wiring
check("searcher is in SEARCHERS and WAVE1_SEARCHERS",
      ba.search_university_lectures in ba.SEARCHERS
      and ba.search_university_lectures in ba.WAVE1_SEARCHERS)
check("searcher has a label",
      ba.SEARCHER_LABEL.get("search_university_lectures")
      == "University lectures")
check("12 wired sub-searchers",
      [n for n, _ in ba._LECTURE_SEARCHERS] ==
      ["MIT OCW", "Open Yale Courses", "Stanford SEE", "Harvard CS50",
       "TU Delft OCW", "METU OCW", "Wikiversity", "OpenStax",
       "NYCU OCW", "Hokkaido OCW", "TÜBA Açık Ders",
       "Bangladesh Open University"])
check("caps are 8 per platform / 24 total",
      ba.LEC_PER_PLATFORM_CAP == 8 and ba.LEC_TOTAL_CAP == 24)

# ----------------------------------------------------------------- parsers
check("MIT sitemap index parses to course sitemaps",
      ("8-282j-introduction-to-astronomy-spring-2006",
       "https://ocw.mit.edu/courses/8-282j-introduction-to-astronomy"
       "-spring-2006/sitemap.xml")
      in ba._parse_mit_sitemap_courses(FIX["mit_index.xml"]))
yc = ba._parse_yale_courses(FIX["yale_courses.html"])
check("Yale catalogue parses (title cell wins over number cell)",
      ("https://oyc.yale.edu/astronomy/astr-160",
       "Frontiers and Controversies in Astrophysics") in yc)
ys = ba._parse_yale_sessions(FIX["yale_course.html"])
check("Yale sessions parse with titles",
      ys and ys[0] == ("https://oyc.yale.edu/astronomy/astr-160/"
                       "lecture-1", "Introduction"))
check("SEE course codes parse",
      ("CS229", "CS229") in ba._parse_see_courses(FIX["see_list.html"]))
check("SEE transcript links parse",
      ("/materials/aimlcs229/transcripts/MachineLearning-Lecture01"
       ".html", "HTML")
      in ba._parse_see_transcripts(FIX["see_course.html"]))
topics = ba._parse_cs50_index(FIX["cs50_index.html"])
check("CS50 index parses week topics",
      topics.get(0) == "Scratch" and topics.get(3) == "Algorithms"
      and topics.get(6) == "Python")
td = ba._parse_tudelft_feed(FIX["tudelft_feed.xml"])
check("TU Delft feed parses course items only (teachers filtered)",
      td and all("/teachers/" not in u for _t, u in td)
      and any("course-" in u for _t, u in td))
check("METU Moodle search parses courses",
      ba._parse_moodle_courses(FIX["metu_search.html"],
                               "ocw.metu.edu.tr")
      == [("https://ocw.metu.edu.tr/course/view.php?id=83",
           "Elements of Probability and Statistics")])
wv = ba._parse_wikiversity_search(json.loads(FIX["wikiversity.json"]))
check("Wikiversity API parses (page is the text)",
      [c["title"] for c in wv][:2] == ["Astronomy",
                                       "Introduction to astronomy"]
      and all(c["index"] == "lecture:wikiversity" for c in wv))
check("NYCU search parses course pages",
      ba._parse_nycu_courses(FIX["nycu_search.html"])
      == [("https://ocw.nycu.edu.tw/?course_page=all-course/"
           "college-of-science/am/calculus-a-ii-academic-109",
           "calculus a ii academic 109")])
check("Hokkaido search parses lecture pages",
      ("https://ocw.hokudai.ac.jp/lecture/"
       "internationalfaculty10omics-2007",
       "internationalfaculty10omics 2007")
      in ba._parse_hokudai_lectures(FIX["hokudai_search.html"]))
tc = ba._parse_tuba_courses(FIX["tuba_home.html"])
check("TÜBA catalogue parses (title from the card h3)",
      tc and tc[0][0].endswith(
          "/ders/algoritmalara-giris-mit-prof-charles-leiserson-ve-"
          "prof-erik-demaine") and "Algoritmalara" in tc[0][1])
nodes = []
ba._openstax_walk(json.loads(FIX["openstax_tree.json"])["tree"], nodes)
check("OpenStax tree walk collects pages with clean titles",
      ("7f11b6c9-3774-45f2-89e7-65c0196f6c29",
       "1.1 The Nature of Astronomy", "1-1-the-nature-of-astronomy")
      in nodes)

# ------------------------------------------------------- stubbed fetch flow
REAL_FETCH = ba._wlib_fetch


def stub(mapping):
    def _stub(url, data=None, timeout=20):
        for key, val in mapping.items():
            if key in url:
                return val
        return (404, "")
    return _stub


ba._wlib_fetch = stub({
    "resources/lecture-1": (200, FIX["mit_resource.html"]),
    "resources/lecture-2": (200, FIX["mit_resource.html"]),
    "5-111-principles": (200, FIX["mit_course_sitemap.xml"]),
    "ocw.mit.edu/sitemap.xml": (200, FIX["mit_index.xml"])})
mit = ba._lec_mit_ocw("chemical science")
check("MIT flow: transcript PDF candidates from resource pages",
      mit and all(c["index"] == "lecture:mitocw" for c in mit)
      and all(c["url"].endswith(".pdf") for c in mit))

ba._wlib_fetch = stub({
    "oyc.yale.edu/astronomy/astr-160": (200, FIX["yale_course.html"]),
    "oyc.yale.edu/courses": (200, FIX["yale_courses.html"])})
yale = ba._lec_yale("frontiers controversies astrophysics")
check("Yale flow: session transcript candidates",
      len(yale) == 3 and yale[0]["index"] == "lecture:yale"
      and "Lecture 1: Introduction" in yale[0]["title"])

ba._SEE_CACHE = None
ba._wlib_fetch = stub({
    "see.stanford.edu/Course/CS229": (
        200, "<title>Stanford Engineering Everywhere | CS229 - "
        "Machine Learning</title>" + FIX["see_course.html"]),
    "see.stanford.edu/Course/CS223A": (
        200, "<title>Stanford Engineering Everywhere | CS223A - "
        "Introduction to Robotics</title>"),
    "see.stanford.edu/Course": (200, FIX["see_list.html"])})
see = ba._lec_stanford_see("machine learning")
check("SEE flow: transcript candidates under the real course title",
      see and see[0]["title"].startswith("Machine Learning")
      and see[0]["index"] == "lecture:stanford-see")

ba._CS50_TOPICS_CACHE = None
ba._wlib_fetch = stub({"cs50.harvard.edu": (200, FIX["cs50_index.html"])})
cs50 = ba._lec_cs50("algorithms python")
check("CS50 flow: notes candidates for matched weeks",
      [c["title"] for c in cs50] == ["CS50 Lecture 3: Algorithms",
                                     "CS50 Lecture 6: Python"])

ba._wlib_fetch = stub({"ocw.tudelft.nl": (200, FIX["tudelft_feed.xml"])})
tdc = ba._lec_tudelft("water")
check("TU Delft flow: lecture candidates from the search feed",
      tdc and all(c["index"] == "lecture:tudelft" for c in tdc))

ba._wlib_fetch = stub({
    "mod/resource/view.php?id=1273": (200, FIX["metu_resource.html"]),
    "course/view.php?id=83": (200, FIX["metu_course.html"]),
    "course/search.php": (200, FIX["metu_search.html"])})
metu = ba._lec_metu("mathematics")
check("METU flow: resource resolved to its pluginfile PDF",
      len(metu) == 1 and metu[0]["url"].endswith("outline.pdf")
      and metu[0]["index"] == "lecture:metu")

ba._wlib_fetch = stub({"en.wikiversity.org":
                       (200, FIX["wikiversity.json"])})
wik = ba._lec_wikiversity("astronomy")
check("Wikiversity flow: API candidates",
      len(wik) == 3 and wik[0]["url"].startswith(
          "https://en.wikiversity.org/wiki/"))

ba._OPENSTAX_CACHE = None
ba._wlib_fetch = stub({
    "pages/preface": (200, FIX["openstax_anchor.html"])})
REAL_CAPPED = ba._fetch_capped_full


def _capped_stub(url, cap=0, timeout=30):
    if "4c29f9e5-a53d-42c0-bdb5-091990527d79@" in url:
        return (FIX["openstax_tree.json"].encode("utf-8"),
                "application/json")
    return (None, "")


ba._fetch_capped_full = _capped_stub
osx = ba._lec_openstax("nature of astronomy")
ba._fetch_capped_full = REAL_CAPPED
check("OpenStax flow: page-JSON candidates under the book title",
      osx and all(c["index"] == "lecture:openstax" for c in osx)
      and any("The Nature of Astronomy" in c["title"] for c in osx)
      and all(c["url"].endswith(".json") for c in osx))

ba._wlib_fetch = stub({
    "course_page=all-course/college-of-science":
        (200, FIX["nycu_course.html"]),
    "ocw.nycu.edu.tw/?s=": (200, FIX["nycu_search.html"])})
nycu = ba._lec_nycu("calculus")
check("NYCU flow: handout PDF candidates from the course page",
      nycu and all(c["url"].startswith(
          "https://video.ocw.nycu.edu.tw/") for c in nycu))

ba._wlib_fetch = stub({
    "lecture/internationalfaculty10omics-2007":
        (200, FIX["hokudai_lecture.html"]),
    "ocw.hokudai.ac.jp/?s=": (200, FIX["hokudai_search.html"])})
hok = ba._lec_hokudai("international economics")
check("Hokkaido flow: note PDF candidates (poster excluded)",
      hok and all("Note-" in c["url"] for c in hok)
      and all(c["index"] == "lecture:hokudai" for c in hok))

ba._wlib_fetch = stub({
    "ders/algoritmalara-giris": (200, FIX["tuba_course.html"]),
    "acikders.tuba.gov.tr/": (200, FIX["tuba_home.html"])})
tuba = ba._lec_tuba("algoritmalara giriş")
check("TÜBA flow: notes PDF candidates from the course page",
      tuba and tuba[0]["url"].endswith("Ders01notlari.pdf")
      and tuba[0]["index"] == "lecture:tuba")

ba._BOU_CACHE = None
_BOU_PAGES = {
    "Books/Text/SOB/CEMBA-CEMPA/core_1601/": FIX["bou_course.html"],
    "Books/Text/SOB/CEMBA-CEMPA/": FIX["bou_prog.html"],
    "Books/Text/SOB/": FIX["bou_school.html"],
    "ebookbou.edu.bd/Books/Text/": FIX["bou_root.html"]}


def _bou_stub(url, cap=0, timeout=30):
    for key, val in _BOU_PAGES.items():
        if key in url:
            return (val.encode("utf-8"), "text/html")
    return (None, "")


ba._fetch_capped_full = _bou_stub
bou = ba._lec_bou("human resource management")
ba._fetch_capped_full = REAL_CAPPED
check("BOU flow: course-book PDF candidates from the catalogue",
      bou and bou[0]["title"] == "Human Resource Management"
      and all(c["index"] == "lecture:bou" for c in bou))

# ------------------------------------------------------- pool-level behaviour
ba._OPENSTAX_CACHE = None
ba._CS50_TOPICS_CACHE = None
ba._SEE_CACHE = None
ba._BOU_CACHE = None
ba._YALE_CACHE = None
ba._TUBA_CACHE = None
ba._wlib_fetch = stub({})
ba._fetch_capped_full = lambda url, cap=0, timeout=30: (None, "")
check("all platforms down -> empty pool, no exception",
      ba.search_university_lectures("astronomy") == [])
ba._fetch_capped_full = REAL_CAPPED

saved = ba._LECTURE_SEARCHERS


def _boom(query):
    raise RuntimeError("boom")


def _many(query):
    return [ba._arab_candidate(f"T{i}", f"https://x.example/{i}",
                               "lecture:fake") for i in range(30)]


ba._LECTURE_SEARCHERS = [("many", _many)]
check("per-platform cap 8 enforced",
      len(ba.search_university_lectures("anything")) == 8)
ba._LECTURE_SEARCHERS = [("boom", _boom), ("many", _many),
                         ("many2", _many), ("many3", _many),
                         ("many4", _many)]
pool = ba.search_university_lectures("anything")
check("a raising platform fails soft; total cap 24 enforced",
      len(pool) == 24)
ba._LECTURE_SEARCHERS = saved
ba._wlib_fetch = REAL_FETCH

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURES:")
    for f in FAILURES:
        print(" -", f)
    sys.exit(1)
print("ALL §32 TESTS PASSED")
