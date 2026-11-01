from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import call_hook_method, flt, get_url
from mollie.api.client import Client
from mollie.api.error import Error as MollieError
from payments.utils import create_payment_gateway


class MollieSettings(Document):
	supported_currencies = [
		"AED",
		"AUD",
		"BGN",
		"BRL",
		"CAD",
		"CHF",
		"CZK",
		"DKK",
		"EUR",
		"GBP",
		"HKD",
		"HUF",
		"ILS",
		"ISK",
		"JPY",
		"MXN",
		"MYR",
		"NOK",
		"NZD",
		"PHP",
		"PLN",
		"RON",
		"RUB",
		"SEK",
		"SGD",
		"THB",
		"TWD",
		"USD",
		"ZAR",
	]

	def get_client(self) -> Client:
		secret_key = self.get_password(fieldname="secret_key", raise_exception=False)
		if not secret_key:
			frappe.throw(_("Mollie Secret Key is not configured for gateway {0}.").format(self.name))
		client = Client()
		client.set_api_key(secret_key.strip())
		return client

	def on_update(self):
		create_payment_gateway(
			"Mollie-" + self.gateway_name,
			settings="Mollie Settings",
			controller=self.gateway_name,
		)
		call_hook_method("payment_gateway_enabled", gateway="Mollie-" + self.gateway_name)
		if not self.flags.ignore_mandatory:
			self.validate_mollie_credentials()

	def validate_mollie_credentials(self):
		if not self.secret_key:
			return
		try:
			client = self.get_client()
			client.payments.list(limit=1)
		except MollieError as exc:
			frappe.throw(_("Invalid Mollie Secret Key: {0}").format(str(exc)))
		except Exception:
			frappe.log_error(title="Mollie Credential Validation Error", message=frappe.get_traceback())
			frappe.throw(_("Unable to validate Mollie credentials. Please verify your Secret Key."))

	def validate_transaction_currency(self, currency: str):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Mollie does not support transactions in currency '{0}'"
				).format(currency)
			)

	def get_payment_url(self, **kwargs) -> str:
		response = self.create_request(kwargs)
		return response.get("redirect_to")

	def create_request(self, data: dict) -> dict:
		self.data = frappe._dict(data)
		self.data.gateway_controller = self.name

		try:
			self._validate_reference_document()

			existing_url = self._get_reusable_checkout_url()
			if existing_url:
				return {"redirect_to": existing_url, "status": "Pending"}

			self.integration_request = create_request_log(
				self.data,
				service_name="Mollie",
				reference_doctype=self.data.reference_doctype,
				reference_docname=self.data.reference_docname,
			)
			return self.create_charge_on_mollie()

		except Exception:
			frappe.log_error(title="Mollie Create Request Error", message=frappe.get_traceback())
			if getattr(self, "integration_request", None):
				self.integration_request.db_set("status", "Failed", update_modified=True)
				self.integration_request.db_set("error", frappe.get_traceback(), update_modified=False)

			return {
				"redirect_to": frappe.redirect_to_message(
					_("Payment Initialization Failed"),
					_(
						"We could not initialize your payment session with Mollie. Please try again or contact support."
					),
				),
				"status": "Failed",
			}

	def _validate_reference_document(self):
		doctype = self.data.get("reference_doctype")
		docname = self.data.get("reference_docname")

		if not doctype or not docname:
			frappe.throw(_("Missing payment reference document."), frappe.ValidationError)

		if not frappe.db.exists("DocType", doctype):
			frappe.throw(_("DocType {0} does not exist.").format(doctype), frappe.DoesNotExistError)

		meta = frappe.get_meta(doctype)
		if meta.issingle or meta.is_virtual:
			frappe.throw(
				_("Payments cannot be processed for Single or Virtual DocTypes."),
				frappe.ValidationError,
			)

		if not frappe.db.exists(doctype, docname):
			frappe.throw(
				_("Reference document {0} {1} does not exist.").format(doctype, docname),
				frappe.DoesNotExistError,
			)

		ref_doc = frappe.get_doc(doctype, docname)

		expected_amount = None
		for field in ("grand_total", "amount", "total_amount", "outstanding_amount", "payable_amount"):
			if meta.has_field(field) and flt(ref_doc.get(field)) > 0:
				expected_amount = flt(ref_doc.get(field))
				break

		if expected_amount is not None:
			if flt(self.data.get("amount")) and abs(flt(self.data.get("amount")) - expected_amount) > 0.01:
				frappe.throw(_("Requested payment amount does not match reference document."))
			self.data.amount = f"{expected_amount:.2f}"
		else:
			if not flt(self.data.get("amount")) or flt(self.data.get("amount")) <= 0:
				frappe.throw(_("Payment amount must be greater than zero."))
			self.data.amount = f"{flt(self.data.amount):.2f}"

		if not self.data.get("currency"):
			for field in ("currency", "party_account_currency"):
				if meta.has_field(field) and ref_doc.get(field):
					self.data.currency = ref_doc.get(field)
					break

		if not self.data.get("currency"):
			self.data.currency = frappe.db.get_default("currency") or "EUR"

		self.validate_transaction_currency(self.data.currency)

	def _get_reusable_checkout_url(self) -> str | None:
		existing_requests = frappe.get_all(
			"Integration Request",
			filters={
				"integration_request_service": "Mollie",
				"reference_doctype": self.data.reference_doctype,
				"reference_docname": self.data.reference_docname,
				"status": ["in", ["Queued", "Authorized"]],
			},
			fields=["name", "request_description", "data"],
			order_by="creation desc",
			limit=5,
		)

		if not existing_requests:
			return None

		client = self.get_client()

		for req in existing_requests:
			payment_id = req.get("request_description")
			if not payment_id or not str(payment_id).startswith("tr_"):
				continue

			try:
				payment = client.payments.get(payment_id)
				payment_amount = flt(payment.amount.get("value"))
				payment_currency = payment.amount.get("currency")

				if (
					payment.is_open()
					and payment.checkout_url
					and abs(payment_amount - flt(self.data.amount)) < 0.01
					and payment_currency == self.data.currency
				):
					return payment.checkout_url

				if payment.is_paid():
					ir_doc = frappe.get_doc("Integration Request", req.name)
					result = self.process_payment_status(ir_doc, payment=payment)
					return get_url(result.get("redirect_to"))

				if payment.is_canceled() or payment.is_expired() or payment.is_failed():
					frappe.db.set_value(
						"Integration Request", req.name, "status", "Cancelled", update_modified=True
					)
			except Exception:
				frappe.log_error(
					title="Mollie Session Reuse Check Failed",
					message=frappe.get_traceback(),
				)

		return None

	def create_charge_on_mollie(self) -> dict:
		client = self.get_client()

		return_url = get_url(
			"api/method/payments.payment_gateways.doctype.mollie_settings.mollie_settings.payment_return?"
			+ urlencode({"integration_request": self.integration_request.name})
		)
		webhook_url = get_url(
			"api/method/payments.payment_gateways.doctype.mollie_settings.mollie_settings.webhook"
		)

		description = (
			self.data.get("description")
			or f"Payment for {self.data.reference_doctype} {self.data.reference_docname}"
		)

		charge_data = {
			"amount": {
				"currency": self.data.currency,
				"value": f"{flt(self.data.amount):.2f}",
			},
			"description": str(description)[:255],
			"redirectUrl": return_url,
			"webhookUrl": webhook_url,
			"metadata": {
				"integration_request": self.integration_request.name,
				"reference_doctype": self.data.reference_doctype,
				"reference_docname": self.data.reference_docname,
				"gateway_controller": self.name,
			},
		}

		charge = client.payments.create(charge_data)

		self.integration_request.db_set("request_description", charge.id, update_modified=False)
		self.integration_request.db_set("output", frappe.as_json(dict(charge)), update_modified=True)

		return {
			"redirect_to": charge.checkout_url,
			"status": "Pending",
		}

	def process_payment_status(self, integration_request_doc, payment=None) -> dict:
		frappe.db.sql(
			"SELECT status FROM `tabIntegration Request` WHERE name = %s FOR UPDATE",
			(integration_request_doc.name,),
		)
		integration_request_doc.reload()

		self.data = frappe._dict(frappe.parse_json(integration_request_doc.data or "{}"))
		payment_id = integration_request_doc.request_description

		if not payment_id or not str(payment_id).startswith("tr_"):
			frappe.throw(_("Missing or invalid Mollie payment ID on Integration Request."))

		if integration_request_doc.status == "Completed":
			self.flags.status_changed_to = "Completed"
			return self.finalize_request(run_hook=False)

		if not payment:
			client = self.get_client()
			payment = client.payments.get(payment_id)

		integration_request_doc.db_set("output", frappe.as_json(dict(payment)), update_modified=True)

		paid_amount = flt(payment.amount.get("value"))
		paid_currency = payment.amount.get("currency")
		expected_amount = flt(self.data.get("amount"))
		expected_currency = self.data.get("currency")

		if abs(paid_amount - expected_amount) > 0.01 or paid_currency != expected_currency:
			integration_request_doc.db_set("status", "Failed", update_modified=True)
			integration_request_doc.db_set(
				"error",
				f"Amount/currency mismatch: expected {expected_currency} {expected_amount:.2f}, got {paid_currency} {paid_amount:.2f}",
				update_modified=False,
			)
			self.flags.status_changed_to = "Failed"
			return self.finalize_request(run_hook=False)

		if payment.is_paid():
			self.flags.status_changed_to = "Completed"
			integration_request_doc.db_set("status", "Completed", update_modified=True)
			return self.finalize_request(run_hook=True)

		if payment.is_pending() or payment.is_authorized():
			self.flags.status_changed_to = "Pending"
			integration_request_doc.db_set("status", "Authorized", update_modified=True)
			return self.finalize_request(run_hook=False)

		if payment.is_open():
			self.flags.status_changed_to = "Open"
			return self.finalize_request(run_hook=False)

		self.flags.status_changed_to = "Cancelled"
		final_ir_status = "Cancelled" if (payment.is_canceled() or payment.is_expired()) else "Failed"
		integration_request_doc.db_set("status", final_ir_status, update_modified=True)
		return self.finalize_request(run_hook=False)

	def finalize_request(self, run_hook: bool = False) -> dict:
		redirect_to = self.data.get("redirect_to") or None
		redirect_message = self.data.get("redirect_message") or None
		doctype = self.data.get("reference_doctype")
		docname = self.data.get("reference_docname")

		ref_doc = None
		if doctype and docname and frappe.db.exists(doctype, docname):
			ref_doc = frappe.get_doc(doctype, docname)
			ref_doc.flags.ignore_permissions = True

		if self.flags.status_changed_to == "Completed":
			if run_hook and ref_doc:
				if hasattr(ref_doc, "on_payment_authorized") and callable(
					getattr(ref_doc, "on_payment_authorized")
				):
					try:
						if ref_doc.get("status") != "Paid":
							custom_redirect_to = ref_doc.run_method(
								"on_payment_authorized", self.flags.status_changed_to
							)
							if custom_redirect_to:
								redirect_to = custom_redirect_to
					except Exception:
						frappe.log_error(
							title="Mollie on_payment_authorized Error",
							message=frappe.get_traceback(),
						)

				if ref_doc.meta.has_field("payment_status"):
					ref_doc.db_set("payment_status", "Completed")

				if ref_doc.meta.has_field("payment_id") and getattr(self, "integration_request", None):
					ref_doc.db_set("payment_id", self.integration_request.request_description)

			if self.redirect_url:
				redirect_url = self.redirect_url
				redirect_to = None
			else:
				redirect_url = f"payment-success?{urlencode({'doctype': doctype, 'docname': docname})}"

		elif self.flags.status_changed_to == "Pending":
			if ref_doc and ref_doc.meta.has_field("payment_status"):
				ref_doc.db_set("payment_status", "Pending")

			redirect_url = f"payment-success?{urlencode({'doctype': doctype, 'docname': docname})}"

		else:
			if ref_doc and ref_doc.meta.has_field("payment_status"):
				ref_doc.db_set("payment_status", self.flags.status_changed_to or "Failed")

			retry_base = self._get_retry_document_url(doctype, docname, redirect_to)
			separator = "&" if "?" in retry_base else "?"
			redirect_url = f"{retry_base}{separator}{urlencode({'payment_status': (self.flags.status_changed_to or 'failed').lower()})}"
			return {"redirect_to": redirect_url, "status": self.flags.status_changed_to}

		if redirect_to:
			separator = "&" if "?" in redirect_url else "?"
			redirect_url += f"{separator}{urlencode({'redirect_to': redirect_to})}"

		if redirect_message:
			separator = "&" if "?" in redirect_url else "?"
			redirect_url += f"{separator}{urlencode({'redirect_message': redirect_message})}"

		return {"redirect_to": redirect_url, "status": self.flags.status_changed_to}

	@staticmethod
	def _get_retry_document_url(doctype: str | None, docname: str | None, fallback_redirect: str | None) -> str:
		if fallback_redirect and fallback_redirect != "None":
			return fallback_redirect

		if not doctype or not docname or not frappe.db.exists(doctype, docname):
			return "payment-failed"

		web_form = frappe.db.get_value("Web Form", {"doc_type": doctype}, "route")
		if web_form:
			return f"/{web_form}?name={docname}"

		if doctype == "Payment Request":
			return f"/payment-request?{urlencode({'name': docname})}"

		meta = frappe.get_meta(doctype)
		if meta.has_field("route"):
			route = frappe.db.get_value(doctype, docname, "route")
			if route:
				return f"/{route}"

		return "payment-failed"


