from pathlib import Path

import pytest

from connection_map.analyzer import analyze_repository
from connection_map.config import AnalysisConfig
from connection_map.evidence import check_freshness


def graph(root: Path, files: dict[str, str]) -> dict:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    return analyze_repository(root, AnalysisConfig(language='web', include_tests=True), deterministic=True)


def calls(document: dict, expression: str) -> list[dict]:
    return [edge for edge in document['edges'] if edge['relation_type'] == 'calls'
            and edge['detail'].get('expression') == expression]


def test_explicit_receiver_does_not_become_a_self_call(tmp_path: Path) -> None:
    document = graph(tmp_path, {
        'client.ts': 'export class Client { logout() {} }',
        'store.ts': "import { Client } from './client'; export class Store { client = new Client(); logout() { this.client.logout(); } }",
    })
    edge, = calls(document, 'this.client.logout()')
    assert edge['target_id'] == 'typescript:client.ts:Client.logout:method'
    assert edge['resolution_status'] == 'resolved'
    assert edge['detail']['resolution_evidence']['declarations']


@pytest.mark.parametrize('source', [
    'export function caller() { helper(); }',
    "import { helper } from './helper'; export function caller(helper: () => void) { helper(); }",
    "import { helper } from './helper'; export function caller() { { const helper = unknown; helper(); } }",
    "import { helper } from './helper'; export function caller() { helper = unknown; helper(); }",
    "import { helper } from './helper'; export function caller({helper}: {helper: () => void}) { helper(); }",
    "import { helper } from './helper'; export function caller() { for (const helper of callbacks) { helper(); } }",
])
def test_unknown_shadowed_or_reassigned_calls_stay_unresolved(tmp_path: Path, source: str) -> None:
    document = graph(tmp_path, {'helper.ts': 'export function helper() {}', 'caller.ts': source})
    edge, = calls(document, 'helper()')
    assert edge['resolution_status'] == 'unresolved'
    assert edge['target_id'] != 'typescript:helper.ts:helper:function'


def test_import_alias_namespace_and_reexport_are_explicit_bindings(tmp_path: Path) -> None:
    document = graph(tmp_path, {
        'impl.ts': 'export function helper() {}',
        'barrel.ts': "export { helper as run } from './impl';",
        'app.ts': "import {run as start} from './barrel'; import * as lib from './impl'; start(); lib.helper();",
    })
    for expression in ('start()', 'lib.helper()'):
        edge, = calls(document, expression)
        assert edge['resolution_status'] == 'resolved'
        assert edge['target_id'] == 'typescript:impl.ts:helper:function'


def test_named_function_expression_shadows_the_import(tmp_path: Path) -> None:
    document = graph(tmp_path, {
        'helper.ts': 'export function helper() {}',
        'caller.ts': "import {helper} from './helper'; const fn = function helper() {helper();};",
    })
    edge, = calls(document, 'helper()')
    assert edge['target_id'].startswith('typescript:caller.ts:')


@pytest.mark.parametrize('reference', ['./services/auth-session.store', './services/auth-session.store.js'])
def test_dotted_module_basename_and_javascript_extension_resolve_typescript(tmp_path: Path, reference: str) -> None:
    document = graph(tmp_path, {
        'services/auth-session.store.ts': 'export class Store { logout() {} }',
        'page.ts': f"import {{Store}} from '{reference}'; const store = new Store(); store.logout();",
    })
    edge, = calls(document, 'store.logout()')
    assert edge['resolution_status'] == 'resolved'
    assert edge['target_id'] == 'typescript:services/auth-session.store.ts:Store.logout:method'


@pytest.mark.parametrize('assignment', ["store['logout'] = unknown", 'store[key] = unknown'])
def test_computed_member_writes_invalidate_receiver_resolution(tmp_path: Path, assignment: str) -> None:
    document = graph(tmp_path, {
        'page.ts': f'class Store {{ logout() {{}} }} const store = new Store(); {assignment}; store.logout();',
    })
    edge, = calls(document, 'store.logout()')
    assert edge['resolution_status'] == 'unresolved'


