#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Audit all pinned module objects without treating missing ABI evidence as success.

Recursive type comparisons preserve qualifiers. Incomplete pointer targets and
function declarations are unknown, not verified. Module CRC inspection and
structural ABI evidence remain separate from firmware/hardware acceptance.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import struct
import subprocess

HERE = Path(__file__).resolve().parent


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SIG = load_module('abi_signatures', HERE / 'type-signatures.py')
BUILD = load_module('abi_builder', HERE / 'build.py')
SHAPE = SIG.shape
AGGREGATES = {'struct', 'union'}
QUALIFIERS = {'const', 'volatile', 'restrict'}
MAX_DEPTH = 200
MAX_GRAPH_PAIRS = 2048


def inspect(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=120,
                                env=BUILD.ENV, cwd='/var/tmp')
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('ABI inspector unavailable or timed out') from error
    if result.returncode or result.stderr.strip() or not result.stdout.strip():
        raise ValueError('ABI inspection failed or returned incomplete output')
    return result.stdout


def checked_objects(work, component):
    names = BUILD.component_objects(component)
    module = BUILD.PINS[component]['module']
    record = work / (module + '.mod')
    raw = SHAPE.materials.read_regular(record, 65536)
    entries = raw.decode('ascii').splitlines()
    expected = ['./' + name for name in names]
    if entries != expected or len(set(entries)) != len(entries):
        raise ValueError('Kbuild module object list differs from pinned component objects')
    command = SHAPE.materials.read_regular(work / ('.' + module + '.o.cmd'), 65536).decode('ascii')
    if len(command.splitlines()) != 1 or not re.fullmatch(
            r'savedcmd_' + re.escape(module) + r'\.o := .+ -r -o ' +
            re.escape(module) + r'\.o @' + re.escape(module) + r'\.mod\s*', command):
        raise ValueError('Kbuild link command does not consume the verified object list')
    return names, hashlib.sha256(raw).hexdigest()


def graph_objects(work, component):
    names, record_sha = checked_objects(work, component)
    graphs = []
    for name in names:
        path = work / name
        before = SHAPE.materials.read_regular(path)
        BUILD.elf_module(path)
        output = inspect([SHAPE.READ_ELF, '--debug-dump=info', str(path)])
        if before != SHAPE.materials.read_regular(path):
            raise ValueError('Object changed during ABI inspection: ' + name)
        graph = SIG.dwarf_graph(output)
        dwarf_variables(graph, output)
        graph.source = name
        graph.input_path = str(path.resolve())
        graph.sha256 = hashlib.sha256(before).hexdigest()
        graph.object_list_path = str((work / (BUILD.PINS[component]['module'] + '.mod')).resolve())
        graph.object_list_sha256 = record_sha
        graphs.append(graph)
    return graphs


def official_graphs(specification, component):
    base, split_inputs = SHAPE.validate_specification(specification, component)
    raw = SHAPE.official_blob(Path(base['kernel']), base['kernel_sha256'], Path(base['map']),
                              base['map_sha256'], base['image_sha256'])
    official_base = btf_graph(raw)
    official_base.source = 'vmlinux'
    official_base.input_path = str(Path(base['kernel']).resolve())
    official_base.sha256 = base['kernel_sha256']
    official_base.btf_sha256 = hashlib.sha256(raw).hexdigest()
    official_base.verification_inputs = {key: base[key] for key in
        ('kernel', 'kernel_sha256', 'map', 'map_sha256', 'image_sha256')}
    graphs = [official_base]
    for item in split_inputs:
        blob = SHAPE.materials.pinned_read(Path(item['path']), item['sha256'])
        graph = btf_graph(blob, official_base)
        graph.source = Path(item['path']).name
        graph.input_path = str(Path(item['path']).resolve())
        graph.sha256 = item['sha256']
        graph.btf_sha256 = hashlib.sha256(blob).hexdigest()
        graph.base = official_base
        graphs.append(graph)
    return graphs