def get_gateway_controller(doctype: str, docname: str) -> str:
	if not frappe.db.exists("DocType", doctype):
		frappe.throw(_("Invalid reference DocType {0}.").format(doctype), frappe.DoesNotExistError)

	meta = frappe.get_meta(doctype)
	payment_gateway = None

	if meta.has_field("payment_gateway"):
		payment_gateway = frappe.db.get_value(doctype, docname, "payment_gateway")

	if not payment_gateway:
		payment_gateway = (
			frappe.db.get_value("Payment Gateway", {"gateway_controller": "Mollie Settings"}, "name")
			or "Mollie"
		)

	gateway_controller = frappe.db.get_value("Payment Gateway", payment_gateway, "gateway_controller")
	if not gateway_controller:
		frappe.throw(_("No Gateway Controller configured for Payment Gateway {0}.").format(payment_gateway))

	return gateway_controller


def _resolve_controller_for_integration_request(integration_request_doc) -> str:
	data = frappe.parse_json(integration_request_doc.data or "{}")
	controller = data.get("gateway_controller")
	if controller and frappe.db.exists("Mollie Settings", controller):
		return controller

	doctype = integration_request_doc.reference_doctype or data.get("reference_doctype")
	docname = integration_request_doc.reference_docname or data.get("reference_docname")
	return get_gateway_controller(doctype, docname)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook(**kwargs):
	payment_id = frappe.form_dict.get("id") or kwargs.get("id")
	if not payment_id or not isinstance(payment_id, str) or not payment_id.startswith("tr_"):
		frappe.local.response["http_status_code"] = 400
		return {"status": "Invalid payment ID"}

	integration_request_name = frappe.db.get_value(
		"Integration Request",
		{
			"integration_request_service": "Mollie",
			"request_description": payment_id,
		},
		"name",
	)

	if not integration_request_name:
		frappe.local.response["http_status_code"] = 200
		return {"status": "Ignored"}

	frappe.enqueue(
		"payments.payment_gateways.doctype.mollie_settings.mollie_settings.process_mollie_webhook",
		queue="short",
		integration_request_name=integration_request_name,
		payment_id=payment_id,
		now=frappe.flags.in_test,
	)

	frappe.local.response["http_status_code"] = 200
	return {"status": "Queued"}


