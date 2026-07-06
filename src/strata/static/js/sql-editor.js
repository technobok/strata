/* SQL editors: attach CodeMirror to every textarea.sql-editor.
   fromTextArea syncs the textarea back on form submit; the theme follows the
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
            editors.push(CodeMirror.fromTextArea(ta, {
                mode: 'text/x-sql',
                lineNumbers: true,
                indentUnit: 2,
                indentWithTabs: false,
                theme: themeFor(),
            }));
        });
        if (!editors.length) return;
        new MutationObserver(function () {
            var theme = themeFor();
            editors.forEach(function (cm) { cm.setOption('theme', theme); });
        }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    });
})();
