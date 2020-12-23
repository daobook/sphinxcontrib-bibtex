# -*- coding: utf-8 -*-
"""
    Classes and methods to maintain any bibtex information that is stored
    outside the doctree.

    .. autoclass:: BibtexDomain
        :members:

    .. autoclass:: BibliographyCache
        :members:
"""

import ast
import collections
import copy
from typing import List, Dict, NamedTuple, Set, cast

import docutils.nodes
import sphinx.util
import re

from pybtex.plugin import find_plugin
import pybtex.style.formatting
from sphinx.addnodes import pending_xref
from sphinx.builders import Builder
from sphinx.domains import Domain
from sphinx.environment import BuildEnvironment
from sphinx.errors import ExtensionError

from .bibfile import BibfileCache, normpath_filename, process_bibfile

logger = sphinx.util.logging.getLogger(__name__)


def _raise_invalid_node(node):
    """Helper method to raise an exception when an invalid node is
    visited.
    """
    raise ValueError("invalid node %s in filter expression" % node)


class _FilterVisitor(ast.NodeVisitor):

    """Visit the abstract syntax tree of a parsed filter expression."""

    entry = None
    """The bibliographic entry to which the filter must be applied."""

    cited_docnames = False
    """The documents where the entry is cited (empty if not cited)."""

    def __init__(self, entry, docname, cited_docnames):
        self.entry = entry
        self.docname = docname
        self.cited_docnames = cited_docnames

    def visit_Module(self, node):
        if len(node.body) != 1:
            raise ValueError(
                "filter expression cannot contain multiple expressions")
        return self.visit(node.body[0])

    def visit_Expr(self, node):
        return self.visit(node.value)

    def visit_BoolOp(self, node):
        outcomes = (self.visit(value) for value in node.values)
        if isinstance(node.op, ast.And):
            return all(outcomes)
        elif isinstance(node.op, ast.Or):
            return any(outcomes)
        else:  # pragma: no cover
            # there are no other boolean operators
            # so this code should never execute
            assert False, "unexpected boolean operator %s" % node.op

    def visit_UnaryOp(self, node):
        if isinstance(node.op, ast.Not):
            return not self.visit(node.operand)
        else:
            _raise_invalid_node(node)

    def visit_BinOp(self, node):
        left = self.visit(node.left)
        op = node.op
        right = self.visit(node.right)
        if isinstance(op, ast.Mod):
            # modulo operator is used for regular expression matching
            if not isinstance(left, str):
                raise ValueError(
                    "expected a string on left side of %s" % node.op)
            if not isinstance(right, str):
                raise ValueError(
                    "expected a string on right side of %s" % node.op)
            return re.search(right, left, re.IGNORECASE)
        elif isinstance(op, ast.BitOr):
            return left | right
        elif isinstance(op, ast.BitAnd):
            return left & right
        else:
            _raise_invalid_node(node)

    def visit_Compare(self, node):
        # keep it simple: binary comparators only
        if len(node.ops) != 1:
            raise ValueError("syntax for multiple comparators not supported")
        left = self.visit(node.left)
        op = node.ops[0]
        right = self.visit(node.comparators[0])
        if isinstance(op, ast.Eq):
            return left == right
        elif isinstance(op, ast.NotEq):
            return left != right
        elif isinstance(op, ast.Lt):
            return left < right
        elif isinstance(op, ast.LtE):
            return left <= right
        elif isinstance(op, ast.Gt):
            return left > right
        elif isinstance(op, ast.GtE):
            return left >= right
        elif isinstance(op, ast.In):
            return left in right
        elif isinstance(op, ast.NotIn):
            return left not in right
        else:
            # not used currently: ast.Is | ast.IsNot
            _raise_invalid_node(op)

    def visit_Name(self, node):
        """Calculate the value of the given identifier."""
        id_ = node.id
        if id_ == 'type':
            return self.entry.type.lower()
        elif id_ == 'key':
            return self.entry.key.lower()
        elif id_ == 'cited':
            return bool(self.cited_docnames)
        elif id_ == 'docname':
            return self.docname
        elif id_ == 'docnames':
            return self.cited_docnames
        elif id_ == 'author' or id_ == 'editor':
            if id_ in self.entry.persons:
                return u' and '.join(
                    str(person)  # XXX needs fix in pybtex?
                    for person in self.entry.persons[id_])
            else:
                return u''
        else:
            return self.entry.fields.get(id_, "")

    def visit_Set(self, node):
        return frozenset(self.visit(elt) for elt in node.elts)

    # NameConstant is Python 3.4 only
    def visit_NameConstant(self, node):
        return node.value  # pragma: no cover

    # Constant is Python 3.6+ only
    # Since 3.8 Num, Str, Bytes, NameConstant and Ellipsis are just Constant
    def visit_Constant(self, node):
        return node.value

    # Not used on 3.8+
    def visit_Str(self, node):
        return node.s  # pragma: no cover

    def generic_visit(self, node):
        _raise_invalid_node(node)


