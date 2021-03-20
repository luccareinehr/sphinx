"""
    sphinx.transforms.post_transforms
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    Docutils transforms used by Sphinx.

    :copyright: Copyright 2007-2021 by the Sphinx team, see AUTHORS.
    :license: BSD, see LICENSE for details.
"""

from typing import Any, Dict, List, Optional, Tuple, Type, cast

from docutils import nodes
from docutils.nodes import Element

from sphinx import addnodes
from sphinx.addnodes import pending_xref
from sphinx.application import Sphinx
from sphinx.domains import Domain
from sphinx.errors import NoUri
from sphinx.locale import __
from sphinx.transforms import SphinxTransform
from sphinx.util import logging
from sphinx.util.docutils import SphinxTranslator
from sphinx.util.nodes import find_pending_xref_condition, process_only_nodes

logger = logging.getLogger(__name__)

if False:
    # For type annotation
    from docutils.nodes import Node


class SphinxPostTransform(SphinxTransform):
    """A base class of post-transforms.

    Post transforms are invoked to modify the document to restructure it for outputting.
    They do resolving references, convert images, special transformation for each output
    formats and so on.  This class helps to implement these post transforms.
    """
    builders: Tuple[str, ...] = ()
    formats: Tuple[str, ...] = ()

    def apply(self, **kwargs: Any) -> None:
        if self.is_supported():
            self.run(**kwargs)

    def is_supported(self) -> bool:
        """Check this transform working for current builder."""
        if self.builders and self.app.builder.name not in self.builders:
            return False
        if self.formats and self.app.builder.format not in self.formats:
            return False

        return True

    def run(self, **kwargs: Any) -> None:
        """main method of post transforms.

        Subclasses should override this method instead of ``apply()``.
        """
        raise NotImplementedError


class ReferencesResolver(SphinxPostTransform):
    """
    Resolves cross-references on doctrees.
    """

    default_priority = 10

    def run(self, **kwargs: Any) -> None:
        for node in self.document.traverse(addnodes.pending_xref):
            contnode = cast(nodes.TextElement, node[0].deepcopy())
            newnode = None

            typ = node['reftype']
            target = node['reftarget']
            refdoc = node.get('refdoc', self.env.docname)
            domain = None

            try:
                if 'refdomain' in node and node['refdomain']:
                    # let the domain try to resolve the reference
                    try:
                        domain = self.env.domains[node['refdomain']]
                    except KeyError as exc:
                        raise NoUri(target, typ) from exc
                    newnode = domain.resolve_xref(self.env, refdoc, self.app.builder,
                                                  typ, target, node, contnode)
                # really hardwired reference types
                elif typ == 'any':
                    newnode = self.resolve_anyref(refdoc, node, contnode)
                # no new node found? try the missing-reference event
                if newnode is None:
                    newnode = self.app.emit_firstresult('missing-reference', self.env,
                                                        node, contnode,
                                                        allowed_exceptions=(NoUri,))
                    # still not found? warn if node wishes to be warned about or
                    # we are in nit-picky mode
                    if newnode is None:
                        self.warn_missing_reference(refdoc, typ, target, node, domain)
            except NoUri:
                newnode = None

            if newnode:
                newnodes: List[Node] = [newnode]
            else:
                newnodes = [contnode]
                if newnode is None and isinstance(node[0], addnodes.pending_xref_condition):
                    matched = find_pending_xref_condition(node, "*")
                    if matched:
                        newnodes = matched.children
                    else:
                        logger.warning(__('Could not determine the fallback text for the '
                                          'cross-reference. Might be a bug.'), location=node)

            node.replace_self(newnodes)

    def resolve_anyref(self, refdoc: str, node: pending_xref, contnode: Element) -> Element:
        """Resolve reference generated by the "any" role."""
        stddomain = self.env.get_domain('std')
        target = node['reftarget']
        results: List[Tuple[str, Element]] = []
        # first, try resolving as :doc:
        doc_ref = stddomain.resolve_xref(self.env, refdoc, self.app.builder,
                                         'doc', target, node, contnode)
        if doc_ref:
            results.append(('doc', doc_ref))
        # next, do the standard domain (makes this a priority)
        results.extend(stddomain.resolve_any_xref(self.env, refdoc, self.app.builder,
                                                  target, node, contnode))
        for domain in self.env.domains.values():
            if domain.name == 'std':
                continue  # we did this one already
            try:
                results.extend(domain.resolve_any_xref(self.env, refdoc, self.app.builder,
                                                       target, node, contnode))
            except NotImplementedError:
                # the domain doesn't yet support the new interface
                # we have to manually collect possible references (SLOW)
                for role in domain.roles:
                    res = domain.resolve_xref(self.env, refdoc, self.app.builder,
                                              role, target, node, contnode)
                    if res and len(res) > 0 and isinstance(res[0], nodes.Element):
                        results.append(('%s:%s' % (domain.name, role), res))
        # now, see how many matches we got...
        if not results:
            return None
        if len(results) > 1:
            def stringify(name: str, node: Element) -> str:
                reftitle = node.get('reftitle', node.astext())
                return ':%s:`%s`' % (name, reftitle)
            candidates = ' or '.join(stringify(name, role) for name, role in results)
            logger.warning(__('more than one target found for \'any\' cross-'
                              'reference %r: could be %s'), target, candidates,
                           location=node)
        res_role, newnode = results[0]
        # Override "any" class with the actual role type to get the styling
        # approximately correct.
        res_domain = res_role.split(':')[0]
        if (len(newnode) > 0 and
                isinstance(newnode[0], nodes.Element) and
                newnode[0].get('classes')):
            newnode[0]['classes'].append(res_domain)
            newnode[0]['classes'].append(res_role.replace(':', '-'))
        return newnode

    def warn_missing_reference(self, refdoc: str, typ: str, target: str,
                               node: pending_xref, domain: Optional[Domain]) -> None:
        warn = node.get('refwarn')
        if self.config.nitpicky:
            warn = True
            if self.config.nitpick_ignore:
                dtype = '%s:%s' % (domain.name, typ) if domain else typ
                if (dtype, target) in self.config.nitpick_ignore:
                    warn = False
                # for "std" types also try without domain name
                if (not domain or domain.name == 'std') and \
                   (typ, target) in self.config.nitpick_ignore:
                    warn = False
        if not warn:
            return

        if self.app.emit_firstresult('warn-missing-reference', domain, node):
            return
        elif domain and typ in domain.dangling_warnings:
            msg = domain.dangling_warnings[typ] % {'target': target}
        elif node.get('refdomain', 'std') not in ('', 'std'):
            msg = (__('%s:%s reference target not found: %s') %
                   (node['refdomain'], typ, target))
        else:
            msg = __('%r reference target not found: %s') % (typ, target)
        logger.warning(msg, location=node, type='ref', subtype=typ)


