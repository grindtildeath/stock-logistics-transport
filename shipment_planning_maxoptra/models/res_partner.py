# Copyright 2022 Camptocamp SA
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl)
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from ..const import MAXOPTRA_ADDRESS_FORMAT


class ResPartner(models.Model):

    _inherit = "res.partner"

    maxoptra_driver_name = fields.Char(
        string="Driver External Name",
        help="External name of Driver in Maxoptra, used to target the right "
        "partner to set on Batch pickings after import.",
    )

    maxoptra_partner_key = fields.Char(string="Maxoptra Key")

    _sql_constraints = [
        ("code_unique", "unique(maxoptra_partner_key)", "The Maxoptra partner should be unique. Try another value.")
    ]


    def _get_maxoptra_address(self):
        self.ensure_one()
        args = {
            "state_code": self.state_id.code or "",
            "country_name": self._get_country_name(),
        }
        for field in self._formatting_address_fields():
            args[field] = getattr(self, field) or ""
        # TODO: Add possibility to format address for MaxOptra on res.country?
        # TODO: Improve to allow automatic address recognition in Maxoptra
        return MAXOPTRA_ADDRESS_FORMAT % args
