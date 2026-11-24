import json

from odoo.release import version_info
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase, new_test_user


@tagged('post_install', '-at_install', 'axs_report')
class TestReportBuilder(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Report = cls.env['axs.report'].with_context(axs_report_no_reload=True)
        cls.partner_model = cls.env['ir.model']._get('res.partner')
        cls.country = cls.env.ref('base.be')
        cls.tag_a, cls.tag_b = cls.env['res.partner.category'].create([{'name': 'AXS Tag A'}, {'name': 'AXS Tag B'}])
        cls.p1 = cls.env['res.partner'].create({
            'name': 'AXS Alpha', 'country_id': cls.country.id, 'city': 'Brussels', 'color': 3,
            'category_id': [(6, 0, (cls.tag_a | cls.tag_b).ids)],
        })
        cls.p2 = cls.env['res.partner'].create({'name': 'AXS Beta', 'city': 'Ghent', 'color': 5})
        cls.p3 = cls.env['res.partner'].create({'name': 'AXS Gamma (archived)', 'active': False, 'color': 7})
        cls.child = cls.env['res.partner'].create({'name': 'AXS Child', 'parent_id': cls.p1.id, 'type': 'contact'})
        cls.manager = new_test_user(cls.env, login='axs_rb_manager', groups='base.group_user,axs_report_builder.group_report_manager')
        cls.user = new_test_user(cls.env, login='axs_rb_user', groups='base.group_user')

    def _report(self, **vals):
        base = {
            'name': 'AXS Partners', 'model_id': self.partner_model.id,
            'domain': "[('name', 'like', 'AXS ')]",
            'line_ids': [
                (0, 0, {'name': 'Partner', 'field_chain': 'name', 'group_filter': True}),
                (0, 0, {'name': 'Country', 'field_chain': 'country_id', 'group_filter': True, 'pivot_role': 'row'}),
                (0, 0, {'name': 'Country Code', 'field_chain': 'country_id.code'}),
                (0, 0, {'name': 'Tags', 'field_chain': 'category_id'}),
                (0, 0, {'name': 'Color x10', 'field_chain': 'color', 'aggregator': 'sum',
                        'alter_op': 'multiply', 'alter_value': 10, 'pivot_role': 'measure'}),
                (0, 0, {'name': 'Type', 'field_chain': 'type', 'group_filter': True}),
                (0, 0, {'name': 'Created Month', 'field_chain': 'create_date', 'alter_op': 'trunc_month'}),
            ],
        }
        base.update(vals)
        return self.Report.create(base)

    def _rows(self, report, domain=None, user=None):
        Model = self.env[report.model_name]
        if user:
            Model = Model.with_user(user)
        return Model.search_read(domain or [], [l.field_name for l in report.line_ids], order='id')

    # ------------------------------------------------------------------
    def test_publish_field_chain(self):
        report = self._report()
        report.action_publish()
        self.assertEqual(report.state, 'published')
        Model = self.env[report.model_name]
        self.assertFalse(Model._auto)
        self.assertEqual({v.type for v in report.view_ids},
                         {'tree' if 'tree' in dict(self.env['ir.ui.view']._fields['type'].selection) else 'list',
                          'form', 'search', 'pivot', 'graph'})
        self.assertTrue(report.menu_id.action)
        rows = self._rows(report)
        names = {r['x_partner'] for r in rows}
        self.assertEqual(names, {'AXS Alpha', 'AXS Beta', 'AXS Child'}, "archived partners excluded by default")
        alpha = next(r for r in rows if r['x_partner'] == 'AXS Alpha')
        self.assertEqual(alpha['x_country'][0], self.country.id)
        self.assertEqual(alpha['x_country_code'], 'BE')
        self.assertEqual(alpha['x_tags'], 'AXS Tag A, AXS Tag B')
        self.assertEqual(alpha['x_color_x10'], 30)
        self.assertEqual(alpha['x_created_month'].day, 1)
        if version_info[0] >= 17:
            totals = [total for _country, total in Model._read_group([], ['x_country'], ['x_color_x10:sum'])]
        else:
            totals = [g['x_color_x10'] for g in Model.read_group([], ['x_color_x10:sum'], ['x_country'])]
        self.assertEqual(sum(totals), 30 + 50 + 0)
        self.assertEqual(Model.search_count([('x_partner', 'ilike', 'beta')]), 1)
        # record link back to the partner
        record = Model.search([('x_partner', '=', 'AXS Alpha')])
        self.assertEqual(record.x_record_id, self.p1)

    def test_selection_and_rows_mode(self):
        report = self._report(line_ids=[
            (0, 0, {'name': 'Partner', 'field_chain': 'name'}),
            (0, 0, {'name': 'Type', 'field_chain': 'type'}),
            (0, 0, {'name': 'Tag', 'field_chain': 'category_id', 'x2many_mode': 'rows'}),
        ])
        report.action_publish()
        type_line = report.line_ids.filtered(lambda l: l.name == 'Type')
        self.assertEqual(type_line.result_type, 'selection')
        rows = self._rows(report, [('x_partner', '=', 'AXS Alpha')])
        self.assertEqual(sorted(r['x_tag'] for r in rows), ['AXS Tag A', 'AXS Tag B'], "one row per tag")
        self.assertEqual(len({r['id'] for r in rows}), 2, "rows have distinct ids")

    def test_one2many_chain_and_inherited_fields(self):
        report = self._report(line_ids=[
            (0, 0, {'name': 'Partner', 'field_chain': 'name'}),
            (0, 0, {'name': 'Contacts', 'field_chain': 'child_ids'}),
            (0, 0, {'name': 'Contact City', 'field_chain': 'child_ids.city', 'x2many_mode': 'rows'}),
        ])
        report.action_publish()
        rows = self._rows(report, [('x_partner', '=', 'AXS Alpha')])
        self.assertEqual(rows[0]['x_contacts'], 'AXS Child')
        # users inherit partner fields (_inherits)
        users_report = self.Report.create({
            'name': 'AXS Users', 'model_id': self.env['ir.model']._get('res.users').id,
            'line_ids': [(0, 0, {'name': 'Login', 'field_chain': 'login'}),
                         (0, 0, {'name': 'Name', 'field_chain': 'name'})],
        })
        users_report.action_publish()
        rows = self.env[users_report.model_name].search_read([('x_login', '=', 'axs_rb_user')], ['x_name_col'])
        self.assertEqual(rows[0]['x_name_col'], self.user.name)

    def test_archived_and_domain(self):
        report = self._report(include_archived=True)
        report.action_publish()
        names = {r['x_partner'] for r in self._rows(report)}
        self.assertIn('AXS Gamma (archived)', names)
        report.domain = "[('name', 'like', 'AXS'), ('city', '=', 'Ghent')]"
        self.assertEqual({r['x_partner'] for r in self._rows(report)}, {'AXS Beta'})

    def test_record_rules_and_report_groups(self):
        # a global restriction on partners (ir.rule up to Odoo 19, ir.access with a domain on Odoo 20)
        domain = "['|', ('name', 'not like', 'AXS %'), ('name', 'in', ('AXS Alpha', 'AXS Child'))]"
        if 'ir.rule' in self.env:
            rule = self.env['ir.rule'].create({'name': 'AXS test: only Alpha', 'model_id': self.partner_model.id,
                                               'domain_force': domain})
        else:
            rule = self.env['ir.access'].create({'name': 'AXS test: only Alpha', 'model_id': self.partner_model.id,
                                                 'operation': 'r', 'domain': domain})
        report = self._report()
        report.action_publish()
        names = {r['x_partner'] for r in self._rows(report, user=self.user)}
        self.assertNotIn('AXS Beta', names, "record rules of the source model apply")
        report.apply_record_rules = False
        names = {r['x_partner'] for r in self._rows(report, user=self.user)}
        self.assertIn('AXS Beta', names)
        rule.unlink()
        # restrict the report to managers only
        report.group_ids = self.env.ref('axs_report_builder.group_report_manager')
        with self.assertRaises(AccessError):
            self.env[report.model_name].with_user(self.user).search([])
        self.assertTrue(self.env[report.model_name].with_user(self.manager).search([]))

    def test_report_is_read_only(self):
        report = self._report()
        report.action_publish()
        record = self.env[report.model_name].with_user(self.manager).search([], limit=1)
        with self.assertRaises(AccessError):
            record.write({'x_partner': 'hack'})

    def test_custom_query(self):
        report = self.Report.create({
            'name': 'AXS SQL', 'query_type': 'custom_query',
            'query_text': "SELECT p.id AS partner, p.city, p.color * 2 AS double_color, p.create_date AS created "
                          "FROM res_partner p WHERE p.name LIKE 'AXS %' AND p.active "
                          "AND (p.company_id IS NULL OR p.company_id IN {{company_ids}}) ORDER BY p.name",
        })
        report.action_run_query()
        types = {l.sql_column: l.result_type for l in report.line_ids}
        self.assertEqual(types, {'partner': 'integer', 'city': 'char', 'double_color': 'integer', 'created': 'datetime'})
        report.line_ids.filtered(lambda l: l.sql_column == 'partner').relation_model_id = self.partner_model
        report.action_publish()
        rows = self.env[report.model_name].search_read([], ['x_partner', 'x_city', 'x_double_color'])
        self.assertIn(self.p1.id, [r['x_partner'][0] for r in rows if r['x_city'] == 'Brussels'])
        self.assertEqual([r['x_double_color'] for r in rows if r['x_city'] == 'Ghent'], [10])
        # the query changes: columns follow
        report.query_text = "SELECT p.city, COUNT(*) AS total FROM res_partner p WHERE p.name LIKE 'AXS%%' GROUP BY p.city"
        report.action_run_query()
        self.assertEqual(sorted(report.line_ids.mapped('sql_column')), ['city', 'total'])
        report.action_publish()
        self.assertTrue(self.env[report.model_name].search_read([], ['x_total']))

    def test_custom_query_guard(self):
        report = self.Report.create({'name': 'AXS Guard', 'query_type': 'custom_query', 'query_text': 'SELECT 1 AS one'})
        for bad in ["DELETE FROM res_partner", "SELECT 1; DROP TABLE res_partner",
                    "WITH d AS (DELETE FROM res_partner RETURNING id) SELECT * FROM d",
                    "SELECT pg_sleep(5)", "SELECT * FROM res_partner FOR UPDATE", "UPDATE res_partner SET name = 'x'"]:
            with self.assertRaises(ValidationError, msg=bad):
                report.query_text = bad
        # comments and strings are not mistaken for statements
        report.query_text = "SELECT 'delete; drop' AS text -- update\n"
        report.action_run_query()
        self.assertEqual(report.line_ids.sql_column, 'text')
        # a valid but failing query is reported nicely
        report.query_text = "SELECT no_such_column FROM res_partner"
        with self.assertRaises(UserError):
            report.action_run_query()

    def test_custom_query_requires_admin(self):
        with self.assertRaises(AccessError):
            self.Report.with_user(self.manager).create({
                'name': 'AXS forbidden', 'query_type': 'custom_query', 'query_text': 'SELECT 1 AS one'})
        report = self.Report.with_user(self.manager).create({
            'name': 'AXS manager report', 'model_id': self.partner_model.id,
            'line_ids': [(0, 0, {'name': 'Partner', 'field_chain': 'name'})],
        })
        report.action_publish()
        self.assertEqual(report.state, 'published')
        with self.assertRaises(AccessError):
            self.Report.with_user(self.user).search([])

    def test_invalid_columns(self):
        with self.assertRaises(ValidationError):
            self._report(line_ids=[(0, 0, {'name': 'Bad', 'field_chain': 'does_not_exist'})]).action_publish()
        with self.assertRaises(ValidationError):
            # not stored
            self._report(line_ids=[(0, 0, {'name': 'Bad', 'field_chain': 'tz_offset'})]).action_publish()
        with self.assertRaises(ValidationError):
            self._report(line_ids=[(0, 0, {'name': 'Bad', 'field_chain': 'name', 'alter_op': 'multiply',
                                           'alter_value': 2})]).action_publish()
        with self.assertRaises(ValidationError):
            self._report(line_ids=[(0, 0, {'name': 'Bad', 'field_chain': 'name; drop'})])

    def test_republish_rename_remove_and_retype(self):
        report = self._report()
        report.action_publish()
        model_name = report.model_name
        old_action = report.action_id
        report.line_ids.filtered(lambda l: l.name == 'Tags').unlink()
        code = report.line_ids.filtered(lambda l: l.name == 'Country Code')
        code.write({'field_chain': 'country_id.phone_code'})
        report.action_publish()
        Model = self.env[model_name]
        self.assertNotIn('x_tags', Model._fields)
        self.assertEqual(Model._fields['x_country_code'].type, 'integer')
        self.assertEqual(report.action_id, old_action, "the menu action is kept")
        self.assertTrue(Model.search_read([], ['x_country_code']))

    def test_unpublish_archive_and_delete(self):
        report = self._report()
        report.action_publish()
        menu, action, model_name = report.menu_id, report.action_id, report.model_name
        report.action_unpublish()
        self.assertFalse(menu.active)
        report.action_publish()
        report.active = False
        self.assertFalse(menu.active)
        report.unlink()
        self.assertFalse(menu.exists())
        self.assertFalse(action.exists())
        self.assertFalse(self.env['ir.model'].search([('model', '=', model_name)]))

    def test_preview_and_copy(self):
        report = self._report()
        report.action_preview()
        self.assertIn('AXS Alpha', str(report.preview_html))
        copy = report.copy()
        self.assertNotEqual(copy.model_name, report.model_name)
        self.assertEqual(copy.state, 'draft')
        self.assertEqual(len(copy.line_ids), len(report.line_ids))

    def test_export_import_definition(self):
        report = self._report(group_ids=[(6, 0, [self.env.ref('base.group_user').id])])
        data = report._definition()
        self.assertEqual(data['report']['model'], 'res.partner')
        clone = self.Report._create_from_definition(json.loads(json.dumps(data)))
        self.assertEqual(clone.line_ids.mapped('field_chain'), report.line_ids.mapped('field_chain'))
        self.assertEqual(clone.group_ids, report.group_ids)
        action = report.action_export_definition()
        self.assertIn('/web/content/', action['url'])
        wizard = self.env['axs.report.import'].with_context(axs_report_no_reload=True).create({
            'file': __import__('base64').b64encode(json.dumps([data]).encode()).decode(), 'filename': 'r.json',
            'publish': True,
        })
        result = wizard.action_import()
        imported = self.Report.browse(result['domain'][0][2])
        self.assertEqual(imported.state, 'published')

    def test_scheduled_email(self):
        report = self._report(email_enabled=True, email_interval='weekly',
                              email_partner_ids=[(6, 0, [self.p1.id])])
        self.p1.email = 'alpha@example.com'
        report.action_publish()
        mail = report._send_email()
        self.assertEqual(mail.attachment_ids.mimetype,
                         'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.assertIn('alpha@example.com', mail.email_to)
        report.email_next = False
        self.Report._cron_send_emails()
        self.assertTrue(report.email_next)
        self.assertTrue(report.email_last_sent)
