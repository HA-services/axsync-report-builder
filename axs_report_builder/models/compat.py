"""Helpers hiding API differences between Odoo 16 and Odoo 20."""
import base64


def can_access(model, operation):
    if hasattr(model, 'has_access'):  # Odoo 18+
        return model.browse().has_access(operation)
    return model.check_access_rights(operation, raise_exception=False)


def check_access(records, operation):
    if hasattr(records, 'check_access'):
        records.check_access(operation)
    else:
        records.check_access_rights(operation)
        records.check_access_rule(operation)


def sql_text(cr, sql):
    """Literal SQL text of an Odoo SQL object (`.code`/`.params` up to 19, `_sql_tuple` in 20)."""
    if hasattr(sql, '_sql_tuple'):
        return cr.mogrify(sql).decode()
    return cr.mogrify(sql.code, sql.params).decode()


def query_to_sql(cr, query):
    """Parenthesized literal SQL selecting the ids of an ORM Query, usable after ``IN``.

    Odoo 16 returns a (code, params) tuple without parentheses, Odoo 17+ an SQL
    object with parentheses (or the literal id tuple when the ids are known).
    """
    res = query.subselect()
    if isinstance(res, tuple):
        code, params = res
        return '(%s)' % cr.mogrify(code, params).decode()
    sql = sql_text(cr, res).strip()
    return sql if sql.startswith('(') else '(%s)' % sql


def literal(cr, value):
    return cr.mogrify('%s', [value]).decode()


def clear_caches(env):
    """Clear the ORM caches (menus, views, access rights) after publishing."""
    registry = env.registry
    if hasattr(env, 'transaction') and hasattr(env.transaction, 'invalidate_ormcache'):  # Odoo 20
        env.transaction.invalidate_ormcache('stable')
        if hasattr(env.transaction, 'invalidate_access_cache'):
            env.transaction.invalidate_access_cache()
    elif hasattr(registry, 'clear_cache'):  # Odoo 17 to 19
        registry.clear_cache()
    else:  # Odoo 16
        registry.clear_caches()


def binary_content(value):
    if not value:
        return b''
    if hasattr(value, 'content'):  # Odoo 20 BinaryValue
        return value.content
    return base64.b64decode(value)


def binary_value(content):
    """Value to write in a Binary field from raw bytes."""
    return base64.b64encode(content).decode()


def get_param(env, key, default=None):
    icp = env['ir.config_parameter'].sudo()
    if hasattr(icp, 'get_str'):
        value = icp.get_str(key, default)
        return default if value in ('', None) else value
    return icp.get_param(key, default)


def tr(env, source, *args, **kwargs):
    """Translate with the environment language (Odoo 18+ ``env._``), or the classic ``_``."""
    if env is not None and hasattr(env, '_'):
        return env._(source, *args, **kwargs)
    from odoo.tools.translate import _
    return _(source, *args, **kwargs)
