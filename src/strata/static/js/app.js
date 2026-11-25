/* Strata custom JavaScript */

// Theme management (light/dark toggle, defaults to browser preference)
(function() {
    var THEME_KEY = 'strata-theme';
    var html = document.documentElement;

    function getSystemTheme() {
        return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }

    function getCurrentTheme() {
        return localStorage.getItem(THEME_KEY) || getSystemTheme();
    }

    function applyTheme(theme) {
        html.setAttribute('data-theme', theme);
    }

    // Apply immediately to prevent FOUC
    applyTheme(getCurrentTheme());

    document.addEventListener('DOMContentLoaded', function() {
        var checkbox = document.getElementById('mode-checkbox');
        if (!checkbox) return;

        checkbox.checked = (getCurrentTheme() === 'dark');

        checkbox.addEventListener('change', function() {
            html.classList.add('trans');
            var theme = checkbox.checked ? 'dark' : 'light';
            applyTheme(theme);
            localStorage.setItem(THEME_KEY, theme);
        });
    });

    // Respond to system preference changes when no stored preference
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function() {
        if (!localStorage.getItem(THEME_KEY)) {
            var theme = getSystemTheme();
            applyTheme(theme);
            var checkbox = document.getElementById('mode-checkbox');
            if (checkbox) checkbox.checked = (theme === 'dark');
        }
    });
})();

// HTMX configuration
document.addEventListener('htmx:configRequest', function(event) {
    // Add timezone header to all HTMX requests
    event.detail.headers['X-Timezone'] = Intl.DateTimeFormat().resolvedOptions().timeZone;
});

// Auto-dismiss flash messages after 5 seconds
document.addEventListener('DOMContentLoaded', function() {
    var flashes = document.querySelectorAll('.flash');
    flashes.forEach(function(flash) {
        setTimeout(function() {
            flash.style.transition = 'opacity 0.5s';
            flash.style.opacity = '0';
            setTimeout(function() {
                flash.remove();
            }, 500);
        }, 5000);
    });
});

// Send browser timezone with all requests
(function() {
    var tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    // Set as HTMX header for AJAX requests
    document.body.setAttribute('hx-headers', JSON.stringify({'X-Timezone': tz}));
    // Set as cookie for full page requests
    document.cookie = 'tz=' + tz + ';path=/;SameSite=Lax';
})();

// A form with data-confirm asks before it submits (the post_button macro's confirm).
document.addEventListener('submit', function(event) {
    var message = event.target.getAttribute && event.target.getAttribute('data-confirm');
    if (message && !window.confirm(message)) event.preventDefault();
});

// Copy-to-clipboard for [data-copy] buttons (Excel and API-key links).
// navigator.clipboard needs a secure context (https/localhost); elsewhere the
// button is a no-op and the text stays selectable.
document.addEventListener('click', function(event) {
    var btn = event.target.closest('[data-copy]');
    if (!btn || !navigator.clipboard) return;
    navigator.clipboard.writeText(btn.getAttribute('data-copy')).then(function() {
        var original = btn.textContent;
        btn.textContent = 'Copied';
        setTimeout(function() { btn.textContent = original; }, 1500);
    });
});

// "Use in Excel": the format picker swaps the shown link and its Copy button.
document.addEventListener('change', function(event) {
    var select = event.target.closest('select.excel-format');
    if (!select) return;
    var box = select.closest('.excel-body');
    box.querySelector('.excel-url').textContent = select.value;
    box.querySelector('.excel-copy').setAttribute('data-copy', select.value);
});

// Hamburger menu on narrow screens (the CSS shows the button only there)
document.addEventListener('DOMContentLoaded', function() {
    var toggle = document.querySelector('.nav-toggle');
    var menu = document.getElementById('nav-menu');
    if (!toggle || !menu) return;

    function setOpen(open) {
        menu.classList.toggle('open', open);
        toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    }

    toggle.addEventListener('click', function(e) {
        e.stopPropagation();
        setOpen(!menu.classList.contains('open'));
    });

    // Close on a tap outside it, or Escape
    document.addEventListener('click', function(e) {
        if (!menu.contains(e.target)) setOpen(false);
    });
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape' && menu.classList.contains('open')) {
            setOpen(false);
            toggle.focus();
        }
    });
});

// A link to a section inside a closed <details> (e.g. #named-keys after creating a
// key there) opens it.
(function () {
    function openTarget() {
        var id = decodeURIComponent(location.hash.slice(1));
        var el = id && document.getElementById(id);
        if (!el) { return; }
        var details = el.tagName === 'DETAILS' ? el : el.closest('details');
        if (details) { details.open = true; el.scrollIntoView(); }
    }
    document.addEventListener('DOMContentLoaded', openTarget);
    window.addEventListener('hashchange', openTarget);
})();

