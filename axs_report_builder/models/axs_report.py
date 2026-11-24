import io
import json
import logging
import re
from datetime import timedelta

from markupsafe import Markup, escape

from odoo import api, fields, models, release
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools.misc import format_date, format_datetime, formatLang
from odoo.tools.safe_eval import safe_eval
from odoo.tools.translate import _

from .compat import clear_caches, query_to_sql
from .ir_model import REPORT_PREFIX
from .sql_builder import (
    ChainBuilder, apply_alter, quote, render_placeholders, validate_custom_query,
)

_logger = logging.getLogger(__name__)

ODOO_VERSION = release.version_info[0]
LIST_TAG = 'list' if ODOO_VERSION >= 18 else 'tree'
PG_TYPES = {
    16: 'boolean', 20: 'integer', 21: 'integer', 23: 'integer', 26: 'integer', 700: 'float', 701: 'float',
    1700: 'float', 790: 'float', 1082: 'date', 1114: 'datetime', 1184: 'datetime', 25: 'char', 1043: 'char',
    1042: 'char', 19: 'char',
}
SQL_CAST = {
    'integer': 'integer', 'float': 'double precision', 'date': 'date', 'datetime': 'timestamp',
    'boolean': 'boolean', 'char': 'varchar', 'text': 'text', 'selection': 'varchar', 'many2one': 'integer',
}
EMPTY_QUERY_LIMIT = 'SELECT NULL::integer AS id WHERE FALSE'


