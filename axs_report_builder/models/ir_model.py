"""Register report models as SQL-backed models (no table, `_table_query`)."""
from odoo import api, models, release

REPORT_PREFIX = 'x_axs_rpt_'


def _report_table_query(self):
    """SQL of the report, computed for the current user (record rules, language, company)."""
    return self.env['axs.report'].sudo()._table_query_for(self._name, self.env)


def _report_table_sql(self):
    # Odoo 20 replaced `_table_query` by the `_table_sql` property (an SQL object)
    from odoo.tools import SQL
    # the query is already escaped for parameter interpolation (% doubled)
    return SQL('(%s)' % _report_table_query(self))


def report_class_attrs():
    table_attr = '_table_query' if hasattr(models.BaseModel, '_table_query') else '_table_sql'
    return {
        '_auto': False,
        '_log_access': False,
        table_attr: property(_report_table_query if table_attr == '_table_query' else _report_table_sql),
    }


class IrModel(models.Model):
    _inherit = 'ir.model'

    # Odoo 16 to 18 build manual models with _instanciate(), Odoo 19+ with _instanciate_attrs()
    @api.model
    def _instanciate(self, model_data):
        if not model_data['model'].startswith(REPORT_PREFIX):
            return super()._instanciate(model_data)
        # the class must be created with _auto/_log_access False, otherwise the
        # create_uid/write_date columns are added and the SQL query would lack them
        models.check_pg_name(model_data['model'].replace('.', '_'))
        attrs = {
            '_name': model_data['model'],
            '_description': model_data['name'],
            '_module': False,
            '_custom': True,
            '_transient': False,
            '_order': model_data['order'] or 'id',
            '__doc__': model_data['info'],
        }
        attrs.update(report_class_attrs())
        return type('CustomModel', (models.Model,), attrs)

    @api.model
    def _instanciate_attrs(self, model_data):
        attrs = super()._instanciate_attrs(model_data)
        if model_data['model'].startswith(REPORT_PREFIX) and isinstance(attrs, dict) and '_name' in attrs:
            attrs.update(report_class_attrs())
        return attrs


class IrModelFields(models.Model):
    _inherit = 'ir.model.fields'

    @api.model
    def _instanciate_attrs(self, field_data):
        attrs = super()._instanciate_attrs(field_data)
        if attrs and field_data['model'].startswith(REPORT_PREFIX) and field_data['ttype'] in ('integer', 'float'):
            aggregator = self.env['axs.report.line'].sudo()._aggregator_for(field_data['model'], field_data['name'])
            if aggregator:
                # 'group_operator' was renamed 'aggregator' in Odoo 18
                attrs['aggregator' if release.version_info[0] >= 18 else 'group_operator'] = aggregator
        return attrs