def get_docnames(env):
    """Ged document names in order."""
    rel = env.collect_relations()
    docname = env.config.master_doc
    while docname is not None:
        yield docname
        parent, prevdoc, nextdoc = rel[docname]
        docname = nextdoc


class BibliographyCache(NamedTuple):
    """Contains information about a bibliography directive."""
    docname: str         #: Document name.
    line: int            #: Line number of the directive in the document.
    bibfiles: List[str]  #: List of bib files for this directive.
    style: str           #: The pybtex style.
    list_: str           #: The list type.
    enumtype: str        #: The sequence type (for enumerated lists).
    start: int           #: The start of the sequence (for enumerated lists).
    labelprefix: str     #: String prefix for pybtex generated labels.
    keyprefix: str       #: String prefix for citation keys.
    filter_: ast.AST     #: Parsed filter expression.


class Citation(NamedTuple):
    """Information about a citation."""
    bibliography_id: str  #: Unique id of its bibliography directive.
    key: str              #: Unique citation id used for referencing.
    label: str            #: Label (including brackets and label prefix).
    entry_key: str        #: The original entry key (no prefix).
    entry_label: str      #: The original entry label (no brackets or prefix).


class CitationRef(NamedTuple):
    """Information about a citation reference."""
    docname: str  #: Document name where the citation is referenced.
    line: int     #: Line number.
    key: str      #: Citation key (including key prefix).