def test_this_computed_member_write_invalidates_method_resolution(tmp_path: Path) -> None:
    document = graph(tmp_path, {
        'page.ts': "class Store { logout() {} caller() { this['logout'] = unknown; this.logout(); } }",
    })
    edge, = calls(document, 'this.logout()')
    assert edge['resolution_status'] == 'unresolved'


def test_angular_inject_has_to_be_the_angular_import(tmp_path: Path) -> None:
    document = graph(tmp_path, {
        'client.ts': 'export class Client { logout() {} }',
        'real.ts': "import {inject} from '@angular/core'; import {Client} from './client'; export class Real { client = inject(Client); logout() { this.client.logout(); } }",
        'fake.ts': "import {Client} from './client'; function inject(token) {return unknown;} class Fake { client = inject(Client); logout() { this.client.logout(); } }",
    })
    edges = calls(document, 'this.client.logout()')
    real = next(e for e in edges if ':real.ts:' in e['source_id'])
    fake = next(e for e in edges if ':fake.ts:' in e['source_id'])
    assert real['target_id'] == 'typescript:client.ts:Client.logout:method'
    assert real['resolution_status'] == 'resolved'
    assert fake['resolution_status'] == 'unresolved'


def test_dom_query_is_scoped_to_the_document_loading_the_script(tmp_path: Path) -> None:
    document = graph(tmp_path, {
        'a.html': '<p class="label">A</p><script src="a.ts"></script>',
        'b.html': '<p class="label">B</p><script src="b.ts"></script>',
        'a.ts': "document.querySelector('.label');",
        'b.ts': "const custom = {querySelector(selector: string) { return null; }}; custom.querySelector('.label');",
    })
    references = [e for e in document['edges'] if e['relation_type'] == 'references']
    assert len(references) == 1
    assert references[0]['target_id'].startswith('html:a.html:')
    assert references[0]['resolution_status'] == 'resolved'


def test_dynamic_selectors_and_imports_are_not_literal_names(tmp_path: Path) -> None:
    document = graph(tmp_path, {
        'page.html': '<selector>Example</selector><script src="page.ts"></script>',
        'page.ts': 'document.querySelector(selector); import(moduleName);',
        'moduleName.ts': 'export function run() {}',
    })
    assert not any(e['relation_type'] == 'references' and e['resolution_status'] == 'resolved'
                   for e in document['edges'])
    edge, = [e for e in document['edges'] if e['relation_type'] == 'dynamic_imports']
    assert edge['resolution_status'] == 'unresolved'


def test_tsconfig_paths_and_inherited_base_are_used(tmp_path: Path) -> None:
    document = graph(tmp_path, {
        'tsconfig.json': '{"files": [], "references": [{"path":"./tsconfig.app.json"}]}',
        'config/base.json': '{// comment\n "compilerOptions":{"baseUrl":"..", "paths":{"@shared/*":["shared/*"]}},}',
        'tsconfig.app.json': '{"extends":"./config/base.json","include":["src/**/*.ts"]}',
        'shared/rules.ts': 'export function check() {}',
        'src/app.ts': "import {check} from '@shared/rules'; check();",
    })
    edge, = calls(document, 'check()')
    assert edge['target_id'] == 'typescript:shared/rules.ts:check:function'
    assert edge['resolution_status'] == 'resolved'
    assert any(e['relation_type'] == 'imports' and e['target_id'] == 'typescript:shared/rules.ts:module'
               and e['resolution_status'] == 'resolved' for e in document['edges'])
    assert check_freshness(document, tmp_path)['status'] == 'current'
    (tmp_path / 'config/base.json').write_text('{"compilerOptions":{}}', encoding='utf-8')
    freshness = check_freshness(document, tmp_path)
    assert freshness['status'] == 'stale'
    assert 'config/base.json' in freshness['changes']['modified']


