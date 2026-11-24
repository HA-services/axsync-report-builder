"""SQL generation for Report Builder reports.

Field chain reports are turned into one SELECT with LEFT JOINs following the
chain of relational fields. Custom query reports wrap the user query.
"""
import re

from odoo.exceptions import UserError, ValidationError
from odoo.tools.translate import _

from .compat import literal, sql_text, tr

# field types usable at the end of a chain -> report field type
RESULT_TYPES = {
    'char': 'char', 'text': 'text', 'html': 'text', 'integer': 'integer', 'float': 'float',
    'monetary': 'float', 'date': 'date', 'datetime': 'datetime', 'boolean': 'boolean',
    'selection': 'selection', 'many2one': 'many2one', 'one2many': 'char', 'many2many': 'char',
    'many2one_reference': 'integer',
}
NUMERIC = ('integer', 'float', 'monetary')
DATES = ('date', 'datetime')

ALTER_NUMERIC = {
    'multiply': '(({expr}) * {value})',
    'divide': '(({expr}) / NULLIF({value}, 0))',
    'add': '(({expr}) + {value})',
    'subtract': '(({expr}) - {value})',
    'round': 'ROUND(({expr})::numeric, {digits})',
    'abs': 'ABS({expr})',
    'negate': '(-({expr}))',
}
ALTER_DATE = {
    'trunc_week': "date_trunc('week', {expr})", 'trunc_month': "date_trunc('month', {expr})",
    'trunc_quarter': "date_trunc('quarter', {expr})", 'trunc_year': "date_trunc('year', {expr})",
    'add_days': "(({expr}) + ({value}) * interval '1 day')",
}

FORBIDDEN = re.compile(
    r'\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|copy|vacuum|analyze|cluster|'
    r'reindex|lock|listen|notify|unlisten|execute|call|do|prepare|deallocate|set|reset|refresh|comment|'
    r'security|discard|load|import|checkpoint|begin|commit|rollback|savepoint|release|'
    r'pg_sleep\w*|pg_terminate_backend|pg_cancel_backend|pg_read_\w+|pg_ls_dir|pg_stat_file|lo_\w+|'
    r'dblink\w*|set_config|pg_reload_conf|pg_rotate_logfile|current_setting|query_to_xml\w*|txid_\w+|'
    r'pg_advisory\w*|nextval|setval)\b', re.IGNORECASE)
PLACEHOLDER = re.compile(r'\{\{\s*(uid|company_id|company_ids|lang|today)\s*\}\}')


def quote(name):
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', name or ''):
        raise ValidationError(_("Invalid SQL identifier: %s", name))
    return '"%s"' % name


def strip_sql(text):
    """Remove comments and string literals (to inspect the SQL structure)."""
    text = re.sub(r'--[^\n]*', ' ', text)
    text = re.sub(r'/\*.*?\*/', ' ', text, flags=re.S)
    text = re.sub(r"'(?:[^']|'')*'", "''", text)
    text = re.sub(r'\$([A-Za-z_]*)\$.*?\$\1\$', "''", text, flags=re.S)
    text = re.sub(r'"(?:[^"]|"")*"', '"x"', text)
    return text


def validate_custom_query(query, env=None):
    """Raise ValidationError if the query is not a single read-only SELECT."""
    text = (query or '').strip().rstrip(';').strip()
    if not text:
        raise ValidationError(tr(env, "Enter a SQL query."))
    structure = strip_sql(text)
    if ';' in structure:
        raise ValidationError(tr(env, "Only one SQL statement is allowed."))
    if not re.match(r'^\s*\(*\s*(select|with)\b', structure, re.IGNORECASE):
        raise ValidationError(tr(env, "The query must start with SELECT or WITH."))
    found = FORBIDDEN.search(structure)
    if found:
        raise ValidationError(tr(env, "The query cannot use '%s': reports are read-only.", found.group(0)))
    if re.search(r'\bfor\s+(update|share|no\s+key|key)\b', structure, re.IGNORECASE):
        raise ValidationError(tr(env, "Locking clauses are not allowed in reports."))
    return text


