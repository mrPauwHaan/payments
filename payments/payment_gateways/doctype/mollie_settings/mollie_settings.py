import hashlib
import hmac
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
		if response.get("status") == "Failed":
			return response.get("redirect_to")
		return response.get("payment_url") or response.get("redirect_to")

	def create_request(self, data: dict) -> dict:
		self.data = frappe._dict(data)
		self.data.gateway_controller = self.name

		try:
			self._validate_reference_document()

			public_pay_url = get_public_payment_url(
				self.data.reference_doctype,
				self.data.reference_docname,
				gateway_controller=self.name,
				redirect_to=self.data.get("redirect_to"),
			)

			if self._is_reference_document_already_paid():
				self.flags.status_changed_to = "Completed"
				finalized = self.finalize_request(run_hook=False)
				finalized["payment_url"] = public_pay_url
				return finalized

			existing = self._get_reusable_checkout_session(public_pay_url)
			if existing:
				return existing

			self.integration_request = create_request_log(
				self.data,
				service_name="Mollie",
				reference_doctype=self.data.reference_doctype,
				reference_docname=self.data.reference_docname,
			)
			return self.create_charge_on_mollie(public_pay_url=public_pay_url)

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

	def _is_reference_document_already_paid(self) -> bool:
		doctype = self.data.get("reference_doctype")
		docname = self.data.get("reference_docname")
		if not doctype or not docname or not frappe.db.exists(doctype, docname):
			return False

		ref_doc = frappe.get_doc(doctype, docname)
		meta = ref_doc.meta

		if meta.has_field("paid") and int(ref_doc.get("paid") or 0) == 1:
			return True
		if meta.has_field("status") and str(ref_doc.get("status") or "").lower() == "paid":
			return True
		if meta.has_field("payment_status") and str(ref_doc.get("payment_status") or "").lower() in (
			"completed",
			"paid",
		):
			return True

		completed_ir = frappe.db.exists(
			"Integration Request",
			{
				"integration_request_service": "Mollie",
				"reference_doctype": doctype,
				"reference_docname": docname,
				"status": "Completed",
			},
		)
		return bool(completed_ir)

	def _get_reusable_checkout_session(self, public_pay_url: str) -> dict | None:
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
				ir_doc = frappe.get_doc("Integration Request", req.name)
				ir_doc.db_set("output", frappe.as_json(dict(payment)), update_modified=False)

				payment_amount = flt(payment.amount.get("value"))
				payment_currency = payment.amount.get("currency")

				if (
					payment.is_open()
					and payment.checkout_url
					and abs(payment_amount - flt(self.data.amount)) < 0.01
					and payment_currency == self.data.currency
				):
					self._sync_reference_doc(
						status="Open",
						payment_id=payment.id,
						payment_url=public_pay_url,
						force_active=True,
					)
					return {
						"redirect_to": payment.checkout_url,
						"payment_url": public_pay_url,
						"payment_id": payment.id,
						"status": "Open",
					}

				if payment.is_paid() or payment.is_pending() or payment.is_authorized():
					result = self.process_payment_status(ir_doc, payment=payment)
					result["redirect_to"] = get_url(result.get("redirect_to"))
					result["payment_url"] = public_pay_url
					return result

				# Payment is canceled, expired, or failed: sync terminal status before creating a fresh session
				self.process_payment_status(ir_doc, payment=payment)

			except Exception:
				frappe.log_error(
					title="Mollie Session Reuse Check Failed",
					message=frappe.get_traceback(),
				)

		return None

	def create_charge_on_mollie(self, public_pay_url: str | None = None) -> dict:
		client = self.get_client()

		if not public_pay_url:
			public_pay_url = get_public_payment_url(
				self.data.reference_doctype,
				self.data.reference_docname,
				gateway_controller=self.name,
				redirect_to=self.data.get("redirect_to"),
			)

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

		# Immediately record the Open status, new payment_id, and persistent public retry URL on the document
		self._sync_reference_doc(
			status="Open",
			payment_id=charge.id,
			payment_url=public_pay_url,
			force_active=True,
		)

		return {
			"redirect_to": charge.checkout_url,
			"payment_url": public_pay_url,
			"payment_id": charge.id,
			"status": "Open",
		}

	def process_payment_status(self, integration_request_doc, payment=None) -> dict:
		frappe.db.sql(
			"SELECT status FROM `tabIntegration Request` WHERE name = %s FOR UPDATE",
			(integration_request_doc.name,),
		)
		integration_request_doc.reload()
		self.integration_request = integration_request_doc

		self.data = frappe._dict(frappe.parse_json(integration_request_doc.data or "{}"))
		if not self.data.get("reference_doctype"):
			self.data.reference_doctype = integration_request_doc.reference_doctype
		if not self.data.get("reference_docname"):
			self.data.reference_docname = integration_request_doc.reference_docname

		payment_id = integration_request_doc.request_description
		if not payment_id or not str(payment_id).startswith("tr_"):
			frappe.throw(_("Missing or invalid Mollie payment ID on Integration Request."))

		if integration_request_doc.status == "Completed":
			self.flags.status_changed_to = "Completed"
			self._sync_reference_doc(status="Completed", payment_id=payment_id)
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
			self._sync_reference_doc(status="Failed", payment_id=payment_id)
			return self.finalize_request(run_hook=False)

		if payment.is_paid():
			self.flags.status_changed_to = "Completed"
			integration_request_doc.db_set("status", "Completed", update_modified=True)
			return self.finalize_request(run_hook=True)

		if payment.is_authorized():
			self.flags.status_changed_to = "Authorized"
			integration_request_doc.db_set("status", "Authorized", update_modified=True)
			return self.finalize_request(run_hook=False)

		if payment.is_pending():
			self.flags.status_changed_to = "Pending"
			integration_request_doc.db_set("status", "Queued", update_modified=True)
			return self.finalize_request(run_hook=False)

		if payment.is_open():
			self.flags.status_changed_to = "Open"
			integration_request_doc.db_set("status", "Queued", update_modified=True)
			return self.finalize_request(run_hook=False, checkout_url=payment.checkout_url)

		if payment.is_expired():
			self.flags.status_changed_to = "Expired"
			integration_request_doc.db_set("status", "Cancelled", update_modified=True)
			return self.finalize_request(run_hook=False)

		if payment.is_canceled():
			self.flags.status_changed_to = "Cancelled"
			integration_request_doc.db_set("status", "Cancelled", update_modified=True)
			return self.finalize_request(run_hook=False)

		self.flags.status_changed_to = "Failed"
		integration_request_doc.db_set("status", "Failed", update_modified=True)
		return self.finalize_request(run_hook=False)

	def _sync_reference_doc(
		self,
		status: str,
		payment_id: str | None = None,
		payment_url: str | None = None,
		force_active: bool = False,
	):
		doctype = self.data.get("reference_doctype")
		docname = self.data.get("reference_docname")

		if not doctype or not docname or not frappe.db.exists(doctype, docname):
			return

		ref_doc = frappe.get_doc(doctype, docname)
		meta = ref_doc.meta

		if not payment_id and getattr(self, "integration_request", None):
			payment_id = self.integration_request.request_description

		if not payment_url:
			payment_url = get_public_payment_url(
				doctype,
				docname,
				gateway_controller=self.name,
				redirect_to=self.data.get("redirect_to"),
			)

		current_payment_status = str(ref_doc.get("payment_status") or "").lower() if meta.has_field("payment_status") else ""
		current_payment_id = ref_doc.get("payment_id") if meta.has_field("payment_id") else None
		is_already_paid = (
			current_payment_status in ("completed", "paid")
			or (meta.has_field("paid") and int(ref_doc.get("paid") or 0) == 1)
			or (meta.has_field("status") and str(ref_doc.get("status") or "").lower() == "paid")
		)

		# Do not allow late webhooks from an older payment attempt to downgrade a Completed document
		if is_already_paid and status != "Completed":
			return

		# Do not allow a terminal webhook (Expired/Cancelled/Failed) for an older payment_id
		# to overwrite a newer active payment_id already stored on the document
		if (
			not force_active
			and status in ("Expired", "Cancelled", "Failed")
			and current_payment_id
			and payment_id
			and current_payment_id != payment_id
		):
			return

		if meta.has_field("payment_status"):
			resolved_status = self._resolve_select_field_option(meta.get_field("payment_status"), status)
			ref_doc.db_set("payment_status", resolved_status, update_modified=True)

		if meta.has_field("payment_id") and payment_id:
			ref_doc.db_set("payment_id", payment_id, update_modified=False)

		if meta.has_field("payment_url") and payment_url:
			ref_doc.db_set("payment_url", payment_url, update_modified=False)

		if status == "Completed" and meta.has_field("paid"):
			ref_doc.db_set("paid", 1, update_modified=False)

	@staticmethod
	def _resolve_select_field_option(df, target_status: str) -> str:
		if not df or df.fieldtype != "Select" or not df.options:
			return target_status

		options = [opt.strip() for opt in df.options.split("\n") if opt.strip()]
		if not options:
			return target_status

		synonyms = {
			"Completed": ["Completed", "Paid", "Success", "Succeeded"],
			"Open": ["Open", "Created", "Initiated", "Unpaid", "Pending"],
			"Pending": ["Pending", "Processing", "Open"],
			"Authorized": ["Authorized", "Authorised", "Pending"],
			"Expired": ["Expired", "Timed Out", "Cancelled", "Canceled"],
			"Cancelled": ["Cancelled", "Canceled"],
			"Failed": ["Failed", "Declined", "Error"],
		}

		candidates = synonyms.get(target_status, [target_status])
		lower_map = {opt.lower(): opt for opt in options}

		for candidate in candidates:
			if candidate.lower() in lower_map:
				return lower_map[candidate.lower()]

		return target_status

	def finalize_request(self, run_hook: bool = False, checkout_url: str | None = None) -> dict:
		redirect_to = self.data.get("redirect_to") or None
		redirect_message = self.data.get("redirect_message") or None
		doctype = self.data.get("reference_doctype")
		docname = self.data.get("reference_docname")
		status = self.flags.status_changed_to or "Failed"

		payment_id = (
			self.integration_request.request_description
			if getattr(self, "integration_request", None)
			else None
		)
		public_pay_url = (
			get_public_payment_url(doctype, docname, gateway_controller=self.name, redirect_to=redirect_to)
			if (doctype and docname)
			else None
		)

		self._sync_reference_doc(
			status=status,
			payment_id=payment_id,
			payment_url=public_pay_url,
		)

		ref_doc = None
		if doctype and docname and frappe.db.exists(doctype, docname):
			ref_doc = frappe.get_doc(doctype, docname)
			ref_doc.flags.ignore_permissions = True

		if status == "Completed":
			if run_hook and ref_doc:
				try:
					if ref_doc.get("status") != "Paid":
						custom_redirect_to = ref_doc.run_method("on_payment_authorized", status)
						if custom_redirect_to:
							redirect_to = custom_redirect_to
				except Exception:
					frappe.log_error(
						title="Mollie on_payment_authorized Error",
						message=frappe.get_traceback(),
					)

			if self.redirect_url:
				redirect_url = self.redirect_url
				redirect_to = None
			else:
				redirect_url = f"payment-success?{urlencode({'doctype': doctype, 'docname': docname})}"

		elif status in ("Pending", "Authorized"):
			redirect_url = f"payment-success?{urlencode({'doctype': doctype, 'docname': docname})}"

		else:
			retry_url = self._build_cancel_or_retry_redirect(
				doctype=doctype,
				docname=docname,
				status=status,
				public_pay_url=public_pay_url,
				fallback_redirect=redirect_to,
				checkout_url=checkout_url,
			)
			return {
				"redirect_to": retry_url,
				"payment_url": public_pay_url,
				"status": status,
			}

		if redirect_to and redirect_to != "None":
			separator = "&" if "?" in redirect_url else "?"
			redirect_url += f"{separator}{urlencode({'redirect_to': redirect_to})}"

		if redirect_message:
			separator = "&" if "?" in redirect_url else "?"
			redirect_url += f"{separator}{urlencode({'redirect_message': redirect_message})}"

		return {
			"redirect_to": redirect_url,
			"payment_url": public_pay_url,
			"status": status,
		}

	def _build_cancel_or_retry_redirect(
		self,
		doctype: str | None,
		docname: str | None,
		status: str,
		public_pay_url: str | None,
		fallback_redirect: str | None,
		checkout_url: str | None = None,
	) -> str:
		if fallback_redirect and fallback_redirect != "None":
			separator = "&" if "?" in fallback_redirect else "?"
			params = {"payment_status": status.lower()}
			if public_pay_url:
				params["retry_url"] = public_pay_url
			return f"{fallback_redirect}{separator}{urlencode(params)}"

		if doctype == "Payment Request" and docname:
			return f"/payment-request?{urlencode({'name': docname, 'payment_status': status.lower()})}"

		# For Guests and Web Form records, render a safe status screen with a one-click Retry button
		# pointing to the HMAC-signed public `pay` endpoint so they can retry without logging in
		if public_pay_url:
			status_titles = {
				"Cancelled": _("Payment Cancelled"),
				"Expired": _("Payment Session Expired"),
				"Failed": _("Payment Failed"),
				"Open": _("Payment Incomplete"),
			}
			status_descriptions = {
				"Cancelled": _("Your payment was cancelled. Your registration has been saved—you can complete the payment at any time using the button below."),
				"Expired": _("Your payment session expired before completion. Click below to start a new payment session for your existing record."),
				"Failed": _("Your payment could not be processed. Click below to try again with a new payment session."),
				"Open": _("Your payment has not been completed yet. Click below to continue to the payment screen."),
			}
			action_url = checkout_url if (status == "Open" and checkout_url) else public_pay_url
			title = status_titles.get(status, _("Payment Incomplete"))
			desc = status_descriptions.get(status, _("Click below to retry your payment."))

			html = (
				f"<p>{desc}</p>"
				f"<p class='mt-4'>"
				f"<a href='{frappe.utils.escape_html(action_url)}' class='btn btn-primary'>"
				f"{_('Retry Payment')}"
				f"</a></p>"
			)
			return frappe.redirect_to_message(title, html, indicator_color="orange" if status != "Failed" else "red")

		return "payment-failed"


