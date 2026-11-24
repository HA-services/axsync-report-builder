from . import models
from . import wizard


def uninstall_hook(env_or_cr, registry=None):
    """Remove the generated report models, menus and actions before the module goes away."""
    if registry is not None:  # Odoo 16 passes (cr, registry)
        from odoo import api, SUPERUSER_ID
        env = api.Environment(env_or_cr, SUPERUSER_ID, {})
    else:
        env = env_or_cr
    reports = env['axs.report'].with_context(active_test=False).search([])
    for report in reports:
        report._drop_generated()
