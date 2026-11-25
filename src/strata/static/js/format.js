// The Format page (templates/formatting/page.html): every change to the settings is
// posted to the preview endpoint (after a pause in typing), which answers with the
// problems by field, each column's sample and the preview table. Fields with a problem
// get aria-invalid and their message beside them. Saving works without this script:
// the server checks the same rules and shows the same messages.
(function () {
    'use strict';

    var form = document.getElementById('format-form');
    if (!form) { return; }
    var url = form.dataset.preview;
    var timer = null;
    var sent = 0;

    function apply(data) {
        form.querySelectorAll('[data-field]').forEach(function (el) {
            var msg = data.problems[el.dataset.field] || '';
            if (msg) {
                el.setAttribute('aria-invalid', 'true');
                var fold = el.closest('details');
                if (fold) { fold.open = true; } // a closed link section would hide it
            } else {
                el.removeAttribute('aria-invalid');
            }
        });
        form.querySelectorAll('[data-message-for]').forEach(function (el) {
            el.textContent = data.problems[el.dataset.messageFor] || '';
        });
        Object.keys(data.samples).forEach(function (key) {
            var s = data.samples[key];
            var fmt = form.querySelector('[data-sample="' + key + '-format"]');
            var link = form.querySelector('[data-sample="' + key + '-link"]');
            if (fmt) { fmt.textContent = s.format; }
            if (link) { link.textContent = s.link; }
        });
        var summary = document.getElementById('format-problems');
        summary.replaceChildren.apply(summary, data.summary.map(function (m) {
            var div = document.createElement('div');
            div.textContent = m;
            return div;
        }));
        summary.hidden = data.summary.length === 0;
        var refused = document.querySelector('.format-error');
        if (refused) { refused.remove(); } // it described the settings before this change
        document.getElementById('format-preview').innerHTML = data.preview;
    }

    function check() {
        var mine = ++sent;
        fetch(url, { method: 'POST', body: new FormData(form), headers: { Accept: 'application/json' } })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (data) { if (data && mine === sent) { apply(data); } })
            .catch(function () { /* the next change tries again; Save still checks */ });
    }

    function later() {
        clearTimeout(timer);
        timer = setTimeout(check, 400);
    }

    // A swatch for a style word: "Aa" in its class (static/css/styles.css), or in the
    // exact shade, inside a neutral frame so it reads on any chip.
    function swatch(word, esc) {
        var shade = /^(bg-)?([a-z]+)-(\d+)$/.exec(word);
        var inner;
        if (shade) {
            var prop = shade[1] ? 'background-color' : 'color';
            inner = '<span style="' + prop + ':var(--pico-color-' + esc(shade[2]) + '-' +
                esc(shade[3]) + ')">Aa</span>';
        } else {
            inner = '<span class="st-' + esc(word) + '">Aa</span>';
        }
        return '<span class="st-swatch">' + inner + '</span>';
    }

    function attach(root) {
        if (typeof TomSelect === 'undefined') { return; }
        var words = JSON.parse(form.dataset.styleWords || '[]');
        root.querySelectorAll('input[data-styles]').forEach(function (input) {
            if (input.tomselect) { return; }
            new TomSelect(input, {
                delimiter: ' ',
                persist: false,
                maxOptions: null,
                options: words.map(function (w) { return { value: w, text: w }; }),
                create: function (text) { return { value: text.toLowerCase(), text: text.toLowerCase() }; },
                createFilter: /^[A-Za-z0-9-]+$/,
                hidePlaceholder: true,
                render: {
                    option: function (d, esc) { return '<div>' + swatch(d.value, esc) + ' ' + esc(d.text) + '</div>'; },
                    item: function (d, esc) { return '<div>' + swatch(d.value, esc) + ' ' + esc(d.text) + '</div>'; }
                }
            });
        });
        root.querySelectorAll('select[multiple][data-rule-key="columns"]').forEach(function (sel) {
            if (sel.tomselect) { return; }
            new TomSelect(sel, {
                maxOptions: null,
                placeholder: sel.dataset.placeholder,
                hidePlaceholder: true
            });
        });
    }

    // Rule rows are numbered in page order (style:1:when …), as the server numbers them.
    function renumber() {
        form.querySelectorAll('tr[data-rule]').forEach(function (tr, i) {
            tr.querySelectorAll('[data-rule-key]').forEach(function (el) {
                el.dataset.field = 'style:' + (i + 1) + ':' + el.dataset.ruleKey;
            });
            tr.querySelectorAll('[data-rule-msg]').forEach(function (el) {
                el.dataset.messageFor = 'style:' + (i + 1) + ':' + el.dataset.ruleMsg;
            });
        });
    }

    form.addEventListener('rows-changed', function () {
        renumber();
        attach(form);
        later();
    });
    attach(form);

    // "Insert a column…": put {Column} at the cursor of its link field, or "Column" (a
    // quoted SQL name) in a rule's condition.
    form.addEventListener('change', function (e) {
        var pick = e.target.closest('select[data-insert-into]');
        if (!pick) { later(); return; }
        if (!pick.value) { return; }
        var input = document.getElementById(pick.dataset.insertInto);
        var text = 'quote' in pick.dataset
            ? '"' + pick.value.replace(/"/g, '""') + '"'
            : '{' + pick.value + '}';
        // Where the cursor was when the field was left; at the end if it never had one.
        var caret = (input.dataset.caret || '').split(':');
        var at = caret.length === 2 ? Number(caret[0]) : input.value.length;
        var end = caret.length === 2 ? Number(caret[1]) : at;
        input.value = input.value.slice(0, at) + text + input.value.slice(end);
        pick.value = '';
        input.focus();
        input.setSelectionRange(at + text.length, at + text.length);
        later();
    });
    form.addEventListener('input', later);
    form.addEventListener('focusout', function (e) {
        var input = e.target;
        if (input.id && document.querySelector('select[data-insert-into="' + input.id + '"]')) {
            input.dataset.caret = input.selectionStart + ':' + input.selectionEnd;
        }
    });
})();
