/* NetKey web UI, vanilla JS, no dependencies, no build step. */
"use strict";
(() => {

// utilities
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
const raw = (s) => new Raw(String(s));
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
const val = (v) => (v == null || v === false ? "" : v instanceof Raw ? v.s : Array.isArray(v) ? v.map(val).join("") : esc(v));
// Tagged template: interpolations are HTML-escaped unless wrapped in raw()/html``.
const html = (strings, ...vals) => raw(strings.reduce((acc, s, i) => acc + val(vals[i - 1]) + s));
const icon = (name, cls = "") => raw(`<svg class="i ${cls}" aria-hidden="true"><use href="#i-${name}"></use></svg>`);
const spinner = () => raw('<span class="spinner" aria-hidden="true"></span>');
const pad2 = (n) => String(n).padStart(2, "0");
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const cap = (s) => String(s || "").charAt(0).toUpperCase() + String(s || "").slice(1);
const humanize = (s) => cap(String(s || "").replace(/_/g, " "));
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const store = {
  get(k, d) { try { const v = localStorage.getItem("netkey-" + k); return v == null ? d : v; } catch { return d; } },
  set(k, v) { try { localStorage.setItem("netkey-" + k, v); } catch { /* ignore */ } },
};

/* Minimal DOM morphing: patches `el` to match the HTML string without recreating unchanged nodes,
   so focus, scroll, hover and running CSS animations survive the 2-3 s status refresh. */
function patch(el, markup) {
  const tpl = document.createElement("template");
  tpl.innerHTML = String(markup);
  morphChildren(el, tpl.content);
}
function morphChildren(from, to) {
  const oldKids = Array.from(from.childNodes);
  const newKids = Array.from(to.childNodes);
  newKids.forEach((n, i) => {
    const o = oldKids[i];
    if (!o) { from.appendChild(n); return; }
    if (o.nodeType !== n.nodeType || o.nodeName !== n.nodeName ||
        (o.nodeType === 1 && o.getAttribute("data-key") !== n.getAttribute("data-key"))) {
      from.replaceChild(n, o);
      return;
    }
    if (o.nodeType !== 1) { if (o.nodeValue !== n.nodeValue) o.nodeValue = n.nodeValue; return; }
    syncAttrs(o, n);
    if (o.hasAttribute("data-skip") || o.nodeName === "TEXTAREA") return;
    morphChildren(o, n);
  });
  for (let i = oldKids.length - 1; i >= newKids.length; i--) from.removeChild(oldKids[i]);
}
function syncAttrs(o, n) {
  for (const a of Array.from(o.attributes)) if (!n.hasAttribute(a.name)) o.removeAttribute(a.name);
  for (const a of Array.from(n.attributes)) if (o.getAttribute(a.name) !== a.value) o.setAttribute(a.name, a.value);
  const tag = o.nodeName;
  if ((tag === "INPUT" || tag === "SELECT") && o !== document.activeElement) {
    if (o.type === "checkbox" || o.type === "radio") o.checked = n.hasAttribute("checked");
    else if (tag === "SELECT") { if (n.hasAttribute("data-value")) o.value = n.getAttribute("data-value"); }
    else if (n.hasAttribute("value") && o.value !== n.getAttribute("value")) o.value = n.getAttribute("value");
  }
}

// formatting
let skew = 0;                                    // server clock - browser clock (s)
const nowS = () => Date.now() / 1000 + skew;

function fmtBytes(n) {
  if (n == null || !isFinite(n)) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  n = Number(n);
  while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? Math.round(n) : n.toFixed(Math.abs(n) >= 100 ? 0 : 1)) + " " + u[i];
}
const fmtRate = (n) => (n == null ? "—" : fmtBytes(n) + "/s");
function fmtKbit(n) {
  if (n == null) return "—";
  const k = (n * 8) / 1000;
  return k >= 1000 ? (k / 1000).toFixed(2) + " Mbit/s" : k.toFixed(k >= 100 ? 0 : 1) + " kbit/s";
}
function fmtDur(s) {
  if (s == null || !isFinite(s)) return "—";
  s = Math.max(0, Math.floor(s));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  if (d) return `${d}d ${pad2(h)}h`;
  if (h) return `${h}h ${pad2(m)}m`;
  if (m) return `${m}m ${pad2(x)}s`;
  return `${x}s`;
}
const fmtNum = (n) => (n == null ? "—" : Number(n).toLocaleString());
const fmtAbs = (t) => (t ? new Date(t * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" }) : "");
function fmtRel(t) {
  if (!t) return "never";
  const d = nowS() - t, a = Math.abs(d), sfx = d >= 0 ? " ago" : " from now";
  if (a < 45) return "just now";
  if (a < 90) return "1 min" + sfx;
  if (a < 3600) return Math.round(a / 60) + " min" + sfx;
  if (a < 86400 * 1.5) return Math.round(a / 3600) + " h" + sfx;
  const days = Math.round(a / 86400);
  if (days === 1) return d > 0 ? "yesterday" : "tomorrow";
  if (days < 7) return days + " days" + sfx;
  return new Date(t * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: days > 300 ? "numeric" : undefined });
}
function fmtWhen(t) {
  const d = new Date(t * 1000), now = new Date(nowS() * 1000);
  const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return time;
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return "Yesterday " + time;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: d.getFullYear() !== now.getFullYear() ? "numeric" : undefined }) + " " + time;
}
const relTime = (t, cls = "") => (t ? html`<time class="${cls}" datetime="${new Date(t * 1000).toISOString()}" title="${fmtAbs(t)}">${fmtRel(t)}</time>` : html`<span class="faint ${cls}">never</span>`);
const ticking = (since) => html`<span class="tnum" data-since="${since}">${fmtDur(nowS() - since)}</span>`;

// API
class ApiError extends Error { constructor(msg, status, data = null) { super(msg); this.status = status; this.data = data; } }
let onUnauthorized = () => {};
let onPasswordRequired = () => {};
let onOnline = () => {};

async function api(path, { method = "GET", body } = {}) {
  const opts = { method, credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } };
  if (method !== "GET") {
    opts.headers["X-Requested-With"] = "netkey";
    opts.headers["Content-Type"] = "application/json";
    // only send a body when there is one: the server does not drain unread bodies on keep-alive connections
    if (body !== undefined) opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    onOnline(false);
    throw new ApiError("Cannot reach the NetKey web service. Check the network connection.", 0);
  }
  onOnline(true);
  let data = null;
  try { data = await res.json(); } catch { /* empty or non-JSON body */ }
  if (res.status === 401 && path !== "/api/login") {
    onUnauthorized();
    throw new ApiError((data && data.error) || "Login required", 401);
  }
  if (res.status === 403 && data && data.code === "password_change_required") onPasswordRequired();
  if (!res.ok) throw new ApiError((data && data.error) || `Request failed (HTTP ${res.status})`, res.status, data);
  return data || {};
}

// toasts
function toast(msg, { type = "ok", detail = "", timeout = 4500 } = {}) {
  const box = $("#toasts");
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.setAttribute("role", type === "err" ? "alert" : "status");
  const ic = type === "err" ? "alert" : type === "info" ? "info" : "check";
  el.innerHTML = String(html`${icon(ic)}<div class="tb"><span>${msg}</span>${detail ? html`<small>${detail}</small>` : ""}</div>
    <button type="button" aria-label="Dismiss">${icon("x", "i-sm")}</button><i class="bar" style="animation-duration:${timeout}ms"></i>`);
  const close = () => {
    if (el.classList.contains("leaving")) return;
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 200);
  };
  el.querySelector("button").addEventListener("click", close);
  let timer = setTimeout(close, timeout);
  el.addEventListener("mouseenter", () => { clearTimeout(timer); el.querySelector(".bar").style.animationPlayState = "paused"; });
  el.addEventListener("mouseleave", () => { timer = setTimeout(close, 1800); });
  box.appendChild(el);
  while (box.children.length > 4) box.firstElementChild.remove();
}
const toastErr = (e, prefix) => toast(prefix || e.message, { type: "err", detail: prefix ? e.message : "", timeout: 6500 });

// modals
const modals = [];
// Opens a modal dialog. body/foot are html`` values. If onSubmit is given the dialog is a <form>:
// Enter submits, the handler may return false (or throw) to keep it open.
function openModal({ title, body, foot, iconName, iconCls = "", onSubmit, onClose, wide = false, initialFocus }) {
  const prevFocus = document.activeElement;
  const bd = document.createElement("div");
  bd.className = "modal-backdrop";
  const id = "m" + Math.random().toString(36).slice(2, 8);
  bd.innerHTML = String(html`<form class="modal" role="dialog" aria-modal="true" aria-labelledby="${id}" novalidate style="${wide ? "max-width:560px" : ""}">
    <div class="modal-head">${iconName ? html`<span class="modal-icon ${iconCls}">${icon(iconName)}</span>` : ""}<h2 id="${id}">${title}</h2>
      <button type="button" class="btn btn-ghost btn-icon btn-sm" data-close aria-label="Close">${icon("x")}</button></div>
    <div class="modal-body">${body}</div>
    <div class="modal-foot">${foot}</div></form>`);
  const form = bd.firstElementChild;
  let closed = false;
  const m = {
    el: form,
    close(result) {
      if (closed) return;
      closed = true;
      modals.splice(modals.indexOf(m), 1);
      bd.classList.add("closing");
      setTimeout(() => bd.remove(), 150);
      if (prevFocus && prevFocus.focus && document.contains(prevFocus)) prevFocus.focus({ preventScroll: true });
      onClose && onClose(result);
    },
    busy(on) {
      $$("button, input, select", form).forEach((b) => { if (on) { b.dataset.wasDisabled = b.disabled ? "1" : ""; b.disabled = true; } else b.disabled = b.dataset.wasDisabled === "1"; });
      const sb = form.querySelector('[type="submit"]');
      if (sb) sb.classList.toggle("is-busy", on);
    },
  };
  bd.addEventListener("mousedown", (e) => { if (e.target === bd) m.close(false); });
  form.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) { e.preventDefault(); m.close(false); } });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!onSubmit) return m.close(true);
    m.busy(true);
    let ok;
    try { ok = await onSubmit(form, m); } catch (err) { ok = false; if (err) toastErr(err); }
    if (closed) return;
    m.busy(false);
    if (ok !== false) m.close(true);
  });
  form.addEventListener("keydown", (e) => {
    if (e.key !== "Tab") return;
    const f = $$('button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), [href], [tabindex]:not([tabindex="-1"])', form)
      .filter((x) => x.offsetParent !== null);
    if (!f.length) return;
    if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
    else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
  });
  $("#modal-root").appendChild(bd);
  modals.push(m);
  const focusEl = (initialFocus && form.querySelector(initialFocus)) || form.querySelector("input:not([readonly]), select") || form.querySelector('[type="submit"]');
  requestAnimationFrame(() => focusEl && focusEl.focus());
  return m;
}
function confirmDialog({ title, message, confirmText = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    openModal({
      title, iconName: danger ? "alert" : "info", iconCls: danger ? "danger" : "",
      body: html`<p>${message}</p>`,
      foot: html`<button type="button" class="btn" data-close>Cancel</button>
        <button type="submit" class="btn ${danger ? "btn-danger" : "btn-primary"}">${confirmText}</button>`,
      initialFocus: '[type="submit"]',
      onClose: (r) => resolve(!!r),
    });
  });
}
function closeAllModals() { [...modals].forEach((m) => m.close(false)); }

async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
  } catch { /* fall through */ }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;opacity:0;left:-9999px";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { ok = false; }
  ta.remove();
  return ok;
}

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

// charts (hand-rolled SVG)
let chartSeq = 0;
const f1 = (n) => Math.round(n * 10) / 10;

// Monotone cubic (Fritsch–Carlson / d3 monotoneX) path through points; never overshoots.
function monoPath(pts, move = true) {
  const n = pts.length;
  if (!n) return "";
  let d = (move ? "M" : "L") + f1(pts[0][0]) + "," + f1(pts[0][1]);
  if (n === 1) return d;
  if (n === 2) return d + "L" + f1(pts[1][0]) + "," + f1(pts[1][1]);
  const dx = [], m = [], tan = [];
  for (let i = 0; i < n - 1; i++) {
    dx[i] = pts[i + 1][0] - pts[i][0];
    m[i] = dx[i] ? (pts[i + 1][1] - pts[i][1]) / dx[i] : 0;
  }
  tan[0] = m[0];
  tan[n - 1] = m[n - 2];
  for (let i = 1; i < n - 1; i++) {
    if (m[i - 1] * m[i] <= 0) tan[i] = 0;
    else tan[i] = (3 * (dx[i - 1] + dx[i])) / ((2 * dx[i] + dx[i - 1]) / m[i - 1] + (dx[i] + 2 * dx[i - 1]) / m[i]);
  }
  for (let i = 0; i < n - 1; i++) {
    const h = dx[i] / 3, [x0, y0] = pts[i], [x1, y1] = pts[i + 1];
    d += `C${f1(x0 + h)},${f1(y0 + tan[i] * h)} ${f1(x1 - h)},${f1(y1 - tan[i + 1] * h)} ${f1(x1)},${f1(y1)}`;
  }
  return d;
}
// Step-after path (value holds until the next sample).
function stepPath(pts, move = true, reverse = false) {
  if (!pts.length) return "";
  let d = (move ? "M" : "L") + f1(pts[0][0]) + "," + f1(pts[0][1]);
  for (let i = 1; i < pts.length; i++) {
    d += reverse ? `V${f1(pts[i][1])}H${f1(pts[i][0])}` : `H${f1(pts[i][0])}V${f1(pts[i][1])}`;
  }
  return d;
}
function niceStep(range, count) {
  const raw0 = range / Math.max(1, count);
  const p = Math.pow(10, Math.floor(Math.log10(raw0)));
  const r = raw0 / p;
  return (r <= 1 ? 1 : r <= 2 ? 2 : r <= 2.5 ? 2.5 : r <= 5 ? 5 : 10) * p;
}
const TIME_STEPS = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800];
const WD = (t) => { const d = new Date(t * 1000); return d.toLocaleDateString(undefined, { weekday: "short" }) + " " + d.getDate(); };
const HM = (t) => { const d = new Date(t * 1000); return pad2(d.getHours()) + ":" + pad2(d.getMinutes()); };

class Chart {
  // opts: { yFmt(v) axis label, tipFmt(v) tooltip value, integer, stacked }
  // data: { t: [epoch], step, series: [{ label, color, values, area, stepped, dash }] }
  constructor(el, opts = {}) {
    this.el = el;
    this.opts = opts;
    this.uid = "ch" + ++chartSeq;
    this.data = undefined;
    this.hoverX = null;
    el.innerHTML = '<svg role="img"></svg><div class="tooltip" style="opacity:0"></div>';
    this.svg = el.firstElementChild;
    this.tip = el.lastElementChild;
    this.ro = new ResizeObserver(() => this.draw());
    this.ro.observe(el);
    el.addEventListener("pointermove", (e) => { this.hoverX = e.clientX - el.getBoundingClientRect().left; this.hover(); });
    el.addEventListener("pointerdown", (e) => { this.hoverX = e.clientX - el.getBoundingClientRect().left; this.hover(); });
    el.addEventListener("pointerleave", () => { this.hoverX = null; this.hover(); });
  }
  destroy() { this.ro.disconnect(); }
  set(data) { this.data = data; this.draw(); }

  draw() {
    const el = this.el, W = el.clientWidth, H = el.clientHeight;
    if (!W || !H) return;
    const d = this.data;
    el.querySelectorAll(".chart-empty, .skel").forEach((x) => x.remove());
    if (d === undefined) {
      this.svg.innerHTML = "";
      el.insertAdjacentHTML("beforeend", '<div class="skel" style="position:absolute;inset:6px 0 22px 0"></div>');
      return;
    }
    const o = this.opts;
    const pl = 52, pr = 12, pt = 10, pb = 24, iw = Math.max(10, W - pl - pr), ih = Math.max(10, H - pt - pb);
    const t = (d && d.t) || [], n = t.length;
    // stacking: each series gets top/base arrays
    const acc = new Array(n).fill(0);
    const series = ((d && d.series) || []).map((s) => {
      const base = o.stacked ? acc.slice() : new Array(n).fill(0);
      const top = s.values.map((v, i) => {
        if (v == null) return null;
        if (o.stacked) { acc[i] += v; return acc[i]; }
        return v;
      });
      return { ...s, base, top };
    });
    let max = 0;
    series.forEach((s) => s.top.forEach((v) => { if (v != null && v > max) max = v; }));
    const has = n > 0 && series.some((s) => s.top.some((v) => v != null));
    const count = Math.max(2, Math.min(5, Math.floor(ih / 40)));
    // byte rates get ticks on round KB/MB values instead of decimal ones
    const unit = o.bytes && max >= 1024 ? Math.pow(1024, Math.floor(Math.log(max) / Math.log(1024))) : 1;
    let step = niceStep((max || 1) / unit, count) * unit;
    if (o.integer) step = Math.max(1, Math.ceil(step));
    const yMax = Math.max(step, Math.ceil((max || (o.integer ? 1 : 1)) / step) * step);
    const t0 = n ? t[0] : nowS() - 3600, t1 = n > 1 ? t[n - 1] : t0 + ((d && d.step) || 60);
    const X = (tt) => pl + ((tt - t0) / (t1 - t0)) * iw;
    const Y = (v) => pt + ih - (v / yMax) * ih;
    Object.assign(this, { pl, pr, pt, pb, iw, ih, t0, t1, X, Y, series, W, H, n });

    let out = "<defs>";
    series.forEach((s, k) => {
      if (!s.area) return;
      const a0 = o.stacked ? 0.4 : 0.14, a1 = a0;   // flat fill: the line carries the data
      out += `<linearGradient id="${this.uid}g${k}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${s.color}" stop-opacity="${a0}"/><stop offset="1" stop-color="${s.color}" stop-opacity="${a1}"/></linearGradient>`;
    });
    out += `<clipPath id="${this.uid}c"><rect x="${pl}" y="${pt - 2}" width="${iw}" height="${ih + 4}"/></clipPath></defs>`;
    // horizontal grid + y labels
    for (let v = 0; v <= yMax + 1e-9; v += step) {
      const y = Math.round(Y(v)) + 0.5;
      out += `<line class="grid-line" x1="${pl}" x2="${pl + iw}" y1="${y}" y2="${y}"/>`;
      if (has || v === 0) out += `<text class="axis-label" x="${pl - 8}" y="${y + 3.5}" text-anchor="end">${esc(o.yFmt ? o.yFmt(v) : v)}</text>`;
    }
    // time axis
    const span = t1 - t0, maxTicks = Math.max(2, Math.floor(iw / 78));
    const iv = TIME_STEPS.find((s) => span / s <= maxTicks) || 604800;
    const off = new Date(t0 * 1000).getTimezoneOffset() * 60;
    for (let tt = Math.ceil((t0 - off) / iv) * iv + off; tt <= t1; tt += iv) {
      const x = X(tt);
      if (x < pl + 14 || x > pl + iw - 14) continue;
      const midnight = (tt - off) % 86400 === 0;
      const label = iv >= 86400 || (midnight && span > 6 * 3600) ? WD(tt) : HM(tt);
      out += `<line class="grid-line" x1="${Math.round(x) + 0.5}" x2="${Math.round(x) + 0.5}" y1="${pt + ih}" y2="${pt + ih + 4}"/>`;
      out += `<text class="axis-label" x="${f1(x)}" y="${H - 6}" text-anchor="middle">${esc(label)}</text>`;
    }
    // series
    out += `<g clip-path="url(#${this.uid}c)">`;
    const fills = [], strokes = [];
    series.forEach((s, k) => {
      const segs = [];
      let cur = null;
      for (let i = 0; i < n; i++) {
        if (s.top[i] == null) { cur = null; continue; }
        if (!cur) segs.push((cur = []));
        cur.push(i);
      }
      segs.forEach((seg) => {
        let pts = seg.map((i) => [X(t[i]), s.top[i], s.base[i]]);
        if (s.stepped) {                                   // hold the last value for one step
          const last = seg[seg.length - 1];
          pts.push([Math.min(X(t[last] + ((d && d.step) || 0)), pl + iw), s.top[last], s.base[last]]);
        } else if (pts.length === 1) {                     // isolated sample -> short flat stub
          const p = pts[0];
          pts = [[p[0] - 3, p[1], p[2]], [p[0] + 3, p[1], p[2]]];
        }
        const top = pts.map((p) => [p[0], Y(p[1])]);
        const line = (s.stepped ? stepPath : monoPath)(top);
        if (s.area) {
          const base = pts.map((p) => [p[0], Y(p[2])]).reverse();
          const back = o.stacked ? (s.stepped ? stepPath(base, false, true) : monoPath(base, false)) : `L${f1(base[0][0])},${f1(Y(0))}L${f1(base[base.length - 1][0])},${f1(Y(0))}`;
          fills.push(`<path d="${line}${back}Z" fill="url(#${this.uid}g${k})" stroke="none"/>`);
        }
        strokes.push(`<path d="${line}" fill="none" stroke="${s.color}" stroke-width="${s.width || 1.8}" stroke-linejoin="round" stroke-linecap="round"${s.dash ? ' stroke-dasharray="4 4"' : ""}${s.opacity ? ` stroke-opacity="${s.opacity}"` : ""}/>`);
      });
    });
    out += fills.join("") + strokes.join("") + "</g>";
    out += `<line class="grid-line" x1="${pl}" x2="${pl + iw}" y1="${Math.round(Y(0)) + 0.5}" y2="${Math.round(Y(0)) + 0.5}" style="stroke:var(--border-2)"/>`;
    out += `<g class="hover-layer"></g>`;
    this.svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    this.svg.setAttribute("aria-label", (d && d.label) || "chart");
    this.svg.innerHTML = out;
    if (!has) {
      el.insertAdjacentHTML("beforeend", `<div class="chart-empty" style="left:${pl}px"><div>${icon("activity")}${esc((d && d.emptyText) || "No data for this period")}</div></div>`);
    }
    this.hover();
  }

  hover() {
    const layer = this.svg.querySelector(".hover-layer");
    const tip = this.tip;
    if (!layer) return;
    const d = this.data;
    if (this.hoverX == null || !d || !this.n || this.hoverX < this.pl - 4 || this.hoverX > this.pl + this.iw + 4) {
      layer.innerHTML = "";
      tip.style.opacity = "0";
      tip.style.left = "0px";
      return;
    }
    const { t } = d, tt = this.t0 + ((this.hoverX - this.pl) / this.iw) * (this.t1 - this.t0);
    let lo = 0, hi = this.n - 1;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (t[mid] < tt) lo = mid; else hi = mid; }
    const i = Math.abs(t[lo] - tt) <= Math.abs(t[hi] - tt) ? lo : hi;
    const x = f1(this.X(t[i]));
    let g = `<line class="crosshair" x1="${x}" x2="${x}" y1="${this.pt}" y2="${this.pt + this.ih}"/>`;
    const rows = [];
    let any = false;
    this.series.forEach((s) => {
      const v = s.values[i];
      if (v != null) any = true;
      if (v != null && !s.noDot) g += `<circle class="hover-dot" cx="${x}" cy="${f1(this.Y(s.top[i]))}" r="3.5" fill="${s.color}"/>`;
      if (!s.hideTip) rows.push(`<div class="r"><i class="sw" style="background:${s.color}"></i><span>${esc(s.label)}</span><b>${v == null ? "—" : esc((this.opts.tipFmt || String)(v))}</b></div>`);
    });
    layer.innerHTML = g;
    const step = d.step || 60;
    const when = new Date(t[i] * 1000);
    let title;
    if (step < 60) title = when.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    else if (step < 3600) title = when.toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" }) + (step > 60 ? ` – ${HM(t[i] + step)}` : "");
    else title = when.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) + ` – ${HM(t[i] + step)}`;
    tip.innerHTML = `<div class="t">${esc(title)}</div>` + (any ? rows.join("") + (this.opts.tipExtra ? this.opts.tipExtra(i, d) : "") : '<div class="r faint">No data (service down)</div>');
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let left = x + 14;
    if (left + tw > this.W) left = x - tw - 14;
    tip.style.left = clamp(left, 0, Math.max(0, this.W - tw)) + "px";
    tip.style.top = clamp(this.pt, 0, Math.max(0, this.H - th)) + "px";
    tip.style.opacity = "1";
  }
}

// shared state & view helpers
const S = {
  authed: false, session: null, status: null, online: true,
  range: ["live", "1h", "24h", "7d"].includes(store.get("range")) ? store.get("range") : "live",
};
const LINE_STATES = {
  unplugged: { label: "Unplugged", cls: "gray", text: "USB-serial adapter not connected" },
  stopped: { label: "Stopped", cls: "gray", text: "Modem service is not running" },
  starting: { label: "Starting", cls: "blue pill-pulse", text: "Starting line worker…" },
  idle: { label: "Ready", cls: "green", text: "" },
  dialing: { label: "Dialing", cls: "blue pill-pulse", text: "" },
  initializing: { label: "Initializing", cls: "blue pill-pulse", text: "Sending init string…" },
  modem_error: { label: "Modem error", cls: "red", text: "Modem not responding" },
  ringing: { label: "Ringing", cls: "amber pill-ring", text: "" },
  answering: { label: "Answering", cls: "amber pill-pulse", text: "" },
  connected: { label: "Connected", cls: "green pill-glow", text: "" },
};
const RESULTS = {
  ongoing: { label: "Ongoing", cls: "green pill-glow" },
  connected: { label: "Ongoing", cls: "green pill-glow" },
  ok: { label: "Completed", cls: "green" },
  auth_failed: { label: "Auth failed", cls: "red" },
  no_carrier: { label: "No carrier", cls: "amber" },
  timeout: { label: "Timeout", cls: "amber" },
  negotiation_failed: { label: "PPP failed", cls: "amber" },
  dropped: { label: "Dropped", cls: "amber" },
  error: { label: "Error", cls: "red" },
  busy: { label: "Busy", cls: "amber" },
  no_answer: { label: "No answer", cls: "amber" },
  missed: { label: "Missed", cls: "amber" },
  no_dialtone: { label: "No dial tone", cls: "red" },
  cancelled: { label: "Cancelled", cls: "gray" },
};
const LAN_STATES = {
  up: { label: "Active", cls: "green" },
  no_nic: { label: "No port", cls: "gray" },
  disabled: { label: "Off", cls: "gray" },
  error: { label: "Error", cls: "red" },
  stopped: { label: "Service not running", cls: "gray" },
};
// PCs on the retro LAN: DHCP leases plus PCs with a fixed address the box hears (netkey-lan's scan)
const lanPcCount = (lan) => (lan ? (lan.leases || []).length + (lan.static_pcs || []).length : 0);
const lanPill = (lan) => {
  const s = LAN_STATES[lan.state] || { label: humanize(lan.state || "unknown"), cls: "gray" };
  const glow = lan.state === "up" && lanPcCount(lan) ? " pill-glow" : "";
  return html`<span class="pill pill-${raw(s.cls + glow)}">${s.label}</span>`;
};
const MODE_NAME = { virtual: "Virtual", physical: "Physical" };
const PALETTE = ["#3ddc84", "#5ea8ff", "#f5a524", "#a98bfa", "#f472b6", "#22d3ee", "#facc15", "#fb7185"];

const statePill = (state) => {
  const s = LINE_STATES[state] || { label: humanize(state || "unknown"), cls: "gray" };
  return html`<span class="pill pill-${raw(s.cls)}">${s.label}</span>`;
};
const modeBadge = (mode, title) => (mode === "call"
  ? html`<span class="badge badge-call" title="${title || "Modem call: a NetKey number or a line on this box"}">CALL</span>`
  : mode === "physical"
  ? html`<span class="badge badge-pmm" title="${title || "Physical Modem Mode: a real modem answers phone calls"}">PMM</span>`
  : html`<span class="badge badge-vmm" title="${title || "Virtual Modem Mode: the Pi emulates a Hayes modem"}">VMM</span>`);
const liveCall = (c) => {
  if (c.ended != null || !S.status) return null;
  const l = S.status.lines.find((x) => x.line === c.line && x.call && Math.abs(x.call.started - c.started) < 2);
  return l ? l.call : null;
};
const callKey = (c) => (c.ended == null && c.result === "connected" ? "ongoing" : c.result);
const resultPill = (c) => {
  const r = RESULTS[callKey(c)] || { label: humanize(c.result || "unknown"), cls: "gray" };
  return html`<span class="pill pill-${raw(r.cls)}" title="${c.detail || ""}">${r.label}</span>`;
};
function avatar(name) {
  let h = 0;
  for (const ch of String(name)) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return html`<span class="avatar" style="background:hsl(${h} 45% 40%)" aria-hidden="true">${String(name).slice(0, 1)}</span>`;
}
function syncChip(sync) {
  if (!sync) return "";
  if (sync.error) return html`<span class="chip chip-err" title="${sync.error}">${icon("alert", "i-sm")} Sync failed: ${sync.error}</span>`;
  if (sync.pending) return html`<span class="chip chip-warn">${spinner()} Applying to dial-in server…</span>`;
  if (sync.synced_at) return html`<span class="chip chip-ok" title="Last applied ${fmtAbs(sync.synced_at)}">${icon("check", "i-sm")} Applied to dial-in server</span>`;
  return html`<span class="chip" title="The root helper has not confirmed the user list yet">${icon("clock", "i-sm")} Not applied yet</span>`;
}
const emptyState = (ic, title, text, action = "") =>
  html`<div class="empty"><div class="empty-icon">${icon(ic)}</div><h3>${title}</h3><p>${text}</p>${action}</div>`;
const skelRows = (rows, cols) => html`${Array.from({ length: rows }, () => html`<tr>${Array.from({ length: cols }, (_, i) => html`<td><div class="skel skel-line" style="width:${40 + ((i * 37 + rows * 13) % 50)}%"></div></td>`)}</tr>`)}`;
const segmented = (items, current, attrs = "", cls = "", label = "") => html`<div class="seg ${cls}" role="group" aria-label="${label}">${items.map((it) =>
  html`<button type="button" ${raw(attrs)} data-value="${it.value}" aria-pressed="${String(String(current) === String(it.value))}" ${it.disabled ? raw("disabled") : ""} title="${it.title || ""}">${it.label}</button>`)}</div>`;

// chrome (sidebar, chips, banners)
function renderChrome() {
  const st = S.status, sess = S.session || {};
  const host = (st && st.host && st.host.hostname) || sess.hostname || "";
  $("#brand-host").textContent = (st && st.box_name) || host;
  $("#side-version").textContent = st && st.version ? `NetKey ${st.version}` : "";
  const demo = (st && st.service && st.service.demo) || sess.demo;
  const sum = st && st.summary;
  patch($("#top-chips"), html`
    ${demo ? html`<span class="chip chip-demo" title="Simulated lines and calls (--demo)">${icon("activity", "i-sm")}<span class="lbl">Demo data</span></span>` : ""}
    ${sum ? html`<span class="chip" title="${sum.connected} connected, ${sum.busy} busy"><span class="dot ${sum.connected ? "dot-live" : ""}"></span><b class="tnum">${sum.connected}</b><span class="lbl">online</span></span>` : ""}`);
  if (st) {
    const lanPcs = st.lan && st.lan.state === "up" ? lanPcCount(st.lan) : 0;
    $$("#nav a[data-route=dashboard], #nav a[data-route=modem], #nav a[data-route=lan]").forEach((a) => {
      let c = a.querySelector(".count");
      const n = a.dataset.route === "modem" ? sum.connected : a.dataset.route === "lan" ? lanPcs : 0;
      if (n) {
        if (!c) { c = document.createElement("span"); c.className = "count"; a.appendChild(c); }
        c.textContent = n;
        c.title = a.dataset.route === "lan" ? `${n} PC${n === 1 ? "" : "s"} on the retro LAN` : `${n} connected`;
      } else if (c) c.remove();
    });
  }
  const banners = [];
  if (S.updated) banners.push(html`<div class="banner" role="status">${icon("reset")}<span><b>NetKey was updated on this box.</b> <span class="muted">Reload the page to use the new version.</span></span><button type="button" class="btn btn-sm btn-primary" data-action="reload-page">Reload</button></div>`);
  const up = st && st.update, uj = st && st.update_job;
  const updating = uj && (UPDATE_RUNNING.includes(uj.state) || (uj.state === "done" && up && uj.target === up.latest));
  if (up && up.available && !updating && currentName === "dashboard" && (up.required || updateDismissed() !== up.latest)) {   // the Dashboard only
    banners.push(html`<div class="banner ${up.required ? "banner-warn" : ""}" role="status" data-key="upd-${up.latest}">${icon(up.required ? "alert" : "download")}<span><b>${up.required ? `Update required: NetKey ${up.latest}` : `NetKey ${up.latest} is available.`}</b> <span class="muted">${up.required ? `The cloud hub needs a newer version than ${up.current} for parties.` : `This box runs ${up.current}. Nothing updates by itself.`}</span></span>
      <button type="button" class="btn btn-sm btn-primary" data-action="update-now">Update now</button><button type="button" class="btn btn-sm btn-ghost" data-action="update-howto">Show command</button>${up.required ? "" : html`<button type="button" class="btn btn-sm btn-ghost" data-action="update-later">Hide</button>`}</div>`);
  }
  const job = st && st.update_job;
  if (job && UPDATE_RUNNING.includes(job.state)) {
    banners.push(html`<div class="banner" role="status" data-key="upd-run">${spinner()}<span><b>Updating to NetKey ${job.target || ""}…</b> <span class="muted">${job.message || ""}</span></span><button type="button" class="btn btn-sm" data-action="update-progress">Show progress</button></div>`);
  } else if (job && ["done", "failed", "rolled_back"].includes(job.state) && job.updated && nowS() - job.updated < 6 * 3600
             && currentName === "dashboard" && updateSeen() !== String(job.updated)) {
    const ok = job.state === "done";
    banners.push(html`<div class="banner ${ok ? "" : "banner-warn"}" role="status" data-key="upd-end">${icon(ok ? "check" : "alert")}<span><b>${ok ? `Updated to NetKey ${job.target}.` : job.state === "rolled_back" ? `The update to ${job.target} failed; ${job.from} was put back.` : `The update to ${job.target} failed.`}</b> <span class="muted">${ok ? "" : job.message || ""}</span></span>
      <button type="button" class="btn btn-sm btn-ghost" data-action="update-progress">Details</button><button type="button" class="btn btn-sm btn-ghost" data-action="update-seen" data-t="${job.updated}">Dismiss</button></div>`);
  }
  if (!S.online) banners.push(html`<div class="banner banner-warn" role="status">${spinner()}<span><b>Connection lost.</b> <span class="muted">Retrying… the values below may be stale.</span></span></div>`);
  if (st && st.admin && st.admin.default_credentials && currentName !== "settings") banners.push(html`<div class="banner banner-warn" role="alert">${icon("lock")}<span><b>Default login is still set.</b> <span class="muted">Anyone on your network can use admin / admin.</span> <a href="#/settings">Change it</a></span></div>`);
  if (st && st.service.manager === "stopped") banners.push(html`<div class="banner banner-err" role="alert">${icon("alert")}<span><b>Modem service is not running.</b> <span class="muted">No line can answer calls. Start it on the Pi with <code>sudo systemctl start netkey-modem</code>.</span></span></div>`);
  patch($("#banners"), html`${banners}`);
}

// page: dashboard
function lineCard(l) {
  const st = LINE_STATES[l.state] || { text: "" };
  const c = l.call;
  const cls = c ? "is-connected" : ["ringing", "answering", "dialing"].includes(l.state) ? "is-ringing"
    : l.state === "modem_error" ? "is-error" : ["unplugged", "stopped"].includes(l.state) ? "is-off" : "";
  const idleText = l.mode === "physical" ? "Waiting for a call" : "Waiting for ATD from the PC";
  const detail = l.detail || st.text || (l.state === "idle" ? idleText : "");
  const pending = l.running_mode && l.running_mode !== l.mode;
  return html`<article class="card line-card ${cls}" data-key="line-${l.line}" aria-label="Line ${l.line}">
    <div class="line-head"><span class="line-num">Line ${l.line}</span>${modeBadge(l.mode)}${l.mode_override ? html`<span class="faint" style="font-size:11px" title="Per-line override">override</span>` : ""}${statePill(l.state)}</div>
    <div class="line-adapter" title="${l.device}">${icon("lines")}<span class="ellipsis">${l.adapter || l.device}</span></div>
    ${c && c.kind === "call" ? html`
      <dl class="kv">
        <dt>Call</dt><dd><b>${c.peer || "Modem call"}</b></dd>
        <dt>Link</dt><dd class="mono" title="${c.connect}">${c.connect || "—"}</dd>
        <dt>Duration</dt><dd>${ticking(c.started)}</dd>
      </dl>
      <div class="line-rates">
        <div class="rate"><span class="dir c-rx">${icon("down")}In</span><b>${fmtBytes(c.rx_bytes)}</b><small>from the other side</small></div>
        <div class="rate"><span class="dir c-tx">${icon("up")}Out</span><b>${fmtBytes(c.tx_bytes)}</b><small>to the other side</small></div>
      </div>`
    : c ? html`
      <dl class="kv">
        <dt>User</dt><dd>${c.user ? html`<b>${c.user}</b>` : html`<span class="faint">authenticating…</span>`}</dd>
        <dt>Client IP</dt><dd class="mono">${c.ip || l.client_ip}</dd>
        <dt>Link</dt><dd class="mono" title="${c.connect}">${c.connect || "—"}</dd>
        <dt>Duration</dt><dd>${ticking(c.started)}</dd>
      </dl>
      <div class="line-rates">
        <div class="rate"><span class="dir c-rx">${icon("down")}Down</span><b>${fmtRate(c.rx_bps)}</b><small>${fmtBytes(c.rx_bytes)} total</small></div>
        <div class="rate"><span class="dir c-tx">${icon("up")}Up</span><b>${fmtRate(c.tx_bps)}</b><small>${fmtBytes(c.tx_bytes)} total</small></div>
      </div>`
    : html`<div class="line-detail">${l.state === "idle" ? html`<span class="cursor" aria-hidden="true"></span>` : ""}<span class="ellipsis" title="${detail}">${detail || "—"}</span></div>`}
    ${pending ? html`<div class="hint">${icon("reset")}Switches to ${MODE_NAME[l.mode]}; restart pending (after hang-up)</div>` : ""}
    <div class="line-foot"><span>${l.state_since ? html`${LINE_STATES[l.state] ? LINE_STATES[l.state].label : humanize(l.state)} ${relTime(l.state_since)}` : raw("&nbsp;")}</span><span class="mono">${l.baud ? `${l.baud} bps` : ""}</span></div>
  </article>`;
}

function kpis(st, pub) {
  // the LAN party at a glance: this box's party, its retro PCs, the party traffic and the public parties
  const s = st.summary, h = st.host || {}, lan = st.lan || null;
  const up = lan && lan.state === "up";
  const p = up ? partyState() : null, role = p && p.role;
  const pct = (a, b) => (b ? Math.min(100, Math.round((a / b) * 100)) : 0);
  const pcs = lanPcCount(lan);
  const lanText = !lan ? "Not available on this box" : lan.state === "no_nic" ? "No retro-LAN port yet" : lan.state === "disabled" ? "The LAN port is off"
    : lan.state === "stopped" ? "LAN service not running" : lan.state === "error" ? lan.detail || "The LAN port has a problem" : "";
  // 1 · LAN party
  let party;
  if (role === "host" || role === "member") {
    const boxes = role === "host" ? activeMembers(p).length + 1 : null, np = (p.pending || []).length, max = p.max || 16;
    const name = role === "host" ? p.title || p.name || "Your party" : partyName(p);
    party = html`<div class="kpi-value ellipsis" title="${name}">${role === "host" ? "Hosting" : p.status === "approved" ? "In a party" : p.status === "pending" ? "Waiting" : "Joining…"}</div>
      ${boxes != null ? html`<div class="meter" title="${boxes} of ${max} boxes"><i style="width:${pct(boxes, max)}%"></i></div>` : ""}
      <div class="kpi-sub"><span class="ellipsis">${role === "host" ? html`<b>${name}</b> · ${boxes} of ${max} boxes` : html`${p.status === "approved" ? "" : "Host: "}<b>${name}</b>`}</span>
        ${role === "host" && np ? html`<span class="bad">${np} join request${np === 1 ? "" : "s"}</span>` : ""}
        ${role === "member" && memberUnreachable(p) ? html`<span class="bad">Host unreachable</span>` : ""}
        <span>${p.public ? "public" : isHubParty(p) ? "private · cloud" : "private · direct"}</span></div>`;
  } else {
    party = html`<div class="kpi-value">${up ? "No party" : "—"}</div>
      <div class="kpi-sub"><span>${up ? "Host one, or join one on Parties" : lanText}</span></div>`;
  }
  // 4 · public parties (from the cloud hub)
  let pubBody;
  if (!pub || (!pub.dir && !pub.err)) pubBody = html`<div class="skel" style="height:30px;width:50%"></div><div class="skel skel-line" style="width:70%"></div>`;
  else if (pub.err || !pub.dir) pubBody = html`<div class="kpi-value">—</div><div class="kpi-sub"><span class="ellipsis" title="${pub.err}">${pub.err}</span></div>`;
  else {
    const mine = pub.mine ? [...pub.mine.hosted, ...pub.mine.going].sort((a, b) => (a.state === "live" ? 0 : a.starts) - (b.state === "live" ? 0 : b.starts)) : [];
    const next = mine[0];
    pubBody = html`<div class="kpi-value">${pub.dir.live.length}<small>live</small>${pub.dir.upcoming.length}<small>coming up</small></div>
      <div class="kpi-sub">${next ? html`<span class="ellipsis" title="${next.title}">${next.state === "live" ? "Live now" : fmtStart(next.starts)}: <b>${next.title}</b></span>`
        : pub.me && pub.me.banned ? html`<span class="bad">This box is banned from public parties</span>`
        : pub.me && !pub.me.nick ? html`<span>Pick a nickname to host or join</span>` : html`<span>Nothing on your calendar</span>`}</div>`;
  }
  const inParty = role === "host" || role === "member";
  return html`<section class="grid grid-kpi" aria-label="LAN party at a glance">
    <a class="card kpi kpi-link ${role === "host" && (p.pending || []).length ? "kpi-alert" : ""}" href="#/parties">
      <div class="kpi-label">${icon("network")}LAN party</div>${party}
    </a>
    <a class="card kpi kpi-link" href="#/lan">
      <div class="kpi-label">${icon("ethernet")}Retro PCs</div>
      <div class="kpi-value">${up ? pcs : "—"}${up ? html`<small>on this box</small>` : ""}</div>
      <div class="kpi-sub">${up ? html`${lan.nic && !lan.nic.carrier ? html`<span class="bad">No cable</span>` : ""}
          <span>${role === "member" ? "Addresses from the host" : lan.share_internet ? "Internet shared" : "LAN only, no internet"}</span>`
        : html`<span>${lanText}</span>`}</div>
    </a>
    <div class="card kpi">
      <div class="kpi-label">${icon("activity")}${inParty ? "Party traffic" : "LAN traffic"}</div>
      ${inParty ? html`<div class="kpi-dual">
        <div><span class="dir c-rx" title="Frames from the PCs on this box's retro LAN">Your PCs</span><div class="kpi-value">${fmtRate(s.lan_pcs_bps)}</div></div>
        <div><span class="dir c-tx" title="Frames arriving from the other boxes in the party">Party</span><div class="kpi-value">${fmtRate(s.lan_party_bps)}</div></div>
      </div>` : html`<div class="kpi-value">${up ? fmtRate(s.lan_pcs_bps) : "—"}</div>`}
      <div class="kpi-sub"><span>${fmtBytes(s.lan_bytes_today)} today</span></div>
    </div>
    <a class="card kpi kpi-link" href="#/parties">
      <div class="kpi-label">${icon("globe")}Public parties</div>${pubBody}
    </a>
  </section>
  <div class="host-strip" aria-label="Host health">
    <span class="chip">${icon("pi")}<b>${h.hostname || "—"}</b></span>
    ${h.uptime_s != null ? html`<span class="chip" title="Uptime">${icon("clock")}Up <b>${fmtDur(h.uptime_s)}</b></span>` : ""}
    ${h.load ? html`<span class="chip" title="Load average 1 / 5 / 15 min">${icon("activity")}Load <b>${h.load.map((x) => x.toFixed(2)).join(" · ")}</b></span>` : ""}
    ${h.mem_used_pct != null ? html`<span class="chip ${h.mem_used_pct >= 90 ? "chip-warn" : ""}" title="Memory used">${icon("database")}Mem <b>${h.mem_used_pct}%</b></span>` : ""}
    ${h.cpu_temp_c != null ? html`<span class="chip ${h.cpu_temp_c >= 75 ? "chip-err" : h.cpu_temp_c >= 65 ? "chip-warn" : ""}" title="CPU temperature">${icon("chip")}CPU <b>${h.cpu_temp_c.toFixed(1)} °C</b></span>` : ""}
    ${h.uptime_s == null && !h.load && h.mem_used_pct == null && h.cpu_temp_c == null ? html`<span class="chip faint">Host health not available</span>` : ""}
  </div>`;
}

function kpiSkeleton() {
  return html`<section class="grid grid-kpi">${[1, 2, 3, 4].map(() => html`<div class="card kpi"><div class="skel skel-line" style="width:40%"></div><div class="skel" style="height:30px;width:60%"></div><div class="skel skel-line" style="width:70%"></div></div>`)}</section>`;
}

function recentCallsTable(calls, { compact = false } = {}) {
  return html`<div class="table-wrap"><table class="table ${compact ? "table-compact" : ""}">
    <thead><tr><th>Started</th><th>Line</th><th>User</th><th>Result</th><th class="r">Duration</th><th class="r hide-sm">Data</th></tr></thead>
    <tbody>${calls.map((c) => html`<tr>
      <td class="nowrap" title="${fmtAbs(c.started)}">${fmtWhen(c.started)}</td>
      <td class="nowrap"><span class="mono">${c.line}</span> ${modeBadge(c.mode)}</td>
      <td>${c.mode === "call" ? String(c.detail || "Modem call").split(" · ")[0] : c.user ? c.user : html`<span class="faint">—</span>`}</td>
      <td>${resultPill(c)}</td>
      <td class="r nowrap">${c.ended == null ? ticking(c.started) : fmtDur(c.duration_s)}</td>
      <td class="r nowrap hide-sm">${c.ended == null ? (liveCall(c) ? fmtBytes(liveCall(c).rx_bytes + liveCall(c).tx_bytes) : html`<span class="faint">live</span>`) : fmtBytes((c.rx_bytes || 0) + (c.tx_bytes || 0))}</td>
    </tr>`)}</tbody></table></div>`;
}

// retro-LAN traffic by protocol (counted on the Pi's LAN bridge)
const PROTO_GROUP_COLOR = { IPX: "--accent", NetBEUI: "--amber", AppleTalk: "--violet", "TCP/IP": "--blue", Other: "--text-3" };
const fmtCount = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e4 ? `${Math.round(n / 1e3)}k` : fmtNum(n));
function protocolCard(d, lan) {
  if (!lan || lan.state === "disabled" || !d) return "";
  const party = d.role && d.role !== "standalone";
  const rows = d.protocols.filter((r) => r.lan.packets + r.party.packets > 0);
  const max = Math.max(1, ...rows.map((r) => r.lan.packets + r.party.packets));
  const span = { live: "last 15 minutes", "1h": "last hour", "24h": "last 24 hours", "7d": "last 7 days" }[d.range];
  const cell = (x) => html`<td class="r nowrap tnum">${x.packets ? html`${fmtCount(x.packets)}<div class="faint" style="font-size:11.5px">${fmtBytes(x.bytes)}</div>` : html`<span class="faint">—</span>`}</td>`;
  const now = (r) => r.lan.pps + r.party.pps;
  return html`<section class="card" aria-labelledby="h-proto">
    <div class="card-head">${icon("ethernet")}<h3 id="h-proto">Retro LAN protocols</h3><span class="sub">frames from PCs · ${span}${party ? " · this LAN and the party" : ""}</span></div>
    ${d.ipx_mixed ? html`<div class="banner banner-warn" role="alert" style="margin:0 16px 12px">${icon("alert")}<span><b>Mixed IPX frame types: ${d.ipx_frame_types.join(" and ")}.</b> <span class="muted">PCs only see others that use the same frame type. Set one type everywhere: <code>FRAME</code> in the DOS <code>NET.CFG</code>, and Frame Type under the IPX/SPX protocol's Advanced tab in Windows.</span></span></div>` : ""}
    ${!d.available ? html`<div class="card-pad">${emptyState("ethernet", "No protocol data yet", lan.state === "up" ? "Counting starts now; numbers appear within a few seconds." : "Shown while the retro-LAN port is up.")}</div>`
      : !rows.length ? html`<div class="card-pad">${emptyState("ethernet", "No traffic", `No frames from retro PCs in the ${span}.`)}</div>`
      : html`<div class="table-wrap"><table class="table table-compact">
        <thead><tr><th>Protocol</th><th class="hide-sm">Share</th><th class="r">${party ? "This LAN" : "Frames"}</th>${party ? html`<th class="r">From the party</th>` : ""}<th class="r">Now</th></tr></thead>
        <tbody>${rows.map((r) => html`<tr>
          <td class="nowrap"><span class="dot" style="background:${cssVar(PROTO_GROUP_COLOR[r.group])}"></span> ${r.label}</td>
          <td class="hide-sm" style="width:30%"><div class="meter" title="${fmtNum(r.lan.packets + r.party.packets)} frames"><i style="width:${(100 * r.lan.packets / max).toFixed(1)}%;background:${cssVar(PROTO_GROUP_COLOR[r.group])}"></i><i style="width:${(100 * r.party.packets / max).toFixed(1)}%;background:color-mix(in srgb, ${cssVar(PROTO_GROUP_COLOR[r.group])} 35%, transparent)"></i></div></td>
          ${cell(r.lan)}${party ? cell(r.party) : ""}
          <td class="r nowrap tnum">${now(r) ? html`${now(r).toFixed(now(r) < 10 ? 1 : 0)}<span class="faint">/s</span>` : html`<span class="faint">—</span>`}</td>
        </tr>`)}</tbody></table></div>
        <div class="card-pad faint" style="font-size:12px;padding-top:10px">${d.ipx_frame_types.length ? html`IPX frame type${d.ipx_frame_types.length > 1 ? "s" : ""} seen: ${d.ipx_frame_types.map((t) => html`<span class="chip">${t}</span>`)} · ` : ""}Counts frames as they enter the Pi's LAN bridge; nothing is recorded but the numbers.</div>`}
  </section>`;
}

const Dashboard = {
  title: "Dashboard",
  mount() {
    this.charts = {};
    this.stats = {};
    this.protos = {};
    this.recent = null;
    this.pub = null;
    this.render();
    this.loadStats();
    this.loadRecent();
    this.loadPublic();
  },
  unmount() {
    clearTimeout(this.statsTimer);
    clearTimeout(this.recentTimer);
    clearTimeout(this.pubTimer);
    Object.values(this.charts).forEach((c) => c.destroy());
  },
  update() { this.render(); },
  onVisible() { this.loadStats(); this.loadRecent(); this.loadPublic(); },
  async loadPublic() {
    // the public parties card: what's live, what's coming up and this box's next event (the hub is asked via the box)
    clearTimeout(this.pubTimer);
    const pub = { dir: null, mine: null, me: null, err: null };
    try {
      pub.me = await api("/api/parties/me");
      if (!pub.me.available) pub.err = pub.me.reason || "Public parties are not available";
      else [pub.dir, pub.mine] = await Promise.all([api("/api/parties/directory"), api("/api/parties/mine").catch(() => null)]);
    } catch (e) {
      if (e.status === 401) return;
      pub.err = e.status >= 500 || e.status === 0 ? "The cloud hub can't be reached" : e.message || "Could not load";
    }
    if (current !== this) return;
    this.pub = pub;
    this.render();
    if (!document.hidden) this.pubTimer = setTimeout(() => this.loadPublic(), 60000);
  },
  onTheme() { this.drawCharts(); },
  async loadStats() {
    clearTimeout(this.statsTimer);
    const rng = S.range;
    try {
      const [d, p] = await Promise.all([api(`/api/stats?range=${rng}`), api(`/api/protocols?range=${rng}`).catch(() => null)]);
      if (rng === S.range && current === this) { this.stats[rng] = d; this.protos[rng] = p; this.render(); this.drawCharts(); }
    } catch (e) {
      if (e.status && e.status !== 401) toastErr(e, "Could not load statistics");
    }
    if (!document.hidden && current === this) this.statsTimer = setTimeout(() => this.loadStats(), rng === "live" ? 5000 : 60000);
  },
  async loadRecent() {
    clearTimeout(this.recentTimer);
    try { this.recent = (await api("/api/calls?limit=8")).calls; this.render(); } catch { /* shown by banner */ }
    if (!document.hidden && current === this) this.recentTimer = setTimeout(() => this.loadRecent(), 10000);
  },
  perLineKeys() {
    const d = this.stats[S.range];
    return d ? Object.keys(d.per_line || {}).sort((a, b) => a - b) : [];
  },
  render() {
    const st = S.status;
    const ranges = [{ value: "live", label: "Live" }, { value: "1h", label: "1h" }, { value: "24h", label: "24h" }, { value: "7d", label: "7d" }];
    const lines = st ? st.lines : null;
    const s = st && st.summary;
    patch(view, html`<div class="stack">
      ${getStartedCard(st)}
      ${st ? kpis(st, this.pub) : kpiSkeleton()}
      <section aria-labelledby="h-lines">
        <div class="section-head"><h2 id="h-lines">Modem</h2>${s ? html`<span class="sub">${s.connected} connected · ${fmtNum(s.calls_today)} call${s.calls_today === 1 ? "" : "s"} today${s.failed_logins_today ? html` · <span class="bad">${s.failed_logins_today} failed login${s.failed_logins_today === 1 ? "" : "s"}</span>` : ""} · ${fmtBytes(s.bytes_today)} today</span>` : ""}
          <span class="spacer"></span><a class="btn btn-ghost btn-sm" href="#/modem">Manage lines ${icon("chevron", "i-sm")}</a></div>
        <div class="stack" style="gap:16px">
        ${!lines ? html`<div class="grid grid-lines">${[1, 2, 3].map(() => html`<div class="card line-card"><div class="skel skel-line" style="width:50%"></div><div class="skel skel-line" style="width:80%"></div><div class="skel" style="height:60px"></div></div>`)}</div>`
          : lines.length ? html`<div class="grid grid-lines">${lines.map(lineCard)}</div>`
          : html`<div class="card">${emptyState("lines", "No modem lines yet", "Plug a USB-serial adapter into the Pi. It is registered automatically and gets the next free line number.")}</div>`}
          <div class="card chart-card">
            <div class="card-head"><h3>Dial-up throughput</h3><div class="legend"><span><i class="sw" style="background:${cssVar("--accent")}"></i>Down</span><span><i class="sw" style="background:${cssVar("--blue")}"></i>Up</span></div></div>
            <div class="chart-body"><div class="chart" id="ch-thr" data-skip></div></div>
          </div>
          <div class="grid grid-2">
            <div class="card chart-card">
              <div class="card-head"><h3>Connections</h3><div class="legend"><span><i class="sw" style="background:${cssVar("--accent")}"></i>Connected</span><span><i class="sw dash" style="color:${cssVar("--text-3")}"></i>Lines present</span></div></div>
              <div class="chart-body"><div class="chart short" id="ch-conn" data-skip></div></div>
            </div>
            <div class="card chart-card">
              <div class="card-head"><h3>Per-line traffic</h3><div class="legend">${this.perLineKeys().map((k) => html`<span><i class="sw" style="background:${PALETTE[k % PALETTE.length]}"></i>Line ${k}</span>`)}</div></div>
              <div class="chart-body"><div class="chart short" id="ch-line" data-skip></div></div>
            </div>
          </div>
          <section class="card" aria-labelledby="h-recent">
            <div class="card-head"><h3 id="h-recent">Recent calls</h3><span class="spacer"></span><a class="btn btn-ghost btn-sm" href="#/calls">View all ${icon("chevron", "i-sm")}</a></div>
            ${this.recent == null ? html`<table class="table"><tbody>${skelRows(4, 5)}</tbody></table>`
              : this.recent.length ? recentCallsTable(this.recent, { compact: true })
              : emptyState("phone", "No calls yet", "Dial in from a retro PC and the call will show up here.")}
          </section>
        </div>
      </section>
      <section aria-labelledby="h-traffic">
        <div class="section-head"><h2 id="h-traffic">Retro LAN</h2><span class="sub">${S.range === "live" ? "last 15 minutes · 5 s samples" : S.range === "1h" ? "per minute" : S.range === "24h" ? "10-minute buckets" : "hourly buckets"}</span><span class="spacer"></span>
          ${segmented(ranges, S.range, 'data-action="range"', "", "Time range")}</div>
        <div class="stack" style="gap:16px">
          ${st && st.lan && st.lan.state === "up" ? html`<div class="card chart-card">
            <div class="card-head"><h3>Retro LAN throughput</h3><div class="legend"><span><i class="sw" style="background:${cssVar("--accent")}"></i>From your PCs</span>${st.lan.role && st.lan.role !== "standalone" ? html`<span><i class="sw" style="background:${cssVar("--blue")}"></i>From the party</span>` : ""}</div></div>
            <div class="chart-body"><div class="chart" id="ch-lan" data-skip></div></div>
          </div>` : ""}
          ${protocolCard(this.protos[S.range], st && st.lan)}
        </div>
      </section>
    </div>`);
    this.attachCharts();
  },
  attachCharts() {
    const mk = (key, id, opts) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (!this.charts[key] || this.charts[key].el !== el) {
        if (this.charts[key]) this.charts[key].destroy();
        this.charts[key] = new Chart(el, opts);
        this.drawCharts();
      }
    };
    const rate = (v) => (v ? fmtBytes(v).replace(/\.0 /, " ").replace(" ", "") + "/s" : "0");
    mk("thr", "ch-thr", { bytes: true, yFmt: rate, tipFmt: (v) => `${fmtRate(v)} · ${fmtKbit(v)}` });
    mk("lan", "ch-lan", { bytes: true, yFmt: rate, tipFmt: (v) => `${fmtRate(v)} · ${fmtKbit(v)}` });
    mk("conn", "ch-conn", { integer: true, tipFmt: (v) => String(v) });
    mk("line", "ch-line", { bytes: true, stacked: true, yFmt: rate, tipFmt: fmtRate });
  },
  drawCharts() {
    const d = this.stats[S.range];
    const ch = this.charts;
    if (!ch.thr) return;
    if (!d) { Object.values(ch).forEach((c) => c.set(undefined)); return; }
    const accent = cssVar("--accent"), blue = cssVar("--blue"), muted = cssVar("--text-3");
    const empty = S.range === "live" ? "Collecting samples…" : "No data for this period";
    ch.thr.set({ t: d.t, step: d.step, label: "Dial-up throughput", emptyText: empty, series: [
      { label: "Down", color: accent, values: d.rx_bps, area: true },
      { label: "Up", color: blue, values: d.tx_bps, area: true },
    ] });
    if (ch.lan && d.lan_pcs_bps) {
      const party = d.lan_party_bps.some((v) => v);
      ch.lan.set({ t: d.t, step: d.step, label: "Retro LAN throughput", emptyText: empty, series: [
        { label: "From PCs", color: accent, values: d.lan_pcs_bps, area: true },
        ...(S.status && S.status.lan && S.status.lan.role !== "standalone" || party
          ? [{ label: "From party", color: blue, values: d.lan_party_bps, area: true }] : []),
      ] });
    }
    ch.conn.set({ t: d.t, step: d.step, label: "Connections", emptyText: empty, series: [
      { label: "Lines present", color: muted, values: d.present, stepped: true, dash: true, width: 1.5, noDot: true },
      { label: "Connected", color: accent, values: d.connected, stepped: true, area: true },
    ] });
    const gaps = d.rx_bps.map((v) => v == null);
    ch.line.set({ t: d.t, step: d.step, label: "Per-line traffic", emptyText: empty, series: this.perLineKeys().map((k) => {
      const pl = d.per_line[k];
      return { label: `Line ${k}`, color: PALETTE[k % PALETTE.length], area: true, width: 1.4,
        values: pl.rx_bps.map((v, i) => (gaps[i] ? null : (v || 0) + (pl.tx_bps[i] || 0))) };
    }) });
  },
  actions: {
    "gs-hide"() { setStarted(); this.render(); },
    range(btn) {
      const r = btn.dataset.value;
      if (r === S.range) return;
      S.range = r;
      store.set("range", r);
      this.render();
      this.drawCharts();
      this.loadStats();
    },
  },
};

// page: lines
const diagram = (nodes) => html`<div class="diagram" aria-hidden="true">${nodes.map((n) => (n.link
  ? html`<span class="dg-link ${n.link}"><small>${n.label || ""}</small></span>`
  : html`<span class="dg-node"><span class="box ${n.cls || ""}">${icon(n.icon)}</span>${n.label}</span>`))}</div>`;

// After switching the uplink to Wi-Fi: the address to keep, and what the built-in Ethernet port does now.
function wifiUplinkDialog(n) {
  const w = (n && n.wifi) || {}, eth = (n && n.ethernet) || {};
  const host = (S.session && S.session.hostname) || "";
  const wifiUrl = `https://${w.ip}/`, onCable = !!eth.ip && location.hostname === eth.ip, onWifi = !onCable;
  const now = eth.role === "management" ? html`Right now its cable goes to your home network, so the box is also reachable at <span class="mono">${eth.ip || "a second address"}</span>.`
    : eth.role === "retro" ? html`Right now it is cabled to retro PCs: they are on the retro LAN.`
    : html`Right now nothing is plugged in.`;
  const row = (ic, cls, title, text) => html`<li><span class="wu-ic ${cls}">${icon(ic)}</span><span><b>${title}</b> ${text}</span></li>`;
  openModal({
    title: "Wi-Fi is now the uplink", iconName: "wifi", wide: true,
    body: html`<div class="wu">
      <p>The box goes online through <b>${w.ssid || "your Wi-Fi"}</b>. To manage it from now on, use its Wi-Fi address.</p>
      <div class="wu-addr">
        <div><small>Wi-Fi address</small><a class="mono" href="${wifiUrl}">${wifiUrl}</a>
          ${host ? html`<span class="faint">or <span class="mono">https://${host}.local/</span> on most computers</span>` : ""}</div>
        <button type="button" class="btn btn-sm" data-wu-copy>${icon("copy")}Copy</button>
      </div>
      ${onWifi ? "" : html`<div class="banner banner-warn" role="status">${icon("info")}<span>You are connected through the cable address <span class="mono">${location.hostname}</span>. It stops working when you move the cable, so bookmark the Wi-Fi address and continue there.</span></div>`}
      <h3 class="wu-h">How the box is connected now</h3>
      ${diagram([{ icon: "home", label: "Your router" }, { link: "wifi", label: "Wi-Fi" }, { icon: "pi", label: "NetKey", cls: "pi" },
        { link: "cable", label: "Ethernet cable" }, { icon: "pc", label: "Retro PCs", cls: "phys" }])}
      <h3 class="wu-h">The built-in Ethernet port is now free</h3>
      <ul class="wu-list">
        ${row("pc", "phys", "Cable it to a retro PC", "or to a switch with several. They get an address from the box and play on the retro LAN, kept apart from your home network.")}
        ${row("home", "", "Cable it to your router", "and it becomes a management port: the box gets a second address, handy as a backup.")}
        ${row("swap", "", "Move the cable any time.", "The box notices within a few seconds and works out where it goes. No settings to change.")}
      </ul>
      <p class="faint wu-now">${icon("ethernet", "i-sm")} ${now}</p>
    </div>`,
    foot: html`${onWifi ? "" : html`<a class="btn" href="${wifiUrl}#/network">${icon("external")}Open the Wi-Fi address</a>`}<span class="spacer"></span>
      <button type="submit" class="btn btn-primary">Got it</button>`,
    initialFocus: '[type="submit"]',
  }).el.querySelector("[data-wu-copy]").addEventListener("click", async () => {
    toast((await copyText(wifiUrl)) ? "Address copied" : "Could not copy: select it and copy by hand");
  });
}

// Default modem mode picker (Modem page): applies at once, like the switches on the LAN page.
function defaultModePicker(mode, busy) {
  const opt = (m, ic, title, desc) => html`<button type="button" class="mode-opt ${m === "physical" ? "pmm" : ""}" role="radio" aria-checked="${String(mode === m)}" data-action="default-mode" data-value="${m}" ${busy ? raw("disabled") : ""}>
      <span class="mo-icon">${icon(ic)}</span><b>${title} ${modeBadge(m)}</b><span class="d">${desc}</span><span class="check">${busy === m ? spinner() : icon("check")}</span></button>`;
  return html`<section aria-labelledby="h-mode">
    <div class="section-head"><h2 id="h-mode">Default modem mode</h2><span class="sub">Used by every line unless you switch that line below</span></div>
    <div class="mode-pick" role="radiogroup" aria-labelledby="h-mode">
      ${opt("virtual", "pc", "Virtual Modem Mode", "The Pi emulates a Hayes modem for a PC cabled to its serial port. No phone line needed.")}
      ${opt("physical", "phone", "Physical Modem Mode", "A real modem on the Pi answers phone calls from the PC's modem.")}
    </div></section>`;
}

// ---- phone numbers and modem calls (calls.py on the box): the Modem page's Phone number and Phone book cards
const EXT_BASE = 5550100;
const fmtNumber = (n) => { const d = String(n || "").replace(/\D/g, ""); return d.length === 7 ? `${d.slice(0, 3)}-${d.slice(3)}` : d; };
const extNumber = (line) => fmtNumber(String(EXT_BASE + Number(line)));
const PRESENCE = { available: { cls: "green", label: "Available" }, busy: { cls: "amber", label: "Busy" }, unreachable: { cls: "gray", label: "Can't be reached" } };
const NOTIFY_KEY = "netkey-call-notify";
function callNotifyOn() { try { return localStorage.getItem(NOTIFY_KEY) === "1" && "Notification" in window && Notification.permission === "granted"; } catch { return false; } }
let ringingSeen = new Set();
function callNotices(st) {
  // a browser notification when a virtual line starts ringing (no caller ID: just the line)
  const now = new Set((st.lines || []).filter((l) => l.mode === "virtual" && l.state === "ringing").map((l) => l.line));
  if (callNotifyOn()) {
    for (const n of now) {
      if (ringingSeen.has(n)) continue;
      try {
        const note = new Notification("NetKey: incoming call", { body: `Line ${n} is ringing. Answer on the machine (ATA), or set auto-answer with ATS0=1.`, tag: `netkey-ring-${n}` });
        note.onclick = () => { window.focus(); location.hash = "#/modem"; note.close(); };
      } catch { /* the browser refused (e.g. not a secure page) */ }
    }
  }
  ringingSeen = now;
}
function phoneWhyNot(p, s, st) {
  if (!p || !p.number) return "";
  if (s.incoming === "off") return "Incoming calls are off.";
  if (s.dnd) return "Do not disturb is on: callers hear BUSY.";
  const role = st && st.lan && st.lan.role;
  if (s.busy_in_party && (role === "host" || role === "member")) return "This box is in a LAN party: callers hear BUSY.";
  if (!(p.lines || []).length) return "No modem line yet: plug in a USB-serial adapter for the machine that should take calls. Until then callers get NO CARRIER.";
  if (!(p.ready || []).length) return p.reason === "busy" ? "Every line is in a call: callers hear BUSY." : "No line is ready: the machine on a line must be in modem mode (and idle) to ring.";
  return "";
}
function phoneCard(ctx) {
  const d = ctx.phone, st = S.status;
  if (!d) return html`<section class="card card-pad" data-key="phone"><div class="skel skel-line" style="width:40%"></div><div class="skel skel-line" style="width:70%"></div></section>`;
  const p = d.phone || {}, s = Object.assign({ incoming: "off", busy_in_party: true, dnd: false }, d.settings || {}, ctx.opt || {});
  const lines = (st && st.lines || []).filter((l) => l.mode === "virtual" && l.present !== false);
  const exts = lines.length ? lines.map((l) => html`<code class="mono">${extNumber(l.line)}</code> for line ${l.line}`) : [];
  const extText = html`Lines on this box call each other without a number: dial ${exts.length ? exts.map((x, i) => html`${i ? ", " : ""}${x}`) : html`<code class="mono">555-0100</code> for line 0, <code class="mono">555-0101</code> for line 1`}.`;
  const busy = ctx.busy;
  if (!d.hub) return html`<section class="card card-pad phone-card" data-key="phone"><h3>${icon("phone")}Phone number</h3><p class="muted">Phone numbers need the NetKey cloud hub, and this box has none configured.</p><p class="faint">${extText}</p></section>`;
  if (!p.number) {
    return html`<section class="card card-pad phone-card" data-key="phone">
      <h3>${icon("phone")}Phone number</h3>
      <p>Get a NetKey number so the machines on your serial lines can call your friends' boxes, like a modem call in 1995. Amiga, Atari ST and DOS serial games play over the call.</p>
      <div class="row"><button type="button" class="btn btn-primary" data-action="phone-get" ${busy ? raw("disabled") : ""}>${busy === "get" ? spinner() : icon("plus")}Get a number</button>
        ${d.lan_running ? "" : html`<span class="faint">The LAN service isn't running.</span>`}</div>
      <p class="faint">${extText}</p>
    </section>`;
  }
  const on = s.incoming !== "off", why = phoneWhyNot(p, s, st);
  const sw = (key, label, text, checked, extra = "") => html`<label class="phone-sw"><span><b>${label}</b><small>${text}</small></span>
    <span class="switch"><input type="checkbox" data-change="phone-set" data-key="${key}" ${checked ? raw("checked") : ""} ${busy ? raw("disabled") : ""} ${raw(extra)}><span class="switch-track"></span></span></label>`;
  return html`<section class="card card-pad phone-card" data-key="phone">
    <div class="phone-top">
      <div><h3>${icon("phone")}Phone number</h3>
        <div class="phone-num"><span class="mono">${fmtNumber(p.number)}</span><button type="button" class="btn btn-ghost btn-icon btn-sm" data-action="copy-addr" data-text="${fmtNumber(p.number)}" aria-label="Copy the number" title="Copy">${icon("copy")}</button></div>
        <p class="faint">Friends dial <code class="mono">ATDT${p.number}</code>. Dial theirs the same way: no area code needed.</p></div>
      <span class="pill ${on && !why ? "pill-green" : "pill-gray"}">${on && !why ? "Taking calls" : "Not taking calls"}</span>
    </div>
    <div class="phone-settings">
      ${sw("phone.incoming", "Allow incoming calls", on ? "Callers you allow reach your ready lines." : "Off: nobody can call this box. You can still call others.", on)}
      ${on ? html`<div class="phone-who">${segmented([{ value: "book", label: "People in my phone book" }, { value: "anyone", label: "Anyone with my number" }], s.incoming, 'data-action="phone-who"', "seg-sm", "Who may call")}</div>` : ""}
      ${sw("phone.busy_in_party", "Busy while in a LAN party", "Callers hear BUSY while this box hosts or joins a party.", s.busy_in_party)}
      ${sw("phone.dnd", "Do not disturb", "Callers hear BUSY. Everything else stays as it is.", s.dnd)}
      ${sw("notify", "Notify me in this browser", "A notification when a line rings, while this page is open.", callNotifyOn())}
    </div>
    ${on && why ? html`<p class="hint">${icon("info", "i-sm")}${why}</p>` : ""}
    <p class="faint">${extText} There is no caller ID: a ringing line only knows that someone calls.</p>
    <p class="faint">${icon("lock", "i-sm")} Calls go through NetKey's relays, encrypted from box to box. The hub sees who calls whom and when, never what is sent.</p>
    <div class="row"><button type="button" class="btn btn-ghost btn-sm" data-action="phone-release" ${busy ? raw("disabled") : ""}>${icon("trash", "i-sm")}Give up this number</button></div>
  </section>`;
}
function phoneBookCard(ctx) {
  const d = ctx.phone;
  if (!d || !d.hub || !(d.phone || {}).number) return "";
  const book = d.book || [], pres = ctx.presence || {}, ed = ctx.edit;
  const form = (row, idx) => html`<form class="book-form" data-submit="book-save" data-idx="${idx}" autocomplete="off">
      <input class="input" name="name" placeholder="Name" maxlength="32" value="${row.name || ""}" aria-label="Name">
      <input class="input mono" name="number" placeholder="555-1234" maxlength="12" value="${row.number ? fmtNumber(row.number) : ""}" aria-label="Number" required>
      <button class="btn btn-primary btn-sm">${ctx.bookBusy ? spinner() : "Save"}</button>
      <button type="button" class="btn btn-ghost btn-sm" data-action="book-cancel">Cancel</button></form>`;
  return html`<section class="card" data-key="phone-book" aria-labelledby="h-book">
    <div class="card-head"><h3 id="h-book">Phone book</h3><span class="sub">${book.length ? `${book.length} ${book.length === 1 ? "entry" : "entries"}` : "Your friends' numbers"}</span>
      <span class="spacer"></span>${ed == null ? html`<button type="button" class="btn btn-sm" data-action="book-add">${icon("plus", "i-sm")}Add</button>` : ""}</div>
    ${ed === -1 ? html`<div class="card-pad">${form({}, -1)}</div>` : ""}
    ${book.length ? html`<div class="table-wrap"><table class="table"><thead><tr><th>Name</th><th>Number</th><th>Status</th><th class="hide-sm">Dial</th><th></th></tr></thead><tbody>
      ${book.map((r, i) => ed === i ? html`<tr><td colspan="5">${form(r, i)}</td></tr>` : html`<tr>
        <td>${r.name || html`<span class="faint">—</span>`}</td>
        <td class="mono nowrap">${fmtNumber(r.number)}</td>
        <td>${(() => { const pr = PRESENCE[pres[r.number]]; return pr ? html`<span class="presence"><span class="dot dot-${pr.cls}"></span>${pr.label}</span>` : html`<span class="faint">—</span>`; })()}</td>
        <td class="hide-sm"><code class="mono">ATDT${r.number}</code></td>
        <td class="r nowrap"><button type="button" class="btn btn-ghost btn-icon btn-sm" data-action="book-edit" data-idx="${i}" aria-label="Edit ${r.name || fmtNumber(r.number)}">${icon("edit")}</button><button type="button" class="btn btn-ghost btn-icon btn-sm" data-action="book-del" data-idx="${i}" aria-label="Delete ${r.name || fmtNumber(r.number)}">${icon("trash")}</button></td>
      </tr>`)}</tbody></table></div>`
      : ed == null ? html`<div class="card-pad"><p class="faint">Add your friends' numbers to see when they can take a call. With "People in my phone book", only these numbers can call you.</p></div>` : ""}
    ${book.length ? html`<p class="faint card-pad" style="padding-top:0">"Can't be reached": the box is offline, doesn't take calls, or doesn't take them from you.</p>` : ""}
  </section>`;
}

const Lines = {
  title: "Modem",
  mount() { this.pending = {}; this.defaultBusy = null; this.phoneBusy = null; this.bookEdit = null; this.phoneOpt = null; this.render(); this.loadPhone(); },
  unmount() { clearTimeout(this.presTimer); },
  onVisible() { this.loadPhone(); },
  update() { this.render(); },
  async loadPhone() {
    try { this.phone = await api("/api/phone"); }
    catch (e) { if (e.status !== 401) this.phone = this.phone || { hub: false, book: [], settings: {} }; }
    if (current === this) this.render();
    this.loadPresence();
  },
  async loadPresence() {
    clearTimeout(this.presTimer);
    const d = this.phone;
    if (d && d.phone && d.phone.number && (d.book || []).length) {
      try { this.presence = (await api("/api/phone/presence")).numbers || {}; } catch { /* hub unreachable: no dots */ }
      if (current === this) this.render();
    }
    if (current === this && !document.hidden) this.presTimer = setTimeout(() => this.loadPresence(), 30000);
  },
  async phoneSave(key, value) {
    this.phoneOpt = Object.assign({}, this.phoneOpt, { [key.split(".")[1]]: value });
    this.phoneBusy = "set"; this.render();
    try {
      await api("/api/settings", { method: "PUT", body: { values: { [key]: value } } });
      this.phone = await api("/api/phone");
      const label = { "phone.incoming": value === "off" ? "Incoming calls off" : value === "book" ? "Calls from your phone book only" : "Anyone with your number can call",
        "phone.busy_in_party": value ? "Busy while in a LAN party" : "Calls ring during LAN parties too", "phone.dnd": value ? "Do not disturb is on" : "Do not disturb is off" }[key];
      toast(label, { detail: "The box applies it within a few seconds." });
    } catch (e) { toastErr(e, "Could not save"); }
    this.phoneOpt = null; this.phoneBusy = null; this.render();
  },
  async bookPut(book, okText) {
    this.bookBusy = true; this.render();
    try {
      const r = await api("/api/phone/book", { method: "PUT", body: { book } });
      this.phone = Object.assign({}, this.phone, { book: r.book });
      toast(okText);
      this.loadPresence();
      return true;
    } catch (e) { toastErr(e, "Could not save the phone book"); return false; }
    finally { this.bookBusy = false; this.render(); }
  },
  render() {
    const st = S.status;
    const def = st ? st.summary.default_mode : null;
    const row = (l) => {
      const busy = this.pending[l.line];
      const pend = l.running_mode && l.running_mode !== l.mode;
      return html`<div class="line-row" data-key="row-${l.line}">
        <div class="c-num"><span class="line-num">Line ${l.line}</span></div>
        <div class="c-adapter" style="min-width:0"><div class="ellipsis" style="font-weight:600">${l.adapter || "Unknown adapter"}</div><div class="dev" title="${l.device}">${l.device}</div></div>
        <div class="c-state" style="min-width:0;display:grid;gap:3px;justify-items:start">${statePill(l.state)}<span class="faint ellipsis" style="font-size:12px;max-width:100%" title="${l.detail}">${l.state === "unplugged" && l.absent_since ? `last seen ${fmtRel(l.absent_since)}` : l.detail || (l.state_since ? fmtRel(l.state_since) : "")}</span></div>
        <div class="c-mode mode-cell">
          ${segmented([
            { value: "virtual", label: "Virtual", title: "Virtual Modem Mode: the Pi emulates a Hayes modem", disabled: !!busy },
            { value: "physical", label: "Physical", title: "Physical Modem Mode: a real modem answers phone calls", disabled: !!busy },
          ], busy || l.mode, `data-action="line-mode" data-line="${l.line}"`, "seg-sm", `Mode for line ${l.line}`)}
          ${busy ? html`<span class="faint row" style="font-size:12px;gap:6px">${spinner()} Saving…</span>`
            : pend ? html`<span class="hint">${icon("reset")}Restart pending (after hang-up): running ${MODE_NAME[l.running_mode] || l.running_mode}</span>`
            : l.mode_override && l.mode_override !== def ? html`<span class="faint" style="font-size:12px">Differs from the default</span>`
            : html`<span class="faint" style="font-size:12px">Default mode</span>`}
        </div>
        <div class="c-ip"><span class="lbl">Client IP</span><span class="mono">${l.client_ip}</span></div>
        <div class="c-baud"><span class="lbl">Baud</span><span class="mono">${l.baud || "—"}</span></div>
        <div class="c-del">${l.call && l.call.kind === "call" || (l.mode === "virtual" && ["ringing", "answering"].includes(l.state)) ? html`<button type="button" class="btn btn-ghost btn-icon btn-sm" data-action="line-hangup" data-line="${l.line}" aria-label="Hang up line ${l.line}" title="Hang up">${icon("x")}</button>` : ""}${!l.present ? html`<button type="button" class="btn btn-ghost btn-icon btn-sm" data-action="line-forget" data-line="${l.line}" aria-label="Forget line ${l.line}" title="Forget this unplugged adapter and free line ${l.line}">${icon("trash")}</button>` : ""}</div>
      </div>`;
    };
    patch(view, html`<div class="stack">
      ${st ? defaultModePicker(this.defaultBusy || def, this.defaultBusy) : ""}
      <div class="section-head"><h2>How the two modes work</h2><span class="sub">Pick the one that matches how your old machine connects</span></div>
      <section class="modes-explain" aria-label="The two modem modes">
        <div class="card mode-explain">
          <h3>${modeBadge("virtual")} Virtual Modem Mode</h3>
          <p>The PC's serial port is cabled straight to a USB-serial adapter on the Pi. The Pi answers <code>AT</code> commands like a Hayes modem and "connects" instantly on <code>ATD</code>, no phone line needed. It can also call other lines and other NetKey boxes by number (below).</p>
          ${diagram([{ icon: "pc", label: "Retro PC" }, { link: "data", label: "RS-232 / USB" }, { icon: "pi", label: "Pi", cls: "pi" }, { link: "data", label: "NAT" }, { icon: "globe", label: "Internet" }])}
        </div>
        <div class="card mode-explain">
          <h3>${modeBadge("physical")} Physical Modem Mode</h3>
          <p>A real modem on the Pi answers incoming calls over a phone line (or a line simulator). The PC dials with its own modem, exactly like in the 90s. These lines don't take part in calls between boxes.</p>
          ${diagram([{ icon: "pc", label: "PC" }, { icon: "modem", label: "Modem" }, { link: "phone", label: "☎ phone line" }, { icon: "modem", label: "Modem", cls: "phys" }, { link: "data", label: "serial" }, { icon: "pi", label: "Pi", cls: "pi" }, { link: "data", label: "NAT" }, { icon: "globe", label: "Internet" }])}
        </div>
      </section>
      <section class="card" aria-labelledby="h-lines2">
        <div class="card-head"><h3 id="h-lines2">Lines</h3>${st ? html`<span class="sub">${st.summary.lines_registered} of ${st.summary.lines_max} slots · modem options in <a href="#/settings">Settings</a></span>` : ""}</div>
        ${!st ? html`<div class="line-list">${[1, 2, 3].map(() => html`<div class="line-row"><div class="skel skel-line"></div><div class="skel skel-line"></div><div class="skel skel-line"></div><div class="skel skel-line"></div></div>`)}</div>`
          : st.lines.length ? html`<div class="line-list">
              <div class="line-row head"><div>Line</div><div>Adapter</div><div>State</div><div>Mode</div><div class="c-ip">Client IP</div><div class="c-baud">Baud</div><div class="c-del"><span class="sr-only">Forget</span></div></div>
              ${st.lines.map(row)}</div>`
          : emptyState("lines", "No modem lines yet", "Plug a USB-serial adapter into the Pi. It is registered automatically and gets the next free line number (and client IP).")}
      </section>
      <div class="section-head"><h2>Calls between machines</h2></div>
      ${st && !(st.lines.some((l) => l.mode === "virtual") || (!st.lines.length && def === "virtual"))
        ? html`<section class="card card-pad" data-key="phone-pmm"><p class="muted">${icon("info", "i-sm")} Calls between machines (phone numbers and line extensions) work on lines in <b>Virtual Modem Mode</b>. Every line here uses Physical Modem Mode, where a real modem answers real phone calls. Switch a line to Virtual to call other boxes.</p></section>`
        : html`<div class="grid grid-2 phone-grid">${phoneCard({ phone: this.phone, busy: this.phoneBusy, opt: this.phoneOpt })}${phoneBookCard({ phone: this.phone, presence: this.presence, edit: this.bookEdit, bookBusy: this.bookBusy })}</div>`}
      <p class="faint" style="font-size:12.5px">${icon("info", "i-sm")} Mode changes apply to idle lines within seconds. A line that is in a call keeps its current mode until the caller hangs up.
        Unplugged adapters keep their line; when all ${st ? st.summary.lines_max : ""} lines are taken, a new adapter gets the line of the one unplugged longest.</p>
    </div>`);
  },
  actions: {
    async "phone-get"() {
      this.phoneBusy = "get"; this.render();
      try {
        const r = await api("/api/phone", { method: "POST", body: { cmd: "get_number" } });
        this.phone = r;
        toast(r.result.message, { detail: "Allow incoming calls when you want friends to reach you." });
      } catch (e) { toastErr(e, "Could not get a number"); }
      this.phoneBusy = null; this.render();
    },
    async "phone-release"() {
      const n = this.phone && this.phone.phone && this.phone.phone.number;
      const ok = await confirmDialog({ title: `Give up ${fmtNumber(n)}?`, danger: true, confirmText: "Give up number",
        message: "Friends can't reach this box on it any more. Someone else may get it after 90 days, and this box can get a new number 30 days after it got this one." });
      if (!ok) return;
      this.phoneBusy = "release"; this.render();
      try { this.phone = await api("/api/phone", { method: "POST", body: { cmd: "release" } }); toast("Number given up"); }
      catch (e) { toastErr(e, "Could not give up the number"); }
      this.phoneBusy = null; this.render();
    },
    async "phone-set"(input) {
      const k = input.dataset.key;
      if (k === "notify") {
        if (input.checked) {
          if (!("Notification" in window)) { toast("This browser can't show notifications", { type: "err" }); input.checked = false; return; }
          const perm = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
          if (perm !== "granted") { toast("Notifications are blocked for this page", { type: "err", detail: "Allow them in the browser's site settings." }); input.checked = false; return; }
        }
        try { localStorage.setItem(NOTIFY_KEY, input.checked ? "1" : "0"); } catch { /* private window */ }
        toast(input.checked ? "You'll get a notification when a line rings" : "Call notifications off");
        this.render();
        return;
      }
      const v = k === "phone.incoming" ? (input.checked ? "book" : "off") : input.checked;
      await this.phoneSave(k, v);
    },
    async "phone-who"(btn) { await this.phoneSave("phone.incoming", btn.dataset.value); },
    "book-add"() { this.bookEdit = -1; this.render(); const i = $(".book-form input[name=name]"); if (i) i.focus(); },
    "book-edit"(btn) { this.bookEdit = Number(btn.dataset.idx); this.render(); },
    "book-cancel"() { this.bookEdit = null; this.render(); },
    async "book-del"(btn) {
      const book = [...(this.phone.book || [])], r = book[Number(btn.dataset.idx)];
      if (!r) return;
      const ok = await confirmDialog({ title: `Delete ${r.name || fmtNumber(r.number)}?`, confirmText: "Delete", danger: true, message: "If only your phone book may call you, this number can't call you any more." });
      if (!ok) return;
      book.splice(Number(btn.dataset.idx), 1);
      await this.bookPut(book, "Deleted from the phone book");
    },
    async "book-save"(form) {
      const idx = Number(form.dataset.idx), name = form.elements.namedItem("name").value.trim(), number = form.elements.namedItem("number").value.replace(/\D/g, "").slice(-7);
      if (!/^555[1-9]\d{3}$/.test(number)) return toast("Enter a NetKey number, 555-1000 to 555-9999", { type: "err" });
      const book = [...(this.phone.book || [])];
      let nm = name;
      if (!nm) { try { nm = (await api(`/api/phone/lookup?n=${number}`)).name || ""; } catch (e) { if (e.status === 404) return toast(`No box has ${fmtNumber(number)}`, { type: "err" }); } }
      const row = { name: nm, number };
      if (idx < 0) book.push(row); else book[idx] = row;
      if (await this.bookPut(book, idx < 0 ? `${nm || fmtNumber(number)} added` : "Saved")) this.bookEdit = null;
      this.render();
    },
    async "line-hangup"(btn) {
      try { await api("/api/phone", { method: "POST", body: { cmd: "hangup", line: Number(btn.dataset.line) } }); toast(`Line ${btn.dataset.line} hung up`); await refreshStatus(); }
      catch (e) { toastErr(e, "Could not hang up"); }
    },
    "copy-addr"(el) { copyText(el.dataset.text).then((ok) => toast(ok ? "Copied" : "Could not copy", { type: ok ? "ok" : "err", detail: el.dataset.text })); },
    async "default-mode"(btn) {
      const m = btn.dataset.value;
      const st = S.status;
      if (!st || this.defaultBusy || st.summary.default_mode === m) return;
      this.defaultBusy = m;
      this.render();
      try {
        await api("/api/settings", { method: "PUT", body: { values: { "lines.default_mode": m } } });
        await refreshStatus();
        const own = st.lines.filter((l) => l.mode_override).length;
        toast(`Default is now ${MODE_NAME[m]} Modem Mode`, { detail: `Idle lines switch within seconds${own ? `; ${own} line${own === 1 ? "" : "s"} switched individually keep${own === 1 ? "s" : ""} its mode` : ""}. Lines in a call switch after hang-up.` });
      } catch (e) {
        toastErr(e, "Could not change the default mode");
      } finally {
        this.defaultBusy = null;
        this.render();
      }
    },
    async "line-forget"(btn) {
      const n = btn.dataset.line;
      const l = S.status && S.status.lines.find((x) => String(x.line) === String(n));
      if (!l) return;
      const ok = await confirmDialog({
        title: `Forget line ${n}?`, danger: true, confirmText: "Forget line",
        message: `${l.adapter || "The adapter"} is unplugged. Forgetting frees line ${n} (client IP ${l.client_ip}) and its mode setting for the next adapter you plug in. If you plug this adapter in again, it gets a free line like a new one.`,
      });
      if (!ok) return;
      try {
        await api(`/api/lines/${n}`, { method: "DELETE" });
        toast(`Line ${n} is free again`);
        await refreshStatus();
      } catch (e) {
        toastErr(e, `Could not forget line ${n}`);
      }
    },
    async "line-mode"(btn) {
      const n = btn.closest("[data-line]") ? btn.closest("[data-line]").dataset.line : btn.dataset.line;
      const eff = btn.dataset.value;
      const l = S.status && S.status.lines.find((x) => String(x.line) === String(n));
      if (!l || l.mode === eff) return;
      const mode = eff === S.status.summary.default_mode ? "default" : eff;      // back to the default: no override
      this.pending[n] = eff;
      this.render();
      try {
        await api(`/api/lines/${n}`, { method: "PUT", body: { mode } });
        toast(`Line ${n} now uses ${MODE_NAME[eff]} Modem Mode`, {
          detail: l.call ? `${MODE_NAME[eff]} Modem Mode takes effect after the current call hangs up.` : `${MODE_NAME[eff]} Modem Mode is applied within a few seconds.` });
        await refreshStatus();
      } catch (e) {
        toastErr(e, `Could not change the mode of line ${n}`);
      } finally {
        delete this.pending[n];
        this.render();
      }
    },
  },
};

// page: LAN (retro-LAN port)
const LAN_NO_NIC_TEXT = "Plug a USB Ethernet adapter into the Pi to create a retro LAN port. Retro PCs with a network card connect to it directly or through a hub.";
// No retro-LAN port yet: what this box can do about it (free the built-in port via Wi-Fi, move its cable, or a USB adapter).
function retroPortHint(net) {
  const n = net || {}, eth = n.ethernet || {};
  if (n.mode === "wifi" && eth.role === "management") {
    return { title: "The Ethernet port goes to your router", text: "Wi-Fi is the uplink, so the built-in Ethernet port can be the retro-LAN port: move its cable from your router to a retro PC, or to a switch with several. Or plug in a USB Ethernet adapter.", href: "", cta: "" };
  }
  if (n.wifi_available && n.mode !== "wifi") {
    return { title: "No port for retro PCs yet", text: "Connect the box to your Wi-Fi: the built-in Ethernet port then becomes the retro-LAN port. Or plug in a USB Ethernet adapter and keep the box on its cable.", href: "#/network", cta: "Set up Wi-Fi" };
  }
  return { title: "No USB Ethernet adapter", text: LAN_NO_NIC_TEXT, href: "", cta: "" };
}

// Dashboard: three steps from a fresh box to a retro PC on the retro LAN. Gone once a PC has shown up (or hidden).
const STARTED_KEY = "netkey-started";
function startedDone() { try { return localStorage.getItem(STARTED_KEY) === "1"; } catch { return false; } }
function setStarted() { try { localStorage.setItem(STARTED_KEY, "1"); } catch { /* private window: shows again */ } }
function getStartedCard(st) {
  if (!st || !st.lan || startedDone()) return "";
  const net = st.network || {}, lan = st.lan, w = net.wifi || {};
  const pcs = lanPcCount(lan);
  if (pcs > 0 || (lan.role && lan.role !== "standalone")) { setStarted(); return ""; }   // done: a PC is on the LAN
  const online = !!net.uplink_ok, port = lan.state === "up" || lan.state === "disabled";
  const hint = retroPortHint(net);
  const step = (n, done, active, title, text, action = "") => html`<li class="gs-step ${done ? "is-done" : active ? "is-now" : ""}">
      <span class="gs-num">${done ? icon("check", "i-sm") : n}</span>
      <div><b>${title}</b><p>${text}</p>${action}</div></li>`;
  const via = net.mode === "wifi" && w.ssid ? html`Online via Wi-Fi <b>${w.ssid}</b>.` : "Online via the Ethernet cable.";
  return html`<section class="card gs" aria-labelledby="h-gs" data-key="gs">
    <div class="gs-head"><div><h2 id="h-gs">Get started</h2><span class="sub">Three steps to a retro PC on the retro LAN</span></div>
      <button type="button" class="btn btn-ghost btn-sm" data-action="gs-hide">Hide</button></div>
    <ol class="gs-steps">
      ${step(1, online, !online, "Box online", online ? via : "Connect the box to your router with a cable, or set up Wi-Fi.", online ? "" : html`<a class="btn btn-sm" href="#/network">Network</a>`)}
      ${step(2, port, online && !port, port ? "Retro-LAN port ready" : hint.title, port ? html`Port <span class="mono">${(lan.nic && lan.nic.name) || "ready"}</span> waits for your retro PCs.` : hint.text,
        !port && hint.href ? html`<a class="btn btn-sm btn-primary" href="${hint.href}">${hint.cta}</a>` : "")}
      ${step(3, false, port, "Plug in a retro PC", "Cable it to the retro-LAN port, or to a switch with several. Let it get its address automatically (DHCP): it shows up on the LAN page.",
        port ? html`<a class="btn btn-sm btn-primary" href="#/lan">Open the LAN page</a>` : "")}
    </ol>
    <p class="faint gs-next">${icon("parties", "i-sm")} Then host a LAN party or join one: <a href="#/parties">Parties</a></p>
  </section>`;
}
const LAN_ONLY_TEXT = "LAN only: PCs can talk to each other (IPX, NetBEUI, TCP/IP) but not reach the internet.";
const LAN_SHARE_TEXT = "Retro-LAN PCs will reach the internet through this Pi. All their traffic leaves from your internet connection and IP address.";
const ipNum = (ip) => String(ip || "").split(".").reduce((a, x) => a * 256 + (Number(x) || 0), 0);
const fmtSpeed = (n) => (n == null || n <= 0 ? "—" : n >= 1000 ? `${n / 1000} Gbit` : `${n} Mbit`);

// LAN party state: /api/status carries it without the invite code; /api/party (LAN page only) adds the code
const PARTY_CODE_PREFIX = "netkey1-";
const HUB_CODE_PREFIX = "KEY";
const HUB_CODE_HINT = "KEY-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX";
const HUB_CODE_LEN = 32;
const PARTY_PORT = 51899;
// Cloud-hub invite codes in canonical form ("key 7kq2m9xd..." -> "KEY-7KQ2-M9XD-..."); null for anything else.
const normHubCode = (s) => {
  const t = String(s || "").toUpperCase().replace(/[^0-9A-Z]/g, "");
  if (!t.startsWith(HUB_CODE_PREFIX)) return null;
  return [HUB_CODE_PREFIX, ...(t.slice(HUB_CODE_PREFIX.length).match(/.{1,4}/g) || [])].join("-");
};
const isHubParty = (p) => !!p && p.via === "hub";
const hubHost = (h) => { try { return new URL(h.url).hostname; } catch { return "the cloud hub"; } };
// Round-trip time this box measured to a relay (by name), from the last relay probe.
const relayMs = (p, name) => {
  const r = ((p && p.relay_probe && p.relay_probe.relays) || []).find((x) => x.name === name);
  return r && r.rtt_ms != null ? Math.round(r.rtt_ms) : null;
};
const relayLabel = (p, name) => {
  if (!name) return null;
  const ms = relayMs(p, name);
  return ms != null ? `${name} · ${ms} ms` : name;
};
// Cloud-hub connection line for hub parties (host and member view).
const hubLine = (p) => {
  const h = p.hub || {};
  if (h.online) {
    const via = relayLabel(p, p.relay);
    return html`<div class="party-hub is-online" role="status"><span class="dot dot-live" aria-hidden="true"></span>
      <span class="ph-t"><b>Cloud hub connected</b><small>${via ? html`Relayed via <b class="mono">${via}</b>, the relay the hub picked for this party.` : html`Traffic is relayed by ${hubHost(h)}.`} It is end-to-end encrypted, so relays only see ciphertext.</small></span></div>`;
  }
  return html`<div class="party-hub is-off" role="status">${spinner()}
    <span class="ph-t"><b>${h.last_ok ? "Reconnecting to the cloud hub…" : "Connecting to the cloud hub…"}</b>
      <small>${h.error ? String(h.error).replace(/\.$/, "") : hubHost(h)}${h.last_ok ? html` · last contact ${relTime(h.last_ok)}` : ""}</small></span></div>`;
};
const connBadge = (c, relay) => (c === "relay"
  ? html`<span class="badge badge-relay" title="Relayed through ${relay ? `the relay ${relay}` : "the cloud hub"} (end-to-end encrypted)">RELAY</span>`
  : html`<span class="badge badge-direct" title="Direct WireGuard connection">DIRECT</span>`);
const PARTY_CMD_ERR = {
  create: "Could not host a party", join: "Could not join the party", approve: "Could not approve", reject: "Could not reject",
  kick: "Could not remove the member", regenerate: "Could not create a new invite code", endpoint: "Could not change the address",
  leave: "Could not leave the party", close: "Could not close the party",
};
const partySeen = new Set();
let partyCutoff = null;
// Toasts unsolicited party events ("notice" results) once; on first load only the last ~60 s are replayed.
function partyNotices(p) {
  if (!p || !Array.isArray(p.results)) return;
  if (partyCutoff == null) partyCutoff = (p.updated || nowS()) - 60;
  p.results.forEach((r) => {
    const key = `${r.id}|${r.at}|${r.cmd}|${r.message}`;
    if (partySeen.has(key)) return;
    partySeen.add(key);
    if (r.cmd === "notice" && r.at > partyCutoff) toast(r.message, { type: r.ok === false ? "err" : "ok", timeout: 7000 });
  });
}
function setParty(p) {
  if (!p) return;
  S.party = p;
  S.partyAt = nowS();
  partyNotices(p);
}
// Freshest known party status (status poll vs. /api/party), with the invite code carried over when it is the same party.
function partyState() {
  const sp = S.status && S.status.lan && S.status.lan.party, fp = S.party;
  if (!fp || !sp) return sp || fp || null;
  const ts = (p, at) => p.updated || at || 0;
  const p = ts(fp, S.partyAt) >= ts(sp, S.statusAt) ? fp : sp;
  if (p !== fp && p.role === "host" && fp.role === "host" && fp.party_id === p.party_id && fp.code) return { ...p, code: fp.code };
  return p;
}
const activeMembers = (p) => (p.members || []).filter((m) => !m.kicked);
const memberUnreachable = (p) => !!p.unreachable_since;
const MEMBER_STATES = {
  joining: { label: "Joining…", cls: "blue pill-pulse" },
  pending: { label: "Waiting for approval", cls: "amber pill-pulse" },
  approved: { label: "Connected", cls: "green pill-glow" },
};
const memberPill = (p) => {
  const s = memberUnreachable(p) ? { label: "Host unreachable", cls: "red" } : MEMBER_STATES[p.status] || { label: humanize(p.status || "unknown"), cls: "gray" };
  return html`<span class="pill pill-${raw(s.cls)}">${s.label}</span>`;
};
// host:port with a line-break opportunity before the port.
const wbrEndpoint = (ep) => {
  const s = String(ep || "—"), i = s.lastIndexOf(":");
  return i > 0 ? html`${s.slice(0, i)}<wbr>${s.slice(i)}` : s;
};
const partyName = (p) => p.party_name || (p.view && p.view.name) || (isHubParty(p) ? "" : p.host_endpoint) || "the host";

const Lan = {
  title: "LAN",
  mount() {
    this.opt = {};
    this.busy = null;
    this.showCode = false;
    this.via = "hub";
    this.joinErr = "";
    this.createErr = "";
    this.aa = null;
    this.render();
    this.loadParty();
    this.loadAutoApprove();
  },
  unmount() { clearTimeout(this.partyTimer); },
  update() { this.prune(); this.render(); },
  onVisible() { this.loadParty(); },
  async loadParty() {
    clearTimeout(this.partyTimer);
    try {
      setParty(await api("/api/party"));
      if (current === this) this.render();
    } catch { /* the status poll and the banners report connection problems */ }
    if (!document.hidden && current === this) this.partyTimer = setTimeout(() => this.loadParty(), 3000);
  },
  async loadAutoApprove() {
    try {
      const cfg = await api("/api/settings");
      if (current !== this || (this.aa && this.aa.saving)) return;
      this.aa = cfg.values && "lan.party_auto_approve" in cfg.values ? { value: !!cfg.values["lan.party_auto_approve"], saving: false } : { missing: true };
      this.render();
    } catch { /* switch stays disabled */ }
  },
  // Optimistic switch values win until the status poll reflects them (the Pi applies changes in ~3-5 s).
  prune() {
    const lan = S.status && S.status.lan;
    for (const [k, o] of Object.entries(this.opt)) {
      if (o.saving) continue;
      const actual = lan ? lan[k === "lan.enabled" ? "enabled" : "share_internet"] : undefined;
      if (actual === o.value || Date.now() > o.until) delete this.opt[k];
    }
  },
  value(key) {
    if (key in this.opt) return this.opt[key].value;
    const lan = S.status && S.status.lan;
    return !!(lan && lan[key === "lan.enabled" ? "enabled" : "share_internet"]);
  },
  hero(lan) {
    const nic = lan.nic;
    const n = lanPcCount(lan);
    const p = partyState() || {};
    const pcs = n ? `${n} PC${n === 1 ? "" : "s"} connected` : "waiting for PCs";
    const sub = lan.state === "up" && p.role === "host" ? `Hosting a LAN party · ${pcs} · DHCP for every party box`
      : lan.state === "up" && p.role === "member" ? `Part of ${partyName(p)}'s LAN party: the host hands out addresses`
      : lan.state === "up" ? (n ? `${n} PC${n === 1 ? "" : "s"} connected · isolated network with its own DHCP` : "Isolated network with its own DHCP and DNS, waiting for PCs")
      : lan.state === "disabled" ? "The LAN party port is turned off"
      : lan.state === "no_nic" ? "Waiting for a retro-LAN port"
      : lan.state === "stopped" ? "The retro-LAN service is not running"
      : "The retro-LAN port could not be set up";
    const cls = lan.state === "up" ? "is-up" : lan.state === "error" ? "is-error" : "is-off";
    const applying = Object.keys(this.opt).length;
    let body;
    if (lan.state === "no_nic") {
      const h = retroPortHint(S.status && S.status.network);
      body = emptyState("ethernet", h.title, h.text, h.href ? html`<a class="btn btn-primary" href="${h.href}">${h.cta}</a>` : "");
    }
    else if (lan.state === "stopped" && !nic) body = emptyState("alert", "Retro-LAN service stopped", lan.detail || "The Pi's network helper is not running, so the retro-LAN port is inactive.");
    else {
      const r = S.lanRate, up = lan.state === "up";
      const fact = (label, value, extra = "", cls2 = "") => html`<div class="${cls2}"><small>${label}</small><b>${value}</b>${extra ? html`<span class="faint">${extra}</span>` : ""}</div>`;
      body = html`
        ${lan.state === "error" || (lan.detail && lan.state === "stopped") ? html`<div class="banner ${lan.state === "error" ? "banner-err" : "banner-warn"} lan-banner" role="${lan.state === "error" ? "alert" : "status"}">${icon(lan.state === "error" ? "alert" : "info")}<span>${lan.detail || "Unknown error"}</span></div>` : ""}
        ${lan.state === "disabled" ? html`<p class="muted lan-note">Turn on <b>LAN party port</b> below to give retro PCs their own isolated network. While it is off, the retro-LAN port itself is switched off.</p>` : ""}
        ${(lan.extra_nics || []).length ? html`<div class="banner banner-warn lan-banner" role="status">${icon("info")}<span><b>${lan.extra_nics.length === 1 ? "1 extra Ethernet adapter is" : `${lan.extra_nics.length} extra Ethernet adapters are`} switched off</b> (<span class="mono">${lan.extra_nics.join(", ")}</span>). <span class="muted">Only one adapter is used for the retro LAN${nic ? html` (<span class="mono">${nic.name}</span>)` : ""}. For more PCs, plug a switch into it. Unplug the extra ones: they still use power and USB bandwidth.</span></span></div>` : ""}
        <div class="lan-facts">
          ${nic ? html`${fact("Adapter", nic ? html`<span class="mono ellipsis" title="${nic.name}">${nic.name}</span>` : "—")}
          ${fact("MAC address", nic && nic.mac ? html`<span class="mono">${nic.mac}</span>` : "—")}
          ${fact("Link", nic ? (nic.carrier ? html`<span class="ok">${icon("check", "i-sm")} Cable connected</span>` : html`<span class="warn">${icon("x", "i-sm")} No cable</span>`) : "—",
            nic && !nic.carrier ? "Plug in a PC or hub" : "")}
          ${fact("Speed", nic && nic.carrier ? html`<span class="tnum">${fmtSpeed(nic.speed)}</span>${nic.duplex ? html` <span class="faint">${nic.duplex}</span>` : ""}` : "—",
            "Old NICs: 10 Mbit half-duplex")}` : ""}
          ${fact("Subnet", html`<span class="mono">${lan.subnet || "—"}</span>`)}
          ${fact("Gateway / DNS", html`<span class="mono">${lan.gateway || "—"}</span>`)}
          ${fact("DHCP range", p.role === "member" ? html`<span class="muted">From the party host</span>` : lan.dhcp_range && lan.dhcp_range.length === 2 ? html`<span class="mono">${lan.dhcp_range[0]} – ${lan.dhcp_range[1]}</span>` : "—", "", "span-2")}
        </div>
        ${up && lan.rx_bytes != null ? html`<div class="line-rates lan-rates">
          <div class="rate" title="Received by the Pi from the retro LAN"><span class="dir c-rx">${icon("down")}From PCs</span><b>${r ? fmtRate(r.rx) : html`<span class="faint">measuring…</span>`}</b><small>${fmtBytes(lan.rx_bytes)} total</small></div>
          <div class="rate" title="Sent by the Pi to the retro LAN"><span class="dir c-tx">${icon("up")}To PCs</span><b>${r ? fmtRate(r.tx) : html`<span class="faint">measuring…</span>`}</b><small>${fmtBytes(lan.tx_bytes)} total</small></div>
        </div>` : ""}`;
    }
    return html`<section class="card lan-hero ${cls}" aria-labelledby="h-lan">
      <div class="lan-hero-head">
        <span class="lt-icon">${icon("ethernet")}</span>
        <div class="lan-hero-title"><h2 id="h-lan">Retro-LAN port</h2><span class="sub">${sub}</span></div>
        <span class="spacer"></span>
        ${applying ? html`<span class="chip chip-warn">${spinner()} Applying…</span>` : ""}
        ${lanPill(lan)}
      </div>
      <div class="lan-hero-body">${body}</div>
    </section>`;
  },
  toggles(lan) {
    const en = this.value("lan.enabled"), share = this.value("lan.share_internet");
    const p = partyState() || {};
    const pubHost = p.role === "host" && p.public;
    const shareText = pubHost && share ? "On while you host a public party: strangers' PCs use your connection, and what they do online comes from your home IP."
      : p.role === "member"
      ? `Not used while this box is in ${partyName(p)}'s party: the host decides about internet access${p.view ? (p.view.share_internet ? " (it shares its internet)." : " (LAN only).") : "."}`
      : share ? `Internet shared: retro-LAN PCs reach the internet through this Pi (NAT), using your IP address.${p.role === "host" ? " This includes PCs at member boxes." : ""}` : LAN_ONLY_TEXT;
    const busy = (k) => (this.opt[k] && this.opt[k].saving ? raw("disabled") : "");
    const card = (key, id, ic, title, text, on, extraCls = "") => html`<div class="card lan-toggle ${on ? "is-on" : ""} ${extraCls}" data-key="t-${key}">
      <label class="lan-toggle-main" for="${id}">
        <span class="lt-icon">${icon(ic)}</span>
        <span class="lt-text"><b>${title}</b><small>${text}</small></span>
        <span class="switch"><input type="checkbox" id="${id}" data-change="lan-toggle" data-key="${key}" ${on ? raw("checked") : ""} ${busy(key)}><span class="switch-track"></span></span>
      </label></div>`;
    return html`<section class="grid grid-2 lan-toggles" aria-label="Retro-LAN switches">
      ${card("lan.enabled", "lan-sw-enabled", "ethernet", "LAN party port",
        en ? (lan.state === "no_nic" ? "On: waiting for a retro-LAN port." : "On: the second Ethernet port is an isolated retro LAN with DHCP.") : "Off: the second Ethernet port is not used.", en)}
      ${card("lan.share_internet", "lan-sw-share", "globe", "Share my internet with the LAN", shareText, share, share ? "is-share" : "")}
    </section>`;
  },
  leases(lan, p) {
    const fixed = (lan.static_pcs || []).map((x) => ({ ip: x.ip, mac: x.mac, hostname: x.name || "", fixed: true, in_range: x.in_range, seen: x.seen }));
    const list = [...(lan.leases || []), ...fixed].sort((a, b) => ipNum(a.ip) - ipNum(b.ip));
    const member = p && p.role === "member";
    const fromHost = `Your retro PCs get their addresses from the party host (${partyName(p || {})}).`;
    const nLease = (lan.leases || []).length;
    const sub = member ? "Addresses from the party host" : list.length ? `${nLease} DHCP lease${nLease === 1 ? "" : "s"}${fixed.length ? ` · ${fixed.length} fixed address${fixed.length === 1 ? "" : "es"}` : ""}${p && p.role === "host" ? " · all party boxes" : ""}` : "DHCP leases";
    const inRange = fixed.filter((x) => x.in_range);
    const range = lan.dhcp_range && lan.dhcp_range.length === 2 ? lan.dhcp_range : null;
    const gw = lan.gateway || "10.99.0.1", free = gw.replace(/\.\d+$/, "");
    return html`<section class="card" aria-labelledby="h-pcs" data-key="pcs-${member ? "member" : "dhcp"}">
      <div class="card-head"><h3 id="h-pcs">Connected PCs</h3><span class="sub">${sub}</span></div>
      ${list.length ? html`<div class="table-wrap"><table class="table lan-table">
          <thead><tr><th>PC</th><th>IP address</th><th>MAC address</th><th>Address</th></tr></thead>
          <tbody>${list.map((l) => html`<tr data-key="pc-${l.mac}">
            <td><div class="cell-user"><span class="pc-ic">${icon("pc")}</span>${l.hostname ? html`<b class="mono">${l.hostname}</b>` : html`<span class="faint">unknown</span>`}</div></td>
            <td class="mono nowrap">${l.ip}</td>
            <td class="mono nowrap muted">${l.mac}</td>
            <td class="nowrap">${l.fixed ? html`<span class="pill ${l.in_range ? "pill-amber" : "pill-gray"} no-dot" title="Set on the PC itself; it never asked this box for an address">Fixed address</span>${l.in_range ? html` <span class="warn">${icon("alert", "i-sm")}</span>` : ""}`
              : l.expires ? html`<span class="faint">DHCP · renews</span> ${relTime(l.expires)}` : html`<span class="faint">DHCP · no expiry</span>`}</td></tr>`)}</tbody></table></div>
          ${inRange.length && range ? html`<p class="card-note warn-note">${icon("alert", "i-sm")}<span>${inRange.map((x) => x.ip).join(", ")} ${inRange.length === 1 ? "is a fixed address" : "are fixed addresses"} inside the range the box hands out (${range[0]} to ${range[1]}). The box won't give ${inRange.length === 1 ? "it" : "them"} to another PC while ${inRange.length === 1 ? "that PC is" : "those PCs are"} on, but they can clash after a PC was switched off. Use ${free}.2 to ${free}.${Math.max(2, Number(range[0].split(".")[3]) - 1)} for fixed addresses, or set the PC to obtain an address automatically.</span></p>` : ""}
          ${member ? html`<p class="card-note">${icon("info", "i-sm")}${fromHost}</p>` : ""}`
        : member ? emptyState("network", "Addresses come from the host", fromHost)
        : emptyState("pc", "No PCs yet", "Set the PC's network to obtain an IP address automatically (DHCP).")}
    </section>`;
  },
  isolation(share, p) {
    const row = (ic, label, allowed, note = "") => html`<li><span class="iso-ic">${icon(ic)}</span><span class="iso-t">${label}${note ? html`<small>${note}</small>` : ""}</span>
      ${allowed ? html`<span class="pill pill-green">Allowed</span>` : html`<span class="pill pill-gray no-dot">${icon("lock", "i-sm")}Blocked</span>`}</li>`;
    const role = p ? p.role : "standalone";
    let inet = row("globe", "Internet", share, share ? `via this Pi (NAT)${role === "host" ? ": also for party members" : ""}` : "turn on sharing to allow");
    if (role === "member") {
      const v = p.view;
      inet = v ? row("globe", "Internet", !!v.share_internet, v.share_internet ? `via the party host (${partyName(p)})` : "the party host does not share its internet")
        : html`<li><span class="iso-ic">${icon("globe")}</span><span class="iso-t">Internet<small>decided by the party host</small></span><span class="pill pill-gray no-dot">Host's choice</span></li>`;
    }
    return html`<section class="card" aria-labelledby="h-iso">
      <div class="card-head">${icon("shield")}<h3 id="h-iso">Isolation</h3><span class="sub">What retro-LAN PCs can reach</span></div>
      <ul class="iso-list">
        ${row("pc", role === "standalone" || role === "unknown" ? "Other retro-LAN PCs" : "Retro PCs in the party", true, role === "host" || role === "member" ? "at every party box" : "")}
        ${row("home", "Home network", false)}
        ${row("pi", "This Pi", false, "except DHCP / DNS")}
        ${row("users", "Dial-in users", false)}
        ${inet}
      </ul>
    </section>`;
  },
  help(p) {
    const item = (t, d) => html`<li><b>${t}</b><span>${d}</span></li>`;
    const member = p && p.role === "member";
    return html`<section class="card" aria-labelledby="h-setup">
      <div class="card-head">${icon("info")}<h3 id="h-setup">Retro PC setup</h3></div>
      <ul class="help-list">
        ${item("TCP/IP", member ? html`Choose <i>Obtain an IP address automatically</i> (DHCP). The party host hands out addresses and DNS.`
          : html`Choose <i>Obtain an IP address automatically</i> (DHCP). The Pi hands out addresses and DNS.`)}
        ${item("IPX/SPX", html`The frame type must match on all PCs, Windows 95 <i>Auto</i> is fine.`)}
        ${item("NetBEUI", "Use the same workgroup name on every PC.")}
        ${item("Hardware", "Use a 10 Mbit-capable USB adapter or hub, many old cards only do 10 Mbit half-duplex.")}
      </ul>
    </section>`;
  },
  // the party itself lives on the Parties page; the LAN page shows where this box stands
  partyTile(p) {
    const r = p && p.role;
    const text = r === "host" ? [`Hosting ${p.public ? `the public party ${p.title || ""}` : "a LAN party"} · ${p.public ? "public" : isHubParty(p) ? "private · via the cloud" : "private · direct"}`,
      `${activeMembers(p).length + 1} box${activeMembers(p).length ? "es" : ""} · your PCs and theirs share one LAN; this box hands out the addresses.`]
      : r === "member" ? [p.status === "approved" ? `In ${partyName(p)}'s party` : `Joining ${partyName(p)}'s party…`, "Your retro-LAN port is bridged into the party; the host hands out the addresses."]
      : ["Not in a party", "Your retro PCs only see each other. Host or join a party to link up with other boxes, or browse the public parties."];
    return html`<a class="card party-tile ${r === "host" || r === "member" ? "is-in" : ""}" href="#/parties" data-key="party-tile">
      <span class="lt-icon">${icon("network")}</span><span class="ph-t"><b>${text[0]}</b><small>${text[1]}</small></span>
      <span class="btn ${r === "host" || r === "member" ? "" : "btn-primary"}">${r === "host" || r === "member" ? "Open on Parties" : "Go to Parties"}</span></a>`;
  },
  // LAN party card (standalone: host/join · host: invite, requests, members · member: status, roster)
  party(lan, p) {
    const head = (extra = "") => html`<div class="card-head">${icon("network")}<h3 id="h-party">LAN party</h3>${extra}</div>`;
    if (!p) {
      return html`<section class="card party-card" data-key="party-loading" aria-labelledby="h-party" aria-busy="true">${head()}
        <div class="party-pad"><div class="skel skel-line" style="width:70%"></div><div class="skel" style="height:110px"></div></div></section>`;
    }
    if (p.role !== this.lastRole) { this.lastRole = p.role; this.createErr = this.joinErr = ""; }
    if (p.role === "host") return this.partyHost(lan, p);
    if (p.role === "member") return this.partyMember(p);
    if (p.role === "standalone") return this.partyStandalone(lan);
    return html`<section class="card party-card" data-key="party-unknown" aria-labelledby="h-party">${head(html`<span class="spacer"></span><span class="pill pill-gray">Unavailable</span>`)}
      ${emptyState("alert", "LAN service not running", html`${p.error || "The LAN service is not running"}. Start it on the Pi with <code>sudo systemctl start netkey-lan</code>.`)}</section>`;
  },
  partyStandalone(lan) {
    const dis = this.busy ? raw("disabled") : "";
    const bz = (k) => (this.busy === k ? "is-busy" : "");
    const direct = this.via === "direct";
    const viaOpt = (v, title, text, extra = "") => html`<label class="pv-opt" data-key="via-${v}">
      <input type="radio" name="via" value="${v}" data-change="party-via" ${this.via === v ? raw("checked") : ""} ${dis}>
      <span class="pv-radio" aria-hidden="true"></span>
      <span class="pv-t"><b>${icon(v === "hub" ? "cloud" : "swap", "i-sm")}${title}${extra}</b><small>${text}</small></span></label>`;
    const portNote = lan.state === "disabled" ? "Turn on the LAN party port above first, a party links this box's retro-LAN port."
      : lan.state === "no_nic" ? "Set up a retro-LAN port first (see the LAN page): a party links it." : "";
    return html`<section class="card party-card" data-key="party-standalone" aria-labelledby="h-party">
      <div class="card-head">${icon("network")}<h3 id="h-party">LAN party</h3><span class="sub">Up to 16 boxes on one LAN</span></div>
      <div class="party-pad">
        <p class="muted party-lead">Link NetKey boxes over the internet: retro PCs at different places share one Ethernet LAN, IPX, NetBEUI and TCP/IP work as if they were in the same room.</p>
        <p class="faint" style="font-size:12.5px">${icon("key", "i-sm")} Others see this box as <b>${(S.status && S.status.box_name) || "this box"}</b> · <a href="#/settings">change the name</a></p>
        ${portNote ? html`<div class="banner banner-warn lan-banner" role="status">${icon("info")}<span>${portNote}</span></div>` : ""}
        <div class="party-options">
          <form class="party-opt" data-submit="party-create" novalidate>
            <div class="po-head"><span class="lt-icon">${icon("home")}</span><span class="po-t"><b>Host a party</b><small>Your box runs the party: its DHCP and gateway serve every PC. Sharing your internet stays your choice.</small></span></div>
            <div class="party-host-opts">
              <fieldset class="party-via"><legend class="sr-only">How members connect</legend>
                ${viaOpt("hub", "Cloud hub", html`Works anywhere, no router setup. Traffic is relayed, end-to-end encrypted.`, html`<span class="badge badge-vmm">RECOMMENDED</span>`)}
                ${viaOpt("direct", "Direct", html`Members connect straight to your box. Needs UDP port <span class="mono">${PARTY_PORT}</span> forwarded on your router.`)}
              </fieldset>
              ${direct ? html`<label class="field party-ep" data-key="party-ep"><span class="field-label">Public address members use <span class="faint">(optional)</span></span>
                <input class="input" type="text" name="endpoint" id="party-endpoint" placeholder="your.dyndns.example or public IP" autocomplete="off" autocapitalize="off" spellcheck="false" maxlength="253"
                  data-input="party-clear" data-err="createErr" aria-invalid="${this.createErr ? "true" : "false"}">
                ${this.createErr ? html`<span class="field-error" role="alert">${this.createErr}</span>` : html`<span class="field-help">Leave empty to use this network's public IP.</span>`}</label>` : ""}
            </div>
            <button type="submit" class="btn btn-primary ${bz("create")}" ${dis}>${icon("plus")}Host a party</button>
          </form>
          <form class="party-opt" data-submit="party-join" novalidate>
            <div class="po-head"><span class="lt-icon">${icon("key")}</span><span class="po-t"><b>Join a party</b><small>Enter the invite code from the host. Once the host approves, your retro PCs get their addresses from it.</small></span></div>
            <label class="field"><span class="field-label">Invite code</span>
              <textarea class="input textarea mono party-code-in" name="code" id="party-code" rows="2" placeholder="${HUB_CODE_HINT}" autocomplete="off" autocapitalize="off" spellcheck="false"
                data-input="party-code-input" data-err="joinErr" aria-invalid="${this.joinErr ? "true" : "false"}"></textarea>
              ${this.joinErr ? html`<span class="field-error" role="alert">${this.joinErr}</span>`
                : html`<span class="field-help">Codes for direct parties start with <span class="mono">${PARTY_CODE_PREFIX}</span>. Paste them here too.</span>`}</label>
            <button type="submit" class="btn btn-primary ${bz("join")}" ${dis}>${icon("link")}Join party</button>
          </form>
        </div>
      </div>
    </section>`;
  },
  partyHost(lan, p) {
    const dis = this.busy || p.closing ? raw("disabled") : "";
    const bz = (k) => (this.busy === k ? "is-busy" : "");
    const members = p.members || [], pending = p.pending || [];
    const active = activeMembers(p).length, slots = Math.max(1, (p.max || 16) - 1);
    const online = members.filter((m) => m.online && !m.kicked).length;
    const boxes = active + 1, maxBoxes = slots + 1;   // the host counts as a box of the party
    const code = p.code || "";
    const masked = code.startsWith(PARTY_CODE_PREFIX)
      ? PARTY_CODE_PREFIX + code.slice(PARTY_CODE_PREFIX.length).split("-").slice(0, 4).map((g) => "•".repeat(g.length)).join("-") + "-…"
      : "•".repeat(32);
    const ep = `${p.endpoint || "this Pi"}:${p.port || PARTY_PORT}`;
    const hub = isHubParty(p);
    const aa = this.aa;
    return html`<section class="card party-card is-host ${pending.length ? "has-pending" : ""} ${p.closing ? "is-closing" : ""}" data-key="party-host" aria-labelledby="h-party">
      <div class="party-head">
        <span class="lt-icon">${icon("network")}</span>
        <div class="party-title"><h2 id="h-party" class="ellipsis" title="${p.title || p.name}">${p.title || p.name}</h2>
          <span class="sub">Your LAN party · <b class="tnum">${boxes} of ${maxBoxes}</b> boxes · ${online + 1} online · ${hub ? "via cloud hub" : "direct"}</span></div>
        <span class="spacer"></span>
        ${p.closing ? html`<span class="pill pill-amber pill-pulse">Closing…</span>` : html`<span class="pill pill-green pill-glow">Hosting</span>`}
        <button type="button" class="btn btn-danger btn-sm ${bz("close")}" data-action="party-close" ${p.closing ? raw("disabled") : dis}>${icon("x")}Close party</button>
      </div>
      ${p.closing ? html`<div class="party-pad party-pad-top"><div class="banner banner-warn lan-banner" role="status">${spinner()}<span><b>Closing the party.</b> <span class="muted">Members are told and return to standalone within a few seconds.</span></span></div></div>` : ""}
      <div class="party-body">
        <div class="party-invite">
          <div class="pi-top"><span class="pi-label">${icon("key", "i-sm")}Invite code</span><span class="faint">Anyone with this code can ${aa && aa.value ? "join" : "ask to join"}.</span></div>
          ${hub ? html`<div class="party-hubcode">
              ${code ? html`<output class="phc-code" aria-label="Invite code">${code}</output>` : html`<div class="skel skel-line" style="width:60%;margin:0"></div>`}
              <button type="button" class="btn btn-primary" data-action="party-copy" ${code ? "" : raw("disabled")}>${icon("copy")}Copy</button></div>`
            : code ? html`<div class="party-code ${this.showCode ? "" : "is-masked"}" aria-label="${this.showCode ? "Invite code" : "Invite code (hidden)"}">${this.showCode ? code : masked}</div>`
            : html`<div class="party-code"><div class="skel skel-line" style="width:80%;margin:0"></div></div>`}
          <div class="party-code-actions">
            ${hub ? "" : html`<button type="button" class="btn btn-sm" data-action="party-show" aria-pressed="${String(this.showCode)}" ${code ? "" : raw("disabled")}>${icon(this.showCode ? "eye-off" : "eye")}${this.showCode ? "Hide" : "Show"}</button>
            <button type="button" class="btn btn-sm btn-primary" data-action="party-copy" ${code ? "" : raw("disabled")}>${icon("copy")}Copy code</button>`}
            ${hub ? html`<span class="faint party-code-note">Members paste it under Join with a code on their Parties page, no router setup needed.</span>` : ""}
            <span class="spacer"></span>
            <button type="button" class="btn btn-sm btn-ghost ${bz("regenerate")}" data-action="party-regenerate" ${dis}>${icon("reset")}New code</button>
            ${hub ? "" : html`<button type="button" class="btn btn-sm btn-ghost ${bz("endpoint")}" data-action="party-endpoint" ${dis}>${icon("edit")}Change address</button>`}
          </div>
          ${hub ? hubLine(p)
            : html`<p class="party-reach">${icon("globe", "i-sm")}<span>Members must reach <b class="mono">${ep}/UDP</b>. Forward this port on the host's router if members are outside your network.</span></p>`}
        </div>
        ${aa && aa.missing ? "" : html`<label class="party-auto" for="party-sw-auto">
          <span class="iso-ic">${icon("check")}</span>
          <span class="lt-text"><b>Approve join requests automatically</b><small>${aa && aa.value ? "Every box with the invite code joins without asking." : "You approve each box that asks to join."}</small></span>
          <span class="switch"><input type="checkbox" id="party-sw-auto" data-change="party-auto" ${aa && aa.value ? raw("checked") : ""} ${!aa || aa.saving ? raw("disabled") : ""}><span class="switch-track"></span><span class="sr-only">Approve join requests automatically</span></span>
        </label>`}
        ${this.hostExtras ? this.hostExtras(p) : ""}
        ${pending.length ? this.partyPending(pending, dis, bz) : ""}
        ${this.partyMembers(members, boxes, maxBoxes, dis, bz, hub, p.name)}
      </div>
    </section>`;
  },
  partyPending(pending, dis, bz) {
    return html`<div class="party-pending" role="region" aria-labelledby="h-pending" data-key="pending">
      <div class="pm-head"><span class="pp-dot" aria-hidden="true"></span><h4 id="h-pending">${pending.length} join request${pending.length === 1 ? "" : "s"}</h4><span class="sub hide-sm">Only approve boxes you know</span><span class="spacer"></span>
        ${pending.length > 1 ? html`<button type="button" class="btn btn-sm btn-primary ${bz("approve:all")}" data-action="party-approve" data-member="all" ${dis}>${icon("check")}Approve all</button>` : ""}</div>
      <ul class="pp-list">${pending.map((r) => html`<li data-key="pend-${r.pub}">
        ${avatar(r.name)}<span class="pp-t"><b class="mono ellipsis">${r.name}</b><small>asked ${relTime(r.since)}${r.verify ? html` · key check <b class="mono" title="Ask the other player which key check their box shows; only approve if it matches">${r.verify}</b>` : ""}</small></span>
        <span class="pp-btns"><button type="button" class="btn btn-sm ${bz("reject:" + r.pub)}" data-action="party-reject" data-member="${r.pub}" ${dis}>Reject</button>
        <button type="button" class="btn btn-sm btn-primary ${bz("approve:" + r.pub)}" data-action="party-approve" data-member="${r.pub}" ${dis}>${icon("check")}Approve</button></span></li>`)}</ul>
    </div>`;
  },
  partyMembers(members, boxes, maxBoxes, dis, bz, hub, hostName) {
    const list = [...members].sort((a, b) => a.id - b.id);
    const none = html`<span class="faint">—</span>`;
    return html`<div class="party-members" data-key="members">
      <div class="pm-head"><h4>Boxes in the party</h4><span class="sub">${boxes} of ${maxBoxes} · ${hub ? "relayed by the cloud hub" : "direct connections"}</span></div>
      <div class="table-wrap"><table class="table party-table">
        <thead><tr><th>Box</th><th class="r">Latency</th><th>Connection</th><th>Data</th><th>Joined</th><th><span class="sr-only">Actions</span></th></tr></thead>
        <tbody><tr data-key="mem-host" class="is-self">
          <td><div class="cell-user"><span class="dot dot-live" title="Online"></span>
            <span class="pm-name"><b class="mono">${hostName}</b><small>Host · this box · DHCP and gateway for the party</small></span>
            <span class="badge badge-vmm">YOU</span></div></td>
          <td class="r">${none}</td><td class="nowrap"><span class="faint">Host</span></td><td>${none}</td><td>${none}</td><td></td>
        </tr>${list.map((m) => html`<tr data-key="mem-${m.id}" class="${m.kicked ? "is-kicked" : ""}">
          <td><div class="cell-user"><span class="dot ${m.online && !m.kicked ? "dot-live" : ""}" title="${m.online && !m.kicked ? "Online" : "Offline"}"></span>
            <span class="pm-name"><b class="mono">${m.name}</b><small>Member #${m.id} · ${m.kicked ? "removed by you" : m.online ? (m.handshake_age != null ? `online · handshake ${fmtDur(m.handshake_age)} ago` : "online") : "offline"}${m.verify ? html` · key check <span class="mono" title="The member's box shows the same key check">${m.verify}</span>` : ""}</small></span></div></td>
          <td class="r nowrap tnum">${m.rtt_ms != null && !m.kicked ? `${Math.round(m.rtt_ms)} ms` : html`<span class="faint">—</span>`}</td>
          <td class="nowrap"><span class="pm-conn">${connBadge(m.connection || (hub ? "relay" : "direct"), m.relay)}${(m.connection || (hub ? "relay" : "direct")) === "direct" ? html`<span class="mono muted">${m.endpoint || html`<span class="faint">—</span>`}</span>` : html`<span class="muted">via <span class="mono">${m.relay || "cloud hub"}</span></span>`}</span></td>
          <td class="nowrap tnum pm-data"><span title="Received from this box"><i class="c-rx">${icon("down", "i-sm")}</i>${fmtBytes(m.rx_bytes)}</span><span title="Sent to this box"><i class="c-tx">${icon("up", "i-sm")}</i>${fmtBytes(m.tx_bytes)}</span></td>
          <td class="nowrap">${m.joined ? relTime(m.joined) : html`<span class="faint">—</span>`}</td>
          <td class="r">${m.kicked ? html`<span class="faint nowrap pm-removing">${spinner()}removing…</span>`
            : html`<button type="button" class="btn btn-sm btn-danger-text ${bz("kick:" + m.id)}" data-action="party-kick" data-member="${m.id}" data-name="${m.name}" ${dis}>Kick</button>`}</td>
        </tr>`)}</tbody></table></div>
      ${list.length ? "" : html`<div class="party-empty">${icon("users")}<span><b>Waiting for others.</b> Share the invite code, boxes that join appear here and their retro PCs get addresses from this Pi.</span></div>`}
    </div>`;
  },
  partyMember(p) {
    const dis = this.busy ? raw("disabled") : "";
    const bz = (k) => (this.busy === k ? "is-busy" : "");
    const unreachable = memberUnreachable(p), approved = p.status === "approved";
    const err = p.error || (unreachable ? "Cannot reach the host" : "");
    const fact = (label, value) => html`<div><small>${label}</small><b>${value}</b></div>`;
    const v = p.view;
    const hub = isHubParty(p), title = p.party_name ? `${p.party_name}'s party` : hub ? "LAN party" : p.host_endpoint || "LAN party";
    const tone = unreachable || (err && approved) ? "is-err" : approved ? "is-ok" : "is-wait";
    return html`<section class="card party-card is-member ${tone}" data-key="party-member" aria-labelledby="h-party">
      <div class="card-head">${icon("network")}<h3 id="h-party" class="ellipsis" title="${title}">${title}</h3><span class="spacer"></span>${memberPill(p)}</div>
      <div class="party-pad">
        ${err ? html`<div class="banner ${approved ? "banner-err" : "banner-warn"} lan-banner" role="alert">${icon("alert")}<span><b>${String(err).replace(/\.$/, "")}.</b>
          ${unreachable ? html` <span class="muted">Lost ${relTime(p.unreachable_since)}, retrying automatically.</span>` : ""}</span></div>` : ""}
        ${p.status === "joining" && !err ? html`<p class="party-wait">${spinner()}<span>${hub ? "Contacting the host via the cloud hub…" : html`Contacting the host at <b class="mono">${p.host_endpoint}</b>…`}</span></p>` : ""}
        ${p.status === "pending" ? html`<p class="party-wait is-pending">${icon("clock")}<span>Waiting for the host to approve this box. Ask them to check the Parties page of their NetKey${p.verify ? html` and confirm they see the key check <b class="mono">${p.verify}</b>` : ""}.</span></p>` : ""}
        <div class="lan-facts party-facts">
          ${hub ? fact("Connection", html`<span class="pm-conn">${connBadge(p.connection || "relay", p.relay)}<span>${p.relay ? html`via <span class="mono">${relayLabel(p, p.relay)}</span>` : "via cloud hub"}</span></span>`)
            : fact("Host address", html`<span class="mono" title="${p.host_endpoint || ""}">${wbrEndpoint(p.host_endpoint)}</span>`)}
          ${p.verify ? fact("Key check", html`<span class="mono" title="The host sees the same code next to this box">${p.verify}</span>`) : ""}
          ${fact("Your member #", p.member_id != null ? html`<span class="tnum">#${p.member_id}</span>` : html`<span class="faint">after approval</span>`)}
          ${fact("Latency", p.rtt_ms != null ? html`<span class="tnum">${Math.round(p.rtt_ms)} ms</span>` : html`<span class="faint">—</span>`)}
          ${fact("Last handshake", p.handshake_age != null ? html`<span class="tnum">${fmtDur(p.handshake_age)} ago</span>` : html`<span class="faint">—</span>`)}
        </div>
        ${hub ? hubLine(p) : ""}
        ${v ? html`<div class="party-roster"><div class="pm-head"><h4>In this party</h4><span class="sub">${(v.members || []).length + 1} boxes</span></div>
          <ul class="pr-list">
            <li data-key="pr-host">${avatar(v.name || "?")}<b class="mono ellipsis">${v.name || partyName(p)}</b><span class="badge">HOST</span></li>
            ${(v.members || []).map((m) => html`<li data-key="pr-${m.id}">${avatar(m.name)}<b class="mono ellipsis">${m.name}</b>${m.id === p.member_id ? html`<span class="badge badge-vmm">YOU</span>` : ""}<span class="spacer"></span><span class="faint tnum">#${m.id}</span></li>`)}
          </ul></div>` : ""}
        <div class="party-foot"><span class="faint">${unreachable ? "Reconnects automatically when the host is back." : approved ? "Your retro PCs are on the party LAN." : "Leaving cancels the join request."}</span><span class="spacer"></span>
          <button type="button" class="btn btn-sm btn-danger-text ${bz("leave")}" data-action="party-leave" ${dis}>${icon("logout")}Leave party</button></div>
      </div>
    </section>`;
  },
  // Sends one party command; the response carries the new party status (also on 400).
  async cmd(cmd, body = {}, key = cmd) {
    if (this.busy) return false;
    this.busy = key;
    this.render();
    let ok = false;
    try {
      const res = await api("/api/party", { method: "POST", body: { cmd, ...body } });
      setParty(res.party);
      toast((res.result && res.result.message) || "Done");
      ok = true;
    } catch (e) {
      if (e.data && e.data.party) setParty(e.data.party);
      if (e.status !== 401) toastErr(e, PARTY_CMD_ERR[cmd]);
    }
    this.busy = null;
    if (current === this) this.render();
    refreshStatus();
    return ok;
  },
  render() {
    const st = S.status;
    if (!st) {
      patch(view, html`<div class="stack"><div class="card card-pad"><div class="skel skel-line" style="width:35%"></div><div class="skel" style="height:80px;margin-top:12px"></div></div>
        <div class="grid grid-2">${[1, 2].map(() => html`<div class="card card-pad"><div class="skel skel-line"></div><div class="skel skel-line" style="width:60%"></div></div>`)}</div></div>`);
      return;
    }
    const lan = st.lan;
    if (!lan) {
      patch(view, html`<div class="card">${emptyState("ethernet", "LAN status not available", "This NetKey version does not report a retro-LAN port.")}</div>`);
      return;
    }
    const share = this.value("lan.share_internet");
    const p = partyState();
    const host = p && p.role === "host";
    patch(view, html`<div class="stack">
      ${this.hero(lan)}
      ${ipClashBanner(lan)}
      ${this.toggles(lan)}
      ${this.partyTile(p)}
      ${this.leases(lan, p)}
      <div class="grid grid-2 lan-row">${this.help(p)}${this.isolation(share, p)}</div>
    </div>`);
    // patch() leaves a focused checkbox alone; force the switches to the (optimistic) truth
    $$('[data-change="lan-toggle"]', view).forEach((i) => { i.checked = this.value(i.dataset.key); });
    const aa = $("#party-sw-auto", view);
    if (aa && this.aa) aa.checked = !!this.aa.value;
  },
  async save(key, value) {
    this.opt[key] = { value, saving: true, until: 0 };
    this.render();
    try {
      await api("/api/settings", { method: "PUT", body: { values: { [key]: value } } });
      this.opt[key] = { value, saving: false, until: Date.now() + 12000 };
      if (key === "lan.enabled") toast(value ? "LAN party port turned on" : "LAN party port turned off", { detail: "The Pi applies it within a few seconds." });
      else toast(value ? "Internet sharing on" : "Internet sharing off", { detail: value ? "Retro-LAN PCs can now reach the internet through this Pi." : "LAN only: PCs can still talk to each other." });
      this.render();
      refreshStatus();
    } catch (e) {
      delete this.opt[key];
      this.render();
      toastErr(e, key === "lan.enabled" ? "Could not change the LAN party port" : "Could not change internet sharing");
    }
  },
  actions: {
    async "lan-toggle"(input) {
      const key = input.dataset.key, on = input.checked;
      if (key === "lan.share_internet" && on) {
        input.checked = false;
        const p = partyState() || {};
        const ok = p.role === "host" && p.public ? await confirmPublicShare()
          : await confirmDialog({ title: "Share your internet?", message: LAN_SHARE_TEXT, confirmText: "Share internet" });
        if (!ok || current !== this) { if (current === this) this.render(); return; }
      }
      this.save(key, on);
    },
    "party-via"(input) {
      if (!input.checked || this.via === input.value) return;
      this.via = input.value;
      this.createErr = "";
      this.render();
      if (this.via === "direct") { const ep = $("#party-endpoint", view); if (ep) ep.focus(); }
    },
    async "party-create"(form) {
      if (this.via !== "direct") {
        this.createErr = "";
        await this.cmd("create", { via: "hub" });
        return;
      }
      const input = form.elements.endpoint, ep = input.value.trim();
      if (ep && !/^[A-Za-z0-9.:[\]-]+$/.test(ep)) {
        this.createErr = "Enter a host name or IP address (no spaces).";
        this.render();
        input.focus();
        return;
      }
      this.createErr = "";
      await this.cmd("create", ep ? { via: "direct", endpoint: ep } : { via: "direct" });
    },
    async "party-join"(form) {
      const ta = form.elements.code, hubCode = normHubCode(ta.value);
      const code = hubCode || ta.value.replace(/\s+/g, "");
      this.joinErr = !code ? "Enter the invite code from the party host."
        : hubCode ? (hubCode.replace(/-/g, "").length !== HUB_CODE_PREFIX.length + HUB_CODE_LEN ? `That code is incomplete, it looks like ${HUB_CODE_HINT}.` : "")
        : !code.startsWith(PARTY_CODE_PREFIX) ? `That is not a NetKey invite code, it looks like ${HUB_CODE_HINT} or starts with ${PARTY_CODE_PREFIX}` : "";
      if (this.joinErr) { this.render(); ta.focus(); return; }
      if (hubCode) ta.value = hubCode;
      await this.cmd("join", { code });
    },
    // Upper-cases cloud-hub codes while typing (direct netkey1- codes are case-sensitive and stay untouched).
    "party-code-input"(ta) {
      if (/^\s*retro/i.test(ta.value) && ta.value !== ta.value.toUpperCase()) {
        const { selectionStart: a, selectionEnd: b } = ta;
        ta.value = ta.value.toUpperCase();
        ta.setSelectionRange(a, b);
      }
      this.actions["party-clear"].call(this, ta);
    },
    "party-clear"(input) {
      const k = input.dataset.err;
      if (this[k]) { this[k] = ""; this.render(); }
    },
    "party-show"() { this.showCode = !this.showCode; this.render(); },
    async "party-copy"() {
      const p = partyState();
      if (!p || !p.code) return;
      if (await copyText(p.code)) toast("Invite code copied", { detail: isHubParty(p) ? "Send it to the boxes that should join, they need no router setup." : "Send it to the boxes that should join." });
      else {
        this.showCode = true;
        this.render();
        toast("Could not copy automatically", { type: "err", detail: "The code is shown now, select it and copy it by hand." });
      }
    },
    async "party-regenerate"() {
      const ok = await confirmDialog({ title: "Create a new invite code?", message: "The old code stops working. Boxes that already joined stay in the party.", confirmText: "New code" });
      if (ok && current === this) this.cmd("regenerate");
    },
    "party-endpoint"() {
      const p = partyState();
      if (!p || p.role !== "host") return;
      openModal({
        title: "Change the host address", iconName: "globe",
        body: html`<div class="field"><label class="field-label" for="pe-input">Public address members use</label>
          <input class="input mono" id="pe-input" name="endpoint" value="${p.endpoint || ""}" placeholder="your.dyndns.example or public IP" autocomplete="off" autocapitalize="off" spellcheck="false" maxlength="253">
          <span class="field-error" id="pe-err"></span>
          <span class="field-help">Members connect to this name or IP on UDP port ${p.port || 51899}. The invite code contains it, share the new code afterwards.</span></div>`,
        foot: html`<button type="button" class="btn" data-close>Cancel</button><button type="submit" class="btn btn-primary">Save address</button>`,
        onSubmit: async (form) => {
          const input = form.elements.endpoint, ep = input.value.trim();
          const bad = !ep ? "Enter a host name or IP address." : !/^[A-Za-z0-9.:[\]-]+$/.test(ep) ? "Use a host name or IP address without spaces." : "";
          if (bad) {
            form.querySelector("#pe-err").textContent = bad;
            input.setAttribute("aria-invalid", "true");
            setTimeout(() => input.focus(), 0);
            return false;
          }
          if (ep === p.endpoint) return true;
          return this.cmd("endpoint", { endpoint: ep });
        },
      });
    },
    "party-approve"(b) { const m = b.dataset.member; this.cmd("approve", { member: m }, "approve:" + m); },
    "party-reject"(b) { const m = b.dataset.member; this.cmd("reject", { member: m }, "reject:" + m); },
    async "party-kick"(b) {
      const id = b.dataset.member, name = b.dataset.name || `member ${id}`;
      const ok = await confirmDialog({ title: `Remove ${name}?`, message: "Its retro PCs lose the LAN immediately.", confirmText: "Remove", danger: true });
      if (ok && current === this) this.cmd("kick", { member: id }, "kick:" + id);
    },
    async "party-close"() {
      const ok = await confirmDialog({ title: "Close the LAN party?", message: "All members are disconnected and return to standalone. The invite code stops working.", confirmText: "Close party", danger: true });
      if (ok && current === this) this.cmd("close");
    },
    async "party-leave"() {
      const p = partyState() || {};
      const ok = await confirmDialog({ title: "Leave the LAN party?", message: `This box leaves ${partyName(p)}'s party and your retro PCs lose the party LAN.`, confirmText: "Leave party", danger: true });
      if (ok && current === this) this.cmd("leave");
    },
    async "party-auto"(input) {
      const on = input.checked;
      this.aa = { value: on, saving: true };
      this.render();
      try {
        await api("/api/settings", { method: "PUT", body: { values: { "lan.party_auto_approve": on } } });
        this.aa = { value: on, saving: false };
        toast(on ? "Join requests are approved automatically" : "Join requests need your approval", { detail: on ? "Every box with the invite code joins without asking." : "" });
      } catch (e) {
        this.aa = { value: !on, saving: false };
        toastErr(e, "Could not change auto-approve");
      }
      if (current === this) this.render();
    },
  },
};

// page: parties (public directory and events, hosting and joining, chat; netkey-dev/docs/public-parties.md)
// Sharing your internet with a public party: strangers' traffic leaves from your home IP. Asked every time.
const PUBLIC_SHARE_TEXT = "Everyone in a public party is a stranger. Their PCs will use your internet connection, and everything they do online (downloads, chat, anything illegal) comes from your home IP address. You are responsible for it, and your internet provider or the police will come to you, not to them. Only turn this on if you trust everyone in the party, and turn it off again when you don't need it.";
const confirmPublicShare = () => confirmDialog({ title: "Share your internet with strangers?", message: PUBLIC_SHARE_TEXT, confirmText: "I understand, share it", danger: true });
const PING = (ms) => (ms == null ? ["", "Ping unknown: the boxes haven't measured the relay yet"] : ms <= 50 ? ["g", "Great for IPX games"]
  : ms <= 90 ? ["a", "OK, lockstep games may stutter"] : ["r", "Too slow for lockstep games"]);
const pingPill = (l) => {
  const [c, label] = PING(l.est_ms);
  const tip = l.est_parts ? `Estimated from relay times: your box → ${l.relay} ${Math.round(l.est_parts[0])} ms + host → ${l.relay} ${Math.round(l.est_parts[1])} ms. ${label}.` : label;
  return html`<span class="ping ${c}" title="${tip}"><span class="bars"><i></i><i></i><i></i></span>${l.est_ms == null ? "≈? ms" : `≈${l.est_ms} ms`}</span>`;
};
const gameTag = (g) => (g.key === "other" ? "" : html`<em>${g.custom ? "OTHER · " : ""}${g.proto}</em>`);
const gameChip = (g) => html`<span class="game ${g.custom ? "custom" : ""}" title="${g.hint || ""}">${g.name}${gameTag(g)}</span>`;
const dateBlock = (t) => {
  const d = new Date(t * 1000);
  return html`<span class="lp-date"><small>${d.toLocaleDateString(undefined, { weekday: "short" })}</small><b>${d.getDate()}</b><span>${d.toLocaleDateString(undefined, { month: "short" })}</span></span>`;
};
const fmtStart = (t) => new Date(t * 1000).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const REPEAT_NAME = { weekly: "Weekly", monthly: "Monthly" };
const EVENT_ID_RE = /^[0-9A-Za-z]{4}-?[0-9A-Za-z]{4}$/;

const Parties = {
  title: "Parties",
  mount() {
    this.busy = null;
    this.showCode = false;
    this.aa = null;
    this.joinErr = "";
    this.createErr = "";
    this.pub = { me: null, err: null, draft: "", check: null, saving: false, editing: false, seq: 0, skipped: false };
    this.dir = null; this.dirErr = null; this.mine = null; this.games = null;
    this.tab = "all"; this.q = ""; this.near = false;
    this.lst = null; this.lstErr = null;
    this.chat = null;
    this.form = null;
    this.timers = {};
    this.onHash();
    this.loadMe();
    this.loadParty();
    this.loadAutoApprove();
  },
  unmount() { Object.values(this.timers).forEach(clearTimeout); this.timers = {}; },
  update() { this.render(); },
  onVisible() {
    this.loadParty();
    this.refreshScreen();
    if (this.chat) { clearTimeout(this.timers.chat); this.pollChat(); }     // catch up on what was said meanwhile
  },
  // A poll timer that survives a hidden tab: while hidden it doesn't fetch, but stays armed (and onVisible fetches
  // at once). Dropping it instead left the chat frozen until this box posted something itself.
  later(name, ms, fn) {
    clearTimeout(this.timers[name]);
    this.timers[name] = setTimeout(() => {
      if (current !== this) return;
      if (document.hidden) this.later(name, ms, fn); else fn();
    }, ms);
  },
  // screens: #/parties · #/parties/l/<listing> · #/parties/host
  onHash() {
    const m = location.hash.match(/^#\/parties(?:\/(l|host))?(?:\/([0-9a-f]{12}))?/) || [];
    const screen = m[1] === "l" && m[2] ? { name: "listing", id: m[2] } : m[1] === "host" ? { name: "host" } : { name: "main" };
    const changed = !this.screen || this.screen.name !== screen.name || this.screen.id !== screen.id;
    this.screen = screen;
    if (changed) {
      this.lst = null; this.lstErr = null;
      if (screen.name !== "listing") this.chatFor(null);
      if (screen.name === "host" && !this.form) this.form = this.newForm();
      if (screen.name === "host") this.loadGames();
      window.scrollTo(0, 0);
    }
    this.refreshScreen();
    this.render();
  },
  refreshScreen() {
    if (this.screen.name === "main") { this.loadDir(); this.loadMine(); this.partyChat(); }
    if (this.screen.name === "listing") this.loadListing();
  },
  go(hash) { if (location.hash !== hash) location.hash = hash; else this.onHash(); },

  // ---- loading
  async loadMe() {
    try { this.pub.me = await api("/api/parties/me"); this.pub.err = null; }
    catch (e) { if (e.status !== 401) this.pub.err = e.message; }
    if (current === this) this.render();
  },
  inSession(p = partyState()) { return !!p && !!p.role && p.role !== "standalone"; },
  async loadDir() {
    if (this.inSession()) {                       // hidden while in a party: don't poll the hub for it
      if (current === this && this.screen.name === "main") this.later("dir", 15000, () => this.loadDir());
      return;
    }
    try { this.dir = await api("/api/parties/directory"); this.dirErr = null; }
    catch (e) { if (e.status !== 401) this.dirErr = e.message; }
    if (current !== this) return;
    this.render();
    if (this.screen.name === "main") this.later("dir", 15000, () => this.loadDir());
  },
  async loadMine() {
    try { this.mine = await api("/api/parties/mine"); } catch { /* "Your events" stays hidden */ }
    if (current === this) this.render();
  },
  async loadGames() {
    if (this.games) return;
    try { this.games = await api("/api/parties/games"); } catch (e) { this.gamesErr = e.message; }
    if (current === this) this.render();
  },
  async loadListing() {
    const id = this.screen.id;
    try {
      const l = await api(`/api/parties/listing/${id}`);
      if (this.screen.id !== id) return;
      this.lst = l; this.lstErr = null;
      this.chatFor(this.inParty(l) && l.chat !== false ? l.room : null);
    } catch (e) { if (this.screen.id === id && e.status !== 401) { this.lstErr = e.message; this.chatFor(null); } }
    if (current !== this) return;
    this.render();
    if (this.screen.name === "listing") this.later("lst", 10000, () => this.loadListing());
  },
  async loadParty() {
    const was = this.inSession();
    try { setParty(await api("/api/party")); } catch { /* the banners report a missing LAN service */ }
    if (was && !this.inSession() && current === this && this.screen.name === "main") { this.loadDir(); this.loadMine(); }   // party over
    if (current === this) { this.partyChat(); this.render(); }
    this.later("party", 3000, () => this.loadParty());
  },
  async loadAutoApprove() {
    try {
      const cfg = await api("/api/settings");
      if (current !== this) return;
      if (!(this.aa && this.aa.saving)) this.aa = "lan.party_auto_approve" in cfg.values ? { value: !!cfg.values["lan.party_auto_approve"], saving: false } : { missing: true };
      this.shareCfg = !!cfg.values["lan.share_internet"];
      if (this.form && this.form.share == null) this.form.share = this.shareCfg;
      this.render();
    } catch { /* switches stay disabled */ }
  },
  // the chat of this box's own party (cloud parties only), shown next to the party card
  partyChat() {
    if (this.screen.name !== "main") return;
    const p = partyState();
    const room = p && isHubParty(p) && (p.role === "host" || (p.role === "member" && p.status === "approved"))
      ? (p.public && p.listing ? "l:" + p.listing : p.hub_party ? "p:" + p.hub_party : null) : null;
    this.chatFor(room && !(p.role === "host" && p.public && !this.chatOn(p)) ? room : null);
  },
  chatFor(room) {
    if ((this.chat && this.chat.room) === room) return;
    clearTimeout(this.timers.chat);
    this.chat = room ? { room, msgs: [], last: 0, meta: null, err: null, draft: "" } : null;
    if (room) this.pollChat();
  },
  async pollChat() {
    const c = this.chat;
    if (!c) return;
    try {
      const d = await api(`/api/parties/chat?room=${encodeURIComponent(c.room)}&after=${c.last}`);
      if (this.chat !== c) return;
      const known = new Set(c.msgs.map((m) => m.id)), gone = new Set(d.deleted || []);
      c.msgs = c.msgs.concat(d.messages.filter((m) => !known.has(m.id))).filter((m) => !gone.has(m.id)).slice(-500);
      c.last = Math.max(c.last, d.last || 0);
      c.meta = d; c.err = null;
    } catch (e) { if (this.chat === c) c.err = e.message; }
    if (current !== this || this.chat !== c) return;
    const log = $("#chatlog", view), atEnd = !log || log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    this.render();
    const log2 = $("#chatlog", view);
    if (log2 && atEnd) log2.scrollTop = log2.scrollHeight;
    this.later("chat", 3000, () => this.pollChat());
  },
  // switches on the host card: internet sharing (any party) and the chat (public parties), changeable any time
  shareOn() {
    const o = this.shareOpt;
    const actual = !!(S.status && S.status.lan && S.status.lan.share_internet);
    if (o && (o.saving || (o.value !== actual && Date.now() < o.until))) return o.value;
    return actual;
  },
  myListing(p) { return p && p.public && p.listing && this.dir ? this.dir.live.find((l) => l.id === p.listing) : null; },
  chatOn(p) {
    const o = this.chatOpt;
    if (o && o.id === p.listing && (o.saving || Date.now() < o.until)) return o.value;
    const l = this.myListing(p);
    return l ? l.chat !== false : true;
  },
  hostExtras(p) {
    const share = this.shareOn(), so = this.shareOpt && this.shareOpt.saving;
    const sw = (id, change, on, busy) => html`<span class="switch"><input type="checkbox" id="${id}" data-change="${change}" ${on ? raw("checked") : ""} ${busy ? raw("disabled") : ""}><span class="switch-track"></span></span>`;
    return html`<label class="party-auto ${share ? "is-warn" : ""}" for="party-sw-share" data-key="sw-share">
        <span class="iso-ic">${icon("globe")}</span>
        <span class="lt-text"><b>Share my internet with the party</b><small>${share ? (p.public ? "On: everyone in this public party uses your connection, and what they do online comes from your home IP." : "On: PCs in the party reach the internet through this box, using your IP address.") : "Off: a LAN only. PCs in the party can't reach the internet."}</small></span>
        ${sw("party-sw-share", "party-share", share, so)}</label>
      ${p.public ? html`<label class="party-auto" for="party-sw-chat" data-key="sw-chat">
        <span class="iso-ic">${icon("users")}</span>
        <span class="lt-text"><b>Party chat</b><small>${this.chatOn(p) ? "On: boxes in the party can chat here." : "Off: nobody can read or post. Messages are kept for when you turn it back on."}</small></span>
        ${sw("party-sw-chat", "party-chat", this.chatOn(p), this.chatOpt && this.chatOpt.saving)}</label>` : ""}`;
  },
  // only the party's own boxes see its chat: the host, members who were let in, or (planned) boxes that RSVP'd
  inParty(l) { return !!(l && (l.host_view || l.mine || (l.state === "live" ? l.joined : l.going))); },
  canPublic() { const me = this.pub.me; return !!(me && me.available && me.nick && !me.ban); },

  // ---- nickname (first visit: registration card; without one the public part is greyed out)
  async checkNick(value) {
    const p = this.pub, seq = ++p.seq, nick = value.trim();
    if (!nick) { p.check = null; this.render(); return; }
    if (!/^[A-Za-z0-9_-]{3,16}$/.test(nick)) { p.check = { free: false, reason: "3–16 characters: letters, digits, - and _" }; this.render(); return; }
    p.check = { pending: true };
    this.render();
    try {
      const r = await api("/api/parties/nick/check", { method: "POST", body: { nick } });
      if (seq === p.seq) { p.check = r; this.render(); }
    } catch (e) { if (seq === p.seq) { p.check = { free: false, reason: e.message }; this.render(); } }
  },
  nickArea() {
    const p = this.pub, me = p.me;
    if (p.err) return html`<div class="blocked-note" data-key="nick-err">${icon("alert")}<span>Public parties: ${p.err}</span><button type="button" class="btn-link" data-action="nick-retry">${icon("reset", "i-sm")}Retry</button></div>`;
    if (!me) return "";
    if (!me.available) return html`<div class="blocked-note" data-key="nick-na">${icon("info")}<span>${me.reason}</span></div>`;
    const until = (t) => new Date(t * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
    if (me.ban) {
      return html`<section class="card nick-card is-banned" data-key="nick-ban"><div class="nick-ban">${icon("lock")}<span><b>This box is banned from public parties</b>
        <small>Reason from the hub operator: “${me.ban.reason}” · ${me.ban.until ? `until ${until(me.ban.until)}` : "permanent"}${me.nick ? html` · nickname <b class="mono">${me.nick}</b> is kept` : ""}. Private parties work as normal.</small></span></div></section>`;
    }
    if (me.nick && !p.editing) {
      return html`<div class="nick-strip" data-key="nick-strip">${icon("users")}<span>Your nickname: <b class="mono">${me.nick}</b></span>
        ${me.can_change_at ? html`<span class="faint">· can be changed again on ${until(me.can_change_at)}</span>` : html`<button type="button" class="btn-link" data-action="nick-edit">Change</button>`}</div>`;
    }
    if (!me.nick && p.skipped && !p.editing) {
      return html`<div class="blocked-note" data-key="nick-skip">${icon("lock")}<span>Public parties need a nickname: browsing works, but joining, chat and hosting a public party are off.</span>
        <button type="button" class="btn-link" data-action="nick-open">Choose a nickname</button></div>`;
    }
    const c = p.check, ok = c && c.free;
    const msg = !c ? "3–16 characters: letters, digits, - and _. Lookalikes count as taken (0/o, 1/l)." : c.pending ? "Checking…" : c.free ? `“${p.draft.trim()}” is free` : c.reason;
    return html`<section class="card nick-card" data-key="nick-card">
      <div class="po-head"><span class="lt-icon">${icon("users")}</span><span class="po-t"><b>${me.nick ? "Change your nickname" : "Choose a nickname for public parties"}</b>
        <small>${me.nick ? "You can change it once every 30 days. The old one stays reserved for you for 30 days." : "It's how others see you in the directory, join requests and chat. First come, first served, and it belongs to this box."}</small></span></div>
      <form class="nick-form" data-submit="nick-save" novalidate autocomplete="off">
        <input class="input mono" id="nick-in" data-input="nick-input" maxlength="16" placeholder="e.g. lars" value="${p.draft}" spellcheck="false" autocapitalize="none" aria-describedby="nick-msg" aria-invalid="${String(!!c && !c.pending && !c.free)}">
        <button class="btn btn-primary" type="submit" ${ok && !p.saving ? "" : raw("disabled")}>${p.saving ? spinner() : icon("check")}${me.nick ? "Save" : "Register"}</button>
        <button type="button" class="btn btn-ghost" data-action="${me.nick ? "nick-cancel" : "nick-skip"}">${me.nick ? "Cancel" : "Not now"}</button>
      </form>
      <p class="nick-msg ${c && !c.pending ? (c.free ? "ok" : "bad") : ""}" id="nick-msg" role="status">${msg}</p>
      ${me.nick ? "" : html`<p class="nick-help faint">Private parties never need a nickname.</p>`}</section>`;
  },

  // ---- main screen
  yourParty(lan, p) {
    if (!p) return html`<section class="card party-card" data-key="party-loading"><div class="party-pad"><div class="skel skel-line" style="width:60%"></div><div class="skel" style="height:90px"></div></div></section>`;
    if (p.role !== this.lastRole) { this.lastRole = p.role; this.createErr = this.joinErr = ""; }
    if (p.role === "host" || p.role === "member") {
      const card = p.role === "host" ? Lan.partyHost.call(this, lan, p) : Lan.partyMember.call(this, p);
      const pubStrip = p.public ? html`<a class="card public-strip" href="#/parties/l/${p.listing}" data-key="pub-strip">${icon("globe")}<span><b>${p.title || p.party_name || "Public party"}</b>
        <small>${p.role === "host" ? html`Listed in the public directory${p.event_code ? html` · event ID <b class="mono">${p.event_code.slice(0, 4)}-${p.event_code.slice(4)}</b>` : ""}${S.status && S.status.lan && S.status.lan.share_internet ? " · you share your internet with everyone in it" : ""}` : "A public party: open its page for votes and who's in"}</small></span>${icon("chevron", "chev")}</a>` : "";
      const chatOff = p.role === "host" && p.public && !this.chatOn(p);
      const side = this.chat ? this.chatPanel(this.chat.room.startsWith("p:") ? "Only boxes in this party" : "Only boxes in this party")
        : chatOff ? html`<aside class="card chat-closed" data-key="chat-off">${icon("lock")}<b>Party chat is off</b><span class="faint">Turn it on with the switch on your party card.</span></aside>` : "";
      return html`${pubStrip}<div class="${side ? "detail" : ""}" data-key="party-row-${side ? "chat" : "solo"}">${card}${side}</div>`;
    }
    if (p.role === "standalone") return this.startCards(lan);
    return html`<section class="card party-card" data-key="party-unknown">${emptyState("alert", "LAN service not running", html`${p.error || "The LAN service is not running"}. Start it on the Pi with <code>sudo systemctl start netkey-lan</code>.`)}</section>`;
  },
  startCards(lan) {
    const dis = this.busy ? raw("disabled") : "";
    const portNote = lan.state === "disabled" ? html`Turn on the <a href="#/lan">LAN party port</a> first: a party links this box's retro-LAN port.`
      : lan.state === "no_nic" ? "Set up a retro-LAN port first (see the LAN page): a party links it." : "";
    return html`${portNote ? html`<div class="banner banner-warn lan-banner" role="status" data-key="port-note">${icon("info")}<span>${portNote}</span></div>` : ""}
    <div class="start-grid" data-key="start">
      <section class="card start-card">
        <div class="po-head"><span class="lt-icon">${icon("plus")}</span><span class="po-t"><b>Host a party</b><small>A quick private party with an invite code, or a public one in the directory, now or planned.</small></span></div>
        <a class="btn btn-primary" href="#/parties/host" style="justify-self:start">${icon("plus")}Host a party</a>
      </section>
      <form class="card start-card" data-submit="party-join" novalidate>
        <div class="po-head"><span class="lt-icon blue">${icon("key")}</span><span class="po-t"><b>Join with a code</b><small>An invite code from a friend (KEY-…), or an event ID someone posted (e.g. 7KQ2-9X4M).</small></span></div>
        <div class="join-in"><textarea class="input textarea mono party-code-in" name="code" id="party-code" rows="1" placeholder="KEY-…  or  7KQ2-9X4M" autocomplete="off" autocapitalize="off" spellcheck="false"
          data-input="party-code-input" data-err="joinErr" aria-invalid="${this.joinErr ? "true" : "false"}"></textarea>
          <button type="submit" class="btn ${this.busy === "join" ? "is-busy" : ""}" ${dis}>${icon("link")}Join</button></div>
        ${this.joinErr ? html`<span class="field-error" role="alert">${this.joinErr}</span>` : ""}
      </form>
    </div>`;
  },
  myEvents() {
    const m = this.mine;
    if (!m || (!m.hosted.length && !m.going.length)) return "";
    const row = (l, role) => html`<li data-key="ev-${l.id}"><a href="#/parties/l/${l.id}">${l.starts ? dateBlock(l.starts) : html`<span class="lp-ic">${icon("network")}</span>`}
      <span class="pp-t"><b>${l.title}</b><small>${role === "host" ? "You host" : html`Hosted by <b>${l.host}</b>`} · ${l.state === "live" ? "live now" : fmtStart(l.starts)} · ${l.count} ${l.state === "live" ? "in" : "going"}${l.repeat !== "none" ? ` · ${REPEAT_NAME[l.repeat]}` : ""}</small></span>
      ${l.state === "live" ? html`<span class="pill pill-green pill-glow">Live</span>` : html`<span class="pill pill-blue">${role === "host" ? "Planned" : "Going"}</span>`}${icon("chevron", "chev")}</a></li>`;
    return html`<section class="sec" data-key="mine"><div class="sec-h"><h2>${icon("clock")}Your events</h2><span class="sub">Ones you host and ones you've RSVP'd to</span></div>
      <ul class="mine-list">${m.hosted.map((l) => row(l, "host"))}${m.going.map((l) => row(l, "going"))}</ul></section>`;
  },
  lpCard(l) {
    const full = l.count >= l.max;
    const status = l.state === "live" ? (l.joined ? html`<span class="pill pill-green">${icon("check", "i-sm")} You're in</span>` : html`<span class="pill pill-green pill-glow">Live</span>`)
      : l.going ? html`<span class="pill pill-blue">${icon("check", "i-sm")} You're going</span>`
      : html`<span class="pill pill-blue no-dot">${icon("clock", "i-sm")} ${fmtTime(l.starts)} · ${fmtRel(l.starts)}</span>`;
    return html`<a class="card lp ${full ? "is-full" : ""}" href="#/parties/l/${l.id}" data-key="lp-${l.id}">
      ${l.state === "live" ? html`<span class="lp-ic">${icon("network")}</span>` : dateBlock(l.starts)}
      <span class="lp-main">
        <span class="lp-top"><b class="lp-title">${l.title}</b>${status}${l.repeat !== "none" ? html`<span class="badge">${REPEAT_NAME[l.repeat]}</span>` : ""}${l.mine ? html`<span class="badge badge-vmm">YOURS</span>` : ""}</span>
        <span class="games">${l.games.map(gameChip)}</span>
        <span class="lp-meta"><span>${icon("users")}${l.host}</span>
          <span>${icon("clock")}${l.state === "live" ? `started ${fmtRel(l.started)}` : `${fmtStart(l.starts)} your time`}</span>
          ${l.relay ? html`<span>${icon("cloud")}${l.relay}</span>` : ""}
          <span>${l.auto ? html`${icon("check")}Auto-approve` : html`${icon("shield")}Host approves`}</span>
          ${l.share ? html`<span>${icon("globe")}Internet shared</span>` : ""}</span>
      </span>
      <span class="lp-side">${pingPill(l)}<span class="lp-slots ${full ? "is-full" : ""}"><b class="tnum">${l.count}</b>/${l.max} ${l.state === "live" ? "boxes" : "going"}<span class="meter"><i style="width:${Math.min(100, (l.count / l.max) * 100)}%"></i></span></span></span>
    </a>`;
  },
  directory() {
    if (this.dirErr && !this.dir) return html`<div class="card" data-key="dir-err">${emptyState("alert", "Public parties not available", this.dirErr, html`<button type="button" class="btn" data-action="dir-retry">${icon("reset")}Retry</button>`)}</div>`;
    const p = partyState() || {};
    const d = this.dir;
    const q = this.q.trim().toLowerCase();
    const match = (l) => (!this.near || (l.est_ms != null && l.est_ms <= 50)) && (!q || (l.title + " " + l.host + " " + l.games.map((g) => g.name).join(" ")).toLowerCase().includes(q));
    const live = d ? d.live.filter(match) : [], up = d ? d.upcoming.filter(match) : [];
    const skel = html`<div class="card card-pad"><div class="skel skel-line" style="width:50%"></div><div class="skel skel-line" style="width:80%"></div></div>`;
    const none = (h, t) => html`<div class="card">${emptyState("globe", h, t)}</div>`;
    return html`<section class="sec" data-key="dir">
      <div class="sec-h"><h2>${icon("globe")}Public parties</h2><span class="sub">Only boxes running NetKey can see these</span></div>
      ${p.role === "host" || p.role === "member" ? html`<div class="blocked-note">${icon("info")}<span>A box is in one party at a time. ${p.role === "host" ? "Close yours" : "Leave yours"} to join another; you can still browse and RSVP.</span></div>` : ""}
      <div class="filters">
        ${segmented([{ value: "all", label: "All" }, { value: "live", label: "Live now" }, { value: "upcoming", label: "Upcoming" }], this.tab, 'data-action="dir-tab"', "", "Show")}
        <label class="search">${icon("search")}<input class="input" id="dir-q" data-input="dir-q" placeholder="Search games, parties or hosts" value="${this.q}" autocomplete="off"></label>
        <label class="switch near-sw"><input type="checkbox" data-change="dir-near" ${this.near ? raw("checked") : ""}><span class="switch-track"></span>Only ≈50 ms or better</label>
      </div>
    </section>
    ${this.tab !== "upcoming" ? html`<section class="sec" data-key="dir-live"><div class="sec-h"><h3>${icon("network")}Live now</h3><span class="sub">${d ? `${live.length} part${live.length === 1 ? "y" : "ies"} · ping estimated from your box` : ""}</span></div>
      <div class="lp-list">${!d ? skel : live.length ? live.map((l) => this.lpCard(l)) : none(q || this.near ? "No live parties match" : "No public parties right now", q || this.near ? "Try another search, or turn off the ping filter." : "Host one, or check the upcoming events.")}</div></section>` : ""}
    ${this.tab !== "live" ? html`<section class="sec" data-key="dir-up"><div class="sec-h"><h3>${icon("clock")}Upcoming</h3><span class="sub">Times in your time zone</span></div>
      <div class="lp-list">${!d ? skel : up.length ? up.map((l) => this.lpCard(l)) : none("No upcoming events", "Plan one yourself: it shows up here for everyone.")}</div></section>` : ""}`;
  },

  // ---- chat panel (public party rooms and private cloud parties)
  chatPanel(sub) {
    const c = this.chat, meta = c.meta || {};
    const pub = c.room.startsWith("l:");
    let prev = "";
    const rows = c.msgs.map((m) => {
      const cont = prev === m.nick && !m.sys; prev = m.nick;
      return html`<div class="msg ${cont ? "cont" : ""}" data-key="m-${m.id}">${avatar(m.nick)}
        <div class="msg-h"><b>${m.nick}</b>${m.host ? html`<span class="badge badge-vmm">HOST</span>` : ""}${m.me ? html`<span class="badge">YOU</span>` : ""}${m.muted ? html`<span class="badge badge-pmm" title="You muted this box in this chat">MUTED</span>` : ""}<time title="${fmtAbs(m.at)}">${fmtTime(m.at)}</time></div>
        <p>${m.text}</p>
        ${!m.me && (meta.host || pub) ? html`<span class="msg-tools">${meta.host ? html`<button type="button" title="Delete message" data-action="chat-del" data-id="${m.id}">${icon("trash")}</button><button type="button" title="${m.muted ? "Unmute this box" : "Mute this box in this chat"}" data-action="chat-mute" data-id="${m.id}" data-on="${m.muted ? "0" : "1"}">${icon(m.muted ? "check" : "lock")}</button>` : ""}${pub ? html`<button type="button" title="Report to the hub operator" data-action="chat-report" data-id="${m.id}">${icon("alert")}</button>` : ""}</span>` : ""}
      </div>`;
    });
    return html`<aside class="card chat" aria-label="Party chat" data-key="chat-${c.room}">
      <div class="chat-head">${icon("users")}<h3>Party chat</h3><span class="sub">${sub}</span></div>
      <div class="chat-log" id="chatlog">${c.err ? html`<div class="chat-empty faint">${c.err}</div>` : !c.meta ? html`<div class="chat-empty">${spinner()}</div>` : rows.length ? rows : html`<div class="chat-empty faint">No messages yet. Say hello!</div>`}</div>
      <div class="chat-foot">${c.err || meta.can_post === false ? html`<div class="chat-locked">${icon("lock", "i-sm")} ${c.err || meta.why}</div>` : html`
        <form class="chat-in" data-submit="chat-send" autocomplete="off"><input class="input" id="chat-in" maxlength="300" placeholder="Say hi, agree on games…" value="${c.draft}" data-input="chat-draft"><button class="btn btn-primary btn-icon" aria-label="Send" ${c.sending ? raw("disabled") : ""}>${icon("up")}</button></form>
        <div class="chat-rules"><span>${this.pub.me && this.pub.me.nick ? html`As <b class="mono">${this.pub.me.nick}</b> · ` : ""}300 characters · plain text</span><span>Deleted 24 h after the party ends</span></div>`}</div>
    </aside>`;
  },

  // ---- party page (live listing or planned event; the host gets its controls here)
  listingScreen() {
    const back = html`<a class="back" href="#/parties">${icon("chevron")}Parties</a>`;
    if (this.lstErr) return html`<div class="stack">${back}<div class="card">${emptyState("alert", "Party not found", this.lstErr)}</div></div>`;
    const l = this.lst;
    if (!l) return html`<div class="stack">${back}<div class="card card-pad"><div class="skel skel-line" style="width:40%"></div><div class="skel" style="height:140px;margin-top:12px"></div></div></div>`;
    const live = l.state === "live";
    const facts = html`<div class="facts">
      <div><small>Ping to host (est.)</small><b>${pingPill(l)}</b><span class="faint">${PING(l.est_ms)[1]}</span></div>
      <div><small>${live ? "Boxes" : "Going"}</small><b class="tnum">${l.count} / ${l.max}</b><span class="faint">${Math.max(0, l.max - l.count - (live ? 0 : 1))} places left</span></div>
      <div><small>Relay</small><b class="mono">${l.relay || "—"}</b><span class="faint">End-to-end encrypted</span></div>
      <div><small>Internet for PCs</small>${l.share ? html`<b>${icon("globe", "i-sm")} Shared</b><span class="faint">Through the host's connection</span>` : html`<b>${icon("lock", "i-sm")} Off</b><span class="faint">LAN only</span>`}</div></div>`;
    const votes = html`<div class="votes">${l.games.map((g) => {
      const n = l.votes[g.key] || 0, mine = l.my_votes.includes(g.key), top = Math.max(1, ...Object.values(l.votes));
      return html`<div class="vote" data-key="v-${g.key}"><div class="vote-t"><b>${g.name}${g.key === "other" ? "" : html`<span class="game ${g.custom ? "custom" : ""}">${gameTag(g)}</span>`}</b><small>${g.hint || ""}</small></div>
        <button type="button" class="vote-btn" data-action="vote" data-game="${g.key}" aria-pressed="${String(mine)}" ${this.canPublic() ? "" : raw("disabled")} title="${mine ? "Take back your +1" : "+1 this game"}">${icon("check", "i-sm")}${n}</button>
        <div class="bar"><i style="width:${(n / top) * 100}%"></i></div></div>`;
    })}</div>`;
    const people = l.people.length ? html`<div class="who">${l.people.map((x) => html`<span>${avatar(x.nick)}${x.nick}${x.host ? html`<em class="faint">host</em>` : ""}<i class="dot ${x.online ? "dot-live" : ""}"></i></span>`)}</div>` : html`<p class="faint" style="font-size:12.5px">Nobody yet.</p>`;
    return html`<div class="stack">${back}
      <div class="detail">
        <section class="card ${live ? "lan-hero is-up" : "party-card is-planned"}" data-key="lst-${l.id}">
          <div class="d-head">${live ? html`<span class="lt-icon d-ic">${icon("network")}</span>` : dateBlock(l.starts)}
            <div class="d-title"><h2>${l.title}</h2><span class="sub">Hosted by <b>${l.host}</b> · ${live ? `started ${fmtRel(l.started)}` : html`${fmtStart(l.starts)} your time · ${fmtRel(l.starts)}${l.repeat !== "none" ? ` · ${REPEAT_NAME[l.repeat]}` : ""}`}</span></div>
            ${live ? html`<span class="pill pill-green pill-glow">Live</span>` : html`<span class="pill pill-blue">Planned</span>`}
            <button type="button" class="btn btn-ghost btn-sm" data-action="copy-event" title="Copy the event ID to post on a forum or Discord">${icon("copy")}<span class="mono">${l.code}</span></button>
          </div>
          <div class="d-body">
            ${l.description ? html`<p class="d-desc">${l.description}</p>` : ""}
            ${facts}
            ${l.host_view && !live ? this.hostControls(l) : this.joinBox(l)}
            <div class="pm-head"><h4>What we'll play</h4><span class="sub">+1 your favourites · setup hints from the game list</span></div>
            ${votes}
            <div class="pm-head"><h4>${live ? "In the party" : "Going"}</h4><span class="sub">${l.people.length} box${l.people.length === 1 ? "" : "es"}</span></div>
            ${people}
            ${!l.host_view && this.canPublic() ? html`<p class="report-line"><button type="button" class="btn-link" data-action="report-listing">${icon("alert", "i-sm")}Report this party</button></p>` : ""}
          </div>
        </section>
        ${this.chat ? this.chatPanel(live ? `${l.people.filter((x) => x.online).length} online` : "Plan the night here")
          : html`<aside class="card chat-closed" data-key="chat-closed">${icon("lock")}<b>Party chat</b><span class="faint">${l.chat === false ? "The host turned the chat off." : live ? "Only boxes in the party see its chat. Join to say hello." : "Only the host and boxes that RSVP'd see the event's chat."}</span></aside>`}
      </div></div>`;
  },
  joinBox(l) {
    const p = partyState() || {};
    const box = (cls, inner) => html`<div class="join-box ${cls}" data-key="jb-${cls || "x"}"><div class="join-row">${inner}</div></div>`;
    if (!this.canPublic()) {
      const me = this.pub.me || {};
      return box("", html`<span class="ph-t"><b>${me.ban ? "This box is banned from public parties" : "Choose a nickname to join"}</b><small>${me.ban ? "Private parties still work." : "Public parties show who's asking by nickname. It takes ten seconds."}</small></span>
        ${me.ban ? "" : html`<a class="btn btn-primary" href="#/parties" data-action="nick-open">${icon("users")}Choose a nickname</a>`}`);
    }
    if (l.state === "live") {
      const mine = p.listing === l.id && (p.role === "member" || p.role === "host");
      if (mine && p.role === "host") return box("is-in", html`<span class="ph-t"><b>This is your party</b><small>Join requests, members and the invite code are on the Parties page.</small></span><a class="btn" href="#/parties">${icon("network")}Manage</a>`);
      if (mine && p.status === "approved") return box("is-in", html`<span class="lt-icon ok-ic">${icon("check")}</span><span class="ph-t"><b>You're in the party</b><small>Your retro PCs share a LAN with everyone here: IPX, NetBEUI and TCP/IP.</small></span><button type="button" class="btn btn-sm btn-danger-text" data-action="party-leave">Leave party</button>`);
      if (mine) return box("is-wait", html`${spinner()}<span class="ph-t"><b>Waiting for ${l.host} to approve…</b><small>${p.verify ? html`Key check <b class="mono">${p.verify}</b>: the host sees the same code. ` : ""}Say hi in the chat so they know who you are.</small></span><button type="button" class="btn btn-sm" data-action="party-leave">Cancel request</button>`);
      if (p.role === "host" || p.role === "member") return box("", html`<span class="ph-t"><b>You're already in a party</b><small>A box is in one party at a time. ${p.role === "host" ? "Close yours" : "Leave yours"} first.</small></span><a class="btn" href="#/parties">${icon("network")}Your party</a>`);
      if (l.count >= l.max) return box("", html`<span class="ph-t"><b>This party is full</b><small>${l.max} boxes is the limit the host set.</small></span>`);
      return box("", html`<span class="ph-t"><b>${l.auto ? "Join straight away" : `${l.host} approves each box`}</b><small>Your retro-LAN port is bridged into this party: your PCs get addresses from the host. You can leave at any time.</small></span>
        <button type="button" class="btn btn-primary ${this.busy === "join" ? "is-busy" : ""}" data-action="join-listing" ${this.busy ? raw("disabled") : ""}>${icon("link")}${l.auto ? "Join party" : "Request to join"}</button>`);
    }
    const watched = !!(p.watches && p.watches[l.id]);
    if (l.going) {
      return html`<div class="join-box is-in" data-key="jb-going"><div class="join-row"><span class="lt-icon ok-ic blue">${icon("check")}</span>
        <span class="ph-t"><b>You're going</b><small>${l.host} sees your RSVP and can approve you ahead of time. When the party starts, the hub tells your box.</small></span>
        <a class="btn btn-sm" href="/api/parties/ics/${l.id}" download>${icon("clock")}Add to calendar</a>
        <button type="button" class="btn btn-sm" data-action="rsvp" data-going="0">Not going</button></div>
        <label class="party-auto"><span class="iso-ic">${icon("link")}</span><span class="lt-text"><b>Join automatically when it starts</b><small>Only if this box is on and not in another party.</small></span>
          <span class="switch"><input type="checkbox" data-change="autojoin" ${watched ? raw("checked") : ""}><span class="switch-track"></span></span></label></div>`;
    }
    if (l.count >= l.max - 1) return box("", html`<span class="ph-t"><b>This event is full</b><small>${l.max} boxes including the host.</small></span>`);
    return box("", html`<span class="ph-t"><b>${fmtStart(l.starts)}</b><small>RSVP to tell ${l.host} you're coming. It counts as an early join request, so you can be approved before the night.</small></span>
      <a class="btn" href="/api/parties/ics/${l.id}" download>${icon("clock")}Add to calendar</a>
      <button type="button" class="btn btn-primary" data-action="rsvp" data-going="1">${icon("check")}RSVP</button>`);
  },
  hostControls(l) {
    const now = nowS(), canStart = now >= l.start_from && now <= l.start_until;
    const left = Math.max(0, l.starts - now), d = Math.floor(left / 86400), h = Math.floor((left % 86400) / 3600), mi = Math.floor((left % 3600) / 60);
    const p = partyState() || {};
    return html`<div class="host-ctl" data-key="host-ctl">
      <div class="countdown"><div class="cd-n">${left > 0 ? html`${d}<small>d</small>${h}<small>h</small>${pad2(mi)}<small>m</small>` : "Now"}</div>
        <div class="cd-t"><b>${fmtStart(l.starts)}</b><span>${canStart ? "You can start the party now." : now < l.start_from ? `Start unlocks at ${fmtTime(l.start_from)}.` : "The start window has passed."} If you don't start by ${fmtTime(l.start_until)}, the event ${l.repeat !== "none" ? "moves to its next date" : "expires"}.</span></div>
        <button type="button" class="btn btn-primary ${this.busy === "start" ? "is-busy" : ""}" data-action="start-event" ${canStart && !this.busy && p.role === "standalone" ? "" : raw("disabled")} title="${p.role !== "standalone" ? "Leave or close your current party first" : ""}">${icon("network")}Start party</button></div>
      <div class="party-pending rsvps"><div class="pm-head"><h4>RSVPs</h4><span class="sub">Pre-approved boxes get in without asking when you start</span></div>
        ${l.people.length ? html`<ul class="rsvp-list">${l.people.map((x) => html`<li data-key="r-${x.id}">${avatar(x.nick)}<span class="pp-t"><b>${x.nick}</b><small>RSVP'd ${fmtRel(x.since)}${x.est_ms != null ? ` · ≈${x.est_ms} ms to you${x.est_ms > 90 ? " · may lag in lockstep games" : ""}` : ""}</small></span>
          ${x.pre_approved ? html`<span class="pill pill-green">Pre-approved</span><button type="button" class="btn btn-sm btn-ghost" data-action="preapprove" data-id="${x.id}" data-on="0">Undo</button>`
            : html`<button type="button" class="btn btn-sm" data-action="decline" data-id="${x.id}" data-nick="${x.nick}">Decline</button><button type="button" class="btn btn-sm btn-primary" data-action="preapprove" data-id="${x.id}" data-on="1">${icon("check")}Pre-approve</button>`}</li>`)}</ul>`
          : html`<p class="faint" style="font-size:12.5px">No RSVPs yet. Share the event ID ${l.code} on a forum or Discord: people type it under Join with a code.</p>`}</div>
      <div class="row wrap" style="gap:8px;justify-content:flex-end"><button type="button" class="btn btn-sm btn-danger-text" data-action="cancel-event">${icon("x")}Cancel event</button></div>
    </div>`;
  },

  // ---- host form
  newForm() {
    const t = new Date(Date.now() + 86400000);
    return { vis: "hub", endpoint: "", share: this.shareCfg ?? false, title: "", desc: "", games: [], chat: true, when: "now", date: `${t.getFullYear()}-${pad2(t.getMonth() + 1)}-${pad2(t.getDate())}`, time: "20:00", repeat: "none", max: 8, auto: false, gq: "", genre: "All", other: null, err: "" };
  },
  picker() {
    const f = this.form, gl = this.games;
    if (!gl) return html`<div class="picker">${this.gamesErr ? html`<span class="field-error">${this.gamesErr}</span>` : html`<div class="skel skel-line" style="width:60%"></div>`}</div>`;
    const q = f.gq.trim().toLowerCase();
    const list = gl.games.filter((g) => (f.genre === "All" || g.genre === f.genre) && (!q || g.name.toLowerCase().includes(q)));
    const name = (x) => (typeof x === "string" ? (gl.games.find((g) => g.key === x) || { name: x }).name : x.name);
    const proto = (x) => (typeof x === "string" ? (gl.games.find((g) => g.key === x) || {}).proto : x.proto);
    const has = (k) => f.games.some((x) => x === k);
    return html`<div class="field"><span class="field-label">Games <span class="faint" style="font-weight:400">· optional, up to 5</span></span>
      <div class="picker">
        <div class="picked">${f.games.length ? f.games.map((x, i) => html`<span class="game ${typeof x === "string" ? "" : "custom"}">${name(x)}<em>${typeof x === "string" ? "" : "OTHER · "}${proto(x)}</em><button type="button" data-action="game-remove" data-i="${i}" aria-label="Remove ${name(x)}">${icon("x")}</button></span>`) : html`<span class="faint" style="font-size:12.5px">None picked: the party shows “Others”, and you agree on games in the chat.</span>`}</div>
        <div class="picker-top"><label class="search">${icon("search")}<input class="input" id="game-q" data-input="game-q" placeholder="Search ${gl.games.length} games" value="${f.gq}" autocomplete="off"></label>
          ${segmented(["All"].concat(gl.genres).map((g) => ({ value: g, label: g })), f.genre, 'data-action="game-genre"', "seg-sm", "Genre")}</div>
        <div class="pick">${list.map((g) => html`<button type="button" data-action="game-pick" data-key="${g.key}" aria-pressed="${String(has(g.key))}" title="${g.hint}">${g.name}<em>${g.proto}</em></button>`)}
          <button type="button" class="other" data-action="game-other" aria-pressed="${String(!!f.other)}">${icon("plus", "i-sm")}Other…</button></div>
        ${!list.length && q ? html`<span class="faint" style="font-size:12.5px">No game called “${f.gq}” in the list. Add it with <b>Other…</b></span>` : ""}
        ${f.other ? html`<div class="other-row"><input class="input" id="other-name" data-input="other-name" maxlength="40" placeholder="Game name, e.g. TetriNET" value="${f.other.name}" autocomplete="off">
          <select class="select" data-change="other-proto" data-value="${f.other.proto}">${gl.protos.map((p) => html`<option ${p === f.other.proto ? raw("selected") : ""}>${p}</option>`)}</select>
          <button type="button" class="btn" data-action="game-add-other">${icon("plus")}Add game</button></div>
          <span class="field-help">Games you type in show as OTHER and have no setup hints.</span>` : ""}
      </div><span class="field-help">Members +1 the games, and each listed game shows its setup hints.</span></div>`;
  },
  hostScreen(lan) {
    const f = this.form, pub = f.vis === "public", direct = f.vis === "direct", canPub = this.canPublic();
    const p = partyState() || {};
    const opt = (v, ic, title, text, off) => html`<label class="pv-opt ${off ? "is-off" : ""}" data-key="vis-${v}"><input type="radio" name="vis" value="${v}" data-change="form-vis" ${f.vis === v ? raw("checked") : ""} ${off ? raw("disabled") : ""}><span class="pv-radio" aria-hidden="true"></span>
      <span class="pv-t"><b>${icon(ic, "i-sm")}${title}</b><small>${text}</small></span></label>`;
    const busy = this.busy === "create" || this.busy === "plan";
    const label = pub ? (f.when === "now" ? "Start public party" : "Publish event") : "Start party";
    const portNote = lan.state === "disabled" ? "The LAN party port is off (LAN page)." : lan.state === "no_nic" ? "No retro-LAN port yet (see the LAN page)." : "";
    return html`<div class="stack"><a class="back" href="#/parties">${icon("chevron")}Parties</a>
      ${p.role === "host" || p.role === "member" ? html`<div class="banner banner-warn lan-banner">${icon("info")}<span>This box is already in a party. ${p.role === "host" ? "Close it" : "Leave it"} before hosting another (you can still plan an event).</span></div>` : ""}
      ${portNote ? html`<div class="banner banner-warn lan-banner">${icon("info")}<span>${portNote} A party links this box's retro-LAN port.</span></div>` : ""}
      <form class="card form-card" data-submit="host-submit" novalidate autocomplete="off">
        <div class="pm-head"><h3>Host a LAN party</h3></div>
        <fieldset class="party-via"><span class="field-label">Who can find it and how boxes connect</span>
          <div class="three-opts">
            ${opt("hub", "cloud", "Private, via the cloud", "Quick party with an invite code. No router setup.")}
            ${opt("direct", "swap", "Private, direct", html`Quick party with an invite code. You forward UDP <span class="mono">${PARTY_PORT}</span> to this box.`)}
            ${opt("public", "globe", "Public", canPub ? "Listed in the directory. Any NetKey box can ask to join." : this.pub.me && this.pub.me.ban ? "This box is banned from public parties." : "Needs a nickname (top of the Parties page).", !canPub)}
          </div>
          <span class="field-help">${direct ? "Ad-hoc: start it and share the code. Direct is only for private parties: a public listing would show your IP to strangers."
            : pub ? "Goes in the directory, so it needs a name, games and a time. Always through the relay, so your IP is never shown."
            : "Ad-hoc: start it and share the code. Works behind any router."}</span></fieldset>
        ${direct ? html`<label class="field"><span class="field-label">Public address members use <span class="faint">(optional)</span></span>
          <input class="input" data-input="form-text" data-k="endpoint" value="${f.endpoint}" placeholder="your.dyndns.example or public IP" maxlength="253" autocapitalize="off" spellcheck="false">
          <span class="field-help">Leave empty to use this network's public IP.</span></label>` : ""}
        ${pub ? html`
          <label class="field"><span class="field-label">Name <span class="faint">(optional)</span></span><input class="input" data-input="form-text" data-k="title" value="${f.title}" maxlength="40" placeholder="${this.defaultTitle()}">
            <span class="field-help">Left empty, the party is called “${this.defaultTitle()}”.</span></label>
          <label class="field"><span class="field-label">Description</span><textarea class="input textarea" data-input="form-text" data-k="desc" maxlength="400" rows="3" placeholder="What to expect, what hardware, house rules">${f.desc}</textarea></label>
          ${this.picker()}
          <fieldset class="party-via"><span class="field-label">When</span><div class="two-opts">
            ${opt2("now", "network", "Start now", "Goes live right away.")}
            ${opt2("plan", "clock", "Plan for later", "Listed as Upcoming. You press Start on the night.")}</div></fieldset>
          ${f.when === "plan" ? html`<div class="form-row-3">
            <label class="field"><span class="field-label">Date</span><input class="input" type="date" data-input="form-text" data-k="date" value="${f.date}"></label>
            <label class="field"><span class="field-label">Start (your time)</span><input class="input" type="time" data-input="form-text" data-k="time" value="${f.time}"></label>
            <label class="field"><span class="field-label">Repeat</span><select class="select" data-change="form-select" data-k="repeat" data-value="${f.repeat}"><option value="none" ${f.repeat === "none" ? raw("selected") : ""}>Doesn't repeat</option><option value="weekly" ${f.repeat === "weekly" ? raw("selected") : ""}>Weekly</option><option value="monthly" ${f.repeat === "monthly" ? raw("selected") : ""}>Monthly</option></select></label></div>` : ""}
          <div class="form-row">
            <label class="field"><span class="field-label">Max boxes</span><select class="select" data-change="form-select" data-k="max" data-value="${f.max}">${[2, 4, 6, 8, 12, 16].map((n) => html`<option value="${n}" ${+f.max === n ? raw("selected") : ""}>${n}</option>`)}</select></label></div>
          <label class="opt-line"><span class="iso-ic">${icon("check")}</span><span class="lt-text"><b>Approve join requests automatically</b><small>Off: you approve each box. On: anyone joins until the party is full.</small></span>
            <span class="switch"><input type="checkbox" data-change="form-auto" ${f.auto ? raw("checked") : ""}><span class="switch-track"></span></span></label>
          <label class="opt-line"><span class="iso-ic">${icon("users")}</span><span class="lt-text"><b>Party chat</b><small>${f.chat ? "On: boxes in the party can chat. You can turn it off any time." : "Off: no chat. You can turn it on any time."}</small></span>
            <span class="switch"><input type="checkbox" data-change="form-chat" ${f.chat ? raw("checked") : ""}><span class="switch-track"></span></span></label>
          <label class="opt-line ${f.share ? "is-warn" : ""}"><span class="iso-ic">${icon("globe")}</span><span class="lt-text"><b>Share my internet with the party</b><small>${f.share ? "On: strangers' PCs use your connection, and everything they do online comes from your home IP." : "Off: a LAN only. Recommended for public parties."}</small></span>
            <span class="switch"><input type="checkbox" data-change="form-share" ${f.share ? raw("checked") : ""}><span class="switch-track"></span></span></label>`
        : html`<p class="faint" style="font-size:12.5px">${icon("info", "i-sm")} Auto-approve and internet sharing are the switches on the party card and the LAN page; the party uses them as they are.</p>`}
        ${f.err ? html`<p class="field-error" role="alert">${f.err}</p>` : ""}
        <div class="row" style="justify-content:flex-end;gap:8px"><a class="btn" href="#/parties">Cancel</a>
          <button type="submit" class="btn btn-primary ${busy ? "is-busy" : ""}" ${busy ? raw("disabled") : ""}>${icon(pub && f.when === "plan" ? "clock" : "network")}${label}</button></div>
      </form></div>`;
    function opt2(v, ic, title, text) {
      return html`<label class="pv-opt" data-key="when-${v}"><input type="radio" name="when" value="${v}" data-change="form-when" ${f.when === v ? raw("checked") : ""}><span class="pv-radio" aria-hidden="true"></span><span class="pv-t"><b>${icon(ic, "i-sm")}${title}</b><small>${text}</small></span></label>`;
    }
  },
  // "Victoria's LAN Party" (the nickname), else the box's name, when the name box is left empty
  defaultTitle() {
    const who = (this.pub.me && this.pub.me.nick) || (S.status && S.status.box_name) || "My";
    return (who === "My" ? "My LAN Party" : `${who}'s LAN Party`).slice(0, 40);
  },
  formListing() {
    const f = this.form;
    return { title: f.title.trim() || this.defaultTitle(), description: f.desc.trim(), games: f.games, max: +f.max, auto: f.auto, chat: f.chat !== false };
  },

  // ---- render
  render() {
    const st = S.status;
    const lan = (st && st.lan) || { state: "stopped" };
    if (this.screen.name === "listing") { patch(view, this.listingScreen()); return; }
    if (this.screen.name === "host") { patch(view, this.hostScreen(lan)); return; }
    const p = partyState();
    if (p && p.role === "host" && p.public && p.auto_approve != null && !(this.aa && this.aa.saving)) this.aa = { value: p.auto_approve, saving: false };
    // in a party (hosting, joining or joined): the page is about that party; browsing comes back when it ends
    const busy = this.inSession(p);
    patch(view, html`<div class="stack">
      ${busy ? "" : this.nickArea()}
      ${this.yourParty(lan, p)}
      ${ipClashBanner(lan)}
      ${busy ? html`<p class="muted lan-note" data-key="dir-hidden">${icon("globe", "i-sm")} Public parties and your events come back here when this party ends.</p>`
        : html`${this.myEvents()}${this.directory()}`}
    </div>`);
    const aa = $("#party-sw-auto", view);
    if (aa && this.aa) aa.checked = !!this.aa.value;
  },
  async cmd(cmd, body = {}, key = cmd) { return Lan.cmd.call(this, cmd, body, key); },
  partyPending(...a) { return Lan.partyPending.apply(this, a); },
  partyMembers(...a) { return Lan.partyMembers.apply(this, a); },
  async act(path, body, okMsg) {
    try {
      const l = await api(path, { method: "POST", body });
      if (l && l.id) this.lst = l;
      if (okMsg) toast(okMsg);
      this.loadMine();
      return l;
    } catch (e) { if (e.status !== 401) toastErr(e); return null; } finally { if (current === this) this.render(); }
  },
  actions: {
    ...Object.fromEntries(["party-copy", "party-show", "party-regenerate", "party-endpoint", "party-approve", "party-reject", "party-kick",
      "party-close", "party-leave", "party-auto", "party-clear", "party-code-input"].map((k) => [k, function (...a) { return Lan.actions[k].apply(this, a); }])),
    // nickname
    "nick-retry"() { this.pub.err = null; this.render(); this.loadMe(); },
    "nick-edit"() { Object.assign(this.pub, { editing: true, draft: this.pub.me.nick, check: null }); this.render(); const i = $("#nick-in"); if (i) { i.focus(); i.select(); } },
    "nick-cancel"() { Object.assign(this.pub, { editing: false, draft: "", check: null }); this.pub.seq++; this.render(); },
    "nick-skip"() { this.pub.skipped = true; this.pub.seq++; this.render(); },
    "nick-open"(el, e) { if (e) e.preventDefault(); Object.assign(this.pub, { skipped: false, editing: !!(this.pub.me && this.pub.me.nick) }); this.go("#/parties"); setTimeout(() => { const i = $("#nick-in"); if (i) i.focus(); }, 50); },
    "nick-input"(input) {
      this.pub.draft = input.value;
      clearTimeout(this.pub.timer);
      this.pub.timer = setTimeout(() => this.checkNick(input.value), 350);
    },
    async "nick-save"() {
      const p = this.pub, nick = p.draft.trim();
      if (!p.check || !p.check.free || p.saving) return;
      p.saving = true;
      this.render();
      try {
        p.me = await api("/api/parties/nick", { method: "POST", body: { nick } });
        Object.assign(p, { editing: false, draft: "", check: null });
        toast(`Nickname “${p.me.nick}” is yours`, { detail: "Public parties are open: join, chat and host." });
        this.loadDir();
      } catch (e) { p.check = { free: false, reason: e.message }; if (e.status === 403) this.loadMe(); }
      p.saving = false;
      this.render();
    },
    // directory
    "dir-retry"() { this.dirErr = null; this.render(); this.loadDir(); },
    "dir-tab"(b) { this.tab = b.dataset.value; this.render(); },
    "dir-q"(input) { this.q = input.value; this.render(); },
    "dir-near"(input) { this.near = input.checked; this.render(); },
    // join with a code: KEY-… invite codes go to the LAN service; event IDs open the event's page
    async "party-join"(form) {
      const ta = form.elements.code, raw2 = ta.value.trim(), hubCode = normHubCode(raw2);
      if (EVENT_ID_RE.test(raw2)) {
        try { const l = await api(`/api/parties/listing/${raw2.toUpperCase()}`); this.go(`#/parties/l/${l.id}`); }
        catch (e) { this.joinErr = e.status === 404 ? "No party or event with that ID (it may have ended)." : e.message; this.render(); }
        return;
      }
      return Lan.actions["party-join"].call(this, form);
    },
    // party page
    async "join-listing"() {
      if (await this.cmd("join", { listing: this.lst.id }, "join")) this.loadListing();
    },
    async rsvp(b) {
      const going = b.dataset.going === "1";
      await this.act(`/api/parties/listing/${this.lst.id}/rsvp`, { going, autojoin: true }, going ? "RSVP sent: this box joins automatically when it starts" : "RSVP withdrawn");
      this.loadParty();
    },
    async autojoin(input) {
      const l = this.lst;
      try {
        await api("/api/party", { method: "POST", body: { cmd: "watch", listing: l.id, starts: l.starts, title: l.title, on: input.checked } });
        toast(input.checked ? "This box joins automatically when it starts" : "Auto-join off");
        this.loadParty();
      } catch (e) { toastErr(e); input.checked = !input.checked; }
    },
    vote(b) { const on = b.getAttribute("aria-pressed") !== "true"; this.act(`/api/parties/listing/${this.lst.id}/vote`, { game: b.dataset.game, on }); },
    preapprove(b) { this.act(`/api/parties/listing/${this.lst.id}/preapprove`, { id: b.dataset.id, on: b.dataset.on === "1" }); },
    async decline(b) {
      if (await confirmDialog({ title: `Decline ${b.dataset.nick}?`, message: "Their RSVP is removed and their box is told.", confirmText: "Decline", danger: true }))
        this.act(`/api/parties/listing/${this.lst.id}/decline`, { id: b.dataset.id }, "RSVP declined");
    },
    async "cancel-event"() {
      if (!(await confirmDialog({ title: "Cancel this event?", message: "It disappears from the directory, and everyone who RSVP'd is told.", confirmText: "Cancel event", danger: true }))) return;
      if (await this.act(`/api/parties/listing/${this.lst.id}/cancel`, {}, "Event cancelled")) this.go("#/parties");
    },
    async "start-event"() {
      if (await this.cmd("start", { listing: this.lst.id }, "start")) this.go("#/parties");
    },
    async "copy-event"() {
      if (this.lst && (await copyText(this.lst.code))) toast(`Event ID ${this.lst.code} copied`, { detail: "Post it on a forum or Discord: NetKey users type it under Parties → Join with a code." });
    },
    async "report-listing"() { this.report({ listing: this.lst.id }, `Report “${this.lst.title}”?`); },
    // chat
    "chat-draft"(input) { if (this.chat) this.chat.draft = input.value; },
    async "chat-send"(form) {
      const c = this.chat, text = (c && c.draft || "").trim();
      if (!c || !text || c.sending) return;
      c.sending = true;
      try { await api("/api/parties/chat", { method: "POST", body: { room: c.room, text } }); c.draft = ""; const i = form.querySelector("input"); if (i) i.value = ""; }
      catch (e) { toastErr(e); }
      c.sending = false;
      clearTimeout(this.timers.chat);
      this.pollChat();
      const i = $("#chat-in"); if (i) i.focus();
    },
    async "chat-del"(b) { await this.chatAct("delete", b.dataset.id, "Message deleted"); },
    async "chat-mute"(b) {
      if (b.dataset.on === "0") { await this.chatAct("mute", b.dataset.id, "Unmuted: it can post again", false); return; }
      if (await confirmDialog({ title: "Mute this box in this chat?", message: "It can still read along, but can't post here any more. You can unmute it from any of its messages.", confirmText: "Mute", danger: true }))
        await this.chatAct("mute", b.dataset.id, "Muted in this chat");
    },
    async "chat-report"(b) { this.report({ room: this.chat.room, id: +b.dataset.id }, "Report this message?"); },
    // host form
    "form-vis"(input) { if (input.checked) { this.form.vis = input.value; this.form.err = ""; if (input.value === "public") this.loadGames(); this.render(); } },
    "form-when"(input) { if (input.checked) { this.form.when = input.value; this.render(); } },
    "form-text"(input) { this.form[input.dataset.k] = input.value; },
    "form-select"(sel) { this.form[sel.dataset.k] = sel.value; this.render(); },
    "form-auto"(input) { this.form.auto = input.checked; },
    "form-chat"(input) { this.form.chat = input.checked; this.render(); },
    async "party-share"(input) {
      const on = input.checked, p = partyState() || {};
      if (on) {
        input.checked = false;
        const ok = p.public ? await confirmPublicShare() : await confirmDialog({ title: "Share your internet?", message: LAN_SHARE_TEXT, confirmText: "Share internet" });
        if (!ok || current !== this) { this.render(); return; }
      }
      this.shareOpt = { value: on, saving: true, until: 0 };
      this.render();
      try {
        await api("/api/settings", { method: "PUT", body: { values: { "lan.share_internet": on } } });
        this.shareOpt = { value: on, saving: false, until: Date.now() + 12000 };
        toast(on ? "Internet sharing on" : "Internet sharing off", { detail: on ? "PCs in the party reach the internet through this box within a few seconds." : "The party is a LAN only again." });
        refreshStatus();
      } catch (e) { this.shareOpt = null; toastErr(e, "Could not change internet sharing"); }
      if (current === this) this.render();
    },
    async "party-chat"(input) {
      const p = partyState() || {}, on = input.checked;
      this.chatOpt = { id: p.listing, value: on, saving: true, until: 0 };
      this.render();
      try {
        await api(`/api/parties/listing/${p.listing}/settings`, { method: "POST", body: { chat: on } });
        this.chatOpt = { id: p.listing, value: on, saving: false, until: Date.now() + 20000 };
        toast(on ? "Party chat on" : "Party chat off", { detail: on ? "" : "Nobody can read or post until you turn it back on." });
        this.chatFor(null);
        this.loadDir();
      } catch (e) { this.chatOpt = null; toastErr(e, "Could not change the chat"); }
      if (current === this) { this.partyChat(); this.render(); }
    },
    async "form-share"(input) {
      const on = input.checked;
      if (on) {
        input.checked = false;
        if (!(await confirmPublicShare())) { this.render(); return; }
      }
      this.form.share = on;
      this.render();
    },
    "game-q"(input) { this.form.gq = input.value; this.render(); },
    "game-genre"(b) { this.form.genre = b.dataset.value; this.render(); },
    "game-pick"(b) {
      const f = this.form, k = b.dataset.key, i = f.games.indexOf(k);
      if (i >= 0) f.games.splice(i, 1); else if (f.games.length < 5) f.games.push(k); else toast("Up to 5 games", { type: "info" });
      this.render();
    },
    "game-remove"(b) { this.form.games.splice(+b.dataset.i, 1); this.render(); },
    "game-other"() { const f = this.form; f.other = f.other ? null : { name: f.gq.trim(), proto: "IPX" }; this.render(); const i = $("#other-name"); if (i) i.focus(); },
    "other-name"(input) { this.form.other.name = input.value; },
    "other-proto"(sel) { this.form.other.proto = sel.value; },
    "game-add-other"() {
      const f = this.form, name = f.other.name.trim();
      if (!name) { toast("Type the game's name", { type: "err" }); return; }
      if (f.games.length >= 5) { toast("Up to 5 games", { type: "info" }); return; }
      if (!f.games.some((x) => typeof x !== "string" && x.name.toLowerCase() === name.toLowerCase())) f.games.push({ name, proto: f.other.proto });
      f.other = null; f.gq = "";
      this.render();
    },
    async "host-submit"() {
      const f = this.form;
      f.err = "";
      if (f.vis === "direct") {
        const ep = f.endpoint.trim();
        if (ep && !/^[A-Za-z0-9.:[\]-]+$/.test(ep)) { f.err = "Enter a host name or IP address (no spaces)."; this.render(); return; }
        if (await this.cmd("create", ep ? { via: "direct", endpoint: ep } : { via: "direct" }, "create")) { this.form = null; this.go("#/parties"); }
        return;
      }
      if (f.vis === "hub") {
        if (await this.cmd("create", { via: "hub" }, "create")) { this.form = null; this.go("#/parties"); }
        return;
      }
      const lst = this.formListing();
      if (lst.title.length < 3) f.err = "Use at least 3 characters for the name, or leave it empty.";
      if (f.err) { this.render(); return; }
      if (f.when === "now") {
        if (!!f.share !== !!this.shareCfg) {
          try { await api("/api/settings", { method: "PUT", body: { values: { "lan.share_internet": !!f.share } } }); this.shareCfg = !!f.share; }
          catch (e) { f.err = `Could not change internet sharing: ${e.message}`; this.render(); return; }
        }
        if (await this.cmd("create", { via: "hub", public: lst }, "create")) { this.form = null; this.go("#/parties"); }
        return;
      }
      const starts = new Date(`${f.date}T${f.time || "20:00"}`).getTime() / 1000;
      if (!(starts > nowS() + 60)) { f.err = "Pick a start time in the future."; this.render(); return; }
      this.busy = "plan";
      this.render();
      try {
        const l = await api("/api/parties/listing", { method: "POST", body: { ...lst, starts, repeat: f.repeat } });
        this.form = null;
        toast("Event published", { detail: `Share the event ID ${l.code}: NetKey users type it under Join with a code.` });
        this.busy = null;
        this.go(`#/parties/l/${l.id}`);
      } catch (e) { this.busy = null; f.err = e.message; this.render(); }
    },
  },
  async chatAct(action, id, msg, on = true) {
    try { await api(`/api/parties/chat/${action}`, { method: "POST", body: { room: this.chat.room, id: +id, on } }); toast(msg); }
    catch (e) { toastErr(e); }
    if (this.chat) Object.assign(this.chat, { msgs: [], last: 0 });      // reload: flags on old messages changed
    clearTimeout(this.timers.chat);
    this.pollChat();
  },
  report(target, title) {
    openModal({
      title, iconName: "alert",
      body: html`<p class="muted">The hub operator sees your report with the message or party. Abuse can get a box banned from public parties.</p>
        <label class="field"><span class="field-label">What's wrong? <span class="faint">(optional)</span></span><textarea class="input textarea" name="text" rows="3" maxlength="300" placeholder="e.g. spam, insults, phishing links"></textarea></label>`,
      foot: html`<button type="button" class="btn" data-close>Cancel</button><button type="submit" class="btn btn-danger">Report</button>`,
      onSubmit: async (form) => {
        try { await api("/api/parties/report", { method: "POST", body: { ...target, text: form.elements.text.value } }); toast("Reported. Thanks!"); return true; }
        catch (e) { toastErr(e); return false; }
      },
    });
  },
};

// page: network (uplink, Wi-Fi, built-in port, rescue hotspot)
const WIFI_COUNTRIES = [["NO", "Norway"], ["SE", "Sweden"], ["DK", "Denmark"], ["FI", "Finland"], ["IS", "Iceland"], ["DE", "Germany"],
  ["GB", "United Kingdom"], ["IE", "Ireland"], ["NL", "Netherlands"], ["BE", "Belgium"], ["FR", "France"], ["ES", "Spain"], ["PT", "Portugal"],
  ["IT", "Italy"], ["PL", "Poland"], ["AT", "Austria"], ["CH", "Switzerland"], ["US", "United States"], ["CA", "Canada"], ["AU", "Australia"],
  ["NZ", "New Zealand"], ["JP", "Japan"]];
const OTHER_NET = { other: true };
const ETH_ROLES = {
  uplink: { label: "Internet", text: "Connects this box to your network and the internet." },
  management: { label: "Management port (home network)", text: "Cabled into your home network, the web GUI is reachable on this address too." },
  retro: { label: "Retro-LAN port", text: "Cabled to retro PCs, they get the isolated retro LAN with its own DHCP." },
  checking: { label: "Checking…", text: "Finding out whether the cable goes to your router or to retro PCs…" },
  unplugged: { label: "Unplugged", text: "No cable plugged in." },
};
const sigLevel = (s) => (s == null ? 0 : s >= 75 ? 4 : s >= 55 ? 3 : s >= 35 ? 2 : s >= 10 ? 1 : 0);
const sigBars = (s) => html`<span class="sig sig-${sigLevel(s)}" role="img" aria-label="${s == null ? "Signal unknown" : `Signal ${s}%`}" title="${s == null ? "Signal unknown" : `Signal ${s}%`}"><i></i><i></i><i></i><i></i></span>`;
const hotspotUrl = (hs) => `http://${(hs && hs.address) || "192.168.4.1"}`;
// Wi-Fi joined to a real network (not the box's own hotspot).
const wifiUp = (n) => {
  const w = n && n.wifi;
  return !!(w && w.state === "connected" && w.ip && w.ssid && !(n.hotspot && n.hotspot.state === "on"));
};
const wifiError = (msg) => (/secrets were required|psk|802-11-wireless-security/i.test(msg) && !/password/i.test(msg)
  ? `The Wi-Fi password seems to be wrong. ${msg}` : msg);

const Network = {
  title: "Internet",
  mount() {
    this.net = null;
    this.loadErr = "";
    this.cfg = null;
    this.opt = {};
    this.busy = null;
    this.sel = null;
    this.form = { ssid: "", password: "", country: "", show: false };
    this.formErr = "";
    this.scanErr = "";
    this.lost = "";
    this.wantWifi = false;
    this.showHsPw = false;
    this.render();
    this.load();
    this.loadSettings();
  },
  unmount() { clearTimeout(this.timer); },
  onVisible() { this.load(); },
  async load() {
    clearTimeout(this.timer);
    try {
      this.setNet(await api("/api/network"));
      this.loadErr = "";
    } catch (e) {
      if (e.status === 401) return;
      if (!this.net) this.loadErr = e.message;
    }
    if (current !== this) return;
    this.render();
    if (!document.hidden) this.timer = setTimeout(() => this.load(), 5000);
  },
  // Setting values as a fallback while the network service does not report them.
  async loadSettings() {
    try {
      const cfg = await api("/api/settings");
      this.cfg = cfg.values || {};
      if (current === this) this.render();
    } catch { /* the network status carries the same values */ }
  },
  setNet(n) {
    if (!n) return;
    this.net = n;
    // optimistic switch values win until the network service reflects them (it re-reads config.toml within seconds)
    for (const [k, o] of Object.entries(this.opt)) {
      if (o.saving) continue;
      const actual = k === "mode" ? n.configured_mode || n.mode : n.hotspot && n.hotspot.enabled;
      if (actual === o.value || Date.now() > o.until) delete this.opt[k];
    }
    if (this.wantWifi && this.modeValue() === "wifi") this.wantWifi = false;
  },
  modeValue() {
    if (this.opt.mode) return this.opt.mode.value;
    const n = this.net || {};
    if (!n.error && !n.wifi_available) return "ethernet";
    return n.configured_mode || n.mode || (this.cfg && this.cfg["network.uplink"]) || "ethernet";
  },
  hotspotValue() {
    if (this.opt.hotspot) return this.opt.hotspot.value;
    const hs = this.net && this.net.hotspot;
    if (hs && hs.enabled != null) return !!hs.enabled;
    return !!(this.cfg && this.cfg["network.hotspot"]);
  },
  // Runs one network command; the response carries the new network status (also on 400).
  async cmd(cmd, body = {}) {
    this.busy = cmd;
    this.render();
    let res = null, err = null;
    try {
      res = await api("/api/network", { method: "POST", body: { cmd, ...body } });
      this.setNet(res.network);
    } catch (e) {
      err = e;
      if (e.data && e.data.network) this.setNet(e.data.network);
    }
    this.busy = null;
    return { res, err };
  },
  async scan() {
    if (this.busy) return;
    this.scanErr = "";
    const { err } = await this.cmd("wifi_scan");
    if (err && err.status !== 401) this.scanErr = err.message;
    if (current === this) this.render();
  },
  async connect() {
    if (this.busy) return;
    const n = this.net || {}, other = this.sel === OTHER_NET;
    const x = other ? null : ((n.scan && n.scan.networks) || []).find((y) => y.ssid === this.sel);
    const ssid = other ? this.form.ssid : this.sel;
    const pw = (other || !x || x.secure) ? this.form.password : "";
    const secure = !other && (!x || x.secure);
    const bytes = new TextEncoder().encode(ssid || "").length;
    const bad = !String(ssid || "").trim() ? ["wf-ssid", "Enter the network name."]
      : bytes > 32 ? ["wf-ssid", "Network names are at most 32 bytes long."]
      : secure && !pw ? ["wf-pw", "Enter the Wi-Fi password."]
      : pw && (pw.length < 8 || pw.length > 63) ? ["wf-pw", "Wi-Fi passwords are 8–63 characters long."]
      : !this.form.country ? ["wf-cc", "Choose the country the box is in."] : null;
    if (bad) {
      this.formErr = bad[1];
      this.render();
      const el = document.getElementById(bad[0]);
      if (el) el.focus();
      return;
    }
    this.formErr = "";
    this.lost = "";
    const { res, err } = await this.cmd("wifi_connect", { ssid, password: pw, country: this.form.country });
    if (res) {
      // came here through "Wi-Fi" as the uplink: switch once joined (the result says it joined, even if the
      // status lags a moment behind)
      const want = this.wantWifi && this.modeValue() !== "wifi";
      this.wantWifi = false;
      const hint = !want && this.modeValue() !== "wifi" ? "Wi-Fi is ready: choose Wi-Fi as the uplink above to use it." : "";
      toast((res.result && res.result.message) || `Connected to ${ssid}`, { detail: hint });
      this.sel = null;
      this.form.password = "";
      this.form.show = false;
      if (want) await this.saveMode("wifi");
    } else if (err.status === 0) {
      this.lost = ssid;
    } else if (err.status !== 401) {
      this.formErr = wifiError(err.message);
    }
    if (current === this) this.render();
  },
  async saveMode(value) {
    this.opt.mode = { value, saving: true, until: 0 };
    this.render();
    try {
      await api("/api/settings", { method: "PUT", body: { values: { "network.uplink": value } } });
      this.opt.mode = { value, saving: false, until: Date.now() + 15000 };
      if (value === "wifi" && wifiUp(this.net)) wifiUplinkDialog(this.net);
      else {
        toast(value === "wifi" ? "Internet switched to Wi-Fi" : "Internet switched to Ethernet", {
          detail: value === "wifi" ? "The built-in Ethernet port now works out by itself whether it is a retro-LAN or management port."
            : "The built-in Ethernet port connects the box to your network.",
        });
      }
      refreshStatus();
      this.load();
    } catch (e) {
      delete this.opt.mode;
      toastErr(e, "Could not change the uplink");
    }
    if (current === this) this.render();
  },
  async saveHotspot(on) {
    this.opt.hotspot = { value: on, saving: true, until: 0 };
    this.render();
    try {
      await api("/api/settings", { method: "PUT", body: { values: { "network.hotspot": on } } });
      this.opt.hotspot = { value: on, saving: false, until: Date.now() + 15000 };
      toast(on ? "Rescue hotspot turned on" : "Rescue hotspot turned off", { detail: on ? "It opens when the box has had no network for a while." : "" });
    } catch (e) {
      delete this.opt.hotspot;
      toastErr(e, "Could not change the rescue hotspot");
    }
    if (current === this) this.render();
  },

  hero(n) {
    if (n.error) {
      return html`<section class="card lan-hero is-error" data-key="net-hero-err">
        ${emptyState("alert", "Network status not available", n.error, html`<button type="button" class="btn" data-action="net-retry">${icon("reset")}Retry</button>`)}</section>`;
    }
    const w = n.wifi || {}, hs = n.hotspot || {}, eth = n.ethernet || {}, home = n.home || {};
    const hsOn = hs.state === "on";
    // Wi-Fi chosen and joined reads as Wi-Fi, also while a cable into the router (management port) carries traffic
    const viaWifi = n.uplink_ok && ((n.mode === "wifi" && wifiUp(n)) || (n.uplink_dev ? n.uplink_dev === w.iface : n.mode === "wifi"));
    const none = html`<span class="faint">—</span>`;
    const mono = (v) => (v ? html`<span class="mono">${v}</span>` : none);
    const fact = (label, value, extra = "") => html`<div><small>${label}</small><b>${value}</b>${extra ? html`<span class="faint">${extra}</span>` : ""}</div>`;
    let ic, title, sub, cls, pill, facts, banner = "";
    if (n.uplink_ok && viaWifi) {
      ic = "wifi"; cls = "is-up"; title = "Online via Wi-Fi";
      sub = [w.ssid, w.band, w.ip].filter(Boolean).join(" · ");
      pill = html`<span class="pill pill-green pill-glow">Online</span>`;
      facts = [fact("Wi-Fi network", html`<span class="net-ssid">${sigBars(w.signal)}<span class="ellipsis" title="${w.ssid}">${w.ssid || "—"}</span></span>`),
        fact("Signal", w.signal != null ? html`<span class="tnum">${w.signal}%</span>` : none, w.band || ""),
        fact("IP address", mono(w.ip)), fact("Router", mono(home.gateway))];
    } else if (n.uplink_ok) {
      ic = "ethernet"; cls = "is-up"; title = "Online via Ethernet";
      sub = ["Built-in Ethernet port", eth.ip].filter(Boolean).join(" · ");
      pill = html`<span class="pill pill-green pill-glow">Online</span>`;
      facts = [fact("Port", mono(eth.name || n.uplink_dev)), fact("IP address", mono(eth.ip)),
        fact("Router", mono(home.gateway)), fact("Home network", mono(home.subnet))];
    } else {
      const lost = n.uplink_lost_for, since = lost != null ? (n.updated || nowS()) - lost : null;
      ic = hsOn ? "hotspot" : n.mode === "wifi" ? "wifi" : "ethernet";
      cls = hsOn ? "is-warn" : "is-error";
      title = hsOn ? "Rescue hotspot is on" : "Offline";
      sub = since != null ? html`No network for ${ticking(since)}` : "No network connection";
      pill = hsOn ? html`<span class="pill pill-amber pill-glow">Hotspot</span>` : html`<span class="pill pill-red">Offline</span>`;
      const hsName = html`<b class="mono">${hs.ssid || "NetKey-…"}</b>`;
      let msg;
      if (!n.wifi_available) msg = html`Check the Ethernet cable to your router.`;
      else if (hsOn) msg = html`Join the Wi-Fi ${hsName} and open <a href="${hotspotUrl(hs)}">${hotspotUrl(hs)}</a> to fix the connection. ${hs.clients ? `${hs.clients} device${hs.clients === 1 ? "" : "s"} connected.` : "Without connected devices the box retries your network every 5 min."}`;
      else if (hs.state === "retry") msg = html`Retrying your network… the rescue hotspot ${hsName} comes back if that fails.`;
      else if (!hs.enabled) msg = html`The rescue hotspot is off, plug the Ethernet port into your router to reach the box.`;
      else if (since != null && hs.after_s != null && lost < hs.after_s) msg = html`The rescue hotspot ${hsName} opens in <span class="tnum" data-until="${since + hs.after_s}">${fmtDur(hs.after_s - lost)}</span> unless the network comes back.`;
      else msg = html`The rescue hotspot ${hsName} opens shortly.`;
      banner = html`<div class="banner ${hsOn ? "banner-warn" : "banner-err"} lan-banner" role="status">${icon(hsOn ? "hotspot" : "alert")}<span>${msg}</span></div>`;
      const ethRole = ETH_ROLES[eth.role];
      facts = [fact("Connected via", n.mode === "wifi" ? "Wi-Fi" : "Ethernet"),
        fact("Wi-Fi", !n.wifi_available ? html`<span class="faint">Not available</span>` : w.state === "connecting" ? html`Connecting to ${w.ssid || "…"}` : w.configured ? html`${w.ssid || "Saved network"} <span class="faint">· ${humanize(w.state || "unknown")}</span>` : html`<span class="faint">Not set up</span>`),
        fact("Ethernet port", ethRole ? ethRole.label : humanize(eth.role || "unknown")),
        fact("Rescue hotspot", !n.wifi_available ? html`<span class="faint">Not available</span>` : !hs.enabled ? "Off" : hsOn ? "On" : hs.state === "retry" ? "Retrying uplink" : "Standby")];
    }
    return html`<section class="card lan-hero net-hero ${cls}" aria-labelledby="h-net-hero" data-key="net-hero">
      <div class="lan-hero-head">
        <span class="lt-icon">${icon(ic)}</span>
        <div class="lan-hero-title"><h2 id="h-net-hero">${title}</h2><span class="sub ellipsis">${sub}</span></div>
        <span class="spacer"></span>${pill}
      </div>
      <div class="lan-hero-body">${banner}<div class="lan-facts">${facts}</div></div>
    </section>`;
  },
  uplink(n) {
    const mode = this.modeValue(), avail = !n.error && !!n.wifi_available, hs = n.hotspot || {};
    const saving = !!(this.opt.mode && this.opt.mode.saving);
    const pending = this.wantWifi && mode !== "wifi";
    const opt = (m, ic, title, desc, disabled) => html`<button type="button" class="mode-opt net-opt ${m === "wifi" && pending ? "is-pending" : ""}" role="radio" aria-checked="${String(mode === m)}" data-action="net-mode" data-value="${m}" ${disabled || saving ? raw("disabled") : ""}>
      <span class="mo-icon">${icon(ic)}</span><b>${title}${m === "wifi" && pending ? html`<span class="pill pill-amber no-dot">Connect first</span>` : ""}</b><span class="d">${desc}</span><span class="check">${icon("check")}</span></button>`;
    const wifiDesc = n.error ? "Not available while the network status is unknown."
      : !n.wifi_available ? "Not available: this box has no Wi-Fi hardware (for example a Raspberry Pi 1 or a server)."
      : "Wi-Fi connects the box, the Ethernet port becomes the retro-LAN port.";
    const after = hs.after_s && hs.after_s !== 300 ? "5 min (60 s while Wi-Fi is not set up)" : "5 min";
    return html`<section class="card" aria-labelledby="h-uplink" data-key="net-uplink">
      <div class="card-head">${icon("globe")}<h3 id="h-uplink">Internet connection</h3><span class="sub">How this box reaches your network and the internet</span>
        <span class="spacer"></span>${saving ? html`<span class="chip chip-warn">${spinner()} Applying…</span>` : ""}</div>
      <div class="net-pad">
        <div class="mode-pick" role="radiogroup" aria-labelledby="h-uplink">
          ${opt("ethernet", "ethernet", "Ethernet", "The built-in Ethernet port is cabled to your router.", false)}
          ${opt("wifi", "wifi", "Wi-Fi", wifiDesc, !avail)}
        </div>
        ${pending ? html`<div class="net-callout" role="status">${icon("info")}<span><b>Connect to a Wi-Fi network first.</b> Pick your network below, the uplink switches to Wi-Fi as soon as it is connected.</span>
          <button type="button" class="btn btn-sm btn-ghost" data-action="net-want-cancel">Cancel</button></div>` : ""}
      </div>
      ${avail ? html`<ul class="iso-list net-safety" aria-label="Safety net">
        <li><span class="iso-ic">${icon("ethernet")}</span><span class="iso-t"><span>If Wi-Fi fails: plug the Ethernet port into your router</span><small>The box gets an address there and the GUI is reachable on it.</small></span></li>
        <li><span class="iso-ic">${icon("hotspot")}</span><span class="iso-t"><span>${hs.enabled === false ? "Rescue hotspot is off" : html`After ${after} without network the box opens its own Wi-Fi <span class="mono">${hs.ssid || ""}</span>`}</span>
          <small>${hs.enabled === false ? "Turn it on below as a safety net." : html`Password shown below · GUI at <span class="mono">${hotspotUrl(hs)}</span>`}</small></span></li>
      </ul>` : ""}
    </section>`;
  },
  connectForm(x) {
    const other = !x, connecting = this.busy === "wifi_connect", f = this.form;
    const secure = other || x.secure;
    const dis = this.busy ? raw("disabled") : "";
    const stored = this.net && this.net.wifi && this.net.wifi.country;
    const list = stored && !WIFI_COUNTRIES.some(([c]) => c === stored) ? [[stored, stored], ...WIFI_COUNTRIES] : WIFI_COUNTRIES;
    return html`<form class="wifi-form" data-submit="wifi-connect" novalidate autocomplete="off" data-key="wf-${other ? "other" : x.ssid}">
      ${other ? html`<label class="field wf-wide"><span class="field-label">Network name (SSID)</span>
        <input class="input" id="wf-ssid" name="ssid" data-input="wifi-field" value="${f.ssid}" maxlength="32" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" ${dis}></label>` : ""}
      ${secure ? html`<label class="field"><span class="field-label">Password${other ? html` <span class="faint">· empty for an open network</span>` : ""}</span>
        <span class="input-wrap"><input class="input" id="wf-pw" name="password" type="${f.show ? "text" : "password"}" data-input="wifi-field" value="${f.password}" maxlength="63" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" ${dis}>
          <button type="button" class="input-btn" data-action="wifi-pw-toggle" aria-label="${f.show ? "Hide password" : "Show password"}" title="${f.show ? "Hide password" : "Show password"}" ${dis}>${icon(f.show ? "eye-off" : "eye")}</button></span></label>`
        : html`<p class="wf-open">${icon("info", "i-sm")}Open network: traffic on it is not encrypted.</p>`}
      <label class="field"><span class="field-label">Country</span>
        <select class="select" id="wf-cc" name="country" data-change="wifi-country" data-value="${f.country}" ${dis}>
          ${list.map(([c, name]) => html`<option value="${c}" ${c === f.country ? raw("selected") : ""}>${name} (${c})</option>`)}
        </select></label>
      ${this.formErr ? html`<div class="banner banner-err wf-wide wf-err" role="alert">${icon("alert")}<span>${this.formErr}</span></div>` : ""}
      <div class="wf-actions wf-wide">
        ${connecting ? html`<span class="wf-progress" role="status">${spinner()}Connecting… up to 45 s</span>` : html`<span class="faint wf-hint">The country sets the legal Wi-Fi channels.</span>`}
        <span class="spacer"></span>
        <button type="button" class="btn btn-ghost" data-action="wifi-cancel" ${dis}>Cancel</button>
        <button type="submit" class="btn btn-primary ${connecting ? "is-busy" : ""}" ${dis}>${icon("wifi")}Connect</button>
      </div>
    </form>`;
  },
  wifiPanel(n) {
    if (n.error) return "";
    const w = n.wifi;
    if (!n.wifi_available || !w) {
      return html`<section class="card" id="wifi-panel" aria-labelledby="h-wifi" data-key="wifi-na">
        <div class="card-head">${icon("wifi")}<h3 id="h-wifi">Wi-Fi</h3><span class="spacer"></span><span class="pill pill-gray">Not available</span></div>
        ${emptyState("wifi", "No Wi-Fi on this box", "This hardware has no Wi-Fi adapter (for example a Raspberry Pi 1 or a server), so it always uses the Ethernet port. The rescue hotspot needs Wi-Fi as well.")}
      </section>`;
    }
    const scanning = this.busy === "wifi_scan", busy = !!this.busy;
    const nets = ((n.scan && n.scan.networks) || []).filter((x) => x.ssid);
    const hsOn = n.hotspot && n.hotspot.state === "on";
    const row = (x) => {
      const open = this.sel === x.ssid;
      const meta = [x.secure ? x.security || "Secured" : "Open", x.band].filter(Boolean).join(" · ");
      const isKnown = known.some((k) => k.ssid === x.ssid);
      return html`<li class="wifi-row ${open ? "is-open" : ""} ${x.in_use ? "is-current" : ""}" data-key="wn-${x.ssid}">
        <button type="button" class="wifi-net" data-action="${isKnown && !open ? "wifi-known-connect" : "wifi-pick"}" data-ssid="${x.ssid}" aria-expanded="${String(open)}" ${x.in_use || (busy && !open) ? raw("disabled") : ""}>
          ${sigBars(x.signal)}
          <span class="wn-name"><b class="ellipsis" title="${x.ssid}">${x.ssid}</b><small>${meta}</small></span>
          ${x.in_use ? html`<span class="pill pill-green">Connected</span>` : isKnown ? html`<span class="badge" title="Saved on this box: connects without the password">SAVED</span>` : ""}
          ${x.secure ? icon("lock", "i-sm wn-lock") : ""}
          ${x.in_use ? "" : icon("chevron", "chev")}
        </button>
        ${open ? this.connectForm(x) : ""}
      </li>`;
    };
    const otherOpen = this.sel === OTHER_NET;
    let saved = "";
    const known = w.known || (w.configured && w.ssid ? [{ ssid: w.ssid, preferred: true, connected: wifiUp(n), in_range: true }] : []);
    if (known.length || w.state === "connecting") {
      const up = wifiUp(n);
      const krow = (k) => {
        const live = k.connected && up;
        const state = live ? html`<span class="pill pill-green">Connected</span>`
          : w.state === "connecting" && w.ssid === k.ssid ? html`<span class="pill pill-blue pill-pulse">Connecting…</span>`
          : hsOn && k.preferred ? html`<span class="pill pill-gray">Paused for the hotspot</span>`
          : k.in_range ? html`<span class="pill pill-gray no-dot">In range</span>` : html`<span class="pill pill-gray no-dot">Not in range</span>`;
        const detail = live ? [w.ip, w.band, w.signal != null ? `signal ${w.signal}%` : ""].filter(Boolean).join(" · ")
          : k.preferred ? "Preferred: joined first when it's in range" : "Joined when the preferred network isn't in range";
        return html`<li class="wifi-known-row ${live ? "is-current" : ""}" data-key="wk-${k.ssid}">
          ${live ? sigBars(w.signal) : html`<span class="sig sig-0" aria-hidden="true"><i></i><i></i><i></i><i></i></span>`}
          <span class="wn-name"><b class="ellipsis" title="${k.ssid}">${k.ssid}</b><small>${detail}</small></span>
          ${k.preferred ? html`<span class="badge badge-vmm" title="Joined first when it's in range">PREFERRED</span>` : ""}
          ${state}
          ${live ? "" : html`<button type="button" class="btn btn-sm ${this.busy === "wifi_connect" && this.sel === k.ssid ? "is-busy" : ""}" data-action="wifi-known-connect" data-ssid="${k.ssid}" ${busy ? raw("disabled") : ""}>${icon("wifi")}Connect</button>`}
          <button type="button" class="btn btn-sm btn-danger-text" data-action="wifi-forget" data-ssid="${k.ssid}" ${busy ? raw("disabled") : ""} title="Forget ${k.ssid}">${icon("trash")}<span class="hide-sm">Forget</span></button>
        </li>`;
      };
      saved = html`<div class="wifi-known" data-key="wifi-known">
        <div class="pm-head"><h4>Saved networks</h4><span class="sub">${known.length} of 10 · the box joins the preferred one, else any saved network in range</span></div>
        <ul class="wifi-known-list">${known.map(krow)}</ul></div>`;
    }
    let body;
    if (nets.length) body = html`<ul class="wifi-list" aria-label="Wi-Fi networks">${nets.map(row)}</ul>`;
    else if (scanning) body = html`<ul class="wifi-list is-loading" aria-label="Scanning">${[70, 55, 40].map((p) => html`<li class="wifi-row"><div class="wifi-net"><span class="skel" style="width:20px;height:14px"></span><span class="wn-name"><span class="skel skel-line" style="width:${p}%"></span></span></div></li>`)}</ul>`;
    else body = html`<div class="wifi-empty">${icon("wifi")}<span><b>No networks listed yet.</b> Scan to see the Wi-Fi networks the box can reach.</span>
      <button type="button" class="btn btn-primary btn-sm" data-action="wifi-scan" ${busy ? raw("disabled") : ""}>${icon("search")}Scan</button></div>`;
    return html`<section class="card wifi-card" id="wifi-panel" aria-labelledby="h-wifi" data-key="wifi">
      <div class="card-head">${icon("wifi")}<h3 id="h-wifi">Wi-Fi</h3>
        <span class="sub">${w.iface || "wlan0"}${w.country ? ` · country ${w.country}` : ""}${n.scan && n.scan.at ? html` · scanned ${relTime(n.scan.at)}` : ""}</span>
        <span class="spacer"></span>
        ${nets.length || scanning ? html`<button type="button" class="btn btn-sm" data-action="wifi-scan" ${busy ? raw("disabled") : ""} aria-label="Scan for Wi-Fi networks">${scanning ? spinner() : icon("search")}${scanning ? "Scanning…" : "Scan"}</button>` : ""}
      </div>
      ${saved}
      ${this.lost ? html`<div class="banner banner-warn net-lost" role="status">${icon("info")}<span><b>Lost the connection to the box.</b> That is expected if this device was on the box's rescue hotspot. Join <b>${this.lost}</b> on this device, then open the box's new address (see your router, or try <span class="mono">http://${(S.session && S.session.hostname) || "NetKey"}.local</span>).</span></div>` : ""}
      ${this.scanErr ? html`<div class="banner banner-err net-lost" role="alert">${icon("alert")}<span><b>Wi-Fi scan failed.</b> <span class="muted">${this.scanErr}</span></span></div>` : ""}
      ${scanning && nets.length ? html`<div class="wifi-scanning" role="status">${spinner()}Scanning… takes 5–15 s</div>` : ""}
      ${body}
      <div class="wifi-row wifi-other ${otherOpen ? "is-open" : ""}" data-key="wn-other">
        <button type="button" class="wifi-net" data-action="wifi-other" aria-expanded="${String(otherOpen)}" ${busy && !otherOpen ? raw("disabled") : ""}>
          <span class="wn-plus">${icon("plus", "i-sm")}</span><span class="wn-name"><b>Other network…</b><small>Hidden network: enter its name by hand</small></span>${icon("chevron", "chev")}
        </button>
        ${otherOpen ? this.connectForm(null) : ""}
      </div>
    </section>`;
  },
  ethCard(n) {
    if (n.error) return "";
    const e = n.ethernet;
    if (!e) {
      return html`<section class="card net-card" data-key="eth-none"><div class="lan-hero-head"><span class="lt-icon">${icon("ethernet")}</span>
        <div class="lan-hero-title"><h3>Built-in Ethernet port</h3><span class="sub">Not found on this box</span></div></div></section>`;
    }
    const r = ETH_ROLES[e.role] || { label: humanize(e.role || "unknown"), text: "" };
    const label = e.role === "management" && e.ip ? html`${r.label} · <span class="mono">${e.ip}</span>` : r.label;
    const wifiMode = n.mode === "wifi";
    return html`<section class="card net-card role-${e.role || "unknown"}" aria-labelledby="h-eth" data-key="eth">
      <div class="lan-hero-head"><span class="lt-icon">${icon("ethernet")}</span>
        <div class="lan-hero-title"><h3 id="h-eth">Built-in Ethernet port</h3><span class="sub eth-role">${e.role === "checking" ? spinner() : ""}${label}</span></div></div>
      <div class="net-card-body">
        <p class="muted net-text">${r.text}${e.role === "retro" ? html` <a href="#/lan">Open the LAN page</a>` : ""}</p>
        <div class="lan-facts net-facts">
          <div><small>Interface</small><b class="mono">${e.name || "—"}</b></div>
          <div><small>IP address</small><b>${e.ip ? html`<span class="mono">${e.ip}</span>` : html`<span class="faint">—</span>`}</b></div>
          <div><small>Cable</small><b>${e.carrier ? html`<span class="ok">${icon("check", "i-sm")} Connected</span>` : html`<span class="faint">Not connected</span>`}</b></div>
        </div>
      </div>
      <div class="card-note">${icon("info", "i-sm")}<span>${wifiMode
        ? "In Wi-Fi mode the port decides by itself every time the link comes up: cabled into your router it becomes a management port, cabled to retro PCs the retro-LAN port."
        : n.wifi_available ? "In Ethernet mode this port is the uplink. Choose Wi-Fi above to use it as the retro-LAN port instead."
        : "This port is always the uplink here. Retro PCs with a network card need a USB Ethernet adapter as the retro-LAN port."}</span></div>
    </section>`;
  },
  hotspotCard(n) {
    const hs = n.hotspot || {}, avail = !n.error && !!n.wifi_available;
    const on = this.hotspotValue(), saving = !!(this.opt.hotspot && this.opt.hotspot.saving);
    const pill = !avail && !n.error ? html`<span class="pill pill-gray">Not available</span>`
      : !on ? html`<span class="pill pill-gray">Off</span>`
      : hs.state === "on" ? html`<span class="pill pill-amber pill-glow">On · ${hs.clients || 0} client${hs.clients === 1 ? "" : "s"}</span>`
      : hs.state === "retry" ? html`<span class="pill pill-blue pill-pulse">Retrying uplink</span>`
      : html`<span class="pill pill-green no-dot">Standby</span>`;
    const pw = hs.password || "";
    const url = hotspotUrl(hs);
    return html`<section class="card net-card hs-card ${on && avail ? "is-on" : ""} ${hs.state === "on" ? "is-live" : ""}" aria-labelledby="h-hs" data-key="hotspot">
      <div class="lan-hero-head"><span class="lt-icon">${icon("hotspot")}</span>
        <div class="lan-hero-title"><h3 id="h-hs">Rescue hotspot</h3><span class="sub">The box's own Wi-Fi when it has no network</span></div>
        <span class="spacer"></span>${pill}
        ${avail || n.error ? html`<label class="switch" title="${on ? "Turn the rescue hotspot off" : "Turn the rescue hotspot on"}"><input type="checkbox" id="net-sw-hotspot" data-change="net-hotspot" ${on ? raw("checked") : ""} ${saving ? raw("disabled") : ""}><span class="switch-track"></span><span class="sr-only">Rescue hotspot</span></label>` : ""}
      </div>
      ${!avail ? html`<div class="net-card-body"><p class="muted net-text">${n.error ? "Details appear when the network service is running." : "Needs Wi-Fi hardware, which this box does not have."}</p></div>`
        : html`<div class="net-card-body">
        ${hs.state === "on" ? html`<p class="net-text"><b>On now</b>: ${hs.clients ? `${hs.clients} device${hs.clients === 1 ? "" : "s"} connected.` : "no devices connected; the box retries your network every 5 min."}</p>`
          : hs.state === "retry" ? html`<p class="net-text">Paused while the box retries your network.</p>`
          : html`<p class="muted net-text">${on ? "Opens after 5 min without network (60 s if Wi-Fi was never set up)." : "Off: the box cannot be reached when it loses its network."}</p>`}
        <div class="lan-facts net-facts hs-facts">
          <div><small>Network name</small><b class="mono">${hs.ssid || "—"}</b></div>
          <div><small>Password</small><span class="hs-pw"><b class="mono">${pw || "—"}</b></span></div>
          <div><small>Web GUI</small><b><a class="mono" href="${url}" target="_blank" rel="noopener">${url}</a></b></div>
        </div>
      </div>
      <div class="card-note">${icon("key", "i-sm")}<span>Every NetKey box uses this password for its rescue hotspot. The hotspot only opens when the box has no network, and only gives access to this web GUI.</span></div>`}
    </section>`;
  },
  render() {
    const n = this.net;
    if (!n) {
      patch(view, this.loadErr ? html`<div class="card">${emptyState("alert", "Could not load the network status", this.loadErr, html`<button type="button" class="btn" data-action="net-retry">Retry</button>`)}</div>`
        : html`<div class="stack"><div class="card card-pad"><div class="skel skel-line" style="width:35%"></div><div class="skel" style="height:80px;margin-top:12px"></div></div>
          <div class="card card-pad"><div class="skel skel-line" style="width:25%"></div><div class="skel" style="height:96px;margin-top:12px"></div></div></div>`);
      return;
    }
    patch(view, html`<div class="stack net-page">
      ${this.hero(n)}
      ${this.uplink(n)}
      ${this.wifiPanel(n)}
      <div class="grid grid-2 net-row">${this.ethCard(n)}${this.hotspotCard(n)}</div>
    </div>`);
    // patch() leaves a focused checkbox alone; force the switch to the (optimistic) truth
    const sw = $("#net-sw-hotspot", view);
    if (sw) sw.checked = this.hotspotValue();
  },
  openForm(sel) {
    const n = this.net || {};
    this.sel = sel;
    this.form = { ssid: "", password: "", country: (n.wifi && n.wifi.country) || this.form.country || "NO", show: false };
    this.formErr = "";
    this.render();
    requestAnimationFrame(() => {
      const el = $(sel === OTHER_NET ? "#wf-ssid" : "#wf-pw", view) || $("#wf-cc", view);
      if (el) el.focus({ preventScroll: true });
      const form = $(".wifi-form", view);
      if (form) form.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
  },
  actions: {
    "net-retry"() { this.loadErr = ""; this.render(); this.load(); },
    async "net-mode"(b) {
      const m = b.dataset.value, n = this.net || {};
      if (m === this.modeValue() || (this.opt.mode && this.opt.mode.saving)) { if (m !== "wifi") this.wantWifi = false; this.render(); return; }
      if (m === "wifi") {
        if (!wifiUp(n)) {
          this.wantWifi = true;
          this.render();
          const panel = $("#wifi-panel", view);
          if (panel) panel.scrollIntoView({ behavior: "smooth", block: "start" });
          const nets = ((n.scan && n.scan.networks) || []).filter((x) => x.ssid);
          if (!nets.length && !this.busy) this.scan();
          return;
        }
        const ok = await confirmDialog({
          title: "Use Wi-Fi as the uplink?", confirmText: "Use Wi-Fi",
          message: `The box goes online through ${n.wifi.ssid}${n.wifi.ip ? ` (address ${n.wifi.ip})` : ""}. The built-in Ethernet port then becomes the retro-LAN port when cabled to retro PCs, or a management port when cabled into your router.`,
        });
        if (ok && current === this) this.saveMode("wifi");
        return;
      }
      this.wantWifi = false;
      const ok = await confirmDialog({
        title: "Use Ethernet as the uplink?", confirmText: "Use Ethernet",
        message: "Cable the built-in Ethernet port to your router first, otherwise the box loses its network. Retro PCs on that port lose the retro LAN; Wi-Fi is no longer used as the uplink.",
      });
      if (ok && current === this) this.saveMode("ethernet");
    },
    "net-want-cancel"() { this.wantWifi = false; this.render(); },
    "wifi-scan"() { this.scan(); },
    "wifi-pick"(b) {
      const ssid = b.dataset.ssid;
      if (this.sel === ssid) { this.sel = null; this.render(); return; }
      this.openForm(ssid);
    },
    "wifi-other"() {
      if (this.sel === OTHER_NET) { this.sel = null; this.render(); return; }
      this.openForm(OTHER_NET);
    },
    "wifi-cancel"() { this.sel = null; this.formErr = ""; this.render(); },
    "wifi-field"(input) {
      this.form[input.name] = input.value;
      if (this.formErr) { this.formErr = ""; this.render(); }
    },
    "wifi-country"(select) { this.form.country = select.value; if (this.formErr) { this.formErr = ""; this.render(); } },
    "wifi-pw-toggle"() {
      this.form.show = !this.form.show;
      this.render();
      const pw = $("#wf-pw", view);
      if (pw) { pw.focus(); pw.setSelectionRange(pw.value.length, pw.value.length); }
    },
    "wifi-connect"() { this.connect(); },
    async "wifi-known-connect"(b) {
      // a saved network: no password needed; if that fails (password changed?), the form opens with the error
      const n = this.net || {}, ssid = b.dataset.ssid;
      this.sel = ssid;
      this.lost = "";
      const { res, err } = await this.cmd("wifi_connect", { ssid, password: "", country: (n.wifi && n.wifi.country) || this.form.country || "NO" });
      if (res) { toast((res.result && res.result.message) || `Connected to ${ssid}`, { detail: `${ssid} is now the preferred network.` }); this.sel = null; }
      else if (err.status === 0) this.lost = ssid;
      else if (err.status !== 401) { this.openForm(ssid); this.formErr = wifiError(err.message); }
      if (current === this) this.render();
    },
    async "wifi-forget"(b) {
      const n = this.net || {}, w = n.wifi || {}, ssid = (b && b.dataset.ssid) || w.ssid || "this network";
      const known = w.known || [], k = known.find((x) => x.ssid === ssid) || {}, others = known.filter((x) => x.ssid !== ssid);
      const uplink = this.modeValue() === "wifi" && k.connected;
      const ok = await confirmDialog({
        title: `Forget ${ssid}?`, confirmText: "Forget", danger: true,
        message: !uplink ? "The box will no longer join this network. You can connect again at any time."
          : others.some((x) => x.in_range) ? "The box is on this network now. It switches to another saved network in range within a few seconds."
          : "Wi-Fi is the uplink of this box and no other saved network is in range, so it goes offline unless the Ethernet port is cabled to your router. The rescue hotspot opens after a few minutes so you can set up Wi-Fi again.",
      });
      if (!ok || current !== this) return;
      const { res, err } = await this.cmd("wifi_forget", { ssid });
      if (res) toast((res.result && res.result.message) || "Wi-Fi network forgotten");
      else if (err.status !== 401) toastErr(err, "Could not forget the Wi-Fi network");
      if (current === this) this.render();
    },
    async "net-hotspot"(input) {
      const on = input.checked;
      if (!on) {
        input.checked = true;
        const ok = await confirmDialog({
          title: "Turn off the rescue hotspot?", confirmText: "Turn off", danger: true,
          message: "Without it, a box that loses its network can only be reached by cabling its Ethernet port to your router.",
        });
        if (!ok || current !== this) { if (current === this) this.render(); return; }
      }
      this.saveHotspot(on);
    },
  },
};

// page: users
const NAME_RE = /^[A-Za-z0-9._@-]{1,64}$/;
function checkPassword(pw) {
  if (!pw) return "Password is required.";
  if (pw.length > 64) return "At most 64 characters.";
  if (/["\\]/.test(pw)) return 'Must not contain " or \\.';
  if (/[\x00-\x1f\x7f]/.test(pw)) return "Only printable characters are allowed.";
  return "";
}
function genPassword(len = 12) {
  const abc = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const rnd = new Uint32Array(len);
  crypto.getRandomValues(rnd);
  return Array.from(rnd, (r) => abc[r % abc.length]).join("");
}
const userPath = (name) => "/api/users/" + encodeURIComponent(name).replace(/%40/g, "@");

const Users = {
  title: "Users",
  mount() {
    this.users = null;
    this.sync = null;
    this.q = this.q || "";
    this.revealed = new Set();
    this.busy = {};
    this.render();
    this.load();
  },
  unmount() { clearTimeout(this.timer); },
  onVisible() { this.load(); },
  update() {
    // the status poll carries users_sync too: pick up "applied" without an extra request
    const s = S.status && S.status.users_sync;
    if (s && this.sync && s.pending !== this.sync.pending) this.load();
  },
  async load() {
    clearTimeout(this.timer);
    try {
      const d = await api("/api/users");
      this.users = d.users;
      this.sync = d.sync;
      this.render();
    } catch (e) {
      if (e.status !== 401 && this.users == null) { this.users = []; this.error = e.message; this.render(); }
    }
    if (this.sync && this.sync.pending && current === this) this.timer = setTimeout(() => this.load(), 1200);
  },
  render() {
    const q = this.q.trim().toLowerCase();
    const list = (this.users || []).filter((u) => !q || u.name.toLowerCase().includes(q) || (u.note || "").toLowerCase().includes(q));
    const enabled = (this.users || []).filter((u) => u.enabled).length;
    const row = (u) => {
      const shown = this.revealed.has(u.name);
      return html`<tr data-key="u-${u.name}" class="${u.enabled ? "" : "is-disabled"}">
        <td><div class="cell-user">${avatar(u.name)}<div style="min-width:0"><b class="mono">${u.name}</b>${u.note ? html`<span class="sub ellipsis" style="max-width:260px" title="${u.note}">${u.note}</span>` : ""}</div></div></td>
        <td class="keep"><span class="pw"><code class="${shown ? "" : "masked"}">${shown ? u.password : "••••••••"}</code>
          <button type="button" class="btn btn-ghost btn-icon btn-sm" data-action="reveal" data-name="${u.name}" aria-label="${shown ? "Hide" : "Show"} password of ${u.name}" title="${shown ? "Hide" : "Show"} password">${icon(shown ? "eye-off" : "eye")}</button>
          <button type="button" class="btn btn-ghost btn-icon btn-sm" data-action="copy-pw" data-name="${u.name}" aria-label="Copy password of ${u.name}" title="Copy password">${icon("copy")}</button></span></td>
        <td class="keep"><label class="switch" title="${u.enabled ? "Enabled: can dial in" : "Disabled: login refused"}"><input type="checkbox" data-change="user-enabled" data-name="${u.name}" ${u.enabled ? raw("checked") : ""} ${this.busy[u.name] ? raw("disabled") : ""} aria-label="Enable ${u.name}"><span class="switch-track"></span></label></td>
        <td class="nowrap hide-sm">${relTime(u.last_login)}</td>
        <td class="r hide-sm">${fmtNum(u.calls)}</td>
        <td class="r nowrap hide-sm">${fmtBytes(u.bytes)}</td>
        <td class="keep"><div class="actions">
          <button type="button" class="btn btn-ghost btn-icon btn-sm" data-action="edit-user" data-name="${u.name}" aria-label="Edit ${u.name}" title="Edit">${icon("edit")}</button>
          <button type="button" class="btn btn-ghost btn-icon btn-sm" data-action="delete-user" data-name="${u.name}" aria-label="Delete ${u.name}" title="Delete">${icon("trash")}</button>
        </div></td></tr>`;
    };
    patch(view, html`<div class="stack" style="gap:16px">
      <div class="toolbar" style="margin:0">
        <label class="search"><span class="sr-only">Search users</span>${icon("search")}<input class="input" type="search" placeholder="Search users or notes…" value="${this.q}" data-input="user-search" autocomplete="off"></label>
        ${syncChip(this.sync)}
        <span class="spacer"></span>
        <button type="button" class="btn btn-primary" data-action="add-user">${icon("plus")}Add user</button>
      </div>
      ${this.users && this.users.length ? html`<div class="summary-line"><span><b>${this.users.length}</b> account${this.users.length === 1 ? "" : "s"}</span><span><b>${enabled}</b> enabled</span><span>Shared by both modem modes (PAP authentication)</span></div>` : ""}
      <section class="card">
        ${this.users == null ? html`<table class="table"><tbody>${skelRows(4, 5)}</tbody></table>`
          : this.error && !this.users.length ? emptyState("alert", "Could not load users", this.error, html`<button class="btn" data-action="reload-users">Retry</button>`)
          : !this.users.length ? emptyState("users", "No dial-in accounts yet", "Create an account, then enter the same user name and password in Windows Dial-Up Networking on the retro PC.", html`<button class="btn btn-primary" data-action="add-user">${icon("plus")}Add the first user</button>`)
          : !list.length ? emptyState("search", "No matching users", html`Nothing matches “${this.q}”.`, html`<button class="btn" data-action="clear-user-search">Clear search</button>`)
          : html`<div class="table-wrap"><table class="table users-table">
              <thead><tr><th>User</th><th>Password</th><th>Enabled</th><th class="hide-sm">Last login</th><th class="r hide-sm">Calls</th><th class="r hide-sm">Data</th><th class="r"><span class="sr-only">Actions</span></th></tr></thead>
              <tbody>${list.map(row)}</tbody></table></div>`}
      </section>
      <p class="faint" style="font-size:12.5px">${icon("info", "i-sm")} In Windows Dial-Up Networking use these credentials; any phone number works in Virtual Modem Mode. Passwords are stored in clear text because PAP needs them.</p>
    </div>`);
  },
  userForm(u) {
    const edit = !!u;
    return html`
      <label class="field"><span class="field-label">User name</span>
        <input class="input mono" name="name" value="${edit ? u.name : ""}" ${edit ? raw("readonly") : ""} maxlength="64" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="e.g. win95" aria-describedby="err-name">
        <span class="field-error" id="err-name">${edit ? "" : ""}</span>
        ${edit ? "" : html`<span class="field-help">Letters, digits and <code>. _ @ -</code> · up to 64 characters</span>`}</label>
      <label class="field"><span class="field-label">Password</span>
        <span class="input-wrap has-2"><input class="input mono" name="password" id="f-pw" type="${edit ? "password" : "text"}" maxlength="64" autocomplete="new-password" spellcheck="false" placeholder="${edit ? "Leave empty to keep the current password" : ""}" aria-describedby="err-pw">
          <span class="input-btns"><button type="button" data-action="toggle-pw" data-target="f-pw" aria-label="Show password" title="Show / hide">${icon("eye")}</button><button type="button" data-action="gen-pw" aria-label="Generate password" title="Generate a random password">${icon("dice")}</button></span></span>
        <span class="field-error" id="err-pw"></span></label>
      <label class="field"><span class="field-label">Note <span class="faint" style="font-weight:400">(optional)</span></span>
        <input class="input" name="note" value="${edit ? u.note : ""}" maxlength="200" placeholder="e.g. Win98 SE laptop" autocomplete="off"></label>
      <div class="switch-row"><span class="t"><b>Enabled</b><small>Disabled accounts are refused at login</small></span>
        <label class="switch"><input type="checkbox" name="enabled" ${!edit || u.enabled ? raw("checked") : ""} aria-label="Enabled"><span class="switch-track"></span></label></div>`;
  },
  openForm(u) {
    const edit = !!u;
    openModal({
      title: edit ? `Edit ${u.name}` : "Add dial-in user",
      iconName: edit ? "edit" : "users",
      body: this.userForm(u),
      foot: html`<button type="button" class="btn" data-close>Cancel</button><button type="submit" class="btn btn-primary">${edit ? "Save changes" : "Create user"}</button>`,
      initialFocus: edit ? '[name="password"]' : '[name="name"]',
      onSubmit: async (form) => {
        const f = form.elements;
        const name = f.name.value.trim(), pw = f.password.value, note = f.note.value.trim(), enabled = f.enabled.checked;
        const errName = edit ? "" : !name ? "User name is required." : !NAME_RE.test(name) ? "Only letters, digits and . _ @ - (max. 64)." : "";
        const errPw = edit && !pw ? "" : checkPassword(pw);
        $("#err-name", form).textContent = errName;
        $("#err-pw", form).textContent = errPw;
        f.name.setAttribute("aria-invalid", String(!!errName));
        f.password.setAttribute("aria-invalid", String(!!errPw));
        if (errName || errPw) { (errName ? f.name : f.password).focus(); return false; }
        try {
          if (edit) {
            const body = { enabled, note };
            if (pw) body.password = pw;
            await api(userPath(u.name), { method: "PUT", body });
            toast(`Saved ${u.name}`, { detail: "Applying to the dial-in server…" });
          } else {
            await api("/api/users", { method: "POST", body: { name, password: pw, enabled, note } });
            toast(`Created ${name}`, { detail: "Applying to the dial-in server…" });
          }
        } catch (e) {
          if (e.status === 409 || /name/i.test(e.message)) { $("#err-name", form).textContent = e.message; f.name.focus(); }
          else if (/password/i.test(e.message)) { $("#err-pw", form).textContent = e.message; f.password.focus(); }
          else toastErr(e);
          return false;
        }
        this.load();
        return true;
      },
    });
  },
  find(name) { return (this.users || []).find((u) => u.name === name); },
  actions: {
    "add-user"() { this.openForm(null); },
    "edit-user"(b) { const u = this.find(b.dataset.name); if (u) this.openForm(u); },
    "reload-users"() { this.error = null; this.users = null; this.render(); this.load(); },
    "clear-user-search"() { this.q = ""; this.render(); const i = $(".search input"); if (i) i.focus(); },
    reveal(b) {
      const n = b.dataset.name;
      if (this.revealed.has(n)) this.revealed.delete(n); else this.revealed.add(n);
      this.render();
    },
    async "copy-pw"(b) {
      const u = this.find(b.dataset.name);
      if (!u) return;
      if (await copyText(u.password)) toast(`Password of ${u.name} copied`, { timeout: 2500 });
      else toast("Copy failed: reveal the password and copy it manually", { type: "err" });
    },
    async "delete-user"(b) {
      const u = this.find(b.dataset.name);
      if (!u) return;
      const ok = await confirmDialog({ title: `Delete ${u.name}?`, danger: true, confirmText: "Delete user",
        message: html`The account <b class="mono">${u.name}</b> can no longer dial in. Its call history is kept. This cannot be undone.` });
      if (!ok) return;
      try {
        await api(userPath(u.name), { method: "DELETE" });
        this.users = this.users.filter((x) => x.name !== u.name);
        this.render();
        toast(`Deleted ${u.name}`, { detail: "Applying to the dial-in server…" });
      } catch (e) { toastErr(e, `Could not delete ${u.name}`); }
      this.load();
    },
    async "user-enabled"(input) {
      const u = this.find(input.dataset.name);
      if (!u) return;
      const enabled = input.checked;
      this.busy[u.name] = true;
      u.enabled = enabled ? 1 : 0;
      this.render();
      try {
        await api(userPath(u.name), { method: "PUT", body: { enabled } });
        toast(`${u.name} ${enabled ? "enabled" : "disabled"}`, { timeout: 2500 });
      } catch (e) {
        u.enabled = enabled ? 0 : 1;
        toastErr(e, `Could not update ${u.name}`);
      }
      delete this.busy[u.name];
      this.load();
    },
    "user-search"(input) { this.q = input.value; this.render(); },
  },
};

// page: calls
const Calls = {
  title: "Calls",
  f: { q: "", result: "", user: "", line: "" },
  mount() {
    this.calls = null;
    this.show = 150;
    this.render();
    this.load();
  },
  unmount() { clearTimeout(this.timer); },
  onVisible() { this.load(); },
  update() { if (this.calls && this.calls.some((c) => c.ended == null)) this.render(); },
  async load(manual) {
    clearTimeout(this.timer);
    this.loading = true;
    if (manual) this.render();
    try {
      this.calls = (await api("/api/calls?limit=500")).calls;
      this.error = null;
    } catch (e) {
      if (e.status === 401) return;
      if (this.calls == null) this.error = e.message;
      if (manual) toastErr(e, "Could not load calls");
    }
    this.loading = false;
    if (current === this) this.render();
    if (!document.hidden && current === this) this.timer = setTimeout(() => this.load(), 10000);
  },
  filtered() {
    const f = this.f, q = f.q.trim().toLowerCase();
    return (this.calls || []).filter((c) => {
      if (f.result && callKey(c) !== f.result) return false;
      if (f.user && (f.user === "__none__" ? c.user : c.user !== f.user)) return false;
      if (f.line !== "" && String(c.line) !== f.line) return false;
      if (q && ![c.user, c.ip, c.connect, c.detail, c.result, `line ${c.line}`, c.mode].some((x) => String(x || "").toLowerCase().includes(q))) return false;
      return true;
    });
  },
  render() {
    const all = this.calls || [];
    const list = this.filtered();
    const f = this.f;
    const uniq = (arr) => [...new Set(arr)];
    const results = uniq(all.map(callKey)).sort();
    const users = uniq(all.map((c) => c.user).filter(Boolean)).sort();
    const lines = uniq(all.map((c) => c.line)).sort((a, b) => a - b);
    const opt = (v, label, cur) => html`<option value="${v}" ${String(cur) === String(v) ? raw("selected") : ""}>${label}</option>`;
    const okN = list.filter((c) => c.result === "ok" || callKey(c) === "ongoing").length;
    const failN = list.length - okN;
    const bytes = list.reduce((a, c) => a + (c.rx_bytes || 0) + (c.tx_bytes || 0), 0);
    const secs = list.reduce((a, c) => a + (c.duration_s || 0), 0);
    const anyFilter = f.q || f.result || f.user || f.line !== "";
    const row = (c) => {
      const live = liveCall(c);
      const rx = live ? live.rx_bytes : c.rx_bytes, tx = live ? live.tx_bytes : c.tx_bytes;
      const showDetail = c.detail && c.result !== "ok";
      return html`<tr data-key="c-${c.id}">
      <td class="nowrap" title="${fmtAbs(c.started)}">${fmtWhen(c.started)}<span class="sub">${fmtRel(c.started)}</span></td>
      <td class="nowrap"><span class="mono">${c.line}</span> ${modeBadge(c.mode)}</td>
      <td>${c.mode === "call" ? html`<span>${String(c.detail || "Modem call").split(" · ")[0]}</span>` : c.user ? html`<span class="mono">${c.user}</span>` : html`<span class="faint">—</span>`}${c.ip ? html`<span class="sub mono">${c.ip}</span>` : ""}</td>
      <td>${resultPill(c)}${showDetail && c.mode !== "call" ? html`<span class="sub ellipsis" style="max-width:220px;margin-top:2px" title="${c.detail}">${c.detail}</span>` : ""}</td>
      <td class="r nowrap">${c.ended == null ? ticking(c.started) : fmtDur(c.duration_s)}</td>
      <td class="r"><span class="data-split"><span class="c-rx" title="Down">${icon("down")}${fmtBytes(rx)}</span><span class="c-tx" title="Up">${icon("up")}${fmtBytes(tx)}</span></span></td>
      <td class="mono hide-md" style="font-size:12px" title="${c.detail || ""}">${c.connect || "—"}</td>
    </tr>`;
    };
    patch(view, html`<div class="stack" style="gap:16px">
      <div class="toolbar" style="margin:0">
        <label class="search"><span class="sr-only">Search calls</span>${icon("search")}<input class="input" type="search" placeholder="Search user, IP, connect string…" value="${f.q}" data-input="calls-q" autocomplete="off"></label>
        <select class="select" data-change="calls-filter" data-field="result" data-value="${f.result}" aria-label="Filter by result">${opt("", "All results", f.result)}${results.map((r) => opt(r, (RESULTS[r] || { label: humanize(r) }).label, f.result))}</select>
        <select class="select" data-change="calls-filter" data-field="user" data-value="${f.user}" aria-label="Filter by user">${opt("", "All users", f.user)}${users.map((u) => opt(u, u, f.user))}${opt("__none__", "(no user)", f.user)}</select>
        <select class="select" data-change="calls-filter" data-field="line" data-value="${f.line}" aria-label="Filter by line">${opt("", "All lines", f.line)}${lines.map((l) => opt(String(l), `Line ${l}`, f.line))}</select>
        ${anyFilter ? html`<button type="button" class="btn btn-ghost btn-sm" data-action="calls-clear">${icon("x", "i-sm")}Clear</button>` : ""}
        <span class="spacer"></span>
        <button type="button" class="btn btn-ghost btn-icon hide-sm" data-action="calls-refresh" aria-label="Refresh" title="Refresh">${this.loading ? spinner() : icon("reset")}</button>
      </div>
      ${this.calls ? html`<div class="summary-line" aria-live="polite">
        <span><b>${fmtNum(list.length)}</b> call${list.length === 1 ? "" : "s"}${anyFilter ? html` of ${fmtNum(all.length)}` : ""}</span>
        <span><b>${fmtNum(okN)}</b> successful</span>
        <span><b style="${failN ? "color:var(--red)" : ""}">${fmtNum(failN)}</b> failed</span>
        <span><b>${fmtDur(secs)}</b> online</span>
        <span><b>${fmtBytes(bytes)}</b> transferred</span></div>` : ""}
      <section class="card">
        ${this.calls == null ? (this.error ? emptyState("alert", "Could not load calls", this.error, html`<button class="btn" data-action="calls-refresh">Retry</button>`) : html`<table class="table"><tbody>${skelRows(8, 6)}</tbody></table>`)
          : !all.length ? emptyState("phone", "No calls yet", "Every dial-in attempt: successful or not, is listed here.")
          : !list.length ? emptyState("search", "No matching calls", "Try a different filter.", html`<button class="btn" data-action="calls-clear">Clear filters</button>`)
          : html`<div class="table-wrap"><table class="table calls-table">
              <thead><tr><th>Started</th><th>Line</th><th>User</th><th>Result</th><th class="r">Duration</th><th class="r">Data</th><th class="hide-md">Connect</th></tr></thead>
              <tbody>${list.slice(0, this.show).map(row)}</tbody></table></div>
              ${list.length > this.show ? html`<div class="show-more"><button type="button" class="btn" data-action="calls-more">Show more (${fmtNum(list.length - this.show)} remaining)</button></div>` : ""}`}
      </section>
    </div>`);
  },
  actions: {
    "calls-q": debounce(function (input) { Calls.f.q = input.value; Calls.show = 150; Calls.render(); }, 120),
    "calls-filter"(sel) { this.f[sel.dataset.field] = sel.value; this.show = 150; this.render(); },
    "calls-clear"() { this.f = { q: "", result: "", user: "", line: "" }; this.render(); },
    "calls-refresh"() { this.load(true); },
    "calls-more"() { this.show += 200; this.render(); },
  },
};

// page: settings
const GROUPS = [
  { id: "box", title: "This box", icon: "key", mode: null, sub: "How other boxes see this one in LAN parties", match: (s) => s.group === "box" },
  { id: "virtual", title: "Virtual modem", icon: "pc", mode: "virtual", sub: "Hayes emulation and the serial cable to the PC", match: (s) => s.group === "modem" || s.group === "serial" },
  { id: "physical", title: "Physical modem", icon: "modem", mode: "physical", sub: "A real modem that answers incoming phone calls", match: (s) => s.group === "physical" },
  { id: "log", title: "Logging", icon: "file", mode: null, sub: "Journal output of the modem lines", match: (s) => s.group === "log" },
];
const CHOICE_LABELS = { virtual: "Virtual", physical: "Physical", always_on: "Always on", follow: "Follow call", dsr: "DSR", dcd: "DCD", none: "Not wired" };
const choiceLabel = (c) => (c in CHOICE_LABELS ? CHOICE_LABELS[c] : String(c));
const same = (a, b) => a === b || (typeof a === "number" && typeof b === "number" && Math.abs(a - b) < 1e-9);
const showVal = (s, v) => (s.type === "bool" ? (v ? "on" : "off") : s.type === "choice" ? choiceLabel(v) : v === "" && s.placeholder ? `${s.placeholder} (hostname)` : String(v));

// update notice: the box asks the cloud hub a few times a day which release is current (see the Settings card)
const UPDATE_KEY = "netkey-update-later";
const UPDATE_RUNNING = ["queued", "checking", "downloading", "installing", "verifying", "rolling_back"];
function updateSeen() { try { return localStorage.getItem("netkey-update-seen") || ""; } catch { return ""; } }
const UPDATE_STEPS = [["checking", "Checking"], ["downloading", "Downloading"], ["installing", "Installing"], ["verifying", "Checking it runs"], ["done", "Done"]];
// click-to-update: the admin password again, then the root updater does the work and this dialog follows it
function updateNowDialog(u) {
  const st = S.status || {}, p = partyState() || {};
  const busy = p.role && p.role !== "standalone" ? "This box is in a LAN party: it leaves the party while services restart."
    : st.summary && st.summary.busy ? "A modem line is in a call: the call drops." : "";
  openModal({
    title: `Update to NetKey ${u.latest}`, iconName: "download", wide: true,
    body: html`<p>The box downloads NetKey ${u.latest} and installs it over ${u.current}. <b>Settings, users, files and history are kept.</b> Its services restart, so this takes a few minutes (longer on a Pi 1) and you'll be asked to sign in again afterwards.</p>
      <p class="faint" style="font-size:12.5px">If the new version doesn't start properly, the box puts ${u.current} back by itself.</p>
      ${busy ? html`<label class="check-row"><input type="checkbox" name="force"><span><b>Update anyway.</b> ${busy}</span></label>` : ""}
      <label class="field"><span class="field-label">Admin password</span><input class="input" type="password" name="password" autocomplete="current-password" required></label>
      ${u.notes ? html`<h4 class="upd-h">What's new in ${u.latest}</h4><pre class="upd-notes">${u.notes}</pre>` : ""}`,
    foot: html`<button type="button" class="btn" data-close>Cancel</button><button type="submit" class="btn btn-primary">Update now</button>`,
    initialFocus: 'input[name="password"]',
    onSubmit: async (form) => {
      if (busy && !form.force.checked) { toast(busy.split(":")[0], { type: "err", detail: "Tick “Update anyway”, or update after the party or call." }); return false; }
      await api("/api/update/install", { method: "POST", body: { version: u.latest, password: form.password.value, force: !!(form.force && form.force.checked) } });
      setTimeout(() => updateProgressDialog(), 50);
    },
  });
}
function updateProgressDialog() {
  let timer = 0, lost = 0, done = false;
  const m = openModal({
    title: "Updating NetKey", iconName: "download", wide: true,
    body: html`<div class="upd-prog" aria-live="polite"><p class="muted">Starting…</p></div>`,
    foot: html`<span class="spacer"></span><button type="button" class="btn" data-close>Close</button>`,
    onClose: () => clearTimeout(timer),
  });
  const box = m.el.querySelector(".upd-prog");
  const show = (job, extra = "") => {
    const order = UPDATE_STEPS.map((x) => x[0]), at = job.state === "rolling_back" || job.state === "rolled_back" ? order.indexOf("verifying") : order.indexOf(job.state);
    const steps = UPDATE_STEPS.map(([k, label], i) => html`<li class="${i < at || job.state === "done" ? "is-done" : i === at ? "is-now" : ""}">${i < at || job.state === "done" ? icon("check", "i-sm") : i === at && !["failed", "rolled_back"].includes(job.state) ? spinner() : html`<span class="dot"></span>`}${label}</li>`);
    const end = job.state === "done" ? html`<div class="banner" role="status">${icon("check")}<span><b>Updated to NetKey ${job.target}.</b> Reload to use the new version.</span><button type="button" class="btn btn-sm btn-primary" data-action="reload-page">Reload</button></div>`
      : job.state === "rolled_back" ? html`<div class="banner banner-warn" role="alert">${icon("alert")}<span><b>The update failed; NetKey ${job.from} was put back.</b> ${job.message || ""}</span></div>`
      : job.state === "failed" ? html`<div class="banner banner-err" role="alert">${icon("alert")}<span><b>Not updated.</b> ${job.message || ""}</span></div>` : "";
    box.innerHTML = String(html`<ol class="upd-steps">${steps}</ol>${end}${extra}
      ${job.message && !end ? html`<p class="muted">${job.message}</p>` : ""}
      ${(job.log || []).length ? html`<details ${["failed", "rolled_back"].includes(job.state) ? raw("open") : ""}><summary>Details</summary><pre class="upd-notes upd-log">${(job.log || []).join("\n")}</pre></details>` : ""}`);
  };
  let last = { state: "queued" };
  const poll = async () => {
    try {
      const r = await fetch("/api/update/progress", { credentials: "same-origin", cache: "no-store" });
      if (r.status === 401) {          // the web service restarted: sessions are gone
        show(last, String(html`<div class="banner" role="status">${icon("lock")}<span><b>The box restarted its services and signed you out.</b> Sign in again: the Dashboard shows how the update ended.</span><button type="button" class="btn btn-sm btn-primary" data-action="reload-page">Sign in</button></div>`));
        return;
      }
      const d = await r.json();
      last = d.job; lost = 0;
      show(d.job);
      done = ["done", "failed", "rolled_back"].includes(d.job.state);
      if (done) { refreshStatus(); return; }
    } catch {
      lost++;
      show(last, String(html`<p class="muted">${spinner()} The box is restarting its services; reconnecting…</p>`));
    }
    timer = setTimeout(poll, lost ? 4000 : 2000);
  };
  poll();
}
function updateDismissed() { try { return localStorage.getItem(UPDATE_KEY) || ""; } catch { return ""; } }
function updateDialog(u) {
  openModal({
    title: u.available ? `Update to NetKey ${u.latest}` : "Updating NetKey", iconName: "download", wide: true,
    body: html`<p>Run this on the Pi (over SSH or with a keyboard and screen). It downloads the latest release and installs it over this one. <b>Settings, users, nickname and history are kept.</b></p>
      <div class="upd-cmd"><code class="mono">${u.command}</code><button type="button" class="btn btn-sm" data-action="update-copy">${icon("copy")}Copy</button></div>
      <p class="faint" style="font-size:12.5px">The services restart during the update, so modem calls drop and the box leaves its LAN party for a minute. Do it between parties. Then reload this page.</p>
      ${u.notes ? html`<h4 class="upd-h">What's new${u.latest ? ` in ${u.latest}` : ""}</h4><pre class="upd-notes">${u.notes}</pre>` : ""}`,
    foot: html`${u.url ? html`<a class="btn btn-ghost" href="${u.url}" target="_blank" rel="noopener noreferrer">${icon("external")}Release page</a>` : ""}<span class="spacer"></span><button type="button" class="btn btn-primary" data-close>Done</button>`,
  });
}
function softwareCard(u, busy) {
  const cur = (u && u.current) || (S.status && S.status.version) || "—";
  const state = !u ? html`<span class="pill pill-gray">Not checked yet</span>`
    : u.required ? html`<span class="pill pill-amber">Update required</span>`
    : u.available ? html`<span class="pill pill-blue">Update available</span>`
    : u.latest ? html`<span class="pill pill-green">Up to date</span>` : html`<span class="pill pill-gray no-dot">Unknown</span>`;
  return html`<section class="card" aria-labelledby="h-sw" data-key="sw-card">
    <div class="card-head">${icon("download")}<h3 id="h-sw">Software</h3><span class="sub">The box asks the cloud hub for new releases a few times a day. Updating is up to you.</span><span class="spacer"></span>${state}</div>
    <div class="readonly-grid">
      <div><small>This box</small><b class="mono">${cur}</b></div>
      <div><small>Latest release</small><b class="mono">${(u && u.latest) || "—"}</b></div>
      <div><small>Last checked</small><b>${u && u.checked ? relTime(u.checked) : "—"}</b></div>
    </div>
    ${u && u.error ? html`<p class="card-note">${icon("info", "i-sm")}${u.error}</p>`
      : u && !u.latest ? html`<p class="card-note">${icon("info", "i-sm")}The cloud hub hasn't found a NetKey release on GitHub yet.</p>` : ""}
    <div class="card-pad row wrap" style="gap:8px;padding-top:4px">
      <button type="button" class="btn ${busy ? "is-busy" : ""}" data-action="update-check" ${busy ? raw("disabled") : ""}>${icon("reset")}Check now</button>
      ${u && u.available ? html`<button type="button" class="btn btn-primary" data-action="update-now">${icon("download")}Update to ${u.latest}</button>` : ""}
      <button type="button" class="btn ${u && u.available ? "btn-ghost" : ""}" data-action="update-howto">${u && u.available ? "Update manually" : "How to update"}</button>
      <button type="button" class="btn btn-ghost" data-action="update-history">Update history</button>
    </div>
  </section>`;
}

const Settings = {
  title: "Settings",
  mount() {
    this.cfg = null;
    this.draft = {};
    this.errors = {};
    this.open = {};
    this.saving = false;
    this.render();
    this.load();
  },
  async load() {
    try {
      this.apply(await api("/api/settings"));
    } catch (e) {
      if (e.status !== 401) { this.loadError = e.message; this.render(); }
    }
  },
  apply(cfg) {
    this.cfg = cfg;
    this.draft = { ...cfg.values };
    this.errors = {};
    const mode = cfg.values["lines.default_mode"];
    GROUPS.forEach((g) => { if (!(g.id in this.open)) this.open[g.id] = !g.mode || g.mode === mode; });
    this.render();
  },
  dirtyKeys() {
    if (!this.cfg) return [];
    return this.cfg.schema.map((s) => s.key).filter((k) => !same(this.draft[k], this.cfg.values[k]));
  },
  async canLeave() {
    if (!this.dirtyKeys().length) return true;
    return confirmDialog({ title: "Discard unsaved changes?", message: "You have changed settings that are not saved yet.", confirmText: "Discard changes", danger: true });
  },
  control(s) {
    const v = this.draft[s.key];
    const err = this.errors[s.key];
    const attrs = `data-key="${esc(s.key)}" id="set-${esc(s.key)}"`;
    if (s.type === "bool") {
      return html`<label class="switch"><input type="checkbox" ${raw(attrs)} data-change="setting" ${v ? raw("checked") : ""}><span class="switch-track"></span><span class="sr-only">${s.label}</span></label>`;
    }
    if (s.type === "choice") {
      const idx = s.choices.findIndex((c) => same(c, v) || String(c) === String(v));
      if (s.choices.length <= 3) {
        return segmented(s.choices.map((c, i) => ({ value: i, label: choiceLabel(c) })), idx, `data-action="setting-seg" data-key="${esc(s.key)}"`, "", s.label);
      }
      return html`<select class="select" ${raw(attrs)} data-change="setting" data-value="${idx}">${s.choices.map((c, i) => html`<option value="${i}" ${i === idx ? raw("selected") : ""}>${choiceLabel(c)}</option>`)}</select>`;
    }
    if (s.type === "int" || s.type === "number") {
      return html`<input class="input tnum" type="number" inputmode="${s.type === "int" ? "numeric" : "decimal"}" ${raw(attrs)} data-input="setting" value="${v}" ${s.min != null ? raw(`min="${s.min}"`) : ""} ${s.max != null ? raw(`max="${s.max}"`) : ""} step="${s.type === "int" ? 1 : "any"}" aria-invalid="${String(!!err)}">`;
    }
    return html`<input class="input ${s.key === "box.name" ? "" : "mono"}" type="text" ${raw(attrs)} data-input="setting" value="${v}" maxlength="${s.maxlength || 120}" ${s.placeholder ? raw(`placeholder="${esc(s.placeholder)}"`) : ""} spellcheck="false" autocomplete="off" aria-invalid="${String(!!err)}">`;
  },
  field(s) {
    const cfg = this.cfg, k = s.key;
    const mod = !same(this.draft[k], cfg.defaults[k]);
    const dirty = !same(this.draft[k], cfg.values[k]);
    const err = this.errors[k];
    return html`<div class="setting ${mod ? "is-mod" : ""}" data-key="f-${k}">
      <div><label class="s-label" for="set-${k}">${s.label}${mod ? html`<span class="dot dot-mod" title="Changed from the default"></span>` : ""}${dirty ? html`<span class="pill pill-amber no-dot" style="height:18px;font-size:10.5px">unsaved</span>` : ""}</label>
        <div class="s-help">${s.help} ${s.min != null && s.max != null ? html`<span class="faint">(${s.min}–${s.max})</span>` : ""}</div>
        <div class="s-key">${k}</div></div>
      <div class="s-ctl">${this.control(s)}
        ${err ? html`<span class="field-error">${err}</span>` : ""}
        ${mod ? html`<span class="s-meta">Default: <code>${showVal(s, cfg.defaults[k])}</code><button type="button" class="btn-link" data-action="setting-reset" data-key="${k}">${icon("reset", "i-sm")}Reset</button></span>` : ""}
      </div></div>`;
  },
  render() {
    const cfg = this.cfg;
    if (!cfg) {
      patch(view, this.loadError ? html`<div class="card">${emptyState("alert", "Could not load settings", this.loadError, html`<button class="btn" data-action="settings-retry">Retry</button>`)}</div>`
        : html`<div class="stack"><div class="mode-pick">${[1, 2].map(() => html`<div class="card" style="height:96px"><div class="skel" style="height:100%"></div></div>`)}</div>${[1, 2].map(() => html`<div class="card card-pad"><div class="skel skel-line" style="width:30%"></div><div class="skel skel-line"></div><div class="skel skel-line" style="width:70%"></div></div>`)}</div>`);
      return;
    }
    const mode = this.draft["lines.default_mode"];
    const overrides = Object.entries(cfg.line_modes || {}).filter(([, m]) => m);
    const ro = cfg.readonly || {};
    const dirty = this.dirtyKeys();
    const hasErr = Object.keys(this.errors).length > 0;
    const groupHtml = (g) => {
      const items = cfg.schema.filter((s) => s.group !== "mode" && g.match(s));
      if (!items.length) return "";
      const inactive = g.mode && g.mode !== mode;
      const users = overrides.filter(([, m]) => m === g.mode).map(([n]) => `Line ${n}`);
      const nMod = items.filter((s) => !same(this.draft[s.key], cfg.defaults[s.key])).length;
      const nErr = items.filter((s) => this.errors[s.key]).length;
      return html`<details class="card group ${inactive ? "inactive" : ""}" data-group="${g.id}" ${this.open[g.id] ? raw("open") : ""}>
        <summary><span class="g-icon">${icon(g.icon)}</span><span style="display:grid;min-width:0"><h3>${g.title} ${g.mode ? modeBadge(g.mode) : ""}</h3>
          <span class="sub">${inactive ? (users.length ? `Not the default: used by ${users.join(", ")} (per-line override)` : "Not the default mode, only used by lines overridden to this mode") : g.sub}</span></span>
          <span class="spacer"></span>${nErr ? html`<span class="pill pill-red">${nErr} invalid</span>` : ""}${nMod ? html`<span class="pill pill-amber no-dot hide-sm" title="Settings that differ from the config.toml default">${nMod} modified</span>` : ""}${g.mode && !inactive ? html`<span class="pill pill-green hide-sm">Active</span>` : ""}${icon("chevron", "chev")}</summary>
        <div class="fields">${items.map((s) => this.field(s))}</div></details>`;
    };
    // the "lan" and "network" groups are managed on the LAN and Uplink pages (immediate-save switches), not in the generic form
    const known = new Set(cfg.schema.filter((s) => s.group === "mode" || s.group === "lan" || s.group === "network" || s.group === "share" || s.group === "phone" || GROUPS.some((g) => g.match(s))).map((s) => s.key));
    const other = cfg.schema.filter((s) => !known.has(s.key));
    const hasLan = cfg.schema.some((s) => s.group === "lan");
    const hasNet = cfg.schema.some((s) => s.group === "network");
    patch(view, html`<div class="stack">
      <label class="search find-setting">${icon("search")}<input class="input" type="search" data-input="settings-find" placeholder="Find a setting, e.g. CONNECT, box name, log" autocomplete="off" aria-label="Find a setting"></label>
      <a class="card lan-link" href="#/modem">${icon("modem")}<span><b>Default modem mode and per-line modes are on the Modem page</b><small>Currently ${MODE_NAME[mode] || mode} Modem Mode${overrides.length ? ` · ${overrides.length} line${overrides.length === 1 ? "" : "s"} switched individually` : ""}. The options below fine-tune each mode.</small></span>${icon("chevron", "chev")}</a>
      <a class="card lan-link" href="#/parties">${icon("users")}<span><b>Your public nickname and the public parties are on the Parties page</b><small>Choose a nickname there to join, chat and host public parties. Private parties never need one.</small></span>${icon("chevron", "chev")}</a>
      ${GROUPS.map(groupHtml)}
      ${other.length ? html`<section class="card"><div class="card-head"><h3>Other</h3></div><div class="fields">${other.map((s) => this.field(s))}</div></section>` : ""}
      <a class="card lan-link" href="#/smb">${icon("folder")}<span><b>File sharing settings are on the Fileshare pages</b><small>Windows file sharing (workgroup), Mac file sharing, FTP and the web server each have their own page under Fileshare.</small></span>${icon("chevron", "chev")}</a>
      ${hasLan ? html`<a class="card lan-link" href="#/lan">${icon("ethernet")}<span><b>LAN port settings are on the LAN page</b><small>Retro-LAN port on/off, internet sharing and the LAN party (incl. auto-approve) apply immediately there.</small></span>${icon("chevron", "chev")}</a>` : ""}
      ${hasNet ? html`<a class="card lan-link" href="#/network">${icon("wifi")}<span><b>Internet connection, Wi-Fi and rescue hotspot are on the Internet page</b><small>Choose Ethernet or Wi-Fi, join a Wi-Fi network and manage the rescue hotspot there.</small></span>${icon("chevron", "chev")}</a>` : ""}
      <section class="card" aria-labelledby="h-net">
        <div class="card-head">${icon("network")}<h3 id="h-net">Dial-in network</h3><span class="sub">PPP addresses for dial-in PCs · read-only, set in config.toml</span></div>
        <div class="readonly-grid">
          <div><small>PPP server IP</small><b>${ro["ppp.server_ip"] || "—"}</b></div>
          <div><small>First client IP</small><b>${ro["ppp.first_client_ip"] || "—"}</b></div>
          <div><small>Max. lines</small><b>${ro["lines.max"] ?? "—"}</b></div>
          <div><small>Web port</small><b>${ro["web.port"] ?? "—"}</b></div>
        </div>
      </section>
      ${softwareCard(S.status && S.status.update, this.checking)}
      <section class="card" aria-labelledby="h-pw">
        <div class="card-head">${icon("lock")}<h3 id="h-pw">Admin login</h3><span class="sub">Username and password for this web interface (password min. 6 characters)</span></div>
        <form class="pw-form" id="pw-form" novalidate autocomplete="off">
          <label class="field"><span class="field-label">Username</span><input class="input" type="text" name="username" autocomplete="username" autocapitalize="none" spellcheck="false" maxlength="32" value="${adminUser()}" required></label>
          <label class="field"><span class="field-label">Current password</span><input class="input" type="password" name="current" autocomplete="current-password" required></label>
          <label class="field"><span class="field-label">New password</span><input class="input" type="password" name="new" autocomplete="new-password" minlength="6" placeholder="Leave empty to keep"></label>
          <label class="field"><span class="field-label">Repeat new password</span><input class="input" type="password" name="repeat" autocomplete="new-password" minlength="6"></label>
          <button class="btn btn-primary" type="submit" style="height:36px">Save login</button>
          <p class="field-error" id="pw-err" role="alert" style="grid-column:1/-1;margin-top:-4px"></p>
        </form>
      </section>
      <section class="card card-pad row wrap" style="justify-content:space-between">
        <div><b>Session</b><div class="faint" style="font-size:12.5px">Signed in as ${adminUser()} on ${(S.session && S.session.hostname) || "this gateway"}</div></div>
        <button type="button" class="btn" data-action="logout">${icon("logout")}Sign out</button>
      </section>
      ${dirty.length ? html`<div class="savebar" role="region" aria-label="Unsaved changes">
        <span class="msg"><span class="dot dot-mod"></span>${dirty.length} unsaved change${dirty.length === 1 ? "" : "s"}<span class="faint hide-sm">&nbsp;· <kbd>${/Mac|iP/.test(navigator.platform) ? "⌘" : "Ctrl"} S</kbd></span></span>
        <button type="button" class="btn btn-ghost" data-action="settings-discard" ${this.saving ? raw("disabled") : ""}>Discard</button>
        <button type="button" class="btn btn-primary" data-action="settings-save" ${this.saving || hasErr ? raw("disabled") : ""}>${this.saving ? spinner() : icon("check")}Save</button>
      </div>` : ""}
    </div>`);
  },
  setDraft(key, value, error) {
    this.draft[key] = value;
    if (error) this.errors[key] = error; else delete this.errors[key];
    this.render();
  },
  async save() {
    const dirty = this.dirtyKeys();
    if (!dirty.length || this.saving || Object.keys(this.errors).length) return;
    const values = {};
    dirty.forEach((k) => {
      let v = this.draft[k];
      if (typeof v === "string") v = v.trim();
      values[k] = same(v, this.cfg.defaults[k]) ? null : v;
    });
    this.saving = true;
    this.render();
    try {
      const cfg = await api("/api/settings", { method: "PUT", body: { values } });
      this.saving = false;
      this.apply(cfg);
      toast(`Saved ${dirty.length} setting${dirty.length === 1 ? "" : "s"}`, { detail: "Idle lines apply them within a few seconds; lines in a call restart after hang-up." });
      refreshStatus();
    } catch (e) {
      this.saving = false;
      this.render();
      const s = this.cfg.schema.find((x) => e.message.startsWith(x.label + ":"));
      if (s) { this.errors[s.key] = e.message.slice(s.label.length + 1).trim(); this.render(); }
      toastErr(e, "Settings not saved");
    }
  },
  onSave() { this.save(); },
  actions: {
    "settings-retry"() { this.loadError = null; this.render(); this.load(); },
    "settings-find"(input) {
      // filters the rows already on the page; matching groups open so the hit is visible
      const q = input.value.trim().toLowerCase();
      document.querySelectorAll("#view .setting").forEach((r) => { r.hidden = !!q && !r.textContent.toLowerCase().includes(q); });
      document.querySelectorAll("#view details.group").forEach((g) => { g.hidden = !!q && !g.querySelector(".setting:not([hidden])"); if (q && !g.hidden) g.open = true; });
    },
    "settings-save"() { this.save(); },
    "settings-discard"() { this.draft = { ...this.cfg.values }; this.errors = {}; this.render(); toast("Changes discarded", { type: "info", timeout: 2000 }); },
    "setting-seg"(b) {
      const s = this.cfg.schema.find((x) => x.key === b.dataset.key);
      this.setDraft(s.key, s.choices[+b.dataset.value]);
    },
    "setting-reset"(b) {
      const k = b.dataset.key;
      this.setDraft(k, this.cfg.defaults[k]);
    },
    setting(input) {
      const s = this.cfg.schema.find((x) => x.key === input.dataset.key);
      if (!s) return;
      if (s.type === "bool") return this.setDraft(s.key, input.checked);
      if (s.type === "choice") return this.setDraft(s.key, s.choices[+input.value]);
      if (s.type === "int" || s.type === "number") {
        const str = input.value.trim();
        const n = Number(str);
        let err = "";
        if (str === "" || !isFinite(n)) err = "Enter a number.";
        else if (s.type === "int" && !Number.isInteger(n)) err = "Enter a whole number.";
        else if ((s.min != null && n < s.min) || (s.max != null && n > s.max)) err = `Must be between ${s.min} and ${s.max}.`;
        return this.setDraft(s.key, err ? str : n, err);
      }
      const v = input.value;
      let err = "";
      if (!v.trim()) err = s.optional ? "" : "Required.";
      else if (s.key === "box.name" && !/^[\p{L}\p{N} ._-]{1,32}$/u.test(v.trim())) err = "Up to 32 letters, digits, spaces and - _ .";
      else if (/[\x00-\x1f\x7f]/.test(v)) err = "Only printable characters.";
      else if (s.key === "physical.init_string" && !/^at/i.test(v.trim())) err = "Must start with AT.";
      return this.setDraft(s.key, v, err);
    },
  },
  async submitPassword(form) {
    const f = form.elements, err = $("#pw-err");
    err.textContent = "";
    const user = f.username.value.trim();
    if (!user || /\s/.test(user)) { err.textContent = "Enter a username without spaces."; f.username.focus(); return; }
    if (!f.current.value) { err.textContent = "Enter the current password."; f.current.focus(); return; }
    if (f.new.value && f.new.value.length < 6) { err.textContent = "The new password needs at least 6 characters."; f.new.focus(); return; }
    if (f.new.value !== f.repeat.value) { err.textContent = "The new passwords do not match."; f.repeat.focus(); return; }
    if (!f.new.value && user === adminUser()) { err.textContent = "Change the username or enter a new password."; f.new.focus(); return; }
    const btn = form.querySelector('[type="submit"]');
    btn.disabled = true;
    try {
      const r = await api("/api/password", { method: "PUT", body: { current: f.current.value, username: user, new: f.new.value } });
      if (S.status) S.status.admin = { username: r.username, default_credentials: r.default_credentials };
      form.reset();
      f.username.value = r.username;
      renderChrome();
      toast("Admin login updated");
    } catch (e) {
      err.textContent = e.message;
      if (e.status === 403) { f.current.select(); f.current.focus(); }
    }
    btn.disabled = false;
  },
};

// router
const view = $("#view");
// two PCs on the retro LAN answering for one IP address (netkey-lan's scan); usually fixes itself within minutes
function ipClashBanner(lan) {
  const list = (lan && lan.ip_conflicts) || [];
  if (!list.length) return "";
  const pc = (p) => (p.name ? html`<b class="mono">${p.name}</b> <span class="faint mono">${p.mac}</span>` : html`<span class="mono">${p.mac}</span>`);
  return html`<div class="banner banner-warn" role="status" data-key="ip-clash">${icon("alert")}<span>
    <b>${list.length === 1 ? "Two PCs use the same IP address" : `${list.length} IP addresses are used by two PCs`}</b>
    ${list.map((c) => html`<br><span class="mono">${c.ip}</span>: ${c.pcs.map((p, i) => html`${i ? " and " : ""}${pc(p)}`)}`)}
    <br><span class="muted">TCP/IP between them fails (IPX and NetBEUI games are fine). It fixes itself within about 5 minutes, when the PCs renew their address. To fix it now, on one of them: Windows 95, 98, ME: Start › Run › <code>winipcfg</code> › Release, then Renew. Windows XP: <code>ipconfig /release</code>, then <code>ipconfig /renew</code>. Or restart the PC.</span></span></div>`;
}

// ---- file sharing: one folder for the retro LAN, over Windows and Mac sharing, FTP and the web (fileshare.py)
const shareState = () => (S.status && S.status.lan && S.status.lan.fileshare) || null;
const SHARE_SVC = {
  smb: { route: "smb", title: "Windows file sharing", short: "Windows", ic: "network", key: "share.smb" },
  afp: { route: "mac", title: "Mac file sharing", short: "Mac share", ic: "mac", key: "share.afp" },
  ftp: { route: "ftp", title: "FTP server", short: "FTP", ic: "swap", key: "share.ftp" },
  web: { route: "web", title: "Web server", short: "Web", ic: "globe", key: "share.web" },
};
const shareAddr = (fsx, k) => {
  const a = fsx && fsx.address ? fsx.address : "10.99.0.1", n = (fsx && fsx.name) || "NETKEY";
  return k === "smb" ? `\\\\${n}\\${(fsx && fsx.share) || "FILES"}` : k === "afp" ? `afp://${a}/` : k === "ftp" ? `ftp://${a}` : `http://${a}/`;
};
// running / off / problem / paused, for a service tile or pill
function shareSvcState(fsx, k) {
  const s = fsx && fsx[k];
  if (!fsx) return { cls: "pill-gray", text: "Unknown" };
  if (s && !s.enabled) return { cls: "pill-gray", text: "Off" };
  if (!fsx.active) return { cls: "pill-amber", text: "Paused" };
  if (s && s.error) return { cls: "pill-red", text: "Problem" };
  if (s && s.running) return { cls: "pill-green", text: "On" };
  return { cls: "pill-amber", text: "Starting" };
}
const sharePausedBanner = (fsx) => (fsx && !fsx.active && fsx.paused
  ? html`<div class="banner banner-warn" role="status" data-key="share-paused">${icon("info")}<span><b>File sharing is paused.</b> <span class="muted">${fsx.paused}</span></span></div>` : "");

// settings switches shared by the three service pages (saved at once, like the LAN page)
const ShareSettings = {
  async loadCfg() {
    try { const c = await api("/api/settings"); if (current === this) { this.cfg = c.values; this.render(); } }
    catch (e) { if (e.status !== 401) toastErr(e, "Could not load the settings"); }
  },
  val(k) { return this.opt && k in this.opt ? this.opt[k] : this.cfg ? this.cfg[k] : undefined; },
  async saveKey(k, v, okMsg) {
    this.opt = this.opt || {};
    this.opt[k] = v; this.saving = k; this.render();
    try {
      const c = await api("/api/settings", { method: "PUT", body: { values: { [k]: v } } });
      this.cfg = c.values;
      toast(okMsg, { detail: "The box applies it within a few seconds." });
      refreshStatus();
      return true;
    } catch (e) { toastErr(e, "Could not save"); return false; }
    finally { delete this.opt[k]; this.saving = null; if (current === this) this.render(); }
  },
  switchCard(k, title, text) {
    const on = !!this.val(k), svc = Object.keys(SHARE_SVC).find((x) => SHARE_SVC[x].key === k);
    const st = svc ? shareSvcState(shareState(), svc) : null;
    return html`<div class="card lan-toggle ${on ? "is-on" : ""}" data-key="sw-${k}">
      <label class="lan-toggle-main" for="sw-${k.replace(".", "-")}">
        <span class="lt-icon">${icon(svc ? SHARE_SVC[svc].ic : "settings")}</span>
        <span class="lt-text"><b>${title} ${st && on ? html`<span class="pill ${st.cls}">${st.text}</span>` : ""}</b><small>${text}</small></span>
        <span class="switch"><input type="checkbox" id="sw-${k.replace(".", "-")}" data-change="share-switch" data-key="${k}" ${on ? raw("checked") : ""} ${this.cfg && this.saving !== k ? "" : raw("disabled")}><span class="switch-track"></span></span>
      </label></div>`;
  },
  writableCard() {
    return this.switchCard("share.writable", "PCs and Macs can change files", this.val("share.writable") ? "They may add, change and delete files in the share over Windows and Mac file sharing." : "Read only: they can open and copy files, not change them.");
  },
  svcError(k) {
    const s = shareState() && shareState()[k];
    return s && s.enabled && s.error ? html`<div class="banner banner-err" role="alert" data-key="err-${k}">${icon("alert")}<span>${s.error}</span></div>` : "";
  },
  actions: {
    async "share-switch"(input) {
      const k = input.dataset.key, on = input.checked;
      const names = { "share.smb": "Windows file sharing", "share.afp": "Mac file sharing", "share.ftp": "FTP server", "share.web": "Web server", "share.writable": "Changing files from PCs and Macs" };
      await this.saveKey(k, on, `${names[k] || k} ${on ? "turned on" : "turned off"}`);
    },
    "copy-addr"(el) { copyText(el.dataset.text).then((ok) => toast(ok ? "Copied" : "Could not copy", { type: ok ? "ok" : "err", detail: el.dataset.text })); },
  },
};
const addrBox = (label, text) => html`<div class="share-addr"><small>${label}</small><span><code class="mono">${text}</code>
  <button type="button" class="btn btn-ghost btn-icon btn-sm" data-action="copy-addr" data-text="${text}" aria-label="Copy ${text}" title="Copy">${icon("copy")}</button></span></div>`;
const howTo = (title, steps) => html`<section class="card card-pad share-howto"><h3>${title}</h3>${steps}</section>`;

const ShareSmb = {
  title: "Windows file sharing",
  ...ShareSettings,
  mount() { this.cfg = null; this.opt = {}; this.render(); this.loadCfg(); },
  update() { this.render(); },
  render() {
    const fsx = shareState(), wg = this.wgDraft != null ? this.wgDraft : (this.val("share.workgroup") || "WORKGROUP");
    const name = (fsx && fsx.name) || "NETKEY";
    patch(view, html`<div class="stack">
      ${sharePausedBanner(fsx)}${this.svcError("smb")}
      <section class="grid grid-2 lan-toggles">
        ${this.switchCard("share.smb", "Windows file sharing", "The shared folder appears in Network Neighborhood on the retro LAN. No password.")}
        ${this.writableCard()}
      </section>
      <section class="card card-pad" data-key="smb-ids">
        <div class="share-addrs">${addrBox("Share", shareAddr(fsx, "smb"))}${addrBox("Computer name", name)}${addrBox("By address", `\\\\${(fsx && fsx.address) || "10.99.0.1"}\\FILES`)}</div>
        <form class="share-wg" data-submit="wg-save" autocomplete="off">
          <label class="field"><span class="field-label">Workgroup</span>
            <input class="input mono" id="wg-in" data-input="wg-input" maxlength="15" value="${wg}" spellcheck="false" autocapitalize="characters"></label>
          <button class="btn" ${this.cfg && wg.toUpperCase() !== (this.cfg["share.workgroup"] || "") ? "" : raw("disabled")}>Save</button>
          <p class="faint">Use the same workgroup as the retro PCs (Control Panel › Network › Identification), so the box shows up next to them. Most PCs use WORKGROUP.</p>
        </form>
      </section>
      <div class="grid grid-2">
        ${howTo("Windows 95, 98, ME", html`<ol><li>The PC needs <b>TCP/IP</b> (obtain an address automatically) and <b>Client for Microsoft Networks</b> in Control Panel › Network.</li>
          <li>Open <b>Network Neighborhood</b>, then <b class="mono">${name}</b>, then <b>FILES</b>.</li>
          <li>Not listed yet? Browsing takes a few minutes to settle. Use Start › Find › Computer › <b class="mono">${name}</b>, or Start › Run › <code class="mono">${shareAddr(fsx, "smb")}</code>.</li></ol>`)}
        ${howTo("Windows for Workgroups 3.11, XP and DOS", html`<ul><li><b>WfW 3.11</b> with Microsoft TCP/IP-32: File Manager › Disk › Connect Network Drive › <code class="mono">${shareAddr(fsx, "smb")}</code>.</li>
          <li><b>Windows XP</b>: My Network Places, or <code class="mono">${shareAddr(fsx, "smb")}</code> in the address bar.</li>
          <li><b>DOS</b> with Microsoft Network Client 3.0 (TCP/IP): <code class="mono">NET USE F: ${shareAddr(fsx, "smb")}</code></li>
          <li>Only TCP/IP works for the share; NetBEUI alone is not enough (games can keep using IPX and NetBEUI).</li></ul>`)}
      </div>
      <p class="muted lan-note">${icon("lock", "i-sm")} Only PCs on this box's own retro LAN can reach the share. Your home network can't, and while you host a party, the other boxes' PCs can't either.</p>
    </div>`);
  },
  actions: {
    ...ShareSettings.actions,
    "wg-input"(input) { this.wgDraft = input.value.toUpperCase().replace(/[^A-Z0-9_-]/g, ""); if (input.value !== this.wgDraft) input.value = this.wgDraft; this.render(); },
    async "wg-save"() {
      const v = (this.wgDraft || "").trim();
      if (!v) return toast("Enter a workgroup name", { type: "err" });
      if (await this.saveKey("share.workgroup", v, `Workgroup set to ${v}`)) this.wgDraft = null;
      this.render();
    },
  },
};

const ShareMac = {
  title: "Mac file sharing",
  ...ShareSettings,
  mount() { this.cfg = null; this.opt = {}; this.render(); this.loadCfg(); },
  update() { this.render(); },
  render() {
    const fsx = shareState(), a = (fsx && fsx.address) || "10.99.0.1";
    patch(view, html`<div class="stack">
      ${sharePausedBanner(fsx)}${this.svcError("afp")}
      <section class="grid grid-2 lan-toggles">
        ${this.switchCard("share.afp", "Mac file sharing", "Macs with Ethernet get the shared folder as a disk on their desktop. No password.")}
        ${this.writableCard()}
      </section>
      <section class="card card-pad" data-key="afp-ids"><div class="share-addrs">${addrBox("Server IP address", a)}${addrBox("Mac OS X", `afp://${a}/`)}${addrBox("Disk name", (fsx && fsx.share) || "FILES")}</div></section>
      <div class="grid grid-2">
        ${howTo("Mac OS 7.6 to 9", html`<ol><li>Control Panels › <b>TCP/IP</b>: Connect via <b>Ethernet</b>, Configure <b>Using DHCP Server</b>.</li>
          <li>Open the <b>Chooser</b>, click <b>AppleShare</b>, then <b>Server IP Address…</b> and type <code class="mono">${a}</code>.</li>
          <li>Choose <b>Guest</b>, then select <b>FILES</b>. The disk appears on the desktop.</li>
          <li>Make an alias of the disk: double-click it next time to connect again.</li></ol>`)}
        ${howTo("Mac OS X", html`<ul><li>Finder › Go › <b>Connect to Server</b>, type <code class="mono">afp://${a}/</code> and connect as <b>Guest</b>.</li>
          <li>Mac OS X 10.1 and later can also use the Windows share: <code class="mono">smb://${a}/FILES</code>.</li></ul>`)}
        ${howTo("Mac files stay whole", html`<ul><li>Applications, fonts and StuffIt archives copied over Mac file sharing keep their resource fork, type and icon. PCs never see the extra parts.</li>
          <li>The other ways in (Windows sharing, FTP, uploads on this page) carry only the data. For Mac programs, use <b>.sit</b>, <b>.hqx</b> or <b>.bin</b> files there and open them with StuffIt Expander on the Mac.</li></ul>`)}
        ${howTo("Network games between Macs", html`<ul><li>The box passes <b>AppleTalk</b> between the Macs on this retro LAN, and to the other boxes in a LAN party, just like IPX for PCs.</li>
          <li>Control Panels › <b>AppleTalk</b>: Connect via <b>Ethernet</b>. Marathon, Bolo and other AppleTalk games then find each other.</li></ul>`)}
      </div>
      <p class="muted lan-note">${icon("lock", "i-sm")} Only Macs on this box's own retro LAN can reach the share, like the Windows share. The box itself doesn't speak AppleTalk, so the Chooser doesn't list it by itself: connect by IP address once and keep the alias.</p>
    </div>`);
  },
  actions: { ...ShareSettings.actions },
};

const ShareFtp = {
  title: "FTP server",
  ...ShareSettings,
  mount() { this.cfg = null; this.opt = {}; this.users = null; this.render(); this.loadCfg(); this.loadUsers(); },
  update() { this.render(); },
  async loadUsers() { try { this.users = (await api("/api/users")).users; if (current === this) this.render(); } catch { /* count stays hidden */ } },
  render() {
    const fsx = shareState(), a = (fsx && fsx.address) || "10.99.0.1";
    const n = this.users ? this.users.filter((u) => u.enabled).length : null;
    patch(view, html`<div class="stack">
      ${sharePausedBanner(fsx)}${this.svcError("ftp")}
      ${this.switchCard("share.ftp", "FTP server", "PCs log in with a modem user's name and password, and can upload, download and organise files.")}
      <section class="card card-pad" data-key="ftp-ids">
        <div class="share-addrs">${addrBox("Address", `ftp://${a}`)}${addrBox("Or by name", "ftp netkey")}</div>
        <a class="card lan-link" href="#/users">${icon("users")}<span><b>Logins are your modem users${n != null ? ` (${n} enabled)` : ""}</b><small>The same names and passwords as for dial-up. Add or change them on the Users page.</small></span>${icon("chevron", "chev")}</a>
      </section>
      <div class="grid grid-2">
        ${howTo("DOS", html`<ul><li>With a packet driver loaded, <b>mTCP</b>'s FTP client works well: <code class="mono">FTP ${a}</code></li>
          <li>Type <code class="mono">bin</code> before transferring programs or archives, then <code class="mono">get FILE.ZIP</code> or <code class="mono">put FILE.ZIP</code>.</li></ul>`)}
        ${howTo("Windows", html`<ul><li>The built-in client: Start › Run › <code class="mono">ftp ${a}</code>, then <code class="mono">bin</code> and <code class="mono">get</code> / <code class="mono">put</code>.</li>
          <li>Or WS_FTP, CuteFTP, or a browser: <code class="mono">ftp://USER@${a}/</code></li>
          <li>Passive and active mode both work.</li></ul>`)}
      </div>
    </div>`);
  },
  actions: { ...ShareSettings.actions },
};

const ShareWeb = {
  title: "Web server",
  ...ShareSettings,
  mount() { this.cfg = null; this.opt = {}; this.render(); this.loadCfg(); },
  update() { this.render(); },
  render() {
    const fsx = shareState(), a = (fsx && fsx.address) || "10.99.0.1";
    patch(view, html`<div class="stack">
      ${sharePausedBanner(fsx)}${this.svcError("web")}
      ${this.switchCard("share.web", "Web server", "A plain page with the shared folder that any browser can download from. Read only.")}
      <section class="card card-pad" data-key="web-ids"><div class="share-addrs">${addrBox("Address", `http://${a}/`)}${addrBox("Or by name", "http://netkey/")}</div></section>
      ${howTo("Old browsers welcome", html`<ul><li>The page is simple HTML 3.2 without scripts or styles, so Mosaic, Netscape 1 to 4, Internet Explorer 2 to 6 and Arachne all show it.</li>
        <li>Click a file to download it. Uploads go through this NetKey page (Fileshare), or over FTP and Windows file sharing.</li></ul>`)}
    </div>`);
  },
  actions: { ...ShareSettings.actions },
};

// the file browser: what's in the shared folder, uploads by drag and drop (files and whole folders)
const Files = {
  title: "Fileshare",
  mount() {
    this.path = this.path || "";
    this.data = null; this.err = null;
    this.ups = []; this.drag = 0;
    this.sel = new Set(); this.anchor = null;
    this.render();
    this.load();
    this.onDrag = (e) => this.dragEvent(e);
    ["dragenter", "dragover", "dragleave", "drop"].forEach((t) => document.addEventListener(t, this.onDrag));
    this.onKey = (e) => this.keyEvent(e);
    document.addEventListener("keydown", this.onKey);
  },
  unmount() {
    clearTimeout(this.timer);
    ["dragenter", "dragover", "dragleave", "drop"].forEach((t) => document.removeEventListener(t, this.onDrag));
    document.removeEventListener("keydown", this.onKey);
  },
  // selection: names in the current folder (checkboxes, click on a row, shift for a range, Ctrl/Cmd+A, Esc)
  names() { return this.data ? this.data.entries.map((e) => e.name) : []; },
  select(names, on) { names.forEach((n) => (on ? this.sel.add(n) : this.sel.delete(n))); this.render(); },
  pick(name, e, want = !this.sel.has(name)) {
    const all = this.names();
    if (e && e.shiftKey && this.anchor != null && all.includes(this.anchor)) {      // a range, from the last row picked
      const [a, b] = [all.indexOf(this.anchor), all.indexOf(name)].sort((x, y) => x - y);
      return this.select(all.slice(a, b + 1), want);
    }
    this.anchor = name;
    this.select([name], want);
  },
  keyEvent(e) {
    if (current !== this || modals.length || e.defaultPrevented || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) && !document.activeElement.classList.contains("file-check")) return;
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "a" && this.data && this.data.entries.length) { e.preventDefault(); this.select(this.names(), true); }
    else if (e.key === "Escape" && this.sel.size) { e.preventDefault(); this.sel.clear(); this.render(); }
    else if ((e.key === "Delete" || e.key === "Backspace") && this.sel.size) { e.preventDefault(); this.actions["files-del-sel"].call(this); }
  },
  update() { this.render(); },
  onVisible() { this.load(); },
  async load() {
    clearTimeout(this.timer);
    const want = this.path;
    try {
      const d = await api(`/api/files?path=${encodeURIComponent(want)}`);
      if (want !== this.path) return;
      this.data = d; this.err = null;
      const have = new Set(d.entries.map((x) => x.name));
      [...this.sel].forEach((n) => { if (!have.has(n)) this.sel.delete(n); });     // gone (moved or deleted elsewhere)
    } catch (e) {
      if (e.status === 401) return;
      if (e.status === 404 && this.path) { this.path = ""; return this.load(); }
      this.err = e.message;
    }
    if (current !== this) return;
    this.render();
    this.timer = setTimeout(() => { if (!document.hidden && !this.ups.some((u) => u.state === "up")) this.load(); else this.timer = setTimeout(() => this.load(), 5000); }, 5000);
  },
  go(path) { this.path = path; this.data = null; this.sel.clear(); this.anchor = null; this.render(); this.load(); },
  rel(name) { return this.path ? `${this.path}/${name}` : name; },
  services(fsx) {
    return html`<section class="share-svcs" aria-label="How the PCs get the files">${Object.entries(SHARE_SVC).map(([k, s]) => {
      const st = shareSvcState(fsx, k);
      return html`<a class="card share-svc" href="#/${s.route}" data-key="svc-${k}"><span class="lt-icon">${icon(s.ic)}</span>
        <span class="ss-t"><b>${s.short}</b><code class="mono ellipsis">${shareAddr(fsx, k)}</code></span><span class="pill ${st.cls}">${st.text}</span></a>`;
    })}</section>`;
  },
  crumbs() {
    const parts = this.path ? this.path.split("/") : [];
    const items = [html`<button type="button" class="crumb" data-action="files-go" data-path="" data-drop-dir="">${icon("home", "i-sm")}Files</button>`];
    parts.forEach((p, i) => {
      const to = parts.slice(0, i + 1).join("/");
      items.push(html`<span class="crumb-sep">/</span>`);
      items.push(i === parts.length - 1 ? html`<span class="crumb is-here">${p}</span>`
        : html`<button type="button" class="crumb" data-action="files-go" data-path="${to}" data-drop-dir="${to}">${p}</button>`);
    });
    return html`<nav class="crumbs" aria-label="Folder">${items}</nav>`;
  },
  uploadsCard() {
    if (!this.ups.length) return "";
    const done = this.ups.filter((u) => u.state === "done").length, failed = this.ups.filter((u) => u.state === "err").length;
    const total = this.ups.reduce((a, u) => a + u.size, 0), sent = this.ups.reduce((a, u) => a + (u.state === "done" ? u.size : u.sent), 0);
    const active = this.ups.some((u) => u.state === "up" || u.state === "wait");
    const shown = this.ups.filter((u) => u.state !== "done").slice(0, 6);
    return html`<section class="card card-pad uploads" data-key="uploads" aria-live="polite">
      <div class="up-head"><b>${active ? `Uploading ${done + 1 > this.ups.length ? this.ups.length : done + (active ? 1 : 0)} of ${this.ups.length}` : failed ? `${done} uploaded, ${failed} failed` : `${done} file${done === 1 ? "" : "s"} uploaded`}</b>
        <span class="faint tnum">${fmtBytes(sent)} of ${fmtBytes(total)}</span><span class="spacer"></span>
        ${active ? html`<button type="button" class="btn btn-sm" data-action="files-cancel">Cancel</button>` : html`<button type="button" class="btn btn-ghost btn-sm" data-action="files-clear-ups">Dismiss</button>`}</div>
      <div class="bar"><span style="width:${total ? Math.round((sent / total) * 100) : 0}%"></span></div>
      ${shown.length ? html`<ul class="up-list">${shown.map((u) => html`<li class="${u.state === "err" ? "bad" : ""}"><span class="ellipsis mono">${u.name}</span>
        <span class="faint nowrap">${u.state === "err" ? u.error : u.state === "wait" ? "waiting" : `${Math.round((u.sent / (u.size || 1)) * 100)} %`}</span></li>`)}</ul>` : ""}
    </section>`;
  },
  render() {
    const fsx = shareState(), d = this.data;
    const free = d ? Math.max(0, d.free - d.reserve) : null;
    const row = (e) => {
      const rel = this.rel(e.name);
      const on = this.sel.has(e.name);
      return html`<tr data-key="f-${e.name}" draggable="true" data-drag="${rel}" data-name="${e.name}" data-action="files-row" ${e.dir ? html`data-drop-dir="${rel}"` : ""} class="${e.dir ? "is-dir" : ""} ${on ? "is-sel" : ""}">
        <td class="sel-col"><input type="checkbox" class="file-check" data-action="files-check" data-name="${e.name}" ${on ? raw("checked") : ""} aria-label="Select ${e.name}"></td>
        <td><div class="cell-user">${e.dir ? html`<button type="button" class="file-name" data-action="files-go" data-path="${rel}"><span class="pc-ic is-folder">${icon("folder")}</span><b>${e.name}</b></button>`
          : html`<a class="file-name" href="/api/files/download?path=${encodeURIComponent(rel)}" download><span class="pc-ic">${icon("file")}</span><span>${e.name}</span></a>`}</div></td>
        <td class="r nowrap tnum hide-sm">${e.dir ? "" : fmtBytes(e.size)}</td>
        <td class="nowrap hide-sm">${relTime(e.mtime)}</td>
        <td class="keep"><div class="actions">
          ${e.dir ? "" : html`<a class="btn btn-ghost btn-icon btn-sm" href="/api/files/download?path=${encodeURIComponent(rel)}" download aria-label="Download ${e.name}" title="Download">${icon("download")}</a>`}
          <button type="button" class="btn btn-ghost btn-icon btn-sm" data-action="files-rename" data-name="${e.name}" aria-label="Rename ${e.name}" title="Rename">${icon("edit")}</button>
          <button type="button" class="btn btn-ghost btn-icon btn-sm" data-action="files-delete" data-name="${e.name}" data-dir="${e.dir ? "1" : ""}" aria-label="Delete ${e.name}" title="Delete">${icon("trash")}</button>
        </div></td></tr>`;
    };
    patch(view, html`<div class="stack">
      ${sharePausedBanner(fsx)}
      ${this.services(fsx)}
      ${this.uploadsCard()}
      <section class="card files-card ${this.drag ? "is-drop" : ""}" data-key="files">
        <div class="card-head files-head">${this.crumbs()}<span class="spacer"></span>
          ${this.sel.size ? html`<span class="sel-count" data-key="selbar"><b>${this.sel.size}</b> selected</span>
            <button type="button" class="btn btn-sm" data-action="files-move-sel">${icon("folder")}Move to…</button>
            <button type="button" class="btn btn-sm btn-danger" data-action="files-del-sel">${icon("trash")}Delete</button>
            <button type="button" class="btn btn-ghost btn-sm" data-action="files-sel-none">Clear</button>`
          : html`<button type="button" class="btn btn-sm" data-action="files-mkdir" data-key="mk">${icon("plus")}New folder</button>
          <label class="btn btn-sm">${icon("folder")}Upload folder<input type="file" class="sr-only" data-change="files-pick" webkitdirectory multiple></label>
          <label class="btn btn-primary btn-sm">${icon("up")}Upload files<input type="file" class="sr-only" data-change="files-pick" multiple></label>`}</div>
        ${d == null ? (this.err ? emptyState("alert", "Could not open the shared folder", this.err, html`<button class="btn" data-action="files-reload">Retry</button>`) : html`<table class="table"><tbody>${skelRows(3, 4)}</tbody></table>`)
          : !d.entries.length ? emptyState("folder", this.path ? "This folder is empty" : "Nothing shared yet", "Drag files or whole folders here, or use Upload. The PCs on the retro LAN can get them right away.")
          : html`<div class="table-wrap"><table class="table files-table"><thead><tr><th class="sel-col"><input type="checkbox" class="file-check" data-action="files-check-all" ${d.entries.length && this.sel.size === d.entries.length ? raw("checked") : ""} aria-label="Select all"></th><th>Name</th><th class="r hide-sm">Size</th><th class="hide-sm">Modified</th><th class="r"><span class="sr-only">Actions</span></th></tr></thead>
              <tbody>${this.path ? html`<tr data-key="f-up" data-drop-dir="${this.path.split("/").slice(0, -1).join("/")}"><td></td><td colspan="4"><button type="button" class="file-name" data-action="files-go" data-path="${this.path.split("/").slice(0, -1).join("/")}"><span class="pc-ic is-folder">${icon("folder")}</span><span class="muted">..  (up one folder)</span></button></td></tr>` : ""}${d.entries.map(row)}</tbody></table></div>`}
        <div class="drop-hint" aria-hidden="true">${icon("up")}<b>Drop to upload into ${this.path ? `/${this.path}` : "the shared folder"}</b></div>
        ${d ? html`<p class="card-note" data-key="fnote">${icon("database", "i-sm")}${fmtBytes(free)} free for uploads on the memory card · tick rows (or Shift-click a range) to move or delete several at once · drag rows onto a folder to move them</p>` : ""}
      </section>
    </div>`);
  },
  // drag and drop: files from the computer upload; rows dragged onto a folder (or a breadcrumb) move
  dragEvent(e) {
    const files = e.dataTransfer && [...(e.dataTransfer.types || [])].includes("Files");
    const target = e.target.closest && e.target.closest("[data-drop-dir]");
    if (!files) {
      if (!this.dragRow) return;
      if (e.type === "dragover" && target && !this.dragRow.includes(target.dataset.dropDir)) { e.preventDefault(); $$(".drop-on", view).forEach((x) => x !== target && x.classList.remove("drop-on")); target.classList.add("drop-on"); }
      if (e.type === "dragleave" && target) target.classList.remove("drop-on");
      if (e.type === "drop" && target) { e.preventDefault(); const from = this.dragRow; this.dragRow = null; this.move(from, target.dataset.dropDir); }
      return;
    }
    if (current !== this) return;
    e.preventDefault();
    if (e.type === "dragenter") { this.drag++; if (this.drag === 1) this.render(); }
    else if (e.type === "dragleave") { this.drag = Math.max(0, this.drag - 1); if (!this.drag) this.render(); }
    else if (e.type === "drop") { this.drag = 0; this.render(); this.dropped(e.dataTransfer); }
  },
  async dropped(dt) {
    const items = [...(dt.items || [])].map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
    if (!items.length) return this.queue([...dt.files].map((f) => ({ file: f, rel: f.name })));
    const out = [];
    const walk = async (entry, prefix) => {
      if (entry.isFile) { const f = await new Promise((res, rej) => entry.file(res, rej)); out.push({ file: f, rel: prefix + f.name }); return; }
      const reader = entry.createReader();
      for (;;) {
        const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
        if (!batch.length) break;
        for (const x of batch) await walk(x, `${prefix}${entry.name}/`);
      }
    };
    try { for (const it of items) await walk(it, ""); } catch (e) { toastErr(e, "Could not read the dropped folder"); }
    this.queue(out);
  },
  async queue(list) {
    list = list.filter((x) => !x.rel.split("/").some((p) => p.startsWith(".")));        // .DS_Store and friends
    if (!list.length) return;
    const names = new Set((this.data ? this.data.entries : []).map((e) => e.name.toLowerCase()));
    const clash = [...new Set(list.map((x) => x.rel.split("/")[0]).filter((n) => names.has(n.toLowerCase())))];
    let overwrite = false;
    if (clash.length) {
      overwrite = await confirmDialog({ title: "Replace existing files?", message: `${clash.length === 1 ? `“${clash[0]}” is` : `${clash.length} items are`} already in this folder. Uploading replaces files with the same name.`, confirmText: "Replace" });
      if (!overwrite) return;
    }
    const size = list.reduce((a, x) => a + x.file.size, 0);
    if (this.data && size > this.data.free - this.data.reserve) return toast("Not enough space on the memory card", { type: "err", detail: `${fmtBytes(size)} to upload, ${fmtBytes(Math.max(0, this.data.free - this.data.reserve))} free.` });
    if (!this.ups.some((u) => u.state === "up" || u.state === "wait")) this.ups = [];
    const dir = this.path;
    list.forEach((x) => this.ups.push({ file: x.file, name: x.rel, dir, size: x.file.size, sent: 0, state: "wait", overwrite }));
    this.render();
    this.pump();
  },
  pump() {
    const running = this.ups.filter((u) => u.state === "up").length;
    const next = this.ups.filter((u) => u.state === "wait").slice(0, 2 - running);
    next.forEach((u) => this.send(u));
    if (!running && !next.length && this.ups.length) { this.load(); const ok = this.ups.filter((u) => u.state === "done").length; if (ok) toast(`${ok} file${ok === 1 ? "" : "s"} uploaded`, { detail: "The retro PCs can get them now." }); }
  },
  send(u) {
    u.state = "up";
    const x = new XMLHttpRequest();
    u.xhr = x;
    x.open("PUT", `/api/files/upload?path=${encodeURIComponent(u.dir)}&name=${encodeURIComponent(u.name)}${u.overwrite ? "&overwrite=1" : ""}`);
    x.setRequestHeader("X-Requested-With", "netkey");
    x.setRequestHeader("Content-Type", "application/octet-stream");
    let last = 0;
    x.upload.onprogress = (e) => { u.sent = e.loaded; if (Date.now() - last > 250 && current === this) { last = Date.now(); this.render(); } };
    x.onload = () => {
      let msg = "";
      try { msg = JSON.parse(x.responseText).error || ""; } catch { /* not JSON */ }
      if (x.status === 401) onUnauthorized();
      if (x.status >= 200 && x.status < 300) { u.state = "done"; u.sent = u.size; } else { u.state = "err"; u.error = msg || `HTTP ${x.status}`; }
      this.after();
    };
    x.onerror = () => { u.state = "err"; u.error = "connection lost"; this.after(); };
    x.onabort = () => { u.state = "err"; u.error = "cancelled"; this.after(); };
    x.send(u.file);
  },
  after() { if (current === this) this.render(); this.pump(); },
  async move(from, to) {
    if (!from || !from.length || to == null) return;
    try {
      const r = await api("/api/files/move", { method: "POST", body: { paths: from, to } });
      if (r.moved) toast(from.length === 1 ? `Moved ${from[0].split("/").pop()}` : `Moved ${r.moved} items`, { detail: `to ${to ? "/" + to : "the top folder"}` });
      from.forEach((p) => this.sel.delete(p.split("/").pop()));
    } catch (e) { toastErr(e, from.length === 1 ? "Could not move it" : "Could not move them"); }
    this.load();
  },
  // a small folder browser for "Move to…"
  moveDialog(names) {
    const moving = names.map((n) => this.rel(n));
    let at = this.path, dirs = null, err = "";
    const m = openModal({
      title: `Move ${names.length === 1 ? names[0] : `${names.length} items`}`, iconName: "folder",
      body: html`<div class="mv" data-mv-body></div>`,
      foot: html`<button type="button" class="btn" data-close>Cancel</button><button type="submit" class="btn btn-primary" data-mv-go>Move here</button>`,
      onSubmit: async () => {
        if (at === this.path) { toast("They are already in this folder"); return false; }
        await this.move(moving, at);
      },
    });
    const draw = () => {
      const parts = at ? at.split("/") : [];
      const crumbs = [html`<button type="button" class="crumb" data-mv="">${icon("home", "i-sm")}Files</button>`];
      parts.forEach((p, i) => { crumbs.push(html`<span class="crumb-sep">/</span>`); crumbs.push(html`<button type="button" class="crumb ${i === parts.length - 1 ? "is-here" : ""}" data-mv="${parts.slice(0, i + 1).join("/")}">${p}</button>`); });
      const list = dirs == null ? html`<p class="faint">${err || "Loading…"}</p>`
        : !dirs.length ? html`<p class="faint">No folders in here. Choose <b>Move here</b> to move into this folder.</p>`
        : html`<ul class="mv-list">${dirs.map((dn) => { const to = at ? `${at}/${dn}` : dn, off = moving.includes(to);
            return html`<li><button type="button" class="file-name" data-mv="${to}" ${off ? raw("disabled") : ""}><span class="pc-ic is-folder">${icon("folder")}</span><span>${dn}</span>${off ? html`<span class="faint"> (being moved)</span>` : ""}</button></li>`; })}</ul>`;
      m.el.querySelector("[data-mv-body]").innerHTML = String(html`<nav class="crumbs">${crumbs}</nav>${list}
        <p class="faint mv-to">Moves to <b>${at ? "/" + at : "the top folder"}</b>${at === this.path ? " (where they are now)" : ""}</p>`);
    };
    const open = async (path) => {
      at = path; dirs = null; err = ""; draw();
      try { const d = await api(`/api/files?path=${encodeURIComponent(path)}`); if (at === path) dirs = d.entries.filter((x) => x.dir).map((x) => x.name); }
      catch (e) { err = e.message; }
      if (at === path) draw();
    };
    m.el.addEventListener("click", (e) => { const b = e.target.closest("[data-mv]"); if (b && !b.disabled) { e.preventDefault(); open(b.dataset.mv); } });
    open(at);
  },
  actions: {
    "files-go"(el) { this.go(el.dataset.path || ""); },
    "files-reload"() { this.err = null; this.load(); },
    "files-pick"(input) {
      const list = [...input.files].map((f) => ({ file: f, rel: f.webkitRelativePath || f.name }));
      input.value = "";
      this.queue(list);
    },
    "files-cancel"() { this.ups.forEach((u) => { if (u.state === "wait") { u.state = "err"; u.error = "cancelled"; } if (u.state === "up" && u.xhr) u.xhr.abort(); }); },
    "files-clear-ups"() { this.ups = []; this.render(); },
    "files-mkdir"() {
      openModal({
        title: "New folder", iconName: "folder",
        body: html`<label class="field"><span class="field-label">Name</span><input class="input" name="name" maxlength="120" autocomplete="off" placeholder="e.g. GAMES"></label>
          <p class="faint">Short names in capitals (8 letters or fewer) are easiest to use from DOS.</p>`,
        foot: html`<button type="button" class="btn" data-close>Cancel</button><button type="submit" class="btn btn-primary">Create</button>`,
        onSubmit: async (form) => {
          await api("/api/files/mkdir", { method: "POST", body: { path: this.path, name: form.name.value.trim() } });
          this.load();
        },
      });
    },
    "files-rename"(el) {
      const old = el.dataset.name;
      openModal({
        title: `Rename ${old}`, iconName: "edit",
        body: html`<label class="field"><span class="field-label">New name</span><input class="input" name="name" maxlength="120" autocomplete="off" value="${old}"></label>`,
        foot: html`<button type="button" class="btn" data-close>Cancel</button><button type="submit" class="btn btn-primary">Rename</button>`,
        onSubmit: async (form) => {
          const to = form.name.value.trim();
          if (to === old) return;
          await api("/api/files/rename", { method: "POST", body: { path: this.rel(old), to } });
          this.load();
        },
      });
    },
    "files-check"(el, e) { this.pick(el.dataset.name, e, el.checked); },
    "files-check-all"(el) { if (el.checked) this.select(this.names(), true); else { this.sel.clear(); this.render(); } },
    "files-row"(el, e) {
      if (e.target.closest("a, button, input, label")) return;          // the name, download, rename and delete keep their job
      this.pick(el.dataset.name, e);
    },
    "files-sel-none"() { this.sel.clear(); this.render(); },
    "files-move-sel"() { if (this.sel.size) this.moveDialog([...this.sel]); },
    async "files-del-sel"() {
      const names = [...this.sel];
      if (!names.length) return;
      const dirs = this.data ? this.data.entries.filter((x) => x.dir && this.sel.has(x.name)).length : 0;
      const what = names.length === 1 ? `“${names[0]}”` : `${names.length} items`;
      if (!(await confirmDialog({ title: `Delete ${what}?`, message: `${names.length === 1 ? (dirs ? "The folder and everything in it" : "The file") : dirs ? `${names.length - dirs} file${names.length - dirs === 1 ? "" : "s"} and ${dirs} folder${dirs === 1 ? "" : "s"} (with everything in them)` : `${names.length} files`} ${names.length === 1 ? "is" : "are"} deleted from the box. This can't be undone.`, confirmText: "Delete", danger: true }))) return;
      try {
        const r = await api("/api/files/delete", { method: "POST", body: { paths: names.map((n) => this.rel(n)) } });
        toast(names.length === 1 ? `Deleted ${names[0]}` : `Deleted ${r.deleted} items`);
        this.sel.clear();
      } catch (e) { toastErr(e, "Could not delete them"); }
      this.load();
    },
    async "files-delete"(el) {
      const name = el.dataset.name, dir = el.dataset.dir === "1";
      if (!(await confirmDialog({ title: `Delete ${name}?`, message: dir ? "The folder and everything in it are deleted from the box. This can't be undone." : "The file is deleted from the box. This can't be undone.", confirmText: "Delete", danger: true }))) return;
      try { await api("/api/files/delete", { method: "POST", body: { paths: [this.rel(name)] } }); toast(`Deleted ${name}`); this.sel.delete(name); }
      catch (e) { toastErr(e, "Could not delete it"); }
      this.load();
    },
  },
};
document.addEventListener("dragstart", (e) => {
  const tr = e.target.closest && e.target.closest("[data-drag]");
  if (!tr || current !== Files) return;
  // a selected row drags the whole selection; an unselected one drags just itself
  Files.dragRow = Files.sel.has(tr.dataset.name) ? [...Files.sel].map((n) => Files.rel(n)) : [tr.dataset.drag];
  e.dataTransfer.effectAllowed = "move";
  e.dataTransfer.setData("text/plain", Files.dragRow.join("\n"));
  if (Files.dragRow.length > 1) {
    const g = document.createElement("div");
    g.className = "drag-ghost";
    g.textContent = `${Files.dragRow.length} items`;
    document.body.appendChild(g);
    e.dataTransfer.setDragImage(g, 12, 12);
    setTimeout(() => g.remove(), 0);
  }
});
document.addEventListener("dragend", () => { Files.dragRow = null; $$(".drop-on", view).forEach((x) => x.classList.remove("drop-on")); });

const ROUTES = { dashboard: Dashboard, modem: Lines, lan: Lan, parties: Parties, network: Network, users: Users, calls: Calls, settings: Settings,
  files: Files, smb: ShareSmb, mac: ShareMac, ftp: ShareFtp, web: ShareWeb };
let current = null, currentName = null, routing = false;

async function route() {
  if (!S.authed || routing) return;
  let name = (location.hash.match(/^#\/([a-z]+)/) || [])[1];
  if (name === "lines") { name = "modem"; history.replaceState(null, "", "#/modem"); }     // old links
  if (!ROUTES[name]) { name = "dashboard"; history.replaceState(null, "", "#/dashboard"); }
  if (name === currentName) { if (current && current.onHash) current.onHash(); return; }
  if (current && current.canLeave) {
    routing = true;
    const ok = await current.canLeave();
    routing = false;
    if (!ok) { history.replaceState(null, "", "#/" + currentName); return; }
  }
  if (current && current.unmount) current.unmount();
  closeAllModals();
  current = ROUTES[name];
  currentName = name;
  $$("[data-route]").forEach((a) => (a.dataset.route === name ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  $("#subnav").hidden = !["modem", "users", "calls"].includes(name);       // the modem's pages, as tabs on phones
  $("#subnav-files").hidden = !["files", "smb", "mac", "ftp", "web"].includes(name);
  if (name === "users" || name === "calls") $$('#tabbar [data-route="modem"]').forEach((t) => t.setAttribute("aria-current", "page"));
  if (["smb", "mac", "ftp", "web"].includes(name)) $$('#nav [data-route="files"], #tabbar [data-route="files"]').forEach((t) => t.setAttribute("aria-current", "page"));
  $("#page-title").textContent = current.title;
  document.title = `${current.title} · NetKey${S.session && S.session.hostname ? " · " + S.session.hostname : ""}`;
  view.innerHTML = "";
  window.scrollTo(0, 0);
  current.mount();
  renderChrome();                                   // page-specific banners (the update notice)
}
window.addEventListener("hashchange", route);
window.addEventListener("beforeunload", (e) => {
  if (current === Settings && Settings.dirtyKeys().length) { e.preventDefault(); e.returnValue = ""; }
});

// status polling
let pollTimer = null;
// Derives the retro-LAN byte rate from the cumulative bridge counters of successive status polls.
function trackLanRate(lan) {
  const p = S.lanPrev;
  if (!lan || lan.state !== "up" || lan.rx_bytes == null || lan.tx_bytes == null || !lan.updated) { S.lanPrev = null; S.lanRate = null; return; }
  if (p && lan.updated > p.updated) {
    const dt = lan.updated - p.updated, rx = lan.rx_bytes - p.rx, tx = lan.tx_bytes - p.tx;
    S.lanRate = rx >= 0 && tx >= 0 ? { rx: rx / dt, tx: tx / dt } : null;  // counters reset (bridge recreated)
  }
  if (!p || lan.updated !== p.updated) S.lanPrev = { rx: lan.rx_bytes, tx: lan.tx_bytes, updated: lan.updated };
}
async function refreshStatus() {
  clearTimeout(pollTimer);
  if (!S.authed) return;
  try {
    const st = await api("/api/status");
    if (!S.webBuild) S.webBuild = st.web_build;
    else if (st.web_build && st.web_build !== S.webBuild) S.updated = true;      // the box got new GUI files
    skew = st.time - Date.now() / 1000;
    trackLanRate(st.lan);
    S.status = st;
    S.statusAt = nowS();
    if (st.lan && st.lan.party) partyNotices(st.lan.party);
    callNotices(st);
    renderChrome();
    if (current && current.update) current.update();
  } catch (e) {
    if (e.status === 401) return;
  }
  if (S.authed && !document.hidden) pollTimer = setTimeout(refreshStatus, 2500);
  else if (S.authed && callNotifyOn()) pollTimer = setTimeout(refreshStatus, 5000);     // keep listening for rings
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden || !S.authed) return;
  refreshStatus();
  if (current && current.onVisible) current.onVisible();
});
onOnline = (ok) => {
  if (ok === S.online) return;
  S.online = ok;
  if (S.authed) renderChrome();
};
// live counters (call durations) tick every second without waiting for the next poll
setInterval(() => {
  if (document.hidden) return;
  $$("[data-since]").forEach((el) => { el.textContent = fmtDur(nowS() - Number(el.dataset.since)); });
  $$("[data-until]").forEach((el) => { el.textContent = fmtDur(Number(el.dataset.until) - nowS()); });
}, 1000);

// auth
function adminUser() { return (S.status && S.status.admin && S.status.admin.username) || "admin"; }
function showLogin(msg) {
  S.authed = false;
  S.party = null;
  clearTimeout(pollTimer);
  if (current && current.unmount) current.unmount();
  current = currentName = null;
  closeAllModals();
  $("#boot").hidden = true;
  $("#shell").hidden = true;
  $("#login").hidden = false;
  $("#login-form").hidden = false;
  $("#setpw-form").hidden = true;
  S.setpwCurrent = null;
  $("#login-host").textContent = (S.session && S.session.hostname) || "this box";
  document.title = "Sign in · NetKey";
  $("#login-error").textContent = msg || "";
  const pw = $("#login-password"), user = $("#login-username");
  pw.value = "";
  setTimeout(() => (user.value ? pw : user).focus(), 30);
}
// Factory login in use: the box asks for an own admin password before showing anything else.
function showSetPassword(knownPassword) {
  S.authed = false;
  clearTimeout(pollTimer);
  if (current && current.unmount) current.unmount();
  current = currentName = null;
  closeAllModals();
  S.setpwCurrent = knownPassword || null;
  $("#boot").hidden = true;
  $("#shell").hidden = true;
  $("#login").hidden = false;
  $("#login-form").hidden = true;
  const form = $("#setpw-form");
  form.reset();
  form.hidden = false;
  $("#setpw-current-field").hidden = !!S.setpwCurrent;
  $("#setpw-error").textContent = "";
  document.title = "Set password · NetKey";
  setTimeout(() => (S.setpwCurrent ? $("#setpw-new") : $("#setpw-current")).focus(), 30);
}
onPasswordRequired = () => { if (!S.setpwCurrent && $("#setpw-form").hidden) showSetPassword(null); };
function startApp() {
  S.authed = true;
  $("#boot").hidden = true;
  $("#login").hidden = true;
  $("#setpw-form").hidden = true;
  S.setpwCurrent = null;
  $("#shell").hidden = false;
  renderChrome();
  route();
  refreshStatus();
}
onUnauthorized = () => { if (S.authed) showLogin("Your session has expired. Please sign in again."); };

$("#login-form").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.isComposing) return;
  if (e.target.id === "login-username") {
    e.preventDefault();
    const pw = $("#login-password");
    if (pw.value) $("#login-form").requestSubmit(); else pw.focus();
  } else if (e.target.id === "login-password") {
    e.preventDefault();
    $("#login-form").requestSubmit();
  }
});
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const user = $("#login-username"), pw = $("#login-password"), btn = $("#login-submit"), err = $("#login-error"), card = $("#login-form");
  if (!user.value.trim()) { err.textContent = "Enter the username."; user.focus(); return; }
  if (!pw.value) { err.textContent = "Enter the password."; pw.focus(); return; }
  btn.disabled = true;
  btn.innerHTML = String(html`${spinner()}<span>Signing in…</span>`);
  err.textContent = "";
  try {
    const r = await api("/api/login", { method: "POST", body: { username: user.value.trim(), password: pw.value } });
    const typed = pw.value;
    pw.value = "";
    if (!location.hash || location.hash === "#/") history.replaceState(null, "", "#/dashboard");
    if (r.must_change_password) showSetPassword(typed); else startApp();
  } catch (ex) {
    err.textContent = ex.message;
    card.classList.remove("shake");
    void card.offsetWidth;
    card.classList.add("shake");
    pw.select();
  } finally {
    btn.disabled = false;
    btn.innerHTML = "<span>Sign in</span>";
  }
});

$("#setpw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const cur = $("#setpw-current"), nw = $("#setpw-new"), rep = $("#setpw-repeat"), err = $("#setpw-error");
  const btn = $("#setpw-submit"), card = $("#setpw-form");
  const current = S.setpwCurrent || cur.value;
  err.textContent = "";
  if (!current) { err.textContent = "Enter the current password."; cur.focus(); return; }
  if (nw.value.length < 6) { err.textContent = "The new password needs at least 6 characters."; nw.focus(); return; }
  if (nw.value === current) { err.textContent = "Choose a password that is different from the current one."; nw.select(); return; }
  if (nw.value !== rep.value) { err.textContent = "The new passwords do not match."; rep.focus(); return; }
  btn.disabled = true;
  btn.innerHTML = String(html`${spinner()}<span>Saving…</span>`);
  try {
    await api("/api/password", { method: "PUT", body: { current, new: nw.value } });
    card.reset();
    startApp();
    toast("Admin password saved");
  } catch (ex) {
    err.textContent = ex.message;
    if (ex.status === 403) { S.setpwCurrent = null; $("#setpw-current-field").hidden = false; cur.value = ""; cur.focus(); }
    card.classList.remove("shake");
    void card.offsetWidth;
    card.classList.add("shake");
  } finally {
    btn.disabled = false;
    btn.innerHTML = "<span>Save and continue</span>";
  }
});

// global events
function setTheme(t) {
  const root = document.documentElement;
  root.classList.add("theme-anim");
  root.setAttribute("data-theme", t);
  store.set("theme", t);
  const meta = $('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", t === "light" ? "#f5f6f8" : "#0a0c0f");
  setTimeout(() => root.classList.remove("theme-anim"), 350);
  if (current && current.onTheme) current.onTheme();
  if (current === Dashboard) Dashboard.render();
}
const ACTIONS = {
  "reload-page"() { location.reload(); },
  "update-howto"() {
    const u = (S.status && S.status.update) || {};
    updateDialog({ command: u.command || "curl -fsSL https://raw.githubusercontent.com/ohminty/netkey/main/get.sh | sudo bash", ...u });
  },
  "update-now"() {
    const u = (S.status && S.status.update) || {};
    const job = S.status && S.status.update_job;
    if (job && UPDATE_RUNNING.includes(job.state)) return updateProgressDialog();
    if (!u.available) return toast("No update is available", { detail: "Check for updates on the Settings page." });
    updateNowDialog(u);
  },
  "update-progress"() { updateProgressDialog(); },
  "update-seen"(el) { try { localStorage.setItem("netkey-update-seen", el.dataset.t); } catch { /* shows again */ } renderChrome(); },
  async "update-history"() {
    let d;
    try { d = await api("/api/update/progress"); } catch (e) { return toastErr(e, "Could not load the update history"); }
    const res = { updated: "pill-green", "rolled back": "pill-amber", failed: "pill-red", "not started": "pill-gray" };
    openModal({
      title: "Update history", iconName: "download", wide: true,
      body: d.history.length ? html`<div class="table-wrap"><table class="table"><thead><tr><th>When</th><th>Update</th><th>Result</th><th>By</th></tr></thead><tbody>${d.history.map((h) => html`<tr>
          <td class="nowrap">${h.finished ? relTime(h.finished) : "—"}</td><td class="mono nowrap">${h.from || "?"} → ${h.to || "?"}</td>
          <td><span class="pill ${res[h.result] || "pill-gray"} no-dot">${h.result}</span>${h.message && h.result !== "updated" ? html`<span class="sub" style="white-space:normal">${h.message}</span>` : ""}</td><td>${h.by || ""}</td></tr>`)}</tbody></table></div>`
        : html`<p class="muted">No updates from the web page yet.</p>`,
      foot: html`<span class="spacer"></span><button type="button" class="btn btn-primary" data-close>Done</button>`,
    });
  },
  "update-later"() {
    const u = S.status && S.status.update;
    try { if (u) localStorage.setItem(UPDATE_KEY, u.latest); } catch { /* the banner stays */ }
    renderChrome();
    toast("Update notice hidden", { detail: "Until the next release. The Software card in Settings keeps showing it." });
  },
  async "update-copy"(b) {
    const u = (S.status && S.status.update) || {};
    const ok = await copyText(u.command || b.closest(".upd-cmd").querySelector("code").textContent);
    toast(ok ? "Command copied" : "Could not copy; select the command and copy it by hand");
  },
  async "update-check"() {
    const v = current;
    if (v !== Settings) return;
    v.checking = true; v.render();
    try {
      const u = await api("/api/update/check", { method: "POST", body: {} });
      if (S.status) S.status.update = u;
      toast(u.error ? "Could not check for updates" : u.available ? `NetKey ${u.latest} is available` : "This box is up to date", { detail: u.error || "" });
      renderChrome();
    } catch (e) { if (e.status !== 401) toastErr(e, "Could not check for updates"); }
    v.checking = false;
    if (current === v) v.render();
  },
  theme() { setTheme(document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light"); },
  async logout() {
    if (current && current.canLeave && !(await current.canLeave())) return;
    try { await api("/api/logout", { method: "POST" }); } catch { /* ignore */ }
    if (current === Settings) Settings.cfg = null;
    showLogin();
    history.replaceState(null, "", "#/dashboard");
  },
  "toggle-pw"(b) {
    const input = document.getElementById(b.dataset.target);
    if (!input) return;
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    b.innerHTML = String(icon(show ? "eye-off" : "eye"));
    b.setAttribute("aria-label", show ? "Hide password" : "Show password");
  },
  "gen-pw"(b) {
    const input = b.closest(".input-wrap").querySelector("input");
    input.value = genPassword();
    input.type = "text";
    input.removeAttribute("aria-invalid");
    const err = input.closest(".field") && input.closest(".field").querySelector(".field-error");
    if (err) err.textContent = "";
    input.focus();
    input.select();
  },
};
const findAction = (name) => (current && current.actions && current.actions[name] ? current.actions[name].bind(current) : ACTIONS[name]);

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-action]");
  if (!el || el.disabled) return;
  const fn = findAction(el.dataset.action);
  if (!fn) return;
  if (el.tagName === "A") e.preventDefault();
  fn(el, e);
});
document.addEventListener("change", (e) => {
  const el = e.target.closest("[data-change]");
  if (el) { const fn = findAction(el.dataset.change); if (fn) fn(el, e); }
});
document.addEventListener("input", (e) => {
  const el = e.target.closest("[data-input]");
  if (el) { const fn = findAction(el.dataset.input); if (fn) fn(el, e); }
});
document.addEventListener("submit", (e) => {
  if (e.target.id === "pw-form") { e.preventDefault(); Settings.submitPassword(e.target); return; }
  const f = e.target.closest("[data-submit]");
  if (f) { e.preventDefault(); const fn = findAction(f.dataset.submit); if (fn) fn(f, e); }
});
document.addEventListener("toggle", (e) => {
  const d = e.target;
  if (d.dataset && d.dataset.group && current === Settings) Settings.open[d.dataset.group] = d.open;
}, true);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && modals.length) { e.preventDefault(); modals[modals.length - 1].close(false); return; }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s" && current && current.onSave) { e.preventDefault(); current.onSave(); return; }
  // "/" focuses the search box on list pages
  if (e.key === "/" && !modals.length && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
    const s = $(".view .search input");
    if (s) { e.preventDefault(); s.focus(); }
  }
});

// boot
(async function boot() {
  try {
    S.session = await api("/api/session");
  } catch (e) {
    S.session = { authenticated: false, hostname: "" };
    $("#boot").hidden = true;
    showLogin(e.message);
    return;
  }
  if (S.session.authenticated && S.session.must_change_password) showSetPassword(null);
  else if (S.session.authenticated) startApp(); else showLogin();
})();

})();