class OnlyNodeTransform(SphinxPostTransform):
    default_priority = 50

    def run(self, **kwargs: Any) -> None:
        # A comment on the comment() nodes being inserted: replacing by [] would
        # result in a "Losing ids" exception if there is a target node before
        # the only node, so we make sure docutils can transfer the id to
        # something, even if it's just a comment and will lose the id anyway...
        process_only_nodes(self.document, self.app.builder.tags)


class SigElementFallbackTransform(SphinxPostTransform):
    """Fallback desc_sig_element nodes to inline if translator does not supported them."""
    default_priority = 200

    def run(self, **kwargs: Any) -> None:
        def has_visitor(translator: Type[nodes.NodeVisitor], node: Type[Element]) -> bool:
            return hasattr(translator, "visit_%s" % node.__name__)

        translator = self.app.builder.get_translator_class()
        if isinstance(translator, SphinxTranslator):
            # subclass of SphinxTranslator supports desc_sig_element nodes automatically.
            return

        if all(has_visitor(translator, node) for node in addnodes.SIG_ELEMENTS):
            # the translator supports all desc_sig_element nodes
            return
        else:
            self.fallback()

    def fallback(self) -> None:
        for node in self.document.traverse(addnodes.desc_sig_element):
            newnode = nodes.inline()
            newnode.update_all_atts(node)
            newnode.extend(node)
            node.replace_self(newnode)


class DescSigAddDomainAsClass(SphinxPostTransform):
    """Add the domain name of the parent node as a class in each desc_signature node."""
    default_priority = 200

    def run(self, **kwargs: Any) -> None:
        for node in self.document.traverse(addnodes.desc_signature):
            node['classes'].append(node.parent['domain'])


def setup(app: Sphinx) -> Dict[str, Any]:
    app.add_post_transform(ReferencesResolver)
    app.add_post_transform(OnlyNodeTransform)
    app.add_post_transform(SigElementFallbackTransform)
    app.add_post_transform(DescSigAddDomainAsClass)

    return {
        'version': 'builtin',
        'parallel_read_safe': True,
        'parallel_write_safe': True,
    }
