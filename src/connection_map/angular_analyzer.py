"""Declared Angular template ownership and bounded static event extraction.

No Angular project code or compiler is executed. HTML structure and simple
event calls are extracted; block/expression validation remains explicitly partial.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from .web_bindings import Value
from .web_common import (
    HtmlElementInfo,
    add_relation,
    node_text,
    parser_for,
    resolve_reference,
    span_for_tree,
    walk_tree,
)
from .web_common import (
    literal_string_value as string_value,
)


def discover_components(context) -> None:
    for target, (source, node) in context.bindings.definitions.items():
        if node.type != 'class_declaration':
            continue
        decorators = [child for parent in (node, node.parent) if parent is not None
                      for child in parent.named_children if child.type == 'decorator']
        for decorator in decorators:
            call = next((n for n in decorator.named_children if n.type == 'call_expression'), None)
            if call is None:
                continue
            binding = context.bindings.expression(source, call.child_by_field_name('function'))
            if not binding or binding.origin != ('@angular/core', 'Component'):
                continue
            args = call.child_by_field_name('arguments')
            obj = args.named_children[0] if args and args.named_children else None
            if obj is None or obj.type != 'object':
                continue
            for pair in obj.named_children:
                key = node_text(pair.child_by_field_name('key'), source.source).strip('"\'')
                value = pair.child_by_field_name('value')
                if key == 'template':
                    context.extraction_limitations.append('Angular inline templates are not extracted.')
                if key not in {'templateUrl', 'styleUrl', 'styleUrls'}:
                    continue
                values = value.named_children if value and value.type == 'array' else [value]
                for entry in values:
                    reference = string_value(entry, source.source)
                    if not reference:
                        context.extraction_limitations.append('Computed Angular template/style references are unresolved.')
                        continue
                    related = resolve_reference(context, source, reference, allow_bare=True)
                    if key == 'templateUrl' and related and related.language == 'html':
                        context.component_templates[target] = related.relative_path
                        context.template_owners.setdefault(related.relative_path, []).append(target)
                    is_style = key != 'templateUrl'
                    target_id = related.module_id if related else context.external_node(f'angular-asset:{source.relative_path}:{reference}', unknown=True)
                    add_relation(context, target, target_id, 'imports' if not is_style else 'references',
                                 resolution_status='resolved' if related else 'unsupported' if is_style else 'unresolved',
                                 confidence=1.0 if related else 0.2, source_span=span_for_tree(entry),
                                 detail={'kind': 'angular_style' if is_style else 'angular_template', 'reference': reference,
                                         'resolution_basis': 'angular_component_metadata'})


def collect_template(context, source) -> None:
    context.extraction_limitations.append(
        'Angular templates: HTML structure and simple event calls only; block/expression and type validation are not performed.')
    context.diagnostic('angular_template_partial', 'warning',
                       'AngularテンプレートはHTML構造・単純なイベント呼び出しを抽出します。制御ブロックと式全体の検証は未対応です。',
                       web_file=source, details={'construct': 'angular_template', 'partial': True})
    parser = TemplateParser(context, source)
    parser.feed(source.source.decode('utf-8'))
    parser.close()


class TemplateParser(HTMLParser):
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, context, source):
        super().__init__(convert_charrefs=True)
        self.context, self.source = context, source
        self.stack = []
        self.ordinal = 0

    def handle_startendtag(self, tag, attrs):
        self._element(tag, attrs, closed=True)

    def handle_starttag(self, tag, attrs):
        self._element(tag, attrs, closed=tag in self.VOID)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def _element(self, tag, attrs, *, closed):
        self.ordinal += 1
        line, col = self.getpos()
        raw = self.get_starttag_text() or ''
        parts = raw.split('\n')
        span = {'start_line': line, 'start_col': col, 'end_line': line + len(parts) - 1,
                'end_col': len(parts[-1]) + (col if len(parts) == 1 else 0)}
        attributes = dict((key, value or '') for key, value in attrs)
        classes = attributes.get('class', '').split()
        node_id = f'html:{self.source.relative_path}:element:{self.ordinal}:{tag}:element'
        parent = self.stack[-1][1] if self.stack else self.source.module_id
        self.context.builder.add_node({
            'id': node_id, 'kind': 'element', 'qualified_name': f'{self.source.relative_path}:element:{self.ordinal}:{tag}',
            'display_name': f'<{tag}>', 'file': self.source.relative_path, 'span': span, 'parent_id': parent,
            'visibility': 'public', 'extensions': {'language': 'html', 'framework': 'angular', 'tag': tag,
                                                 'id': attributes.get('id'), 'classes': classes, 'attributes': attributes}})
        add_relation(self.context, parent, node_id, 'contains', resolution_status='resolved', confidence=1.0,
                     source_span=span, detail={'kind': 'html_element', 'tag': tag})
        self.context.html_elements.append(HtmlElementInfo(node_id, self.source.relative_path, tag, attributes.get('id'), tuple(classes)))
        for key, expression in attrs:
            if key.startswith('(') and key.endswith(')') and expression:
                self._events(node_id, key[1:-1], expression, span)
        if not closed:
            self.stack.append((tag, node_id))

    def _events(self, element, event, expression, span):
        content = ('function __event__(){' + expression + '}').encode('utf-8')
        tree = parser_for('typescript').parse(content)
        if tree.root_node.has_error:
            return
        owners = self.context.template_owners[self.source.relative_path]
        template = self.source.source.decode('utf-8')
        for call in walk_tree(tree.root_node):
            if call.type != 'call_expression':
                continue
            function = call.child_by_field_name('function')
            if function is None or function.type != 'identifier':
                continue
            name = node_text(function, content)
            if name.startswith('$'):
                continue
            shadowed = re.search(r'(?:@let\s+|let-|@for\s*\(\s*|\bas\s+)' + re.escape(name) + r'\b', template)
            for owner in owners:
                binding = self.context.bindings.member(Value('instance', owner), name) if not shadowed else None
                target = binding.target if binding and binding.kind == 'symbol' else None
                if target is None:
                    target = self.context.external_node(f'angular-event:{self.source.relative_path}:{name}', unknown=True)
                add_relation(self.context, element, target, 'handles',
                             resolution_status='resolved' if binding and binding.kind == 'symbol' else 'unresolved',
                             confidence=0.95 if binding else 0.2, source_span=span,
                             detail={'event': event, 'expression': expression, 'component_id': owner,
                                     'resolution_basis': 'angular_template_binding' if binding else 'binding_not_established',
                                     'resolution_evidence': {'strategy': 'angular_component_template',
                                                             'declarations': binding.evidence if binding else []}})
