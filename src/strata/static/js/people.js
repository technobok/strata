// The people fields (owners, viewers, alert lists, schedule recipients, a
// connection's "May use"): a <textarea data-people="<suggest URL>"> of one entry
// per line becomes a Tom Select that suggests Gatekeeper's users and @groups as
// you type. The textarea stays in the form (hidden) and is what is submitted, one
// entry per line, so the page works the same without JavaScript. Anything typed
// can be added (an email address on a recipient list, * on a viewer list); the
// server checks every entry when the form is saved.
(function () {
    'use strict';

    function entries(text) {
        return text.split(/\r?\n/).map(function (s) { return s.trim(); }).filter(Boolean);
    }

    function attach(area) {
        if (area.dataset.peopleReady || typeof TomSelect === 'undefined') { return; }
        area.dataset.peopleReady = '1';
        var url = area.dataset.people;
        var initial = entries(area.value);
        var hint = [];
        if (area.dataset.peopleEmails) { hint.push('an email address'); }
        if (area.dataset.peopleEveryone) { hint.push('* for everyone signed in'); }

        var input = document.createElement('input');
        input.type = 'text';
        input.className = 'people-input';
        input.setAttribute('aria-label', area.getAttribute('aria-label') || area.name);
        area.insertAdjacentElement('afterend', input);
        area.hidden = true;

        var ts = new TomSelect(input, {
            plugins: ['remove_button'],
            create: true,
            createOnBlur: true,
            persist: false,
            delimiter: '\n',
            splitOn: /[\n,;]+/,
            maxOptions: 40,
            valueField: 'value',
            labelField: 'value',
            searchField: ['value', 'text', 'detail'],
            // Match as the server does (anywhere in the username, name or email; a
            // leading @ asks for groups), or Tom Select would hide the server's
            // answers: its own matcher needs "@fin" whole, as in "@finance".
            score: function (search) {
                var q = search.trim().toLowerCase().replace(/^@/, '');
                return function (item) {
                    var hay = [item.value, item.text || '', item.detail || ''].join(' ').toLowerCase();
                    return hay.indexOf(q) >= 0 ? 1 : 0;
                };
            },
            options: initial.map(function (e) { return { value: e, text: e }; }),
            items: initial,
            placeholder: area.getAttribute('placeholder') || '',
            hidePlaceholder: true,
            loadThrottle: 250,
            shouldLoad: function (query) { return query.trim().length >= 1; },
            load: function (query, callback) {
                fetch(url + '?q=' + encodeURIComponent(query), {
                    headers: { 'Accept': 'application/json' },
                    credentials: 'same-origin'
                })
                    .then(function (r) { return r.ok ? r.json() : []; })
                    .then(function (found) { callback(found); })
                    .catch(function () { callback(); });
            },
            render: {
                option: function (d, esc) {
                    var name = d.text && d.text !== d.value
                        ? '<span class="people-name">' + esc(d.text) + '</span> ' : '';
                    var detail = d.detail ? ' <small>' + esc(d.detail) + '</small>' : '';
                    return '<div>' + name + '<code>' + esc(d.value) + '</code>' + detail + '</div>';
                },
                item: function (d, esc) {
                    var title = d.text && d.text !== d.value ? d.text : (d.detail || '');
                    return '<div title="' + esc(title) + '">' + esc(d.value) + '</div>';
                },
                option_create: function (d, esc) {
                    return '<div class="create">Add <strong>' + esc(d.input) + '</strong>' +
                        (hint.length ? ' <small>(a username, an @group, ' + hint.join(' or ') + ')</small>' : '') +
                        '</div>';
                },
                no_results: function () {
                    return '<div class="no-results">No user or group matches.</div>';
                }
            },
            onChange: function () {
                area.value = ts.items.join('\n');
            }
        });
    }

    function attachAll(root) {
        (root || document).querySelectorAll('textarea[data-people]').forEach(attach);
    }

    if (window.htmx) {
        htmx.onLoad(attachAll);  // the page, and any part HTMX swaps in later
    } else {
        document.addEventListener('DOMContentLoaded', function () { attachAll(document); });
    }
})();
