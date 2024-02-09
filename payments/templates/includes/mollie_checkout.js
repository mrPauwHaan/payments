+$(document).ready(function() {
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
				"reference_docname": docname,
			},
			callback: function(r){
				payment = r.message
				document.getElementById("status").value = payment.status;
				console.log(payment)
				if (payment.paymentUrl == "Unavailable") {
					$('#submit').hide()
				}
				else {
					$('#submit').show()
				}
			}
		})

	form.addEventListener('submit', e => {
		e.preventDefault();
		if (payment.paymentUrl == "unavailable") {
			window.location.href = payment.redirect_to
		}
		else {
			window.open(payment.paymentUrl, "_blank");
		}
	})
})