// A <select data-fills="<input name>"> fills that input of its form with the chosen
// option's data-local-name (a dependency: the last part of the datamodule's path; a
// connection: its name), unless someone typed a name of their own there.
document.addEventListener('change', function (e) {
    var sel = e.target;
    if (!(sel instanceof HTMLSelectElement) || !sel.dataset.fills || !sel.form) { return; }
    var input = sel.form.querySelector('[name="' + sel.dataset.fills + '"]');
    if (!input) { return; }
    var opt = sel.selectedOptions[0];
    var suggested = (opt && opt.dataset.localName) || '';
    if (input.value === '' || input.value === input.dataset.filled) {
        input.value = suggested;
        input.dataset.filled = suggested;
    }
});

// Rows added and removed by buttons (the report form's arguments):
// <button data-add-row="<template id>" data-into="<tbody id>"> appends a copy of the
// template's row; a [data-remove-row] button removes its own row. A template whose
// field names hold __N__ gets a number no other row of that body has had (the body's
// data-next, set by the server to its row count). The body then fires "rows-changed".
document.addEventListener('click', function (e) {
    var add = e.target.closest && e.target.closest('[data-add-row]');
    if (add) {
        var tpl = document.getElementById(add.dataset.addRow);
        var body = document.getElementById(add.dataset.into);
        if (tpl && body) {
            var row;
            if (tpl.innerHTML.indexOf('__N__') >= 0) {
                var n = Number(body.dataset.next || body.children.length);
                body.dataset.next = String(n + 1);
                var holder = document.createElement('tbody');
                holder.innerHTML = tpl.innerHTML.replace(/__N__/g, 'n' + n);
                row = holder.firstElementChild;
            } else {
                row = tpl.content.firstElementChild.cloneNode(true);
            }
            body.appendChild(row);
            body.dispatchEvent(new CustomEvent('rows-changed', { bubbles: true }));
            var first = row.querySelector('input');
            if (first) { first.focus(); }
        }
        return;
    }
    var remove = e.target.closest && e.target.closest('[data-remove-row]');
    if (remove) {
        var tr = remove.closest('tr');
        if (tr) {
            var parent = tr.parentElement;
            tr.remove();
            if (parent) { parent.dispatchEvent(new CustomEvent('rows-changed', { bubbles: true })); }
        }
    }
});

// Subtotal rows (templates/reports/_grid.html). In the rows view, ▾ hides its group's
// rows on this page: subtotals are footers, so they are the rows above it back to the
// previous subtotal of its level or an outer one; ▸ shows them again (not remembered).
// In a totals view, ▸ on a group's row loads that group's rows under it (its rows view,
// up to a page, then a link to all of them) and ▾ removes them.
(function () {
    function isFooterAtOrAbove(row, level) {
        return row.dataset.level !== undefined && Number(row.dataset.level) <= level &&
            (row.classList.contains('subtotal') || row.classList.contains('total'));
    }

    function setOpen(btn, open) {
        btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        btn.textContent = open ? '▾' : '▸';
    }

    function toggleBlock(tr, btn) {
        var open = btn.getAttribute('aria-expanded') === 'true';
        var level = Number(tr.dataset.level);
        for (var row = tr.previousElementSibling; row && !isFooterAtOrAbove(row, level);
             row = row.previousElementSibling) {
            row.hidden = open;
        }
        setOpen(btn, !open);
    }

    function loadRows(tr, btn) {
        if (tr.dataset.loaded) {
            document.querySelectorAll('tr[data-of="' + tr.dataset.loaded + '"]').forEach(function (r) { r.remove(); });
            delete tr.dataset.loaded;
            setOpen(btn, false);
            return;
        }
        btn.disabled = true;
        fetch(btn.dataset.rows, { headers: { 'HX-Request': 'true' } })
            .then(function (r) { return r.text(); })
            .then(function (html) {
                var doc = new DOMParser().parseFromString(html, 'text/html');
                var rows = doc.querySelectorAll('.results-container tbody tr:not(.subtotal):not(.total)');
                var id = 'g' + Math.random().toString(36).slice(2);
                var at = tr;
                rows.forEach(function (r) {
                    var copy = document.importNode(r, true);
                    copy.dataset.of = id;
                    copy.classList.add('group-rows');
                    at.after(copy);
                    at = copy;
                });
                var count = Number(btn.dataset.count || 0);
                if (count > rows.length) {
                    var more = document.createElement('tr');
                    more.dataset.of = id;
                    more.className = 'group-rows';
                    var cell = document.createElement('td');
                    cell.colSpan = tr.children.length;
                    var link = document.createElement('a');
                    link.href = btn.dataset.rows;
                    link.textContent = 'All ' + count.toLocaleString() + ' rows';
                    cell.appendChild(link);
                    more.appendChild(cell);
                    at.after(more);
                }
                tr.dataset.loaded = id;
                setOpen(btn, true);
            })
            .finally(function () { btn.disabled = false; });
    }

    document.addEventListener('click', function (e) {
        var btn = e.target.closest && e.target.closest('.group-toggle');
        if (!btn) { return; }
        var tr = btn.closest('tr');
        if (btn.dataset.rows) { loadRows(tr, btn); } else { toggleBlock(tr, btn); }
    });
})();