def render_placeholders(cr, query, env):
    """Replace {{uid}}, {{company_id}}, {{company_ids}}, {{lang}}, {{today}} by SQL literals."""
    from odoo import fields

    def repl(match):
        key = match.group(1)
        if key == 'uid':
            return str(int(env.uid))
        if key == 'company_id':
            return str(int(env.company.id))
        if key == 'company_ids':
            ids = env.companies.ids or [env.company.id]
            return '(%s)' % ','.join(str(int(i)) for i in ids)
        if key == 'lang':
            return literal(cr, env.lang or 'en_US')
        return literal(cr, fields.Date.to_string(fields.Date.context_today(env['res.users'])))
    return PLACEHOLDER.sub(repl, query)


class ChainBuilder:
    """Resolve field chains (``partner_id.country_id.name``) into SQL expressions and joins."""

    def __init__(self, env, model_name, lang):
        if model_name not in env:
            raise UserError(tr(self.env, "Unknown model %s.", model_name))
        self.env = env
        self.cr = env.cr
        self.base = env[model_name]
        self.lang = lang or 'en_US'
        self.joins = []
        self.aliases = {(): 'b'}
        self.to_many_aliases = []
        self.counter = 0

    # ------------------------------------------------------------------
    def table_sql(self, Model):
        """FROM item of a model: its table, or its SQL query for SQL-view models."""
        if hasattr(type(Model), '_table_query'):
            query = Model._table_query
        else:
            # Odoo 20: `_table_sql` (an identifier for regular models)
            sql = Model._table_sql
            code = sql_text(self.cr, sql).strip()
            return code if code.startswith('(') else quote(Model._table)
        if query and not isinstance(query, property):
            if not isinstance(query, str):  # SQL object (Odoo 17+)
                query = sql_text(self.cr, query)
            return '(%s)' % query
        return quote(Model._table)

    def _new_alias(self):
        self.counter += 1
        return 'j%d' % self.counter

    def _join(self, path, alias, Model, field):
        """Join the comodel of `field` (relational) from `alias`, reuse joins of the same path."""
        key = path + (field.name,)
        if key in self.aliases:
            return self.aliases[key]
        Comodel = self.env[field.comodel_name]
        new = self._new_alias()
        table = self.table_sql(Comodel)
        if field.type == 'many2one':
            self.joins.append('LEFT JOIN %s AS %s ON %s.id = %s.%s' % (table, new, new, alias, quote(field.name)))
        elif field.type == 'one2many':
            inverse = Comodel._fields.get(field.inverse_name)
            if not inverse or not inverse.store:
                raise ValidationError(tr(self.env, "The field %s cannot be followed in a report.", field.name))
            self.joins.append('LEFT JOIN %s AS %s ON %s.%s = %s.id' % (table, new, new, quote(inverse.name), alias))
            self.to_many_aliases.append(new)
        elif field.type == 'many2many':
            rel = self._new_alias()
            self.joins.append('LEFT JOIN %s AS %s ON %s.%s = %s.id' % (
                quote(field.relation), rel, rel, quote(field.column1), alias))
            self.joins.append('LEFT JOIN %s AS %s ON %s.id = %s.%s' % (table, new, new, rel, quote(field.column2)))
            self.to_many_aliases.append(new)
        else:
            raise ValidationError(tr(self.env, "The field %s is not relational.", field.name))
        self.aliases[key] = new
        return new

    def _column(self, alias, field):
        if getattr(field, 'company_dependent', False):
            raise ValidationError(tr(self.env, "Company dependent fields (%s) cannot be used in reports.", field.string))
        if not field.store or not field.column_type:
            raise ValidationError(tr(self.env, "The field '%s' is not stored in the database and cannot be used in a report.",
                                    field.string))
        column = '%s.%s' % (alias, quote(field.name))
        if field.translate:
            return "COALESCE(%s->>%s, %s->>'en_US')" % (column, literal(self.cr, self.lang), column)
        return column

    def rec_name_expr(self, Model, alias):
        rec = Model._rec_name and Model._fields.get(Model._rec_name)
        if rec and rec.store and rec.column_type and not getattr(rec, 'company_dependent', False) \
                and rec.type in ('char', 'text', 'html', 'selection'):
            return self._column(alias, rec)
        return '%s.id::text' % alias

    def _x2many_list(self, alias, field):
        Comodel = self.env[field.comodel_name]
        table = self.table_sql(Comodel)
        name = self.rec_name_expr(Comodel, 'c')
        if field.type == 'one2many':
            inverse = Comodel._fields.get(field.inverse_name)
            if not inverse or not inverse.store:
                raise ValidationError(tr(self.env, "The field %s cannot be used in a report.", field.name))
            return "(SELECT string_agg(%s, ', ' ORDER BY c.id) FROM %s AS c WHERE c.%s = %s.id)" % (
                name, table, quote(inverse.name), alias)
        return "(SELECT string_agg(%s, ', ' ORDER BY c.id) FROM %s AS r JOIN %s AS c ON c.id = r.%s WHERE r.%s = %s.id)" % (
            name, quote(field.relation), table, quote(field.column2), quote(field.column1), alias)

    def resolve(self, chain, x2many_mode='list'):
        """Return dict(expr, type, field, comodel, selection) for a field chain."""
        parts = [p for p in (chain or '').strip().split('.') if p]
        if not parts:
            raise ValidationError(tr(self.env, "Choose a field."))
        Model, alias, path = self.base, 'b', ()
        index = 0
        while index < len(parts):
            name = parts[index]
            field = Model._fields.get(name)
            if not field:
                raise ValidationError(tr(self.env, "Field '%(field)s' does not exist on %(model)s.", field=name, model=Model._description))
            if getattr(field, 'inherited', False):
                # field of a parent model (_inherits): go through the parent link
                related = field.related.split('.') if isinstance(field.related, str) else list(field.related)
                parts[index:index + 1] = related
                continue
            last = index == len(parts) - 1
            if last:
                if field.type in ('one2many', 'many2many'):
                    if x2many_mode == 'rows':
                        alias = self._join(path, alias, Model, field)
                        Comodel = self.env[field.comodel_name]
                        return {'expr': self.rec_name_expr(Comodel, alias), 'type': 'char', 'field': field,
                                'comodel': False, 'selection': False}
                    return {'expr': self._x2many_list(alias, field), 'type': 'char', 'field': field,
                            'comodel': False, 'selection': False}
                ftype = RESULT_TYPES.get(field.type)
                if not ftype:
                    raise ValidationError(tr(self.env, "Fields of type %s cannot be used in a report.", field.type))
                selection = False
                if field.type == 'selection':
                    try:
                        selection = field._description_selection(self.env)
                    except Exception:  # noqa: BLE001 - dynamic selection depending on a record
                        selection = False
                    if not selection:
                        ftype = 'char'
                return {'expr': self._column(alias, field), 'type': ftype, 'field': field,
                        'comodel': field.comodel_name if field.type == 'many2one' else False,
                        'selection': selection}
            if field.type not in ('many2one', 'one2many', 'many2many'):
                raise ValidationError(tr(self.env, "'%s' is not a relational field: it must be the last element of the chain.", name))
            if field.type == 'many2one' and (not field.store or not field.column_type):
                raise ValidationError(tr(self.env, "The field '%s' is not stored and cannot be followed.", field.string))
            alias = self._join(path, alias, Model, field)
            path = path + (field.name,)
            Model = self.env[field.comodel_name]
            index += 1
        raise ValidationError(tr(self.env, "Invalid field chain: %s", chain))

    def from_clause(self):
        return '%s AS b %s' % (self.table_sql(self.base), ' '.join(self.joins))