def btf_graph(blob, base=None):
    """Retain BTF VAR evidence separately; a variable is not a function signature."""
    graph = SIG.btf_graph(blob, base)
    graph.variables = {name: list(ids) for name, ids in getattr(base, 'variables', {}).items()}
    graph.variable_types = dict(getattr(base, 'variable_types', {}))
    _magic, _version, _flags, header, offset, length, _stroff, _strlen = struct.unpack_from('<HBBIIIII', blob)
    types = blob[header + offset:header + offset + length]
    pos, ident = 0, base.next_id if base is not None else 1
    while pos < len(types):
        _name, info, value = struct.unpack_from('<III', types, pos)
        kind, count = (info >> 24) & 31, info & 65535
        pos += 12
        if kind == 14:
            name = graph.records[ident]['name']
            linkage = struct.unpack_from('<I', types, pos)[0]
            if name and linkage in (1, 2):
                graph.variables.setdefault(name, []).append(ident)
                graph.variable_types[ident] = value
        # SIG's validated parser is authoritative for bounds and supported kinds.
        pos += ({1: 4, 3: 12, 4: count * 12, 5: count * 12, 6: count * 8,
                 13: count * 8, 14: 4, 15: count * 12, 17: 4, 19: count * 12}.get(kind, 0))
        ident += 1
    if pos != len(types) or ident != graph.next_id:
        raise ValueError('BTF variable evidence does not cover the validated graph')
    return graph


def dwarf_variables(graph, output):
    """Record direct DWARF variables; inherited/missing types stay unknown."""
    graph.variables, graph.variable_types = {}, {}
    current, attrs = None, {}
    for line in output.splitlines():
        match = re.match(r'\s*<\d+><([0-9a-f]+)>: Abbrev Number: (\d+)(?: \((DW_TAG_\w+)\))?', line)
        if match:
            current = int(match[1], 16) if match[2] != '0' and match[3] == 'DW_TAG_variable' else None
        elif current is not None:
            match = re.match(r'\s*<[0-9a-f]+>\s+(DW_AT_\w+)\s*:\s*(.*)', line)
            if not match:
                continue
            key, value = match.groups()
            if key == 'DW_AT_type':
                ref = re.fullmatch(r'<0x([0-9a-f]+)>', value)
                if not ref:
                    raise ValueError('Unsupported DWARF variable reference')
                graph.variable_types[current] = int(ref[1], 16)
            elif key in ('DW_AT_specification', 'DW_AT_abstract_origin'):
                graph.variable_types[current] = None
    for ident, row in graph.records.items():
        if row['kind'] == 'unsupported-DW_TAG_variable' and row.get('name'):
            graph.variables.setdefault(row['name'], []).append(ident)


def provenance(graph, ident=None):
    result = {'source': getattr(graph, 'source', ''),
              'path': getattr(graph, 'input_path', None),
              'sha256': getattr(graph, 'sha256', None)}
    if ident is not None:
        result['type_id'] = ident
    if hasattr(graph, 'btf_sha256'):
        result['btf_sha256'] = graph.btf_sha256
    return result


def named_ids(graph, kinds):
    index = getattr(graph, '_named_abi_types', None)
    if index is None:
        index = {}
        for ident, row in graph.records.items():
            if row.get('kind') in AGGREGATES | {'enum', 'typedef'} and row.get('name'):
                index.setdefault(row['name'], []).append(ident)
        graph._named_abi_types = index
    cached = getattr(graph, '_abi_kind_index', {})
    key = frozenset(kinds)
    if key not in cached:
        result = {name: [ident for ident in ids if graph.records[ident]['kind'] in kinds]
                  for name, ids in index.items()}
        cached[key] = {name: ids for name, ids in result.items() if ids}
        graph._abi_kind_index = cached
    return cached[key]


