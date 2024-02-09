$(document).ready(function() {
	var form = document.querySelector('#payment-form');
	var data = {{ frappe.form_dict | json }};
	var doctype = "{{ reference_doctype }}"
	var docname = "{{ reference_docname }}"
	frappe.call({
			method: "payments.templates.pages.mollie_checkout.check_payment",
			freeze: true,
			headers: {
				"X-Requested-With": "XMLHttpRequest"
			},
			args: {
				"data": JSON.stringify(data),
				"reference_doctype": doctype,
				"reference_docname": docname,
				"paymentID": payment.paymentID
			},
			callback: function(r){
				document.getElementById("status").value = r.message;
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
			window.open(payment.paymentUrl, "_blank");
		}
	})
})
