# Copyright 2022 Camptocamp SA
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl)
import base64
import codecs
import csv
import io
import logging
from datetime import datetime

import pytz
from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import format_duration, groupby

from odoo.addons.base.models.res_partner import _tz_get

from ..const import MAXOPTRA_DATETIME_FORMAT

# Copied from odoo.tools.pycompat to use with DictReader
_reader = codecs.getreader("utf-8")

logger = logging.getLogger(__name__)


class ShipmentMaxoptraScheduleImport(models.TransientModel):

    _name = "shipment.maxoptra.schedule.import"
    _description = "Import maxoptra schedule and create batch pickings"

    @api.model
    def _default_tz(self):
        # TODO: Check if must use tz from warehouse in default_get instead?
        return self.env.context.get("tz")

    maxoptra_schedule_file = fields.Binary(attachment=False, required=True)
    maxoptra_schedule_file_name = fields.Char()
    shipment_planning_id = fields.Many2one("shipment.planning")
    warehouse_delivery_steps = fields.Selection(
        related="shipment_planning_id.warehouse_id.delivery_steps"
    )
    tz = fields.Selection(
        _tz_get,
        string="Timezone",
        default=lambda s: s._default_tz(),
        help="Timezone for datetime in CSV. Leave empty if UTC",
    )
    regroup_pick_operations = fields.Boolean(
        help="Create a batch picking for all related pick operations listed in import file",
        default=True,
    )
    reverse_order_pick_operations = fields.Boolean(
        help="Schedule the pick operations in the reverse order of the following operations",
        default=True,
    )
    pick_operations_duration = fields.Float(
        help="Duration between each pick operation",
        default=0.083,
    )

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if self.env.context.get("active_model") != "shipment.planning":
            raise UserError(
                _("This wizard can only be called from shipment planning objects.")
            )
        shipment_planning_id = self.env.context.get("active_id")
        if "shipment_planning_id" in fields_list and not res.get(
            "shipment_planning_id"
        ):
            res["shipment_planning_id"] = shipment_planning_id
        return res

    # TODO Add constraints according to warehouse delivery steps?

    def action_import_schedule(self):
        self.ensure_one()
        pick_batches = self.env["stock.picking.batch"]
        maxoptra_schedule = self.read_csv()
        schedule_by_vehicles = self.group_schedule_by_vehicle(maxoptra_schedule)
        delivery_batch_pickings = self.create_delivery_batch_picking_by_vehicle(
            schedule_by_vehicles
        )
        self.update_scheduled_date(maxoptra_schedule)
        self._update_scheduled_time_on_batch(
            delivery_batch_pickings,
            self.pick_operations_duration,
            self.reverse_order_pick_operations,
        )
        if self.warehouse_delivery_steps == "pick_ship":
            if self.regroup_pick_operations:
                pick_batches = self.regroup_operations(
                    delivery_batch_pickings,
                    self.reverse_order_pick_operations,
                    self.pick_operations_duration,
                )
        elif self.warehouse_delivery_steps == "pick_pack_ship":
            raise UserError(
                _(
                    "Scheduling of operations from Maxoptra in Pick-Pack-Ship "
                    "is not implemented yet."
                )
            )
        self._cleanup_batches()
        self.update_shipment_planning(delivery_batch_pickings | pick_batches)

    def regroup_operations(
        self,
        tour_batches,
        reverse_order=False,
        operation_duration=False,
    ):
        # create batch operation for precedent operations
        # batch created for each vehicle and operation type
        new_batch_picking_ids = []
        batch_obj = self.env["stock.picking.batch"]
        for batch_picking in tour_batches:
            original_pickings = batch_picking.picking_ids
            previous_pickings = original_pickings.move_lines.move_orig_ids.picking_id
            for _type, pick_list in self._group_to_batch_flow(previous_pickings):
                picks = self.env["stock.picking"].browse(
                    [pick.id for pick in pick_list]
                )
                new_batch = batch_obj.create(
                    {
                        "vehicle_id": batch_picking.vehicle_id.id,
                        "driver_id": batch_picking.driver_id.id,
                        "picking_ids": [(6, 0, picks.ids)],
                        "driving_start_time": batch_picking.driving_start_time,
                    }
                )
                picks.write(
                    {
                        "vehicle_id": batch_picking.vehicle_id.id,
                        "driver_id": batch_picking.driver_id.id,
                    }
                )
                new_batch_picking_ids.append(new_batch.id)
                if operation_duration:
                    scheduled_date = batch_picking.driving_start_time
                    picks = self.sort_on_outgoing_date(picks)
                    self._reschedule_time_on_picks(
                        picks, operation_duration, scheduled_date
                    )
        return batch_obj.browse(new_batch_picking_ids)

    def create_delivery_batch_picking_by_vehicle(self, schedule_by_vehicles):
        # TODO add separate check for batch incompatibility
        batch_ids = []
        for vehicle_name, maxoptra_deliveries in schedule_by_vehicles.items():
            for _type, pick_list in self._group_pickings_by_type(maxoptra_deliveries):
                batch_picking_values = self._prepare_batch_picking_values(
                    vehicle_name, delivery=maxoptra_deliveries,
                )
                batch_picking = self.env["stock.picking.batch"].create(
                    batch_picking_values
                )
                batch_ids.append(batch_picking.id)
                picks = self.env["stock.picking"].browse(
                    [pick.id for pick in pick_list]
                )
                picks.write(
                    {
                        "batch_id": batch_picking.id,
                        "vehicle_id": batch_picking.vehicle_id.id,
                        "driver_id": batch_picking.driver_id.id,
                    }
                )
        return self.env["stock.picking.batch"].browse(batch_ids)

    def update_scheduled_date(self, schedule_by_vehicles):
        for delivery in schedule_by_vehicles:
            picking = self.env["stock.picking"].search(
                [("name", "=", delivery.get("picking_name"))]
            )
            picking.write(
                {
                    "scheduled_date": delivery.get("scheduled_delivery_start_datetime"),
                }
            )

    def _group_pickings_by_type(self, maxoptra_deliveries):
        picking_names = [
            delivery.get("picking_name") for delivery in maxoptra_deliveries
        ]
        pickings = self.env["stock.picking"].search(
            [("name", "in", picking_names)], order="picking_type_id"
        )
        if len(pickings) != len(picking_names):
            p_names = set(pickings.mapped("name"))
            missing_names = set(picking_names) - p_names
            raise UserError(
                _("No matching picking found for Order reference \n %s")
                % ", ".join(list(missing_names))
            )
        return self._group_to_batch_flow(pickings)

    def _group_to_batch_flow(self, pickings):
        # TODO grouping same as a type
        return groupby(pickings, key=lambda m: m.picking_type_id)

    def sort_on_outgoing_date(self, picking, order=False):
        # sort pickings to the dates assigned in outgoing picking
        def _component_sort_key(pick):
            out = pick.group_id.stock_move_ids.picking_id.filtered(
                lambda self: self.picking_type_code == "outgoing"
            )
            return fields.first(out).scheduled_date

        return sorted(picking, key=lambda x: _component_sort_key(x), reverse=order)

    def _prepare_batch_picking_values(self, vehicle_name, delivery):
        vehicle = self.env["shipment.vehicle"].search([("name", "=", vehicle_name)])
        if not vehicle:
            logger.warning(
                "Vehicle for %s is not found batch left without driver" % vehicle_name
            )
        values = {
            "company_id": self.shipment_planning_id.company_id.id,
            "vehicle_id": vehicle.id,
            "driving_start_time": delivery[0].get("driving_start_time"),
        }
        driver_name = delivery[0].get("driver")
        if driver_name:
            driver = self.env["res.partner"].search(
                [("maxoptra_driver_name", "=", driver_name)]
            )
            if not driver:
                logger.warning(
                    "Vehicle for %s is not found batch left without driver"
                    % driver_name
                )
            if driver:
                values["driver_id"] = driver.id
        return values

    def group_schedule_by_vehicle(self, maxoptra_schedule):
        res = {}
        for maxoptra_delivery in maxoptra_schedule:
            res.setdefault(maxoptra_delivery.get("vehicle"), []).append(
                maxoptra_delivery
            )
        return res

    def update_shipment_planning(self, batch_pickings):
        values = {
            "maxoptra_exported_file": self.maxoptra_schedule_file,
            "maxoptra_exported_file_name": self.maxoptra_schedule_file_name,
        }
        if batch_pickings:
            values["batch_picking_ids"] = [(6, 0, batch_pickings.ids)]
        self.shipment_planning_id.write(values)
        return True

    def read_csv(self):
        res = []
        csv_data = base64.b64decode(self.maxoptra_schedule_file)
        buff = io.BytesIO(csv_data)
        reader = csv.DictReader(_reader(buff))
        for row in reader:
            # TODO: Allow to use different CSV column?
            res.append(self.read_row(row))
        buff.close()
        return res

    def read_row(self, row):
        scheduled_date = datetime.strptime(
            row.get("Scheduled arrival time"), MAXOPTRA_DATETIME_FORMAT
        )
        driving_start = datetime.strptime(
            row.get("DrivingStartTime"), MAXOPTRA_DATETIME_FORMAT
        )
        if self.tz:
            timezone = pytz.timezone(self.tz)
            utc = pytz.utc
            driving_start = (
                timezone.localize(driving_start).astimezone(utc).replace(tzinfo=None)
            )
        return {
            "picking_name": row.get("Order reference"),
            "picking_scheduled_seq": row.get("Scheduled sequence"),
            "driver": row.get("Performer name"),
            "vehicle": row.get("Vehicle name"),
            "scheduled_delivery_start_datetime": scheduled_date,
            "driving_start_time": driving_start,
        }

    def _update_scheduled_time_on_batch(
        self, batches, operation_duration, reverse_order=False
    ):
        if operation_duration:
            for batch in batches:
                scheduled_date = batch.driving_start_time
                picks = self.sort_on_outgoing_date(batch.picking_ids, reverse_order)
                self._reschedule_time_on_picks(
                    picks, operation_duration, scheduled_date
                )

    def _cleanup_batches(self):
        self.env["stock.picking.batch"].search(
            [("state", "=", "draft"), ("picking_ids", "=", False)]
        ).unlink()

    def _reschedule_time_on_picks(self, pick_ids, operation_duration, planned_date):
        cnt = 0
        hours, minutes = format_duration(operation_duration).split(":")
        for pick in pick_ids:
            delay = relativedelta(hours=cnt * int(hours), minutes=cnt * int(minutes))
            pick.write({"scheduled_date": planned_date + delay})
            cnt += 1
