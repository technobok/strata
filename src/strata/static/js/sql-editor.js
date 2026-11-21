/* SQL editors: attach CodeMirror to every textarea.sql-editor.
   Each change is saved back to the textarea at once, so an htmx request that
   includes it (e.g. "Preview rendered script") sends what is on screen. The theme follows the
   site's data-theme attribute (set by app.js / the FOUC guard in base.html). */
(function () {
    function themeFor() {
        return document.documentElement.getAttribute('data-theme') === 'dark'
            ? 'material-darker' : 'default';
    }

    document.addEventListener('DOMContentLoaded', function () {
        if (typeof CodeMirror === 'undefined') return;
        var editors = [];
        document.querySelectorAll('textarea.sql-editor').forEach(function (ta) {
            var cm = CodeMirror.fromTextArea(ta, {
                mode: 'text/x-sql',
                lineNumbers: true,
                indentUnit: 2,
                indentWithTabs: false,
                theme: themeFor(),
            });
            cm.on('change', function () { cm.save(); });
            editors.push(cm);
        });
        if (!editors.length) return;
        new MutationObserver(function () {
            var theme = themeFor();
            editors.forEach(function (cm) { cm.setOption('theme', theme); });
        }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    });
})();