class AxsReport(models.Model):
    _name = 'axs.report'
    _description = 'Report Builder Report'
    _order = 'sequence, name'

    name = fields.Char("Report Name", required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    description = fields.Text("Description", translate=True,
                              help="Shown to users when the report has no data.")
    company_id = fields.Many2one('res.company', "Company",
                                 help="Leave empty to share the report definition between companies.")
    user_id = fields.Many2one('res.users', "Responsible", default=lambda self: self.env.user,
                              help="Scheduled emails are generated with the access rights of this user.")
    state = fields.Selection([('draft', 'Draft'), ('published', 'Published')], default='draft', copy=False,
                             readonly=True)
    query_type = fields.Selection([('field_chain', 'Field Chain'), ('custom_query', 'Custom Query')],
                                  default='field_chain', required=True, string="Query Type")
    # field chain
    model_id = fields.Many2one('ir.model', "Model", ondelete='cascade',
                               domain="[('transient', '=', False), ('model', 'not like', 'x_axs_rpt_%')]")
    base_model = fields.Char(related='model_id.model', string="Model Name")
    domain = fields.Char("Records", default='[]', help="Only the records matching this filter are reported.")
    include_archived = fields.Boolean("Include Archived Records")
    apply_record_rules = fields.Boolean(
        "Apply Access Rules", default=True,
        help="Each user only sees the records of the model that his access rules allow (companies, teams...). "
             "Uncheck to show every record to the users who can open the report.")
    # custom query
    query_text = fields.Text("Custom Query",
                             help="A single SELECT (or WITH ... SELECT). Placeholders: {{uid}}, {{company_id}}, "
                                  "{{company_ids}}, {{lang}}, {{today}}.")
    line_ids = fields.One2many('axs.report.line', 'report_id', "Columns", copy=True)
    # menu
    menu_name = fields.Char("Menu Name", translate=True)
    parent_menu_id = fields.Many2one('ir.ui.menu', "Parent Menu", ondelete='set null',
                                     default=lambda self: self.env.ref('axs_report_builder.menu_reports_root',
                                                                       raise_if_not_found=False))
    menu_sequence = fields.Integer("Menu Priority", default=10, help="Lower numbers come first in the menu.")
    group_ids = fields.Many2many('res.groups', 'axs_report_group_rel', 'report_id', 'group_id', "Access Groups",
                                 help="Groups allowed to open the report. Empty: every internal user.")
    # views
    show_pivot = fields.Boolean("Pivot View", default=True)
    show_graph = fields.Boolean("Graph View", default=True)
    graph_type = fields.Selection([('bar', 'Bar'), ('line', 'Line'), ('pie', 'Pie')], default='bar',
                                  string="Graph Type")
    graph_stacked = fields.Boolean("Stacked")
    default_view = fields.Selection([('list', 'List'), ('pivot', 'Pivot'), ('graph', 'Graph')], default='list',
                                    string="Opens On")
    order_line_id = fields.Many2one('axs.report.line', "Sort By", domain="[('report_id', '=', id)]",
                                    ondelete='set null')
    order_desc = fields.Boolean("Descending")
    # generated objects
    model_name = fields.Char("Report Model", readonly=True, copy=False, index=True)
    report_model_id = fields.Many2one('ir.model', "Generated Model", readonly=True, copy=False, ondelete='set null')
    action_id = fields.Many2one('ir.actions.act_window', readonly=True, copy=False, ondelete='set null')
    menu_id = fields.Many2one('ir.ui.menu', readonly=True, copy=False, ondelete='set null')
    view_ids = fields.Many2many('ir.ui.view', 'axs_report_view_rel', 'report_id', 'view_id', readonly=True,
                                copy=False)
    published_date = fields.Datetime(readonly=True, copy=False)
    preview_html = fields.Html("Preview", readonly=True, sanitize=False, copy=False)
    # scheduled email
    email_enabled = fields.Boolean("Send by Email", copy=False)
    email_interval = fields.Selection([('daily', 'Every day'), ('weekly', 'Every week'), ('monthly', 'Every month')],
                                      default='weekly', string="Frequency")
    email_next = fields.Datetime("Next Sending", copy=False)
    email_partner_ids = fields.Many2many('res.partner', 'axs_report_partner_rel', 'report_id', 'partner_id',
                                         "Recipients")
    email_row_limit = fields.Integer("Rows in the Excel File", default=5000)
    email_last_sent = fields.Datetime("Last Sent", readonly=True, copy=False)

    # ------------------------------------------------------------------
    # constraints
    # ------------------------------------------------------------------
    @api.constrains('query_type', 'model_id', 'query_text', 'domain')
    def _check_definition(self):
        for report in self:
            if report.query_type == 'field_chain':
                if not report.model_id:
                    raise ValidationError(_("Choose the model of the report."))
                try:
                    domain = safe_eval(report.domain or '[]')
                except Exception as e:  # noqa: BLE001 - invalid user input
                    raise ValidationError(_("Invalid record filter: %s", e))
                if not isinstance(domain, (list, tuple)):
                    raise ValidationError(_("The record filter must be a list."))
            elif report.query_text:
                validate_custom_query(report.query_text, report.env)

    def _check_custom_query_rights(self, vals=None):
        """Custom SQL can read any table: only Settings administrators may write or run it."""
        if self.env.su or self.env.user.has_group('base.group_system'):
            return
        if vals is None:
            needed = any(r.query_type == 'custom_query' for r in self)
        else:
            needed = vals.get('query_type') == 'custom_query' or bool(vals.get('query_text'))
        if needed:
            raise AccessError(_("Only administrators (Settings access) can write custom SQL queries."))

    # ------------------------------------------------------------------
    # ORM
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('query_type') == 'custom_query' or vals.get('query_text'):
                self._check_custom_query_rights(vals)
        reports = super().create(vals_list)
        for report in reports:
            report.model_name = '%s%d' % (REPORT_PREFIX, report.id)
        return reports

    def write(self, vals):
        self._check_custom_query_rights(vals)
        res = super().write(vals)
        structural = set(vals) - {'preview_html', 'email_last_sent', 'email_next', 'published_date', 'state',
                                  'email_enabled', 'email_interval', 'email_partner_ids', 'email_row_limit',
                                  'description', 'user_id', 'sequence'}
        if structural and not self.env.context.get('axs_report_no_sync'):
            for report in self.filtered(lambda r: r.state == 'published'):
                if report.query_type == 'custom_query' and 'query_text' in vals:
                    # the columns follow the new query
                    report.action_run_query()
                report._publish()
        if 'active' in vals:
            for report in self.filtered('menu_id'):
                report.menu_id.sudo().active = report.active and report.state == 'published'
        return res

    def unlink(self):
        for report in self:
            report._drop_generated()
        return super().unlink()

    def copy(self, default=None):
        default = dict(default or {})
        if 'name' not in default:
            default['name'] = _("%s (copy)", self.name)
        return super().copy(default)

    # ------------------------------------------------------------------
    # columns and SQL
    # ------------------------------------------------------------------
    def _ensure_field_names(self):
        for report in self:
            used = set()
            for line in report.line_ids:
                if line.field_name:
                    used.add(line.field_name)
            for line in report.line_ids.filtered(lambda l: not l.field_name):
                name = line._make_field_name(used)
                used.add(name)
                line.with_context(axs_report_no_sync=True).field_name = name

    def _columns(self, env=None, lang=None):
        """Describe each column: dict(line, name, expr, type, relation, selection). Plus the FROM clause."""
        self.ensure_one()
        env = env or self.env
        if self.query_type == 'field_chain':
            builder = ChainBuilder(env, self.model_id.model, lang or env.lang)
            columns = []
            for line in self.line_ids:
                if not line.field_chain:
                    raise ValidationError(_("Column '%s' has no field.", line.name))
                info = builder.resolve(line.field_chain, line.x2many_mode or 'list')
                expr, ftype = apply_alter(info['expr'], info['type'], line.alter_op, line.alter_value, self.env)
                columns.append({
                    'line': line, 'name': line.field_name, 'expr': expr, 'type': ftype,
                    'relation': info['comodel'] or False,
                    'selection': info['selection'] if ftype == 'selection' else False,
                })
            name_expr = builder.rec_name_expr(builder.base, 'b')
            return columns, builder, name_expr
        columns = []
        for line in self.line_ids:
            if not line.sql_column:
                continue
            ftype = line.result_type or 'char'
            relation = False
            if line.relation_model_id:
                ftype, relation = 'many2one', line.relation_model_id.model
            expr = 't.%s' % quote(line.sql_column)
            if ftype in ('char', 'text') and line.result_type not in ('char', 'text'):
                expr = '%s::text' % expr
            expr, ftype = apply_alter(expr, ftype, line.alter_op, line.alter_value, self.env)
            columns.append({'line': line, 'name': line.field_name, 'expr': expr, 'type': ftype,
                            'relation': relation, 'selection': False})
        return columns, None, None

    def _restriction_sql(self, env):
        """SQL condition on the base records (domain, archived records, access rules)."""
        self.ensure_one()
        Model = env[self.model_id.model].with_context(active_test=not self.include_archived)
        domain = safe_eval(self.domain or '[]')
        has_active = Model._active_name and Model._active_name in Model._fields
        if self.apply_record_rules and not env.su:
            check = Model.browse()
            if hasattr(check, 'has_access'):
                if not check.has_access('read'):
                    raise AccessError(_("You are not allowed to read %s, the source of this report.", Model._description))
            else:
                Model.check_access_rights('read')
            query = Model._search(domain)
        elif domain or (has_active and not self.include_archived):
            query = Model.sudo()._search(domain)
        else:
            return ''
        return 'b.id IN %s' % query_to_sql(env.cr, query)

    def _build_sql(self, env=None, limit=None):
        """Full SELECT of the report for the given environment (user, language, companies)."""
        self.ensure_one()
        env = env or self.env
        columns, builder, name_expr = self._columns(env)
        if self.query_type == 'field_chain':
            if builder.to_many_aliases:
                order = ', '.join(['b.id'] + ['%s.id' % a for a in builder.to_many_aliases])
                id_expr = 'ROW_NUMBER() OVER (ORDER BY %s)' % order
            else:
                id_expr = 'b.id'
            selects = ['%s AS id' % id_expr, 'b.id AS x_record_id', '(%s)::varchar AS x_name' % name_expr]
            for col in columns:
                selects.append('(%s)::%s AS %s' % (col['expr'], SQL_CAST[col['type']], quote(col['name'])))
            where = self._restriction_sql(env)
            sql = 'SELECT %s FROM %s%s' % (', '.join(selects), builder.from_clause(),
                                           (' WHERE %s' % where) if where else '')
        else:
            query = validate_custom_query(self.query_text, self.env)
            query = render_placeholders(env.cr, query, env)
            has_id = any(l.sql_column == 'id' for l in self.line_ids)
            first = self.line_ids.filtered(lambda l: l.result_type in ('char', 'text'))[:1] or self.line_ids[:1]
            name_expr = ('t.%s::varchar' % quote(first.sql_column)) if first else "''::varchar"
            id_expr = 't."id"' if has_id else 'ROW_NUMBER() OVER ()'
            selects = ['%s AS id' % id_expr, '%s AS x_name' % name_expr]
            for col in columns:
                selects.append('(%s)::%s AS %s' % (col['expr'], SQL_CAST[col['type']], quote(col['name'])))
            sql = 'SELECT %s FROM (\n%s\n) AS t' % (', '.join(selects), query)
        if limit:
            sql = 'SELECT * FROM (%s) AS axs_q LIMIT %d' % (sql, int(limit))
        return sql

    def _empty_sql(self, Model):
        selects = ['NULL::integer AS id']
        for name, field in Model._fields.items():
            if name.startswith('x_') and field.store:
                ftype = 'integer' if field.type == 'many2one' else field.type
                selects.append('NULL::%s AS %s' % (SQL_CAST.get(ftype, 'varchar'), quote(name)))
        return 'SELECT %s WHERE FALSE' % ', '.join(selects)

    @api.model
    def _table_query_for(self, model_name, env):
        """Called by the report models (see ir_model.py) each time they are queried."""
        report = self.with_context(active_test=False).search([('model_name', '=', model_name)], limit=1)
        Model = env[model_name]
        if not report:
            return self._empty_sql(Model)
        if report.company_id and report.company_id not in env.companies and not env.su:
            return self._empty_sql(Model)
        sql = report._build_sql(env)
        # the ORM inserts this text in queries executed with parameters: escape literal percent signs
        return sql.replace('%', '%%')

    # ------------------------------------------------------------------
    # custom query columns
    # ------------------------------------------------------------------
    def _describe_query(self):
        """Run the custom query with LIMIT 0 in a read-only savepoint; return [(name, type)]."""
        self.ensure_one()
        query = validate_custom_query(self.query_text, self.env)
        query = render_placeholders(self.env.cr, query, self.env)
        cr = self.env.cr
        if hasattr(self.env, 'flush_all'):
            self.env.flush_all()
        cr.execute('SAVEPOINT axs_report_describe')
        try:
            cr.execute('SET LOCAL transaction_read_only = on')
            cr.execute('SET LOCAL statement_timeout = 30000')
            cr.execute('SELECT * FROM (\n%s\n) AS t LIMIT 0' % query.replace('%', '%%'))
            description = [(col.name, PG_TYPES.get(col.type_code, 'char')) for col in cr.description]
        except Exception as e:  # noqa: BLE001 - report the database error to the user
            cr.execute('ROLLBACK TO SAVEPOINT axs_report_describe')
            cr.execute('RELEASE SAVEPOINT axs_report_describe')
            raise UserError(_("The query failed: %s", getattr(e, 'pgerror', None) or e))
        cr.execute('ROLLBACK TO SAVEPOINT axs_report_describe')
        cr.execute('RELEASE SAVEPOINT axs_report_describe')
        names = [name for name, _type in description]
        if len(set(names)) != len(names):
            raise UserError(_("Each column of the query needs a different name (use AS)."))
        return description

    def action_run_query(self):
        self.ensure_one()
        self._check_custom_query_rights()
        description = self._describe_query()
        existing = {line.sql_column: line for line in self.line_ids}
        commands = []
        seq = 10
        for name, ftype in description:
            label = name.replace('_', ' ').strip().title() or name
            if name in existing:
                commands.append((1, existing[name].id, {'sequence': seq, 'result_type': ftype}))
            else:
                commands.append((0, 0, {
                    'sequence': seq, 'name': label, 'sql_column': name, 'result_type': ftype,
                    'search_filter': ftype in ('char', 'text'), 'group_filter': ftype in ('char', 'date', 'boolean'),
                    'aggregator': 'sum' if ftype in ('integer', 'float') and name != 'id' else False,
                    'list_visible': name != 'id',
                }))
            seq += 10
        for name, line in existing.items():
            if name not in dict(description):
                commands.append((2, line.id))
        self.with_context(axs_report_no_sync=True).write({'line_ids': commands})
        return True

    # ------------------------------------------------------------------
    # publishing
    # ------------------------------------------------------------------
    def _refresh_types(self):
        """Store the result type of every column (field chain) and validate the definition."""
        for report in self:
            if not report.line_ids:
                raise UserError(_("Add at least one column to the report."))
            report._ensure_field_names()
            columns = report._columns()[0]
            for col in columns:
                line = col['line']
                vals = {'result_type': col['type'], 'result_relation': col['relation'] or False,
                        'selection_json': json.dumps(col['selection']) if col['selection'] else False}
                if line.aggregator and col['type'] not in ('integer', 'float'):
                    vals['aggregator'] = False
                if any(line[key] != value for key, value in vals.items()):
                    line.with_context(axs_report_no_sync=True).write(vals)

    def _validate_sql(self):
        """Execute the report once (COUNT) inside a savepoint to catch SQL errors early."""
        self.ensure_one()
        sql = self._build_sql(self.env)
        cr = self.env.cr
        if hasattr(self.env, 'flush_all'):
            self.env.flush_all()
        cr.execute('SAVEPOINT axs_report_check')
        try:
            cr.execute('SET LOCAL statement_timeout = 60000')
            cr.execute('SELECT COUNT(*) FROM (\n%s\n) AS t' % sql.replace('%', '%%'))
            count = cr.fetchone()[0]
        except Exception as e:  # noqa: BLE001
            cr.execute('ROLLBACK TO SAVEPOINT axs_report_check')
            cr.execute('RELEASE SAVEPOINT axs_report_check')
            raise UserError(_("The report cannot be executed: %s", getattr(e, 'pgerror', None) or e))
        # only reads happened: roll back to drop the statement timeout set for the check
        cr.execute('ROLLBACK TO SAVEPOINT axs_report_check')
        cr.execute('RELEASE SAVEPOINT axs_report_check')
        return count

    def action_publish(self):
        self.ensure_one()
        self._publish()
        self.with_context(axs_report_no_sync=True).write({'state': 'published', 'published_date': fields.Datetime.now()})
        if self.env.context.get('axs_report_no_reload'):
            return self.action_open_report()
        # reload the web client so that the new menu is visible right away, and open the report
        return {'type': 'ir.actions.client', 'tag': 'reload',
                'params': {'menu_id': self.menu_id.id, 'action_id': self.action_id.id}}

    def action_unpublish(self):
        for report in self:
            if report.menu_id:
                report.menu_id.sudo().active = False
            report.with_context(axs_report_no_sync=True).state = 'draft'
        return True

    def _publish(self):
        self.ensure_one()
        if not self.env.su and not self.env.user.has_group('axs_report_builder.group_report_manager'):
            raise AccessError(_("Only Report Builder managers can publish reports."))
        if self.query_type == 'custom_query':
            self._check_custom_query_rights()
            if not self.line_ids:
                self.action_run_query()
        self._refresh_types()
        self._validate_sql()
        self.sudo()._sync_model()
        clear_caches(self.env)

    def _field_vals(self, line):
        vals = {
            'name': line.field_name, 'field_description': line.name, 'ttype': line.result_type,
            'store': True, 'state': 'manual', 'readonly': True,
        }
        if line.result_type == 'many2one':
            relation = line.relation_model_id.model if self.query_type == 'custom_query' else line.result_relation
            vals.update(relation=relation, on_delete='set null')
        if line.result_type == 'selection' and line.selection_json:
            # set through the "selection" inverse: x2many commands on ir.model.* are not
            # executed as superuser when the publisher is a Report Builder manager
            vals['selection'] = repr([(str(value), label or str(value))
                                      for value, label in json.loads(line.selection_json)])
        return vals

    def _sync_model(self):
        """Create or update the SQL backed model, its fields, access rights, views, action and menu."""
        self.ensure_one()
        IrModel = self.env['ir.model']
        Fields = self.env['ir.model.fields']
        model = self.report_model_id
        if not model:
            model = IrModel.search([('model', '=', self.model_name)], limit=1)
        if not model:
            model = IrModel.create({'name': self.name, 'model': self.model_name, 'state': 'manual',
                                    'order': 'id', 'field_id': []})
        elif model.name != self.name:
            model.name = self.name
        self.with_context(axs_report_no_sync=True).report_model_id = model
        # fields
        wanted = {'x_name': {'name': 'x_name', 'field_description': _('Report Row'), 'ttype': 'char', 'store': True,
                             'state': 'manual', 'readonly': True}}
        if self.query_type == 'field_chain':
            wanted['x_record_id'] = {
                'name': 'x_record_id', 'field_description': self.model_id.name, 'ttype': 'many2one',
                'relation': self.model_id.model, 'store': True, 'state': 'manual', 'readonly': True,
                'on_delete': 'set null'}
        for line in self.line_ids:
            wanted[line.field_name] = self._field_vals(line)
        existing = {f.name: f for f in model.field_id if f.state == 'manual'}
        to_drop = Fields.browse()
        for name, field in existing.items():
            vals = wanted.get(name)
            if not vals or field.ttype != vals['ttype'] or (field.relation or False) != (vals.get('relation') or False) \
                    or vals['ttype'] == 'selection':
                to_drop |= field
        # views reference the fields: remove them before the fields
        self.view_ids.unlink()
        if to_drop:
            to_drop.unlink()
            existing = {f.name: f for f in model.field_id if f.state == 'manual'}
        for name, vals in wanted.items():
            if name in existing:
                if existing[name].field_description != vals['field_description']:
                    existing[name].field_description = vals['field_description']
            else:
                Fields.create(dict(vals, model_id=model.id))
        # access rights: read only, for the selected groups and the report managers
        self._sync_access(model)
        views = self._create_views(model)
        action = self._sync_action(views)
        menu = self._sync_menu(action)
        self.with_context(axs_report_no_sync=True).write({
            'view_ids': [(6, 0, views.ids)], 'action_id': action.id, 'menu_id': menu.id,
        })

    def _access_model(self):
        # Odoo 20 replaced ir.model.access by ir.access
        return self.env['ir.access'] if 'ir.access' in self.env else self.env['ir.model.access']

    def _sync_access(self, model):
        Access = self._access_model()
        Access.with_context(active_test=False).search([('model_id', '=', model.id)]).unlink()
        groups = (self.group_ids or self.env.ref('base.group_user')) | self.env.ref('axs_report_builder.group_report_manager')
        vals_list = []
        for group in groups:
            vals = {'name': 'axs_report %s %s' % (self.model_name, group.name), 'model_id': model.id,
                    'group_id': group.id}
            if Access._name == 'ir.access':
                vals['operation'] = 'r'
            else:
                vals.update(perm_read=True, perm_write=False, perm_create=False, perm_unlink=False)
            vals_list.append(vals)
        return Access.create(vals_list)

    def _create_views(self, model):
        View = self.env['ir.ui.view']
        lines = self.line_ids
        name = self.model_name

        def fattr(line):
            return 'name="%s" string="%s"' % (line.field_name, escape(line.name))

        list_cols = []
        if self.query_type == 'field_chain':
            list_cols.append('<field name="x_record_id" optional="hide" widget="many2one"/>')
        for line in lines:
            attrs = fattr(line)
            if line.aggregator and line.result_type in ('integer', 'float'):
                label = dict(line._fields['aggregator'].selection).get(line.aggregator)
                attrs += ' %s="%s"' % (line.aggregator, escape(label))
            list_cols.append('<field %s%s/>' % (attrs, '' if line.list_visible else ' optional="hide"'))
        default_order = ''
        if self.order_line_id and self.order_line_id.field_name:
            default_order = ' default_order="%s%s"' % (self.order_line_id.field_name, ' desc' if self.order_desc else '')
        arch_list = '<%s string="%s" create="0" edit="0" delete="0"%s>%s</%s>' % (
            LIST_TAG, escape(self.name), default_order, ''.join(list_cols), LIST_TAG)
        views = View.create({'name': '%s.list' % name, 'model': name, 'type': LIST_TAG, 'arch': arch_list,
                             'priority': 1})
        # form (one report row)
        form_fields = ''.join('<field %s/>' % fattr(l) for l in lines)
        record = '<field name="x_record_id"/>' if self.query_type == 'field_chain' else ''
        arch_form = ('<form string="%s" create="0" edit="0" delete="0"><sheet><group>%s%s</group></sheet></form>'
                     % (escape(self.name), record, form_fields))
        views |= View.create({'name': '%s.form' % name, 'model': name, 'type': 'form', 'arch': arch_form})
        # search
        search = []
        filters = []
        groups = []
        for line in lines:
            if line.search_filter:
                search.append('<field %s/>' % fattr(line))
            if line.search_filter and line.result_type in ('date', 'datetime'):
                filters.append('<filter name="f_%s" string="%s" date="%s"/>' % (
                    line.field_name, escape(line.name), line.field_name))
            if line.result_type == 'boolean' and line.search_filter:
                filters.append('<filter name="b_%s" string="%s" domain="[(\'%s\', \'=\', True)]"/>' % (
                    line.field_name, escape(line.name), line.field_name))
            if line.group_filter or line.default_group:
                interval = ''
                if line.result_type in ('date', 'datetime'):
                    interval = ':' + {'trunc_week': 'week', 'trunc_quarter': 'quarter',
                                      'trunc_year': 'year'}.get(line.alter_op, 'month')
                groups.append('<filter name="gb_%s" string="%s" context="{\'group_by\': \'%s%s\'}"/>' % (
                    line.field_name, escape(line.name), line.field_name, interval))
        if self.query_type == 'field_chain':
            search.append('<field name="x_record_id"/>')
        arch_search = '<search string="%s">%s%s%s</search>' % (
            escape(self.name), ''.join(search), ''.join(filters),
            ('<group>%s</group>' % ''.join(groups)) if groups else '')
        views |= View.create({'name': '%s.search' % name, 'model': name, 'type': 'search', 'arch': arch_search})
        numeric = lines.filtered(lambda l: l.result_type in ('integer', 'float'))
        if self.show_pivot:
            pivot = []
            for line in lines:
                role = line.pivot_role
                if role == 'measure' and line.result_type not in ('integer', 'float'):
                    role = False
                if role:
                    interval = ' interval="month"' if role in ('row', 'col') and line.result_type in ('date', 'datetime') else ''
                    pivot.append('<field name="%s" type="%s"%s/>' % (line.field_name, role, interval))
            if not any(l.pivot_role == 'measure' for l in numeric) and numeric.filtered('aggregator'):
                pivot.append('<field name="%s" type="measure"/>' % numeric.filtered('aggregator')[0].field_name)
            arch_pivot = '<pivot string="%s" disable_linking="0">%s</pivot>' % (escape(self.name), ''.join(pivot))
            views |= View.create({'name': '%s.pivot' % name, 'model': name, 'type': 'pivot', 'arch': arch_pivot})
        if self.show_graph:
            graph = []
            for line in lines:
                role = line.graph_role
                if role == 'measure' and line.result_type not in ('integer', 'float'):
                    role = False
                if role:
                    interval = ' interval="month"' if role == 'group' and line.result_type in ('date', 'datetime') else ''
                    graph.append('<field name="%s" type="%s"%s/>' % (
                        line.field_name, 'row' if role == 'group' else 'measure', interval))
            if not any(l.graph_role == 'measure' for l in numeric) and numeric.filtered('aggregator'):
                graph.append('<field name="%s" type="measure"/>' % numeric.filtered('aggregator')[0].field_name)
            arch_graph = '<graph string="%s" type="%s" stacked="%s">%s</graph>' % (
                escape(self.name), self.graph_type or 'bar', '1' if self.graph_stacked else '0', ''.join(graph))
            views |= View.create({'name': '%s.graph' % name, 'model': name, 'type': 'graph', 'arch': arch_graph})
        return views

    def _sync_action(self, views):
        modes = [LIST_TAG]
        if self.show_pivot:
            modes.append('pivot')
        if self.show_graph:
            modes.append('graph')
        first = {'list': LIST_TAG, 'pivot': 'pivot', 'graph': 'graph'}.get(self.default_view or 'list')
        if first in modes:
            modes.remove(first)
            modes.insert(0, first)
        modes.append('form')
        context = {'search_default_gb_%s' % l.field_name: 1 for l in self.line_ids if l.default_group}
        search_view = views.filtered(lambda v: v.type == 'search')
        help_html = Markup('<p class="o_view_nocontent_empty_folder">%s</p><p>%s</p>') % (
            _("No data for this report"), self.description or _("Change the filters or come back later."))
        vals = {
            'name': self.name, 'res_model': self.model_name, 'view_mode': ','.join(modes),
            'search_view_id': search_view.id, 'context': repr(context), 'help': help_html,
            'view_id': views.filtered(lambda v: v.type == first)[:1].id or False,
        }
        if self.action_id:
            self.action_id.write(vals)
            return self.action_id
        return self.env['ir.actions.act_window'].create(vals)

    def _sync_menu(self, action):
        parent = self.parent_menu_id or self.env.ref('axs_report_builder.menu_reports_root')
        vals = {
            'name': self.menu_name or self.name, 'parent_id': parent.id, 'sequence': self.menu_sequence,
            'action': 'ir.actions.act_window,%d' % action.id, 'active': self.active,
            'groups_id' if 'groups_id' in self.env['ir.ui.menu']._fields else 'group_ids':
                [(6, 0, (self.group_ids | self.env.ref('axs_report_builder.group_report_manager')).ids)]
                if self.group_ids else [(5, 0, 0)],
        }
        if self.menu_id:
            self.menu_id.write(vals)
            return self.menu_id
        return self.env['ir.ui.menu'].create(vals)

    def _drop_generated(self):
        self.ensure_one()
        sudo = self.sudo()
        if sudo.menu_id:
            sudo.menu_id.unlink()
        if sudo.action_id:
            sudo.action_id.unlink()
        sudo.view_ids.unlink()
        model = sudo.report_model_id or self.env['ir.model'].sudo().search([('model', '=', self.model_name)])
        if model:
            sudo._access_model().with_context(active_test=False).search([('model_id', '=', model.id)]).unlink()
            model.with_context(**{'_force_unlink': True}).unlink()
        clear_caches(self.env)

    # ------------------------------------------------------------------
    # user actions
    # ------------------------------------------------------------------
    def action_open_report(self):
        self.ensure_one()
        if self.state != 'published' or not self.action_id:
            raise UserError(_("Publish the report first."))
        action = self.action_id.sudo().read()[0]
        keep = ('type', 'name', 'res_model', 'views', 'view_mode', 'search_view_id', 'context', 'domain', 'help',
                'target', 'id')
        action = {k: v for k, v in action.items() if k in keep}
        action['target'] = 'current'
        return action

    def _format_preview(self, line, value):
        """Format a raw SQL value like the web client does (language, timezone)."""
        if value is None:
            return value
        try:
            if line.result_type == 'float' and isinstance(value, (int, float)):
                return formatLang(self.env, value, digits=2)
            if line.result_type == 'integer' and isinstance(value, int):
                return formatLang(self.env, value, digits=0)
            if line.result_type == 'date' and value:
                return format_date(self.env, value)
            if line.result_type == 'datetime' and value:
                return format_datetime(self.env, value)
            if line.result_type == 'boolean':
                return _("Yes") if value else _("No")
        except Exception:  # noqa: BLE001 - formatting must never break the preview
            pass
        return value

    def action_preview(self):
        self.ensure_one()
        if self.query_type == 'custom_query' and not self.line_ids:
            self.action_run_query()
        self._refresh_types()
        sql = self._build_sql(self.env, limit=20)
        cr = self.env.cr
        if hasattr(self.env, 'flush_all'):
            self.env.flush_all()
        cr.execute('SAVEPOINT axs_report_preview')
        try:
            cr.execute('SET LOCAL statement_timeout = 30000')
            cr.execute(sql.replace('%', '%%'))
            rows = cr.dictfetchall()
        except Exception as e:  # noqa: BLE001
            cr.execute('ROLLBACK TO SAVEPOINT axs_report_preview')
            cr.execute('RELEASE SAVEPOINT axs_report_preview')
            raise UserError(_("The report cannot be executed: %s", getattr(e, 'pgerror', None) or e))
        # only reads happened: roll back to drop the statement timeout set for the check
        cr.execute('ROLLBACK TO SAVEPOINT axs_report_preview')
        cr.execute('RELEASE SAVEPOINT axs_report_preview')
        total = self._validate_sql()
        lines = self.line_ids
        names = self._relation_names(rows)
        head = ''.join('<th%s>%s</th>' % (' class="text-end"' if l.result_type in ('integer', 'float') else '',
                                          escape(l.name)) for l in lines)
        body = []
        for row in rows:
            cells = []
            for line in lines:
                value = row.get(line.field_name)
                if line.result_type == 'many2one' and value:
                    value = names.get((line.field_name, value), value)
                if line.result_type == 'selection' and line.selection_json and value is not None:
                    value = dict(json.loads(line.selection_json)).get(value, value)
                value = self._format_preview(line, value)
                numeric = line.result_type in ('integer', 'float')
                cells.append('<td%s>%s</td>' % (' class="text-end"' if numeric else '',
                                                 escape('' if value is None or value is False else value)))
            body.append('<tr>%s</tr>' % ''.join(cells))
        html = ('<p class="text-muted mb-2">%s</p><div class="table-responsive">'
                '<table class="table table-sm table-striped table-hover o_axs_report_preview">'
                '<thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>') % (
            escape(_("%(shown)s of %(total)s rows, computed with your access rights.", shown=len(rows), total=total)),
            head, ''.join(body) or '<tr><td colspan="%d">%s</td></tr>' % (len(lines), escape(_("No data"))))
        self.with_context(axs_report_no_sync=True).preview_html = Markup(html)
        return True

    def _relation_names(self, rows):
        names = {}
        for line in self.line_ids.filtered(lambda l: l.result_type == 'many2one'):
            relation = line.relation_model_id.model if self.query_type == 'custom_query' else line.result_relation
            ids = {row.get(line.field_name) for row in rows if row.get(line.field_name)}
            if not relation or relation not in self.env or not ids:
                continue
            records = self.env[relation].browse(ids).exists()
            try:
                for record in records:
                    names[(line.field_name, record.id)] = record.display_name
            except AccessError:
                continue
        return names

    def _notify(self, message, kind='success'):
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'message': message, 'type': kind, 'sticky': False,
                           'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'}}}

    # ------------------------------------------------------------------
    # export / import of definitions
    # ------------------------------------------------------------------
    def _definition(self):
        self.ensure_one()
        keys = ['name', 'description', 'query_type', 'domain', 'include_archived', 'apply_record_rules', 'query_text',
                'menu_name', 'menu_sequence', 'show_pivot', 'show_graph', 'graph_type', 'graph_stacked',
                'default_view', 'order_desc']
        data = {key: self[key] for key in keys}
        data['model'] = self.model_id.model or False
        data['parent_menu'] = self.parent_menu_id.get_external_id().get(self.parent_menu_id.id) if self.parent_menu_id else False
        data['groups'] = [x for x in self.group_ids.get_external_id().values() if x]
        data['order_by'] = self.order_line_id.field_chain or self.order_line_id.sql_column or False
        line_keys = ['sequence', 'name', 'field_chain', 'x2many_mode', 'sql_column', 'result_type', 'list_visible',
                     'search_filter', 'group_filter', 'default_group', 'aggregator', 'pivot_role', 'graph_role',
                     'alter_op', 'alter_value']
        data['columns'] = []
        for line in self.line_ids:
            col = {key: line[key] for key in line_keys}
            col['relation_model'] = line.relation_model_id.model or False
            data['columns'].append(col)
        return {'format': 'axsync-report-builder', 'version': 1, 'report': data}

    def action_export_definition(self):
        content = json.dumps([r._definition() for r in self], indent=2, default=str).encode()
        name = re.sub(r'[^\w\-]+', '_', self[:1].name or 'report') if len(self) == 1 else 'reports'
        attachment = self.env['ir.attachment'].create({
            'name': '%s.json' % name, 'raw': content, 'mimetype': 'application/json',
            'res_model': self._name, 'res_id': self[:1].id,
        })
        return {'type': 'ir.actions.act_url', 'url': '/web/content/%d?download=true' % attachment.id,
                'target': 'self'}

    @api.model
    def _create_from_definition(self, data):
        data = data.get('report', data)
        vals = {key: data[key] for key in ('name', 'description', 'query_type', 'domain', 'include_archived',
                                           'apply_record_rules', 'query_text', 'menu_name', 'menu_sequence',
                                           'show_pivot', 'show_graph', 'graph_type', 'graph_stacked',
                                           'default_view', 'order_desc') if key in data}
        if data.get('model'):
            model = self.env['ir.model']._get(data['model'])
            if not model:
                raise UserError(_("The model %s is not installed in this database.", data['model']))
            vals['model_id'] = model.id
        if data.get('parent_menu'):
            menu = self.env.ref(data['parent_menu'], raise_if_not_found=False)
            if menu:
                vals['parent_menu_id'] = menu.id
        groups = [self.env.ref(x, raise_if_not_found=False) for x in data.get('groups') or []]
        vals['group_ids'] = [(6, 0, [g.id for g in groups if g])]
        lines = []
        for col in data.get('columns') or []:
            line = {k: col[k] for k in ('sequence', 'name', 'field_chain', 'x2many_mode', 'sql_column', 'result_type',
                                        'list_visible', 'search_filter', 'group_filter', 'default_group', 'aggregator',
                                        'pivot_role', 'graph_role', 'alter_op', 'alter_value') if k in col}
            if col.get('relation_model'):
                rel = self.env['ir.model']._get(col['relation_model'])
                if rel:
                    line['relation_model_id'] = rel.id
            lines.append((0, 0, line))
        vals['line_ids'] = lines
        report = self.create(vals)
        if data.get('order_by'):
            line = report.line_ids.filtered(lambda l: data['order_by'] in (l.field_chain, l.sql_column))[:1]
            report.order_line_id = line
        return report

    # ------------------------------------------------------------------
    # scheduled email
    # ------------------------------------------------------------------
    def _next_date(self, start):
        if self.email_interval == 'daily':
            return start + timedelta(days=1)
        if self.email_interval == 'monthly':
            from dateutil.relativedelta import relativedelta
            return start + relativedelta(months=1)
        return start + timedelta(weeks=1)

    def _excel_content(self, user):
        """Excel file of the report computed with the access rights of `user`."""
        import xlsxwriter
        self.ensure_one()
        env = self.env(user=user.id, su=False)
        Model = env[self.model_name]
        fields_ = [l for l in self.line_ids if l.list_visible]
        records = Model.search([], limit=self.email_row_limit or 5000, order=Model._order)
        rows = records.read([l.field_name for l in fields_])
        output = io.BytesIO()
        book = xlsxwriter.Workbook(output, {'in_memory': True})
        sheet = book.add_worksheet(re.sub(r'[\[\]:*?/\\]', ' ', self.name)[:31] or 'Report')
        bold = book.add_format({'bold': True, 'bg_color': '#E9ECEF', 'border': 1})
        date_fmt = book.add_format({'num_format': 'yyyy-mm-dd'})
        dt_fmt = book.add_format({'num_format': 'yyyy-mm-dd hh:mm'})
        for c, line in enumerate(fields_):
            sheet.write(0, c, line.name, bold)
            sheet.set_column(c, c, 18)
        for r, row in enumerate(rows, start=1):
            for c, line in enumerate(fields_):
                value = row.get(line.field_name)
                if value in (False, None):
                    continue
                if line.result_type == 'many2one':
                    sheet.write(r, c, value[1])
                elif line.result_type == 'date':
                    sheet.write_datetime(r, c, fields.Datetime.to_datetime(value), date_fmt)
                elif line.result_type == 'datetime':
                    sheet.write_datetime(r, c, value, dt_fmt)
                elif line.result_type == 'selection':
                    sheet.write(r, c, dict(Model._fields[line.field_name]._description_selection(env)).get(value, value))
                else:
                    sheet.write(r, c, value)
        sheet.freeze_panes(1, 0)
        book.close()
        return output.getvalue(), len(rows)

    def action_send_email(self):
        for report in self:
            report._send_email()
        return self._notify(_("The report was sent."))

    def _send_email(self):
        self.ensure_one()
        if self.state != 'published':
            raise UserError(_("Publish the report first."))
        partners = self.email_partner_ids.filtered('email')
        if not partners:
            raise UserError(_("Add recipients with an email address."))
        user = self.user_id or self.env.user
        content, count = self._excel_content(user)
        name = '%s %s.xlsx' % (self.name, fields.Date.context_today(self))
        attachment = self.env['ir.attachment'].sudo().create({
            'name': name, 'raw': content, 'res_model': self._name, 'res_id': self.id,
            'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        })
        body = Markup('<p>%s</p><p>%s</p>') % (
            _("Please find attached the report %s.", self.name), _("%s rows.", count))
        mail = self.env['mail.mail'].sudo().create({
            'subject': self.name, 'body_html': body,
            'email_to': ','.join(p.email for p in partners),
            'email_from': (user.email_formatted if user.email else self.env.company.email_formatted) or False,
            'attachment_ids': [(4, attachment.id)],
        })
        mail.send()
        self.with_context(axs_report_no_sync=True).write({'email_last_sent': fields.Datetime.now()})
        return mail

    @api.model
    def _cron_send_emails(self):
        now = fields.Datetime.now()
        reports = self.search([('email_enabled', '=', True), ('state', '=', 'published'),
                               '|', ('email_next', '=', False), ('email_next', '<=', now)])
        for report in reports:
            try:
                with self.env.cr.savepoint():
                    report._send_email()
            except Exception:  # noqa: BLE001 - one broken report must not block the others
                _logger.exception("Report Builder: scheduled email of report %s failed", report.id)
            report.with_context(axs_report_no_sync=True).email_next = report._next_date(report.email_next or now)
