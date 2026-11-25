"""Source ownership is distinct from the automation's stable queue project."""


def product_scope(zentao):
    return zentao.get('scope_mode', 'project') == 'project_or_product'


def object_id(value):
    if isinstance(value, dict):
        value = value.get('id')
    if isinstance(value, bool) or not str(value).isascii() or not str(value).isdecimal():
        raise ValueError('Source ownership requires an explicit non-negative ID')
    return str(int(value))


def source_of(row, *, project_optional=False):
    # Product Story lists do not expose a singular project: Stories can have
    # multiple project associations. None means unreported, not project zero.
    project = None if project_optional and row.get('project') is None else object_id(row.get('project'))
    return {'project_id': project, 'product_id': object_id(row.get('product'))}


def normalize_source(source):
    if not isinstance(source, dict):
        raise ValueError('Source ownership must be an object')
    if 'project_id' not in source or 'product_id' not in source:
        raise ValueError('Source ownership requires both fields')
    return {'project_id': None if source['project_id'] is None else object_id(source['project_id']),
            'product_id': object_id(source['product_id'])}


def matches_source(zentao, project_id, source):
    source = normalize_source(source)
    return (source['project_id'] == str(project_id) or
            (product_scope(zentao) and source['product_id'] == str(zentao['product_id'])))
