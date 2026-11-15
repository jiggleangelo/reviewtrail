import html


def e(value):
    return html.escape(str(value), quote=True)


def page(title, body, csrf='', user=False):
    auth = (
        "<a href='/account'>Account</a> <form class='inline' method='post' action='/logout'><input type='hidden' name='csrf' value='" + e(csrf) + "'><button>Log out</button></form>"
        if user else "<a href='/login'>Log in</a>"
    )
    return "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>" + e(title) + " · ReviewTrail</title><link rel='stylesheet' href='/static/style.css'></head><body><header><a href='/'><strong>ReviewTrail</strong></a> <nav><a href='/models'>Models</a> <a href='/review/new'>Write a review</a> <a href='/waitlist/vendor'>Vendor reports</a> " + auth + "</nav></header><main><h1>" + e(title) + "</h1>" + body + "</main><footer><a href='/methodology'>Methodology</a> · <a href='/independence'>Independence</a> · <a href='/privacy'>Privacy</a></footer></body></html>"


def review_form(models, csrf, values=None, errors=None):
    values = values or {}
    errors = errors or {}
    value = lambda key: e(values.get(key, ''))
    error = lambda key: "<p class='error'>" + e(errors[key]) + '</p>' if key in errors else ''
    options = ''.join("<option value='" + e(model['slug']) + "'" + (" selected" if values.get('model_slug') == model['slug'] else '') + '>' + e(model['name']) + '</option>' for model in models)
    categories = ''.join("<option value='" + e(x) + "'" + (" selected" if values.get('task_category') == x else '') + '>' + e(x) + '</option>' for x in ('coding', 'extraction', 'rag', 'agents'))
    harnesses = ''.join("<option value='" + x + "'" + (" selected" if values.get('harness') == x else '') + '>' + x + '</option>' for x in ('api-direct', 'openrouter', 'ide-plugin', 'cli-agent'))
    selected_other = values.get('harness') == 'other' or str(values.get('harness', '')).startswith('other:')
    other = values.get('harness_other', '') or (str(values.get('harness', ''))[6:] if str(values.get('harness', '')).startswith('other:') else '')
    checked = " checked" if values.get('consent') == 'on' else ''
    return "<p>Do not paste secrets or personal data. Describe the task so someone else could repeat it.</p><form method='post' action='/review'><input type='hidden' name='csrf' value='" + e(csrf) + "'><label>Model<select name='model_slug'>" + options + "</select></label>" + error('model_slug') + "<label>Version<input name='model_version' value='" + value('model_version') + "' required></label>" + error('model_version') + "<label>Category<select name='task_category'>" + categories + "</select></label>" + error('task_category') + "<label>Task description<textarea name='task_description' minlength='40' maxlength='1000' required>" + value('task_description') + "</textarea></label>" + error('task_description') + "<label>Stack<input name='stack' value='" + value('stack') + "' required></label>" + error('stack') + "<label>Harness<select name='harness'>" + harnesses + "<option value='other'" + (' selected' if selected_other else '') + ">other</option></select></label><label>Other harness details<input name='harness_other' maxlength='40' value='" + e(other) + "'></label>" + error('harness') + "<label>Outcome 1–5<input type='number' name='outcome' min='1' max='5' value='" + value('outcome') + "' required></label>" + error('outcome') + "<label>Retries<input type='number' name='retries' min='0' max='50' value='" + e(values.get('retries', '0')) + "'></label>" + error('retries') + "<label>Cost USD<input type='number' name='cost_usd' step='.01' value='" + value('cost_usd') + "'></label>" + error('cost_usd') + "<label>Date used<input type='date' name='used_on' value='" + value('used_on') + "' required></label>" + error('used_on') + "<label>Self-attested proof line<input name='proof_line' maxlength='300' value='" + value('proof_line') + "'></label>" + error('proof_line') + "<label><input type='checkbox' name='show_handle'" + (" checked" if values.get('show_handle') == 'on' else '') + "> Show my GitHub handle</label><label><input type='checkbox' name='consent' value='on' required" + checked + "> I consent to publication for the stated purpose.</label>" + error('consent') + "<button>Publish review</button></form>"
