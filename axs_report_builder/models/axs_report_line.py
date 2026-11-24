import re

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools.translate import _

ALTER_OPS = [
    ('none', 'No change'),
    ('multiply', 'Multiply by'),
    ('divide', 'Divide by'),
    ('add', 'Add'),
    ('subtract', 'Subtract'),
    ('round', 'Round (decimals)'),
    ('abs', 'Absolute value'),
    ('negate', 'Change sign'),
    ('trunc_week', 'First day of the week'),
    ('trunc_month', 'First day of the month'),
    ('trunc_quarter', 'First day of the quarter'),
    ('trunc_year', 'First day of the year'),
    ('add_days', 'Shift by days'),
]
RESULT_TYPES = [
    ('char', 'Text'), ('text', 'Long Text'), ('integer', 'Integer'), ('float', 'Decimal'), ('date', 'Date'),
    ('datetime', 'Date & Time'), ('boolean', 'Checkbox'), ('selection', 'Selection'), ('many2one', 'Link'),
]
ALLOWED_FIELD_TYPES = ('char', 'text', 'html', 'integer', 'float', 'monetary', 'date', 'datetime', 'boolean',
                       'selection', 'many2one', 'one2many', 'many2many')


class AxsReportLine(models.Model):
    _name = 'axs.report.line'
    _description = 'Report Builder Column'
    _order = 'sequence, id'

    report_id = fields.Many2one('axs.report', required=True, ondelete='cascade', index=True)
    query_type = fields.Selection(related='report_id.query_type')
    sequence = fields.Integer(default=10)
    name = fields.Char("Column Label", required=True, translate=True)
    field_name = fields.Char("Technical Name", copy=False, readonly=True,
                             help="Name of the field in the generated report model.")
    # field chain helpers: up to five levels, or type the chain directly
    model_id = fields.Many2one(related='report_id.model_id')
    level1_id = fields.Many2one('ir.model.fields', "Field", ondelete='cascade',
                                domain="[('model_id', '=', model_id), ('ttype', 'in', %s)]" % (ALLOWED_FIELD_TYPES,))
    level1_model_id = fields.Many2one('ir.model', compute='_compute_level_models')
    level2_id = fields.Many2one('ir.model.fields', "Then", ondelete='cascade',
                                domain="[('model_id', '=', level1_model_id), ('ttype', 'in', %s)]" % (ALLOWED_FIELD_TYPES,))
    level2_model_id = fields.Many2one('ir.model', compute='_compute_level_models')
    level3_id = fields.Many2one('ir.model.fields', "Then ", ondelete='cascade',
                                domain="[('model_id', '=', level2_model_id), ('ttype', 'in', %s)]" % (ALLOWED_FIELD_TYPES,))
    level3_model_id = fields.Many2one('ir.model', compute='_compute_level_models')
    level4_id = fields.Many2one('ir.model.fields', "Then  ", ondelete='cascade',
                                domain="[('model_id', '=', level3_model_id), ('ttype', 'in', %s)]" % (ALLOWED_FIELD_TYPES,))
    level4_model_id = fields.Many2one('ir.model', compute='_compute_level_models')
    level5_id = fields.Many2one('ir.model.fields', "Then   ", ondelete='cascade',
                                domain="[('model_id', '=', level4_model_id), ('ttype', 'in', %s)]" % (ALLOWED_FIELD_TYPES,))
    field_chain = fields.Char(
        "Field Chain", help="Technical path of the field, for example partner_id.country_id.name. "
                            "You can type a longer chain than the five pickers allow.")
    x2many_mode = fields.Selection(
        [('list', 'One row, values separated by commas'), ('rows', 'One row per value')],
        string="Multiple Values", default='list',
        help="What to do when the chain reaches several records (tags, order lines...).")
    # custom query
    sql_column = fields.Char("Query Column", readonly=True)
    relation_model_id = fields.Many2one(
        'ir.model', "Link To", ondelete='set null',
        help="Custom queries: the column contains IDs of this model. The report shows names and links.")
    # result
    result_type = fields.Selection(RESULT_TYPES, "Type", readonly=True)
    result_relation = fields.Char(readonly=True)
    selection_json = fields.Text(readonly=True)
    # display and filters
    list_visible = fields.Boolean("In List", default=True)
    search_filter = fields.Boolean("Search", default=True, help="Searchable from the search bar.")
    group_filter = fields.Boolean("Group By", help="Available in the Group By menu.")
    default_group = fields.Boolean("Default Group", help="Group the report by this column when it opens.")
    aggregator = fields.Selection(
        [('sum', 'Sum'), ('avg', 'Average'), ('min', 'Minimum'), ('max', 'Maximum')],
        string="Total", help="Total shown in the list, the groups and the pivot measures.")
    pivot_role = fields.Selection([('row', 'Row'), ('col', 'Column'), ('measure', 'Measure')], string="Pivot")
    graph_role = fields.Selection([('group', 'Group'), ('measure', 'Measure')], string="Graph")
    alter_op = fields.Selection(ALTER_OPS, "Alter Values", default='none',
                                help="Transform numbers and dates before they are displayed.")
    alter_value = fields.Float("Alter Value")

    @api.depends('level1_id', 'level2_id', 'level3_id', 'level4_id')
    def _compute_level_models(self):
        IrModel = self.env['ir.model']
        for line in self:
            for level in range(1, 5):
                field = line['level%d_id' % level]
                model = IrModel._get(field.relation) if field and field.relation else IrModel
                line['level%d_model_id' % level] = model

    @api.onchange('level1_id', 'level2_id', 'level3_id', 'level4_id', 'level5_id')
    def _onchange_levels(self):
        for line in self:
            chain, label = [], []
            for level in range(1, 6):
                field = line['level%d_id' % level]
                if not field:
                    break
                chain.append(field.name)
                label.append(field.field_description)
                if field.ttype not in ('many2one', 'one2many', 'many2many'):
                    break
            if chain:
                line.field_chain = '.'.join(chain)
                if not line.name or line._origin.name == line.name or not line._origin:
                    line.name = ' / '.join(label[-2:]) if len(label) > 1 else label[0]
            # clear the levels after a non relational field
            stop = False
            for level in range(1, 6):
                field = line['level%d_id' % level]
                if stop:
                    line['level%d_id' % level] = False
                elif field and field.ttype not in ('many2one', 'one2many', 'many2many'):
                    stop = True

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        lines._sync_from_chain()
        return lines

    def write(self, vals):
        res = super().write(vals)
        if 'field_chain' in vals and not self.env.context.get('axs_line_sync'):
            self._sync_from_chain()
        return res

    def _sync_from_chain(self):
        """Fill the field pickers and the column type from a typed (or imported) field chain."""
        Fields = self.env['ir.model.fields'].sudo()
        simple = {'char': 'char', 'text': 'text', 'html': 'text', 'integer': 'integer', 'float': 'float',
                  'monetary': 'float', 'date': 'date', 'datetime': 'datetime', 'boolean': 'boolean',
                  'selection': 'selection', 'many2one': 'many2one', 'one2many': 'char', 'many2many': 'char'}
        for line in self:
            if line.query_type != 'field_chain' or not line.field_chain or not line.model_id:
                continue
            vals, model_name, field = {}, line.model_id.model, Fields
            for level, name in enumerate(line.field_chain.split('.')[:5], start=1):
                field = Fields._get(model_name, name)
                if not field:
                    break
                vals['level%d_id' % level] = field.id
                if field.ttype not in ('many2one', 'one2many', 'many2many'):
                    break
                model_name = field.relation
            for level in range(len(vals) + 1, 6):
                vals['level%d_id' % level] = False
            if field and not line.result_type and line.alter_op in (False, 'none'):
                vals['result_type'] = simple.get(field.ttype, 'char')
            if any(line[k].id != v if k.startswith('level') else line[k] != v for k, v in vals.items()):
                line.with_context(axs_line_sync=True).write(vals)

    @api.constrains('field_chain')
    def _check_field_chain(self):
        for line in self:
            if line.field_chain and not re.match(r'^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*$', line.field_chain):
                raise ValidationError(_("Invalid field chain: %s", line.field_chain))

    @api.constrains('aggregator', 'result_type')
    def _check_aggregator(self):
        for line in self:
            if line.aggregator and line.result_type and line.result_type not in ('integer', 'float'):
                raise ValidationError(_("Totals are only available for numbers (%s).", line.name))

    def _make_field_name(self, used):
        self.ensure_one()
        base = re.sub(r'[^a-z0-9]+', '_', (self.name or 'column').lower()).strip('_') or 'column'
        base = ('x_' + base)[:50]
        if base in ('x_name', 'x_record_id', 'x_id'):
            base = base + '_col'
        name, index = base, 2
        while name in used:
            name = '%s_%d' % (base, index)
            index += 1
        return name

    @api.model
    def _aggregator_for(self, model_name, field_name):
        self.env.cr.execute("""
            SELECT l.aggregator FROM axs_report_line l JOIN axs_report r ON r.id = l.report_id
             WHERE r.model_name = %s AND l.field_name = %s
        """, [model_name, field_name])
        row = self.env.cr.fetchone()
        return row and row[0] or None