def process_mollie_webhook(integration_request_name: str, payment_id: str):
	if not frappe.db.exists("Integration Request", integration_request_name):
		return

	ir_doc = frappe.get_doc("Integration Request", integration_request_name)
	if ir_doc.status == "Completed":
		return

	doctype = ir_doc.reference_doctype
	docname = ir_doc.reference_docname

	if not doctype or not docname or not frappe.db.exists(doctype, docname):
		ir_doc.db_set("status", "Failed", update_modified=True)
		ir_doc.db_set(
			"error", f"Reference document does not exist: {doctype} {docname}", update_modified=False
		)
		return

	try:
		controller_name = _resolve_controller_for_integration_request(ir_doc)
		settings_doc = frappe.get_doc("Mollie Settings", controller_name)
		settings_doc.process_payment_status(ir_doc)
	except Exception:
		frappe.log_error(
			title=f"Mollie Webhook Processing Error ({payment_id})",
			message=frappe.get_traceback(),
		)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def payment_return(integration_request: str | None = None, **kwargs):
	ir_name = integration_request or frappe.form_dict.get("integration_request")
	if not ir_name or not frappe.db.exists("Integration Request", ir_name):
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = get_url("payment-failed")
		return

	try:
		ir_doc = frappe.get_doc("Integration Request", ir_name)
		if ir_doc.integration_request_service != "Mollie":
			frappe.throw(_("Invalid Integration Request service."), frappe.PermissionError)

		if not frappe.db.exists(ir_doc.reference_doctype, ir_doc.reference_docname):
			frappe.throw(_("Payment reference document no longer exists."), frappe.DoesNotExistError)

		controller_name = _resolve_controller_for_integration_request(ir_doc)
		settings_doc = frappe.get_doc("Mollie Settings", controller_name)
		result = settings_doc.process_payment_status(ir_doc)
		target_url = get_url(result.get("redirect_to") or "payment-failed")

	except Exception:
		frappe.log_error(title="Mollie Payment Return Error", message=frappe.get_traceback())
		target_url = get_url("payment-failed")

	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = target_url


def exempt_webhook_csrf():
	if frappe.request and frappe.request.path.endswith(
		"/api/method/payments.payment_gateways.doctype.mollie_settings.mollie_settings.webhook"
	):
		frappe.local.flags.ignore_csrf = True