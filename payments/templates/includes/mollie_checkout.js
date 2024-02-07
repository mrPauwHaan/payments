var mollie = Mollie("{{ profile_id }}");

var style = {
	base: {
		color: '#32325d',
		lineHeight: '18px',
		fontFamily: '"Helvetica Neue", Helvetica, sans-serif',
		fontSmoothing: 'antialiased',
		fontSize: '16px',
		'::placeholder': {
			color: '#aab7c4'
		}
	},
	invalid: {
		color: '#fa755a',
		iconColor: '#fa755a'
	}
};

function setOutcome(result) {

	if (result.token) {
		$('#submit').prop('disabled', true)
		$('#submit').html(__('Processing...'))
		frappe.call({
			method:"payments.templates.pages.mollie_checkout.make_payment",
			freeze:true,
			headers: {"X-Requested-With": "XMLHttpRequest"},
			args: {
				"mollie_token_id": result.token.id,
				"data": JSON.stringify({{ frappe.form_dict|json }}),
				"reference_doctype": "{{ reference_doctype }}",
				"reference_docname": "{{ reference_docname }}"
			},
			callback: function(r) {
				if (r.message.status == "Completed") {
					$('#submit').hide()
					$('.success').show()
					setTimeout(function() {
						window.location.href = r.message.redirect_to
					}, 2000);
				} else {
					$('#submit').hide()
					$('.error').show()
					setTimeout(function() {
						window.location.href = r.message.redirect_to
					}, 2000);
				}
			}
		});

	} else if (result.error) {
		$('.error').html(result.error.message);
		$('.error').show()
	}
}

frappe.ready(function() {
	$('#submit').off("click").on("click", function(e) {	
		e.preventDefault();
		
		mollie.createToken();
		
		  if (error) {
		    // Something wrong happened while creating the token. Handle this situation gracefully.
		    return;
		  }
		
		  // Submit form to the server
		  form.addEventListener('submit', async e => {
	})
});
