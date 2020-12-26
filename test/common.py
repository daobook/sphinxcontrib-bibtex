"""Some common helper functions for the test suite."""

import re

RE_ID = r'[a-z][-?a-z0-9]*'
RE_NAME = r'[-?a-z0-9]+'  # what follows 'bibtex-citation-' ids
RE_NUM = r'\d+'
RE_LABEL = r'[^<]+'
RE_TEXT = r'.*'


def html_citation_refs(name=RE_NAME, label=RE_LABEL):
    return re.compile(
        '<a class="reference internal" href="#bibtex-citation-{name}">'
        '<span>{label}</span>'
        '</a>'.format(name=name, label=label))


def html_citations(name=RE_NAME, label=RE_LABEL, text=RE_TEXT):
    return re.compile(
        r'<dt class="label" id="bibtex-citation-(?P<name>{name})">'
        r'<span class="brackets">'
        r'(?:<a class="fn-backref" href="#(?P<backref>{id_})">)?'
        r'(?P<label>{label})'
        r'(?:</a>)?'
        r'</span>'
        r'(?:<span class="fn-backref">\('
        r'<a href="#(?P<backref1>{id_})">1</a>'
        r',<a href="#(?P<backref2>{id_}\w+)">2</a>'
        r'(,<a href="#(?P<backref3>{id_}\w+)">3</a>)?'
        r'(,<a href="#\w+">\d+</a>)*'  # no named group for additional backrefs
        r'\)</span>)?'
        r'</dt>\n'
        r'<dd><p>(?P<text>{text})</p>\n</dd>'.format(
            name=name, label=label, text=text, id_=RE_ID))


def html_footnote_refs(refid=RE_ID):
    return re.compile(
        '<a class="footnote-reference brackets"'
        ' href="#(?P<refid>{refid})" id="(?P<id_>{id_})">'
        '(?P<label>{label})'
        '</a>'.format(refid=refid, id_=RE_ID, label=RE_NUM))


def html_footnotes(id_=RE_ID):
    return re.compile(
        '<dt class="label" id="(?P<id_>{id_})">'
        '<span class="brackets">'
        '<a class="fn-backref" href="#(?P<backref>{backref_id})">'
        '(?P<label>{label})'
        '</a>'
        '</span>'
        '</dt>'.format(id_=id_, backref_id=RE_ID, label=RE_NUM))