def deref(graph, ident):
    seen = set()
    while ident not in (None, 0):
        if ident in seen:
            raise ValueError('Recursive alias type')
        seen.add(ident)
        row = graph.records.get(ident)
        if row is None:
            raise ValueError('Unresolved type reference')
        if row['kind'] not in ('typedef', 'function-alias'):
            return ident
        if row.get('type') is None:
            raise ValueError('Missing alias target')
        ident = row['type']
    return 0


UNKNOWN_ACTIONS = {
    'opaque-pointer': 'Obtain complete pointee definitions on both sides; pointer width alone is not ABI proof.',
    'missing-signature': 'Obtain an independent complete declaration for the recorded symbol or function type.',
    'missing-btf': 'Obtain pinned official type/symbol evidence; CRC equality is not a signature.',
    'provider-ambiguity': 'Resolve conflicting or unproven provider evidence against the actual linked provider.',
    'pair-budget': 'Reduce comparisons only with proven graph equivalence; do not increase the budget or waive unknown.',
    'depth-limit': 'Investigate the recorded graph path without raising limits or treating truncation as proof.',
    'data-symbol': 'Obtain and compare independent variable type/layout evidence, not a function prototype.',
    'symbol-kind-unknown': 'Obtain DWARF/BTF or typed ELF evidence; NOTYPE does not distinguish data from code.',
    'incomplete-type': 'Complete the recorded missing type/layout evidence on both sides.',
    'unsupported-type': 'Add bounded validated handling for the recorded type kind, retaining negative tests.',
    'unresolved-type': 'Resolve the recorded missing/recursive alias reference from verified inputs.',
    'unmatched-local-type': 'Establish an official counterpart or justify scope independently; do not silently exclude it.',
    'missing-crc-evidence': 'Run the existing pinned-header CRC gate separately from structural verification.',
}


def unknown_category(reason):
    if reason in ('type-graph-pair-budget', 'variant-comparison-budget'):
        return 'pair-budget'
    if reason == 'depth-limit':
        return 'depth-limit'
    if reason.startswith(('ambiguous-official-type:', 'unproven-assembly-provider')):
        return 'provider-ambiguity'
    if reason in ('missing-local-declaration', 'incomplete-function', 'missing-function-argument-type'):
        return 'missing-signature'
    if reason == 'missing-official-information':
        return 'missing-btf'
    if reason.startswith('unsupported-'):
        return 'unsupported-type'
    if reason in ('Recursive alias type', 'Unresolved type reference', 'Missing alias target'):
        return 'unresolved-type'
    return 'incomplete-type'


