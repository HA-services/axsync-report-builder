import json

from odoo import fields, models
from odoo.exceptions import UserError
from odoo.tools.translate import _

from ..models.compat import binary_content


class AxsReportImport(models.TransientModel):
    _name = 'axs.report.import'
    _description = 'Import Report Definitions'

    file = fields.Binary("Definition File", required=True)
    filename = fields.Char()
    publish = fields.Boolean("Publish After Import", default=True)

    def action_import(self):
        self.ensure_one()
        try:
            data = json.loads(binary_content(self.file).decode('utf-8-sig'))
        except (ValueError, UnicodeDecodeError) as e:
            raise UserError(_("This is not a Report Builder definition file: %s", e))
        items = data if isinstance(data, list) else [data]
        reports = self.env['axs.report']
        for item in items:
            if not isinstance(item, dict) or item.get('format') != 'axsync-report-builder':
                raise UserError(_("This is not a Report Builder definition file."))
            reports |= self.env['axs.report']._create_from_definition(item)
        if self.publish:
            for report in reports:
                report.action_publish()
        return {
            'type': 'ir.actions.act_window', 'res_model': 'axs.report', 'name': _("Imported Reports"),
            'views': [[False, 'list' if 'list' in dict(self.env['ir.ui.view']._fields['type'].selection) else 'tree'],
                      [False, 'form']],
            'domain': [('id', 'in', reports.ids)], 'target': 'current',
        }
