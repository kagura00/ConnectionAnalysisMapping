"""Express registrations and literal HTTP contracts, without claiming routing."""

from .web_common import add_relation, node_text, span_for_tree, walk_tree
from .web_common import literal_string_value as string_value

METHODS = {'get', 'post', 'put', 'patch', 'delete', 'head', 'options'}


def discover_routes(context) -> None:
    for source in context.files:
        if source.language not in {'typescript', 'javascript'}:
            continue
        for call in walk_tree(source.tree.root_node):
            if call.type != 'call_expression':
                continue
            function = call.child_by_field_name('function')
            if function is None or function.type != 'member_expression':
                continue
            method = node_text(function.child_by_field_name('property'), source.source)
            receiver = context.bindings.expression(source, function.child_by_field_name('object'))
            arguments = call.child_by_field_name('arguments')
            args = arguments.named_children if arguments else []
            if (method not in METHODS or not receiver or receiver.kind != 'router' or not args
                    or method in receiver.blocked_members or '*' in receiver.blocked_members):
                continue
            path = string_value(args[0], source.source)
            if not path or not path.startswith('/'):
                continue
            for handler in args[1:]:
                if handler.type not in {'arrow_function', 'function_expression', 'function'}:
                    continue
                owner = context.enclosing_definition(source, call.parent) or source.module_id
                node_id = context.add_definition(source, handler, kind='lambda', name=f'{method.upper()} {path} handler',
                                                 extensions={'http_method': method.upper(), 'http_path': path, 'framework': 'express'})
                context.http_routes.setdefault((method.upper(), path), []).append(node_id)
                add_relation(context, owner, node_id, 'registers', resolution_status='resolved', confidence=1.0,
                             source_span=span_for_tree(call), detail={'method': method.upper(), 'path': path,
                                                                   'resolution_basis': 'express_route_registration'})


def collect_request(context, source, call, caller) -> None:
    function = call.child_by_field_name('function')
    binding = context.bindings.expression(source, function)
    arguments = call.child_by_field_name('arguments')
    args = arguments.named_children if arguments else []
    if not binding or not binding.origin or not args:
        return
    package, symbol = binding.origin
    method = None
    if package == '@angular/common/http' and symbol.startswith('HttpClient.') and symbol.split('.')[-1] in METHODS:
        method = symbol.split('.')[-1].upper()
    if (package, symbol) == ('global', 'fetch'):
        method = 'GET'
        if len(args) > 1:
            if args[1].type != 'object':
                return
            for pair in args[1].named_children:
                if pair.type != 'pair':
                    return
                if node_text(pair.child_by_field_name('key'), source.source).strip('"\'') == 'method':
                    value = string_value(pair.child_by_field_name('value'), source.source)
                    method = value.upper() if value else None
    path = string_value(args[0], source.source)
    if method is None or not path or not path.startswith('/') or path.startswith('//'):
        return
    path = path.split('?', 1)[0].split('#', 1)[0]
    targets = context.http_routes.get((method, path), [])
    for target in targets:
        add_relation(context, caller, target, 'requests', resolution_status='unresolved', confidence=0.4,
                     source_span=span_for_tree(call),
                     detail={'method': method, 'path': path, 'expression': node_text(call, source.source),
                             'resolution_basis': 'http_endpoint_candidate', 'candidate_target_id': target,
                             'reason': 'Method/path match only; origin, proxy, middleware and deployment routing are unverified.'})
    if targets:
        context.extraction_limitations.append('HTTP connections match literal methods and paths; origin/proxy and middleware routing remain unverified.')