def compare_type(ours, local_id, official, official_id, seen=None, depth=0,
                 issues=None, path=(), pointer=False):
    """Return True/False/None without relaxing unknown; optionally record leaf causes."""
    def unknown(reason, category=None):
        if issues is not None:
            issues.append({'category': category or unknown_category(reason), 'reason': reason,
                           'path': list(path), 'local': provenance(ours, local_id),
                           'provider': provenance(official, official_id)})
        return None, reason

    if depth > MAX_DEPTH:
        return unknown('depth-limit')
    local_id, official_id = deref(ours, local_id), deref(official, official_id)
    if not local_id or not official_id:
        return (True, 'void') if local_id == official_id else (False, 'void-mismatch')
    local, other = ours.records[local_id], official.records[official_id]
    kind = local['kind']
    if kind.startswith(('unsupported-', 'incomplete-')) or other['kind'].startswith(('unsupported-', 'incomplete-')):
        kinds = (kind, other['kind'])
        category = 'missing-signature' if 'incomplete-function' in kinds else 'unsupported-type' if any(k.startswith('unsupported-') for k in kinds) else 'incomplete-type'
        return unknown('incomplete-or-unsupported-type', category)
    if kind != other['kind']:
        return False, 'kind-mismatch:' + kind + ':' + other['kind']
    if kind in AGGREGATES and local.get('name') != other.get('name'):
        return False, 'aggregate-identity-mismatch'
    if kind in AGGREGATES and (local.get('size') is None or other.get('size') is None):
        return unknown('incomplete-aggregate:' + local.get('name', ''),
                       'opaque-pointer' if pointer else 'incomplete-type')
    pair = (local_id, official_id)
    trail = set() if seen is None else seen
    if pair in trail:
        return True, 'recursive-edge'
    if len(trail) >= MAX_GRAPH_PAIRS:
        return unknown('type-graph-pair-budget')
    trail.add(pair)

    def compare(a, b, edge, through_pointer=False):
        return compare_type(ours, a, official, b, trail, depth + 1, issues,
                            path + (edge,), through_pointer)

    if kind in QUALIFIERS:
        if 'type' not in local or 'type' not in other:
            return unknown('missing-qualified-target')
        return compare(local['type'], other['type'], kind, pointer)
    if kind == 'scalar':
        keys = ('size', 'encoding', 'bits')
        if any(key not in local or key not in other for key in keys):
            return unknown('incomplete-scalar')
        return (True, 'matched') if all(local[key] == other[key] for key in keys) else (False, 'scalar-mismatch')
    if kind == 'pointer':
        if local.get('size') != other.get('size'):
            return False, 'pointer-size-mismatch'
        if 'type' not in local or 'type' not in other:
            return unknown('missing-pointer-target', 'opaque-pointer')
        return compare(local['type'], other['type'], 'pointee', True)
    if kind == 'array':
        # BTF can flatten dimensions; this proves storage extent, not indexing semantics.
        counts = []
        for row in (local, other):
            dimensions = row.get('dimensions')
            if not dimensions or any(type(value) is not int or value < 0 for value in dimensions):
                return unknown('incomplete-array-dimensions')
            count = 1
            for value in dimensions:
                count *= value
            counts.append(count)
        if counts[0] != counts[1]:
            return False, 'array-element-count-mismatch'
        if local.get('type') in (None, 0) or other.get('type') in (None, 0):
            return unknown('missing-array-element-type')
        return compare(local['type'], other['type'], 'element')
    if kind == 'enum':
        if any(key not in row for row in (local, other) for key in ('size', 'values')):
            return unknown('incomplete-enum')
        return (True, 'matched') if (local['size'], local['values']) == (other['size'], other['values']) else (False, 'enum-mismatch')
    if kind in AGGREGATES:
        if local['size'] != other['size']:
            return False, 'aggregate-size-mismatch'
        if 'members' not in local or 'members' not in other:
            return unknown('missing-member-list')
        if len(local['members']) != len(other['members']):
            return False, 'member-count-mismatch'
        unknown_reason = None
        for index, (a, b) in enumerate(zip(local['members'], other['members'])):
            for key in ('name', 'offset', 'bits'):
                if key not in a or key not in b:
                    return unknown('incomplete-member-layout')
                if a[key] != b[key]:
                    return False, 'member-' + key + '-mismatch:' + str(index)
            if a.get('type') in (None, 0) or b.get('type') in (None, 0):
                equal, reason = unknown('missing-member-type')
            else:
                equal, reason = compare(a['type'], b['type'], 'member:' + a['name'])
            if equal is False:
                return False, 'member-type:' + a['name'] + ':' + reason
            if equal is None:
                unknown_reason = 'member-type:' + a['name'] + ':' + reason
        return (None, unknown_reason) if unknown_reason else (True, 'matched')
    if kind == 'function':
        if any(key not in row for row in (local, other) for key in ('return', 'args', 'variadic')):
            return unknown('incomplete-function')
        if local['variadic'] != other['variadic'] or len(local['args']) != len(other['args']):
            return False, 'function-argument-count-or-variadic-mismatch'
        unknown_reason = None
        for index, (a, b) in enumerate([(local['return'], other['return'])] + list(zip(local['args'], other['args']))):
            if index and (a in (None, 0) or b in (None, 0)):
                equal, reason = unknown('missing-function-argument-type')
            else:
                equal, reason = compare(a, b, 'return' if not index else 'argument:' + str(index))
            if equal is False:
                return False, 'function-type:' + str(index) + ':' + reason
            if equal is None:
                unknown_reason = 'function-type:' + str(index) + ':' + reason
        return (None, unknown_reason) if unknown_reason else (True, 'matched')
    return unknown('unsupported-kind:' + kind)


