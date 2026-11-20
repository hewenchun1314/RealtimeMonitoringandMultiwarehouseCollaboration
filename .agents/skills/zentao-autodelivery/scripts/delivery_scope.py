"""Determine a work item's delivery scope from its admission and reviewed material."""

import hashlib
import re
from graphlib import CycleError, TopologicalSorter
from pathlib import Path


SCOPES = {'纯前端': 'frontend_only', '纯后端': 'backend_only', '前后端': 'fullstack'}
SIDE_STEPS = ('design', 'plan', 'branch_check', 'coding', 'unit_test', 'code_review')
PREFIX = ('material_check', 'split')


def project_mode(development):
    types = development.get('allowed_requirement_types', ['frontend_only'])
    if isinstance(types, list) and all(isinstance(t, str) for t in types):
        if types in (['frontend_only'], ['backend_only']):
            return types[0]
        if len(types) == 3 and set(types) == set(SCOPES.values()):
            return 'fullstack'
    raise ValueError('Invalid development.allowed_requirement_types')


def route_stages(scope, *, e2e_enabled=True):
    if scope not in SCOPES.values():
        raise ValueError('Unknown delivery scope')
    stages = list(PREFIX)
    if scope in ('backend_only', 'fullstack'):
        stages.extend('backend.' + step for step in (*SIDE_STEPS, 'acceptance', 'integration'))
    if scope == 'fullstack':
        stages.append('handoff')
    if scope in ('frontend_only', 'fullstack'):
        stages.extend('frontend.' + step for step in SIDE_STEPS)
        if e2e_enabled:
            stages.append('frontend.e2e')
        stages.append('frontend.integration')
    return tuple(stages)


def _markers(body):
    values = []
    fence = ''
    for raw in body.splitlines():
        line = raw.lstrip(' ')
        if len(raw) - len(line) > 3 or line.startswith('\t'):
            continue
        token = re.match(r'^(`{3,}|~{3,})', line)
        if fence:
            if token and token[1][0] == fence[0] and len(token[1]) >= len(fence) and not line[token.end():].strip():
                fence = ''
        elif token:
            fence = token[1]
        else:
            match = re.fullmatch(r'研发范围\s*[:：]\s*(.*)', line)
            if match:
                values.append(match[1].strip())
    return values


def read_material(config, config_directory, kind, evidence):
    if not isinstance(evidence, dict):
        raise ValueError('Material review evidence required')
    if kind == 'bug':
        body = evidence.get('detail')
        if not isinstance(body, str) or not body.strip():
            raise ValueError('Bug review requires detail from the reported bug')
        raw = body.encode('utf-8')
        source = 'bug.detail'
    else:
        workspace = config.get('workspaces', {}).get('requirement')
        value = evidence.get('path')
        if not isinstance(workspace, str) or not isinstance(value, str) or not value.strip():
            raise ValueError('Reviewed intent.md path and workspaces.requirement required')
        root = (Path(config_directory) / workspace).resolve()
        path = (root / value).resolve()
        if not path.is_relative_to(root) or path.name != 'intent.md':
            raise ValueError('Reviewed material must be intent.md under the requirement workspace')
        try:
            raw = path.read_bytes()
            body = raw.decode('utf-8-sig')
        except (OSError, UnicodeError) as exc:
            raise ValueError(f'Cannot read reviewed requirement material: {exc}') from exc
        source = path.relative_to(root).as_posix()
    return {'path': source, 'sha256': hashlib.sha256(raw).hexdigest(), 'markers': _markers(body)}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _strings(value):
    return isinstance(value, list) and all(_text(entry) for entry in value)


def _validate_tasks(tasks, scope, blockers):
    if not isinstance(tasks, dict) or set(tasks) - {'frontend', 'backend'}:
        raise ValueError('tasks must be a mapping keyed by frontend/backend')
    owners, graph = {}, {}
    for side in ('backend', 'frontend'):
        entries = tasks.get(side, [])
        active = scope in ('fullstack', side + '_only')
        if not isinstance(entries, list) or (entries and not active) or (active and not entries and not blockers):
            raise ValueError(f'Invalid or missing {side} tasks for {scope}')
        for task in entries:
            if not isinstance(task, dict) or not _text(task.get('description')) or not _text(task.get('id')):
                raise ValueError('Each task requires id and description')
            task_id, deps = task['id'], task.get('depends_on', [])
            if task_id in owners or not _strings(deps) or not _strings(task.get('acceptance')) or not task['acceptance']:
                raise ValueError('Each task needs a unique id, acceptance criteria and valid dependencies')
            owners[task_id], graph[task_id] = side, deps
    for task_id, deps in graph.items():
        for dep in deps:
            if dep not in owners or (owners[task_id] == 'backend' and owners[dep] == 'frontend'):
                raise ValueError(f'Task dependency is unavailable before execution: {task_id} -> {dep}')
    try:
        tuple(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise ValueError('Cyclic task dependency') from exc


def resolve_split(config, evidence):
    """Use material read at review time; the caller supplies that trusted snapshot."""
    if not isinstance(evidence, dict):
        raise ValueError('Split evidence required')
    material = evidence.get('material')
    if not isinstance(material, dict) or not _text(material.get('path')) or not isinstance(material.get('markers'), list):
        raise ValueError('Reviewed material required before scope selection')
    markers = material['markers']
    mode = project_mode(config['development'])
    scope, source = mode, 'config'
    rationale = '项目配置限定执行范围'
    if mode == 'fullstack' and markers:
        if not all(isinstance(marker, str) and marker in SCOPES for marker in markers) or len(set(markers)) != 1:
            raise ValueError(f'Invalid or conflicting 研发范围 in {material["path"]}: {markers!r}')
        scope, source, rationale = SCOPES[markers[0]], 'document', '需求材料明确标记研发范围'
    elif mode == 'fullstack':
        scope, source, rationale = evidence.get('ai_scope'), 'ai', evidence.get('rationale')
        if scope not in SCOPES.values() or not _text(rationale):
            raise ValueError('Unmarked material requires ai_scope and rationale')
    blockers = evidence.get('blockers', [])
    if not _strings(blockers):
        raise ValueError('blockers must list unmet acceptance conditions')
    handoff_blockers = evidence.get('handoff_blockers', blockers if scope == 'fullstack' else [])
    if (not _strings(handoff_blockers) or set(handoff_blockers) - set(blockers)
            or (handoff_blockers and scope != 'fullstack')):
        raise ValueError('handoff_blockers must be cross-side dependencies already listed in blockers')
    tasks = evidence.get('tasks')
    _validate_tasks(tasks, scope, blockers)
    contract = evidence.get('interface_contract')
    if scope == 'fullstack' and not _text(contract):
        raise ValueError('Cross-side delivery requires interface_contract')
    return {'scope': scope, 'source': source, 'material': material, 'rationale': rationale,
            'tasks': tasks, 'blockers': blockers, 'handoff_blockers': handoff_blockers, 'interface_contract': contract}
