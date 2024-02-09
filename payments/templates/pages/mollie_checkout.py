import json

import frappe
from frappe import _
from frappe.utils import cint, fmt_money

from payments.payment_gateways.doctype.mollie_settings.mollie_settings import (
	get_gateway_controller,
)

no_cache = 1

expected_keys = (
	"amount",
	"title",
	"description",
	"reference_doctype",
	"reference_docname",
	"payer_name",
	"payer_email",
	"order_id",
	"currency",
)


def get_context(context):
	context.no_cache = 1

	# all these keys exist in form_dict
	if not (set(expected_keys) - set(list(frappe.form_dict))):
		for key in expected_keys:
			context[key] = frappe.form_dict[key]

		gateway_controller = get_gateway_controller(context.reference_doctype, context.reference_docname)
		context.profile_id = get_api_key(context.reference_docname, gateway_controller)
		context.image = get_header_image(context.reference_docname, gateway_controller)

		context["amount"] = fmt_money(amount=context["amount"], currency=context["currency"])

	else:
		frappe.log_error("Data to complete the payment is missing", frappe.form_dict)
		frappe.redirect_to_message(
			_("Some information is missing"),
			_("Looks like someone sent you to an incomplete URL. Please ask them to look into it."),
		)
		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect
		


def get_api_key(doc, gateway_controller):
	profile_id = frappe.db.get_value("Mollie Settings", gateway_controller, "profile_id")
	if cint(frappe.form_dict.get("use_sandbox")):
		profile_id = frappe.conf.sandbox_profile_id

	return profile_id


def get_header_image(doc, gateway_controller):
	header_image = frappe.db.get_value("Mollie Settings", gateway_controller, "header_img")

	return header_image


@frappe.whitelist(allow_guest=True)
def make_payment(data, reference_doctype, reference_docname):
	data = json.loads(data)
	gateway_controller = get_gateway_controller(reference_doctype, reference_docname)
	paymentID = frappe.db.get_value(reference_doctype, reference_docname, 'payment_id')
	
	if not paymentID:
		data = frappe.get_doc("Mollie Settings", gateway_controller).create_request(data)
		paymentID = data["paymentID"]
	
	status = frappe.get_doc("Mollie Settings", gateway_controller).check_request(data, paymentID)
	
	if status["status"] == "Cancelled":
		data = frappe.get_doc("Mollie Settings", gateway_controller).create_request(data)
		paymentID = data["paymentID"]
		data["status"] = "Open"
	else:
		data["status"] = status["status"]


	if status["paymentUrl"]:
		data["paymentUrl"] = status["paymentUrl"]

	if hasattr(reference_doctype, 'payment_status'):
		frappe.db.set_value(reference_doctype, reference_docname, 'payment_status', status["status"])
	
	frappe.db.commit()
	return data
