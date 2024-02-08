$(document).ready(function() {
	var form = document.querySelector('#payment-form');
	var data = {{ frappe.form_dict | json }};
	var doctype = "{{ reference_doctype }}"
	var docname = "{{ reference_docname }}"
	frappe.call({
		method: "payments.templates.pages.mollie_checkout.make_payment",
		freeze: true,
		headers: {
			"X-Requested-With": "XMLHttpRequest"
		},
		args: {
			"data": JSON.stringify(data),
			"reference_doctype": doctype,
			"reference_docname": docname
		},
		callback: function(r){
			payment = r.message
		}
		
	})

	form.addEventListener('submit', e => {
		e.preventDefault();
		if (payment.status == "Completed") {
					$('#submit').hide()
					setTimeout(function() {
						window.location.href = r.message.redirect_to
					}, 2000);
				} else {
			}
			if (payment.paymentUrl) {
					setTimeout(function() {
						window.open(payment.paymentUrl, "_blank");
					}, 1000);
				} else {
			}
	})
})