class BibtexDomain(Domain):

    """Global bibtex extension information cache."""

    name = 'cite'
    label = 'BibTeX Citations'
    data_version = 1

    @property
    def bibfiles(self) -> Dict[str, BibfileCache]:
        """Map each bib filename to some information about the file (including
        the parsed data).
        """
        return self.data.setdefault('bibfiles', {})  # filename -> cache

    @property
    def bibliographies(self) -> Dict[str, BibliographyCache]:
        """Map each bibliography directive id to further information about the
        directive.
        """
        return self.data.setdefault('bibliographies', {})  # id -> cache

    @property
    def citations(self) -> Dict[str, Citation]:
        """Map each citation id to citation data."""
        return self.data.setdefault('citations', {})

    @property
    def citation_refs(self) -> Dict[str, CitationRef]:
        """Map each citation reference id to citation reference data."""
        return self.data.setdefault('citation_refs', {})

    # TODO switch to temp_data and remove from domain
    @property
    def enum_count(self) -> Dict[str, int]:
        """Keeps track of the current bibliography enum count in each
        document.
        """
        return self.data.setdefault('enum_count', {})  # doc -> enum count

    def __init__(self, env: BuildEnvironment):
        super().__init__(env)
        # check config
        if env.app.config.bibtex_bibfiles is None:
            raise ExtensionError(
                "You must configure the bibtex_bibfiles setting")
        # update bib file information in the cache
        for bibfile in env.app.config.bibtex_bibfiles:
            process_bibfile(
                self.bibfiles,
                normpath_filename(env, "/" + bibfile),
                env.app.config.bibtex_encoding)

    def clear_doc(self, docname: str) -> None:
        for id_, bibcache in list(self.bibliographies.items()):
            if bibcache.docname == docname:
                del self.bibliographies[id_]
        for id_, citation in list(self.citations.items()):
            if citation.docname == docname:
                del self.citations[id_]
        for id_, citation_ref in list(self.citation_refs.items()):
            if citation_ref.docname == docname:
                del self.citation_refs[id_]
        self.enum_count.pop(docname, None)

    def merge_domaindata(self, docnames: List[str], otherdata: Dict) -> None:
        for id_, bibcache in otherdata['bibliographies'].items():
            if bibcache.docname in docnames:
                self.bibliographies[id_] = bibcache
        for docname in docnames:
            if docname in otherdata['enum_count']:
                self.enum_count[docname] = otherdata['enum_count'][docname]
        for id_, citation in otherdata['citations'].items():
            if citation.docname in docnames:
                self.citations[id_] = citation
        for id_, citation_ref in otherdata['citation_refs'].items():
            if citation_ref.docname in docnames:
                self.citation_refs[id_] = citation_ref

    def check_consistency(self) -> None:
        # This function is called when all doctrees are parsed,
        # but before any post transforms are applied. We use it to
        # determine which citations will be added to which bibliography
        # directive, and also to format the labels. We need to format
        # the labels and construct the citation ids here because they must be
        # known when resolve_xref is called.
        docnames = list(get_docnames(self.env))
        for id_, bibcache in self.bibliographies.items():
            entries = self.get_bibliography_entries(id_=id_, docnames=docnames)
            # locate and instantiate style and backend plugins
            style = cast(
                pybtex.style.formatting.BaseStyle,
                find_plugin('pybtex.style.formatting', bibcache.style)())
            sorted_entries = style.sort(entries)
            labels = style.format_labels(sorted_entries)
            for label, entry in zip(labels, sorted_entries):
                key = bibcache.keyprefix + entry.key
                citation = Citation(
                    bibliography_id=id_,
                    key=key,
                    label='[' + bibcache.labelprefix + label + ']',
                    entry_key=entry.key,
                    entry_label=label,
                )
                if (bibcache.list_ == 'citation'):
                    for othercitation in self.citations.values():
                        if othercitation.key == key:
                            logger.warning(
                                'duplicate bibtex citation for key %s' % key,
                                location=(bibcache.docname, bibcache.line))
                        elif othercitation.label == citation.label:
                            logger.warning(
                                'duplicate bibtex label %s for keys %s and %s' % (
                                    citation.label, key, othercitation.key),
                                location=(bibcache.docname, bibcache.line))
                citation_id = 'bibtex-citation-%s-%s' % (
                    bibcache.docname, self.env.new_serialno('bibtex'))
                self.citations[citation_id] = citation

    def resolve_xref(self, env: BuildEnvironment, fromdocname: str,
                     builder: Builder, typ: str, target: str,
                     node: pending_xref, contnode: docutils.nodes.Element
                     ) -> docutils.nodes.Element:
        keys = [key.strip() for key in target.split(',')]
        node = docutils.nodes.inline('', '', classes=['cite'])
        for key in keys:
            citation = None
            for citation_id, citation in self.citations.items():
                bibcache = self.bibliographies[citation.bibliography_id]
                if citation.key == key and bibcache.list_ == 'citation':
                    break
            if citation is None:
                # TODO can handle missing reference warning using the domain
                logger.warning('could not find bibtex key %s' % key)
                return None
            refuri = builder.get_relative_uri(fromdocname, bibcache.docname)
            lrefuri = '#'.join([refuri, citation_id])
            node += docutils.nodes.reference(
                citation.label, citation.label,
                internal=True, refuri=lrefuri)
        return node

    # TODO remove this function
    def get_label_from_key(self, key):
        """Return label for the given key."""
        return

    def get_all_cited_keys(self, docnames):
        """Yield all citation keys for given *docnames* in order, then
        ordered by citation order.
        """
        for docname in docnames:
            for key, citation_ref in self.citation_refs.items():
                if docname == citation_ref.docname:
                    yield key

    def _get_bibliography_entries(self, id_):
        """Return filtered bibliography entries, sorted by occurrence
        in the bib file.
        """
        # get the information of this bibliography node
        bibcache = self.bibliographies[id_]
        # generate entries
        for bibfile in bibcache.bibfiles:
            data = self.bibfiles[bibfile].data
            for entry in data.entries.values():
                key = bibcache.keyprefix + entry.key
                cited_docnames = {
                    citation_ref.docname
                    for citation_ref in self.citation_refs.values()
                    if citation_ref.key == key
                }
                visitor = _FilterVisitor(
                    entry=entry,
                    docname=bibcache.docname,
                    cited_docnames=cited_docnames)
                try:
                    success = visitor.visit(bibcache.filter_)
                except ValueError as err:
                    logger.warning(
                        "syntax error in :filter: expression; %s" % err,
                        location=(bibcache.docname, bibcache.line))
                    # recover by falling back to the default
                    success = bool(cited_docnames)
                if success:
                    # entries are modified in an unpickable way
                    # when formatting, so fetch a deep copy
                    # and return this copy with prefixed key
                    # we do not deep copy entry.collection because that
                    # consumes enormous amounts of memory
                    entry.collection = None
                    entry2 = copy.deepcopy(entry)
                    entry2.key = bibcache.keyprefix + entry.key
                    entry2.collection = data
                    entry.collection = data
                    yield entry2

    def get_bibliography_entries(self, id_, docnames):
        """Return filtered bibliography entries, sorted by citation order."""
        # get entries, ordered by bib file occurrence
        entries = collections.OrderedDict(
            (entry.key, entry) for entry in
            self._get_bibliography_entries(id_=id_))
        # order entries according to which were cited first
        # first, we add all keys that were cited
        # then, we add all remaining keys
        sorted_entries = []
        for key in self.get_all_cited_keys(docnames):
            try:
                entry = entries.pop(key)
            except KeyError:
                pass
            else:
                sorted_entries.append(entry)
        sorted_entries += entries.values()
        return sorted_entries