def _get_hmac_secret() -> bytes:
	secret = (
		frappe.local.conf.get("encryption_key")
		or frappe.get_site_config().get("encryption_key")
		or frappe.local.site
	)
	return str(secret).encode("utf-8")


def generate_payment_token(
	reference_doctype: str,
	reference_docname: str,
	gateway_controller: str | None = None,
) -> str:
	controller = gateway_controller or get_gateway_controller(reference_doctype, reference_docname)
	msg = f"{reference_doctype}:{reference_docname}:{controller}".encode("utf-8")
	return hmac.new(_get_hmac_secret(), msg, hashlib.sha256).hexdigest()


def verify_payment_token(
	reference_doctype: str,
	reference_docname: str,
	token: str,
	gateway_controller: str | None = None,
) -> bool:
	if not token or not isinstance(token, str):
		return False
	expected = generate_payment_token(reference_doctype, reference_docname, gateway_controller)
	return hmac.compare_digest(expected, token)


@frappe.whitelist()
def get_public_payment_url(
	reference_doctype: str,
	reference_docname: str,
	gateway_controller: str | None = None,
	redirect_to: str | None = None,
) -> str:
	controller = gateway_controller or get_gateway_controller(reference_doctype, reference_docname)
	token = generate_payment_token(reference_doctype, reference_docname, controller)
	params = {
		"doctype": reference_doctype,
		"docname": reference_docname,
		"controller": controller,
		"token": token,
	}
	if redirect_to and redirect_to != "None":
		params["redirect_to"] = redirect_to

	return get_url(
		"api/method/payments.payment_gateways.doctype.mollie_settings.mollie_settings.pay?"
		+ urlencode(params)
	)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def pay(
	doctype: str | None = None,
	docname: str | None = None,
	token: str | None = None,
	controller: str | None = None,
	redirect_to: str | None = None,
	**kwargs,
):
	doctype = doctype or frappe.form_dict.get("doctype")
	docname = docname or frappe.form_dict.get("docname")
	token = token or frappe.form_dict.get("token")
	controller = controller or frappe.form_dict.get("controller")
	redirect_to = redirect_to or frappe.form_dict.get("redirect_to")

	if not doctype or not docname or not token:
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = get_url("payment-failed")
		return

	try:
		resolved_controller = controller or get_gateway_controller(doctype, docname)
		if not verify_payment_token(doctype, docname, token, resolved_controller):
			frappe.throw(_("Invalid or expired payment link signature."), frappe.PermissionError)

		if not frappe.db.exists(doctype, docname):
			frappe.throw(_("Payment reference record does not exist."), frappe.DoesNotExistError)

		settings_doc = frappe.get_doc("Mollie Settings", resolved_controller)
		result = settings_doc.create_request(
			{
				"reference_doctype": doctype,
				"reference_docname": docname,
				"redirect_to": redirect_to,
			}
		)
		# Explicit commit is required because Frappe does not auto-commit on HTTP GET requests
		frappe.db.commit()
		target_url = get_url(result.get("redirect_to") or "payment-failed")

	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Mollie Public Pay Endpoint Error", message=frappe.get_traceback())
		target_url = get_url("payment-failed")

	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = target_url


