$(document).ready(function() {
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
			if (r.message.status == "Completed") {
					$('#submit').hide()
					$('.success').show()
					setTimeout(function() {
						window.location.href = r.message.redirect_to
					}, 2000);
				} else {
			},
			if (r.message.paymentUrl) {
					$('#submit').hide()
					setTimeout(function() {
						window.location.href = r.message.redirect_to
						window.open(r.message.paymentUrl, "_blank");
					}, 2000);
				} else {
			}
		}
		
	})

})
