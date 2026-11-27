"""Complete, read-only project/product union and fresh candidate verification."""

from urllib.parse import urlencode

from intake_scope import matches_source, object_id, product_scope, source_of


def pages(session, path, key, proof):
    result, seen, expected = [], set(), None
    for number in range(1, 1001):
        data = session._v2('GET', path + '?' + urlencode({
            'browseType': 'all', 'orderBy': 'id_asc', 'pageID': number, 'recPerPage': 100}))
        pager, values = data.get('pager'), data.get(key)
        if not isinstance(pager, dict) or not isinstance(values, list) or int(pager['pageID']) != number:
            raise ValueError('Pagination could not be verified')
        total = int(pager['recTotal'])
        if total < 0 or (expected is not None and expected != total):
            raise ValueError('Pagination total changed')
        expected = total
        proof.append({'path': path, 'page': number, 'received': len(values), 'total': total})
        for value in values:
            identity = object_id(value.get('id'))
            if identity == '0' or identity in seen:
                raise ValueError('Invalid or duplicate page identity')
            seen.add(identity)
            result.append(value)
        if len(result) == total:
            return result
        if not values or len(result) > total:
            break
    raise ValueError('Incomplete pagination')


def normalized(kind, row):
    assigned = row.get('assignedTo')
    if isinstance(assigned, dict):
        assigned = assigned.get('account')
    if assigned is None:
        assigned = ''
    if not isinstance(assigned, str):
        raise ValueError('Invalid assignee in ZenTao response')
    return {'kind': kind, 'id': object_id(row.get('id')), 'title': row['title'],
            'assigned_to': assigned, 'status': row['status'], 'priority': row.get('pri'),
            'source': source_of(row, project_optional=kind == 'story'),
            'deleted': str(row.get('deleted', '0')) not in ('0', 'False')}


def eligible(zentao, project_id, row):
    return (not row['deleted'] and matches_source(zentao, project_id, row['source']) and
            row['assigned_to'] in zentao['assigned_to_accounts'] and
            row['status'] in zentao['accepted_statuses'][row['kind']])


def collect_scan(config, session, *, scan_id, observed_at, expected_account, proof=None):
    zentao = config['zentao']
    if proof is None:
        proof = {}
    proof.update(scan_id=scan_id, complete=False, pages=[], counts={}, eligible_ids={})
    profile = session._v2('GET', '/my/profile')
    if profile.get('user', profile).get('account') != expected_account:
        raise ValueError('Authenticated account mismatch')
    projects = pages(session, '/projects', 'projects', proof['pages'])
    selected = [row for row in projects if row.get('name') == zentao['project_name']]
    if len(selected) != 1:
        raise ValueError('Configured project is not unique')
    project_id = object_id(selected[0]['id'])
    proof.update(project_id=project_id, project_count=len(projects), account_verified=True, account=expected_account)
    scopes = [('projects', project_id)]
    if product_scope(zentao):
        products = pages(session, '/products', 'products', proof['pages'])
        selected = [row for row in products if object_id(row.get('id')) == str(zentao['product_id'])
                    and row.get('name') == zentao['product_name']]
        if len(selected) != 1:
            raise ValueError('Configured product identity mismatch')
        scopes.append(('products', object_id(selected[0]['id'])))
        proof['product_id'] = scopes[-1][1]
    matches = []
    for kind, statuses in zentao['accepted_statuses'].items():
        if not statuses:
            continue
        plural = {'story': 'stories', 'bug': 'bugs'}[kind]
        union = {}
        for owner_type, owner_id in scopes:
            path = f'/{owner_type}/{owner_id}/{plural}'
            values = pages(session, path, plural, proof['pages'])
            for value in values:
                row = normalized(kind, value)
                field = 'project_id' if owner_type == 'projects' else 'product_id'
                if kind == 'story' and owner_type == 'projects' and row['source']['project_id'] is None:
                    row['source']['project_id'] = owner_id
                if row['source'][field] != owner_id:
                    raise ValueError('List contains an item outside its actual owner')
                previous = union.get(row['id'])
                if previous is not None and kind == 'story':
                    # Enrich the product list's unreported project from the
                    # verified project list; all other fields must still agree.
                    if row['source']['project_id'] is None:
                        row['source']['project_id'] = previous['source']['project_id']
                if previous is not None and previous != row:
                    raise ValueError('Item changed between project and product reads')
                union[row['id']] = row
        proof['counts'][kind] = len(union)
        selected = [row for row in union.values() if eligible(zentao, project_id, row)]
        proof['eligible_ids'][kind] = [row['id'] for row in selected]
        matches.extend({key: value for key, value in row.items() if key != 'deleted'} for row in selected)
    request = {'action': 'scan', 'scan_id': scan_id, 'observed_at': observed_at, 'complete': True,
               'instance': zentao['base_url'], 'project_id': project_id,
               'project_name': zentao['project_name'], 'items': matches}
    proof['complete'] = True
    return request, proof


def verify_candidate(config, session, item):
    kind = item['kind']
    plural = {'story': 'stories', 'bug': 'bugs'}[kind]
    data = session._v2('GET', f'/{plural}/{item["id"]}')
    original = data.get(kind, data)
    row = normalized(kind, original)
    if row['id'] != str(item['id']) or item['instance'].rstrip('/') != config['zentao']['base_url'].rstrip('/'):
        raise ValueError('Candidate detail identity mismatch')
    if (kind == 'story' and row['source']['project_id'] is None and
            not matches_source(config['zentao'], item['project_id'], row['source'])):
        members = pages(session, f'/projects/{item["project_id"]}/stories', 'stories', [])
        if any(object_id(member.get('id')) == row['id'] for member in members):
            row['source']['project_id'] = str(item['project_id'])
    admitted = eligible(config['zentao'], item['project_id'], row)
    result = {**{key: value for key, value in row.items() if key != 'deleted'}, 'eligible': admitted}
    if admitted:
        fields = ('title', 'spec', 'verify') if kind == 'story' else ('title', 'steps')
        materials = {key: original[key] for key in fields if isinstance(original.get(key), str)}
        files = original.get('files') or []
        if isinstance(files, dict):
            files = files.values()
        materials['files'] = [{'id': str(f['id']), 'title': f['title']} for f in files
                              if isinstance(f, dict) and str(f.get('id', '')).isdecimal()
                              and isinstance(f.get('title'), str)]
        result['materials'] = materials
    return result
