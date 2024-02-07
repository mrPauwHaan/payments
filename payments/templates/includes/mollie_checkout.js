$(document).ready(function(){
	(function(e){
		var options = {
			"key": "{{ api_key }}",
			"amount": cint({{ amount.value }}),
			"currency": "{{ currency }}",
			"name": "{{ title }}",
			"description": "{{ description }}",
			"subscription_id": "{{ subscription_id }}",
			"handler": function (response){
				mollie.make_payment_log(response, options, "{{ reference_doctype }}", "{{ reference_docname }}", "{{ token }}");
			},
			"prefill": {
				"name": "{{ payer_name }}",
				"email": "{{ payer_email }}",
				"order_id": "{{ order_id }}"
			},
			"notes": {{ frappe.form_dict|json }}
		};

		var rzp = new Mollie(options);
		rzp.open();
		//	e.preventDefault();
	})();
})

frappe.provide('mollie');

mollie.make_payment_log = function(response, options, doctype, docname, token){
	$('.mollie-loading').addClass('hidden');
	$('.mollie-confirming').removeClass('hidden');

	frappe.call({
		method:"payments.templates.pages.mollie_checkout.make_payment",
		freeze:true,
		headers: {"X-Requested-With": "XMLHttpRequest"},
		args: {
			"mollie_payment_id": response.mollie_payment_id,
			"options": options,
			"reference_doctype": doctype,
			"reference_docname": docname,
			"token": token
		},
		callback: function(r){
			if (r.message && r.message.status == 200) {
				window.location.href = r.message.redirect_to
			}
			else if (r.message && ([401,400,500].indexOf(r.message.status) > -1)) {
				window.location.href = r.message.redirect_to
			}
		}
	})
}