def test_angular_template_events_fixture_scope_and_partial_coverage(tmp_path: Path) -> None:
    document = graph(tmp_path, {
        'page.ts': "import {Component} from '@angular/core'; @Component({templateUrl:'./page.html', styleUrl:'./page.scss'}) export class Page { logout() {} }",
        'other.ts': "import {Component} from '@angular/core'; @Component({templateUrl:'./other.html'}) export class Other { logout() {} }",
        'page.html': '@if (ready()) { <p class="label">Page</p><button (click)="logout()">Logout</button> }',
        'other.html': '<p class="label">Other</p>',
        'page.scss': '.label {color:red;}',
        'page.spec.ts': "import {ComponentFixture, TestBed} from '@angular/core/testing'; import {Page} from './page'; let fixture: ComponentFixture<Page>; beforeEach(()=>{fixture=TestBed.createComponent(Page);}); function test() { const compiled=fixture.nativeElement as HTMLElement; compiled.querySelector('.label'); }",
    })
    refs = [e for e in document['edges'] if e['relation_type'] == 'references' and e['detail'].get('selector') == '.label']
    assert len(refs) == 1
    assert refs[0]['target_id'].startswith('html:page.html:')
    handlers = [e for e in document['edges'] if e['relation_type'] == 'handles']
    assert len(handlers) == 1
    assert handlers[0]['target_id'] == 'typescript:page.ts:Page.logout:method'
    assert handlers[0]['resolution_status'] == 'resolved'
    assert not any(d['code'] == 'parse_error' for d in document['diagnostics'])
    coverage = document['meta']['extensions']['coverage']
    assert coverage['status'] == 'partial'
    assert coverage['unsupported_source_files'] == ['page.scss']
    assert coverage['extraction_limitations']


def test_solution_references_can_extend_the_solution_config(tmp_path: Path) -> None:
    document = graph(tmp_path, {
        'tsconfig.json': '{"files":[],"references":[{"path":"./tsconfig.app.json"},{"path":"./tsconfig.spec.json"}],"compilerOptions":{"paths":{"@shared/*":["./shared/*"]}}}',
        'tsconfig.app.json': '{"extends":"./tsconfig.json","include":["src/**/*.ts"]}',
        'tsconfig.spec.json': '{"extends":"./tsconfig.json","include":["tests/**/*.ts"]}',
        'shared/check.ts': 'export function check() {}',
        'src/page.ts': "import {check} from '@shared/check'; check();",
    })
    edge, = calls(document, 'check()')
    assert edge['resolution_status'] == 'resolved'
    assert not any(d['code'] == 'typescript_context_error' for d in document['diagnostics'])


def test_angular_event_template_locals_do_not_resolve_to_class_methods(tmp_path: Path) -> None:
    document = graph(tmp_path, {
        'page.ts': "import {Component} from '@angular/core'; @Component({templateUrl:'./page.html'}) export class Page { logout() {} }",
        'page.html': '@let logout = alternative; <button (click)="logout()">Logout</button>',
    })
    edge, = [e for e in document['edges'] if e['relation_type'] == 'handles']
    assert edge['resolution_status'] == 'unresolved'


def test_tsconfig_cycles_missing_aliases_and_outside_paths_do_not_guess(tmp_path: Path) -> None:
    document = graph(tmp_path, {
        'tsconfig.json': '{"extends":"./tsconfig.json"}',
        'src.ts': "import {run} from './absent'; run();",
        'other.ts': 'export function run() {}',
    })
    edge, = calls(document, 'run()')
    assert edge['resolution_status'] == 'unresolved'
    assert any(d['code'] == 'typescript_context_error' for d in document['diagnostics'])


def test_http_literal_contract_links_are_candidates_not_runtime_proof(tmp_path: Path) -> None:
    document = graph(tmp_path, {
        'client.ts': "import {inject} from '@angular/core'; import {HttpClient} from '@angular/common/http'; export class Client {http = inject(HttpClient); getSession() {return this.http.get('/api/me');}} fetch('https://different.example/api/me');",
        'server.ts': "import express from 'express'; const app=express(); app.locals.ready=true; app.get('/api/me', (req,res)=>{res.json({ok:true});}); app.post('/api/me', (req,res)=>{res.json({ok:true});});",
    })
    requests = [e for e in document['edges'] if e['relation_type'] == 'requests']
    assert len(requests) == 1
    assert requests[0]['resolution_status'] == 'unresolved'
    assert requests[0]['detail']['method'] == 'GET'
    assert requests[0]['detail']['candidate_target_id'] == requests[0]['target_id']
    routes = [n for n in document['nodes'] if n.get('extensions', {}).get('framework') == 'express']
    assert len(routes) == 2