def get_gateway_controller(doctype: str, docname: str) -> str:
	if not frappe.db.exists("DocType", doctype):
		frappe.throw(_("Invalid reference DocType {0}.").format(doctype), frappe.DoesNotExistError)

	meta = frappe.get_meta(doctype)
	payment_gateway = None

	if meta.has_field("payment_gateway") and frappe.db.exists(doctype, docname):
		payment_gateway = frappe.db.get_value(doctype, docname, "payment_gateway")

	if not payment_gateway:
		payment_gateway = frappe.db.get_value(
			"Web Form", {"doc_type": doctype, "accept_payment": 1}, "payment_gateway"
		)

	if not payment_gateway:
		payment_gateway = (
			frappe.db.get_value("Payment Gateway", {"gateway_settings": "Mollie Settings"}, "name")
			or frappe.db.get_value("Payment Gateway", {"gateway_controller": "Mollie Settings"}, "name")
			or "Mollie"
		)

	gateway_controller = frappe.db.get_value("Payment Gateway", payment_gateway, "gateway_controller")
	if not gateway_controller:
		gateway_controller = frappe.db.get_value("Mollie Settings", {}, "name")

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
		# Explicit commit is required because Frappe does not auto-commit on HTTP GET requests
		frappe.db.commit()
		target_url = get_url(result.get("redirect_to") or "payment-failed")

	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Mollie Payment Return Error", message=frappe.get_traceback())
		target_url = get_url("payment-failed")

	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = target_url


def exempt_webhook_csrf():
	if frappe.request and frappe.request.path.endswith(
		"/api/method/payments.payment_gateways.doctype.mollie_settings.mollie_settings.webhook"
	):
		frappe.local.flags.ignore_csrf = True