def apply_alter(expr, ftype, op, value, env=None):
    """Wrap a SQL expression with an 'Alter Values' transformation."""
    if not op or op == 'none':
        return expr, ftype
    number = repr(float(value or 0.0))
    if op in ALTER_NUMERIC:
        if ftype not in NUMERIC:
            raise ValidationError(tr(env, "This transformation only applies to numbers."))
        new = ALTER_NUMERIC[op].format(expr=expr, value=number, digits=int(value or 0))
        out_type = 'integer' if ftype == 'integer' and op in ('multiply', 'add', 'subtract', 'abs', 'negate') \
            and float(value or 0).is_integer() else 'float'
        if op in ('abs', 'negate'):
            out_type = ftype if ftype != 'monetary' else 'float'
        return new, out_type
    if op in ALTER_DATE:
        if ftype not in DATES:
            raise ValidationError(tr(env, "This transformation only applies to dates."))
        new = ALTER_DATE[op].format(expr=expr, value=number)
        if op.startswith('trunc_'):
            # periods are calendar days: return a date, also for date & time fields
            return '(%s)::date' % new, 'date'
        cast = '::date' if ftype == 'date' else '::timestamp'
        return '(%s)%s' % (new, cast), ftype
    raise ValidationError(tr(env, "Unknown transformation %s.", op))