def private_type_names(work, component):
    if component == 'iris':
        files = list(BUILD.PINS['iris']['files'])
        paths = [work / name for name in files if name.endswith(('.c', '.h'))]
    else:
        paths = [work / ('fastrpc.c' if component == 'fastrpc' else 'videocc-sm8450.c')]
    names = set()
    for path in paths:
        text = SHAPE.materials.read_regular(path).decode('utf-8')
        names.update(re.findall(r'\b(?:struct|union|enum)\s+([A-Za-z_]\w*)\s*\{', text))
    return names


def import_symbols(ko):
    BUILD.elf_module(ko)
    before = SHAPE.materials.read_regular(ko)
    rows = BUILD.symbol_records(inspect([SHAPE.READ_ELF, '-Ws', str(ko)]))
    if before != SHAPE.materials.read_regular(ko):
        raise ValueError('Module changed during symbol inspection')
    result = {}
    for row in rows:
        if row['index'] == 'UND' and row['name'] and row['binding'] in ('GLOBAL', 'WEAK'):
            previous = result.get(row['name'])
            if previous is not None and previous != row:
                raise ValueError('Conflicting undefined symbol rows')
            result[row['name']] = row
    return result


def named_variants(graphs, name, functions=False):
    result = []
    seen = set()
    for graph in graphs:
        base = getattr(graph, 'base', None)
        if functions and hasattr(graph, 'next_id'):
            # A split FUNC referring to a base prototype belongs to the split
            # input, not to vmlinux. Ignore inherited FUNCs, not new providers.
            ids = [row['type'] for ident, row in graph.records.items()
                   if row['kind'] == 'function-alias' and row.get('name') == name
                   and (base is None or ident >= base.next_id)]
        else:
            ids = graph.functions.get(name, []) if functions else named_ids(graph, AGGREGATES | {'enum', 'typedef'}).get(name, [])
        for ident in ids:
            origin = base if not functions and base is not None and ident < base.next_id else graph
            # Same graph plus identical raw references proves equivalence here;
            # never deduplicate by name, size, or a truncated signature.
            key = (id(origin), repr(origin.records[ident]))
            if key not in seen:
                result.append((origin, ident))
                seen.add(key)
    return result


def variable_evidence(graphs, name):
    result, seen = [], set()
    for graph in graphs:
        for ident in getattr(graph, 'variables', {}).get(name, []):
            base = getattr(graph, 'base', None)
            origin = base if base is not None and ident < base.next_id else graph
            key = (id(origin), ident)
            if key not in seen:
                result.append({**provenance(origin, ident),
                               'variable_type_id': getattr(origin, 'variable_types', {}).get(ident)})
                seen.add(key)
    return result


def unknown_result(reason, local=(), providers=(), category=None, causes=None):
    if causes is None:
        causes = [{'category': category or unknown_category(reason), 'reason': reason,
                   'path': [], 'local': provenance(*local[0]) if local else None,
                   'provider': provenance(*providers[0]) if providers else None}]
    # One actionable witness for each leaf category/reason, not a Cartesian dump.
    roots = {}
    for cause in causes:
        roots.setdefault((cause['category'], cause['reason']), cause)
    return {'status': 'unknown', 'reason': reason,
            'unknown_categories': sorted({key[0] for key in roots}),
            'root_causes': [roots[key] for key in sorted(roots)]}


