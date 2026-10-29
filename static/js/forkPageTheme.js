// Fork: the theme of the fork's standalone pages (/modules/sound, /modules/sound/record,
// /modules/sound/pair, /today, /beliefs). Each page loads this as a plain (not module)
// script in its <head>, after the theme-color meta and before its stylesheets, so the
// colors the owner picked in Odysseus (saved in localStorage as 'odysseus-theme') apply
// from the first paint, like the early script in index.html and login.html.
// theme.js isn't imported: it boots the whole settings UI.
(function () {
  try {
    var t = JSON.parse(localStorage.getItem('odysseus-theme'));
    var s = document.documentElement.style;
    if (t && t.colors) {
      var c = t.colors;
      s.setProperty('--bg', c.bg);
      s.setProperty('--fg', c.fg);
      s.setProperty('--panel', c.panel);
      s.setProperty('--border', c.border);
      if (c.red) s.setProperty('--red', c.red);
      var a = c.advanced || {};
      if (a.inputBg) s.setProperty('--input-bg', a.inputBg);
      if (a.inputBorder) s.setProperty('--input-border', a.inputBorder);
      var m = document.querySelector('meta[name="theme-color"]');
      if (m && c.bg) m.setAttribute('content', c.bg);
    }
    // Same names as FONT_MAP in static/js/theme.js; unknown fonts keep the default.
    var fonts = {
      mono: "'Fira Code', monospace",
      sans: "system-ui, -apple-system, 'Segoe UI', sans-serif",
      serif: "Georgia, 'Times New Roman', serif",
      opendyslexic: "'OpenDyslexic', sans-serif"
    };
    if (t && t.font && fonts[t.font]) s.setProperty('--font-family', fonts[t.font]);
    if (t && t.density && t.density !== 'comfortable') {
      document.documentElement.classList.add('density-' + t.density);
    }
  } catch (e) { /* no saved theme: keep the defaults */ }
})();
