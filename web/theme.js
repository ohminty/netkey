// Applies the saved/preferred colour theme before first paint (kept separate: CSP forbids inline scripts).
(function () {
  var t = null;
  try { t = localStorage.getItem("netkey-theme"); } catch (e) { /* storage disabled */ }
  if (t !== "light" && t !== "dark") {
    t = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  document.documentElement.setAttribute("data-theme", t);
})();