def compare_variants(local, providers):
    if not local or not providers:
        reason = 'missing-local-declaration' if not local else 'missing-official-information'
        result = unknown_result(reason, local, providers)
        if not local and not providers:
            result['unknown_categories'].append('missing-btf')
            result['root_causes'].extend(unknown_result('missing-official-information')['root_causes'])
        return result
    if len(local) * len(providers) > 256:
        return unknown_result('variant-comparison-budget', local, providers)
    for index, (graph, ident) in enumerate(providers):
        for other, other_id in providers[index + 1:]:
            try:
                equal, reason = compare_type(graph, ident, other, other_id)
            except (ValueError, struct.error, KeyError):
                equal, reason = None, 'incomplete-provider-information'
            if equal is False:
                return unknown_result('ambiguous-official-type:' + reason,
                                      [(graph, ident)], [(other, other_id)])
    unknowns, roots = set(), {}
    # A good candidate cannot hide any incomplete or mismatching local variant.
    for graph, ident in local:
        for other, other_id in providers:
            causes = []
            try:
                equal, reason = compare_type(graph, ident, other, other_id, issues=causes)
            except (ValueError, struct.error, KeyError) as error:
                equal, reason = None, str(error)
                causes = unknown_result(reason, [(graph, ident)], [(other, other_id)])['root_causes']
            if equal is False:
                return {'status': 'mismatch', 'reason': reason,
                        'object': getattr(graph, 'source', ''), 'die': ident,
                        'provider': getattr(other, 'source', ''), 'provider_type': other_id}
            if equal is None:
                unknowns.add(reason)
                for cause in causes:
                    roots.setdefault((cause['category'], cause['reason']), cause)
    if unknowns:
        return unknown_result('; '.join(sorted(unknowns)[:4]), causes=list(roots.values()))
    return {'status': 'verified', 'reason': 'all-variants-matched'}


def comparison_row(name, local, providers):
    return {'name': name, 'local_declarations': [provenance(*item) for item in local],
            'provider_declarations': [provenance(*item) for item in providers],
            **compare_variants(local, providers)}


def summarize_unknowns(report):
    rows = [('type', row) for row in report['types']] + [('symbol', row) for row in report['symbols']]
    rows += [('type', {'name': name, **unknown_result('no-official-counterpart', category='unmatched-local-type')})
             for name in report['ours_only_types']]
    if not report['symbol_crc_verified']:
        rows.append(('gate', {'name': 'symbol-crc', **unknown_result('CRC evidence not verified', category='missing-crc-evidence')}))
    counts = {category: 0 for category in UNKNOWN_ACTIONS}
    roots, unknown_rows = {}, 0
    for scope, row in rows:
        if row['status'] != 'unknown':
            continue
        unknown_rows += 1
        for category in set(row['unknown_categories']):
            counts[category] += 1
        for cause in row['root_causes']:
            key = (cause['category'], cause['reason'])
            if key not in roots:
                roots[key] = {**cause, 'affected_items': 0, 'example': {'scope': scope, 'name': row['name']},
                              'action': UNKNOWN_ACTIONS[cause['category']]}
            roots[key]['affected_items'] += 1
    report['unknown_summary'] = {'unknown_items': unknown_rows,
        'count_unit': 'items per category; categories can overlap', 'counts': counts,
        'root_causes': [roots[key] for key in sorted(roots)]}


def empty_report(component):
    return {'component': component, 'status': 'unknown', 'object_sha256': {},
            'objects_checked': 0, 'shared_type_names': 0, 'matched_type_names': 0,
            'mismatched_type_names': 0, 'undecidable_type_names': 0,
            'private_type_names_excluded': 0, 'ours_only_type_names': 0,
            'mismatches': [], 'undecidable': [], 'types': [], 'functions': [],
            'symbols': [], 'data_symbols': [], 'unclassified_symbols': [], 'ours_only_types': [],
            'object_sources': [], 'provider_inputs': [], 'unknown_summary': {},
            'import_count': 0, 'weak_undefined': [], 'imports_with_official_signature': 0,
            'imports_missing_local_signature': [], 'imports_without_btf': [],
            'imports_crc_only': [], 'import_signature_mismatches': [],
            'symbol_crc_verified': False, 'complete_shared_type_graph_verified': False,
            'all_import_signatures_verified': False, 'complete_abi_verified': False,
            'passed': False, 'hardware_ready': False}


def audit(specification, work, component, headers=None):
    report = empty_report(component)
    ours = graph_objects(work, component)
    official = official_graphs(specification, component)
    if not ours or not official:
        raise ValueError('Empty ABI graph inputs')
    report['object_sha256'] = {graph.source: graph.sha256 for graph in ours}
    report['object_sources'] = [provenance(graph) for graph in ours]
    report['provider_inputs'] = [{**provenance(graph),
        'verification_inputs': getattr(graph, 'verification_inputs', {}),
        'base': provenance(graph.base) if hasattr(graph, 'base') else None} for graph in official]
    report['objects_checked'] = len(ours)
    report['object_list_sha256'] = ours[0].object_list_sha256
    report['object_list_path'] = getattr(ours[0], 'object_list_path', None)
    local_names = set().union(*(named_ids(graph, AGGREGATES | {'enum', 'typedef'}) for graph in ours))
    provider_names = set().union(*(named_ids(graph, AGGREGATES | {'enum', 'typedef'}) for graph in official))
    private = private_type_names(work, component)
    shared = sorted((local_names & provider_names) - private)
    report['private_type_names_excluded'] = len(local_names & private)
    report['ours_only_types'] = sorted(local_names - provider_names - private)
    report['ours_only_type_names'] = len(report['ours_only_types'])
    report['shared_type_names'] = len(shared)
    for name in shared:
        row = comparison_row(name, named_variants(ours, name), named_variants(official, name))
        report['types'].append(row)
    report['matched_type_names'] = sum(row['status'] == 'verified' for row in report['types'])
    report['mismatches'] = [row for row in report['types'] if row['status'] == 'mismatch']
    report['undecidable'] = [row for row in report['types'] if row['status'] == 'unknown']
    report['mismatched_type_names'] = len(report['mismatches'])
    report['undecidable_type_names'] = len(report['undecidable'])
    ko = work / (BUILD.PINS[component]['module'] + '.ko')
    symbols = import_symbols(ko)
    report['weak_undefined'] = sorted(name for name, row in symbols.items() if row['binding'] == 'WEAK')
    strong = {name: row for name, row in symbols.items() if row['binding'] == 'GLOBAL'}
    report['import_count'] = len(strong)
    if headers is not None:
        pin = BUILD.PINS[component]
        for name, digest in BUILD.PINS['official_headers'].items():
            BUILD.require_pin(headers / name, digest, 'headers/' + name)
        gate = BUILD.gate_module(ko, pin['module'], pin['alias'], BUILD.official_symbols(headers), headers)
        report['symbol_crc_verified'] = gate['static_inspection_passed']
        report['module_sha256'] = gate['ko_sha256']
    for name, symbol in sorted(strong.items()):
        local_rows = named_variants(ours, name, True)
        rows = named_variants(official, name, True)
        local_data = variable_evidence(ours, name)
        provider_data = variable_evidence(official, name)
        function_evidence = bool(local_rows or rows or symbol['kind'] == 'FUNC')
        data_evidence = bool(local_data or provider_data or symbol['kind'] == 'OBJECT')
        if function_evidence and data_evidence:
            symbol_kind = 'conflicting'
            result = unknown_result('conflicting-function-and-data-evidence', local_rows, rows,
                                    category='provider-ambiguity')
        elif data_evidence:
            symbol_kind = 'data'
            # Data evidence is not a prototype and is never accepted CRC-only.
            result = unknown_result('unverified-data-symbol-type', category='data-symbol')
            if not provider_data:
                result['unknown_categories'].append('missing-btf')
                result['root_causes'].extend(unknown_result('missing-official-information')['root_causes'])
        elif function_evidence:
            symbol_kind = 'function'
            result = compare_variants(local_rows, rows)
        else:
            symbol_kind = 'unknown'
            result = unknown_result('no-independent-symbol-kind-evidence', category='symbol-kind-unknown')
            result['unknown_categories'].extend(['missing-signature', 'missing-btf'])
            result['root_causes'].extend(compare_variants([], [])['root_causes'])
        row = {'name': name, 'elf_kind': symbol['kind'], 'symbol_kind': symbol_kind,
               'local_declarations': [provenance(*item) for item in local_rows],
               'provider_declarations': [provenance(*item) for item in rows],
               'local_data': local_data, 'provider_data': provider_data, **result}
        # Assembly exports can carry synthesized void(void) BTF prototypes.
        # Without a linked C definition, these are not an authoritative signature.
        if symbol_kind == 'function' and len(rows) == 1 and name in ('__arch_copy_from_user', '__arch_copy_to_user'):
            provider_graph, provider_id = rows[0]
            provider = provider_graph.records[deref(provider_graph, provider_id)]
            if provider['kind'] == 'function' and not provider.get('args') and not provider.get('return'):
                if any(graph.records[deref(graph, ident)].get('args') for graph, ident in local_rows):
                    row.update(unknown_result('unproven-assembly-provider-prototype', local_rows, rows))
        if symbol_kind in ('function', 'unknown') and not local_rows:
            report['imports_missing_local_signature'].append(name)
        if not rows and not provider_data:
            report['imports_without_btf'].append(name)
        report['symbols'].append(row)
        report['functions' if symbol_kind == 'function' else
               'data_symbols' if symbol_kind == 'data' else 'unclassified_symbols'].append(row)
    if report['symbol_crc_verified']:
        report['imports_crc_only'] = sorted(set(report['imports_missing_local_signature']) |
                                           set(report['imports_without_btf']))
    report['imports_with_official_signature'] = sum(row['status'] == 'verified' for row in report['functions'])
    report['import_signature_mismatches'] = [row for row in report['functions'] if row['status'] == 'mismatch']
    type_unknown = report['undecidable'] or report['ours_only_type_names']
    function_unknown = any(row['status'] == 'unknown' for row in report['symbols'])
    mismatch = report['mismatches'] or report['import_signature_mismatches']
    report['complete_shared_type_graph_verified'] = bool(shared) and not type_unknown and not report['mismatches']
    report['all_import_signatures_verified'] = bool(strong) and not function_unknown and not report['import_signature_mismatches']
    report['complete_abi_verified'] = (report['complete_shared_type_graph_verified'] and
                                      report['all_import_signatures_verified'] and
                                      report['symbol_crc_verified'])
    report['status'] = 'mismatch' if mismatch else 'verified' if report['complete_abi_verified'] else 'unknown'
    summarize_unknowns(report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--layout-spec', required=True, type=Path)
    parser.add_argument('--layout-spec-sha256', required=True)
    parser.add_argument('--work', required=True, type=Path)
    parser.add_argument('--component', required=True, choices=sorted(BUILD.SCOPES) if hasattr(BUILD, 'SCOPES') else sorted(SHAPE.SCOPES))
    parser.add_argument('--headers', type=Path, help='Pinned official headers for separate CRC inspection')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    report = empty_report(args.component)
    try:
        raw = SHAPE.materials.pinned_read(args.layout_spec, args.layout_spec_sha256, 65536)
        specification = json.loads(raw, object_pairs_hook=SHAPE.materials.unique_json)
        report = audit(specification, args.work, args.component, args.headers)
    except (OSError, ValueError, RuntimeError, struct.error, KeyError) as error:
        report.update(status='error', error=str(error))
    text = json.dumps(report, indent=2) + '\n'
    if args.out:
        args.out.write_text(text)
    print(text)
    return 0 if report['complete_abi_verified'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
