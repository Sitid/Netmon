/* netmon — núcleo de la SPA: router por hash (con estado en la URL y ruta de
 * navegación), API, WebSocket, tema, kiosco, enlaces de entidad, buscador,
 * tarjeta flotante y tablas ordenables/exportables.
 * Las vistas se registran desde views.js con NM.route(nombre, {render}).
 * Sin build step a propósito: se sirve tal cual y se mantiene con un editor.
 */

"use strict";

window.NM = (() => {

  // -------------------------------------------------------------------------
  // Contexto: token kiosco, modo kiosco, rotación
  // -------------------------------------------------------------------------
  const qs0 = new URLSearchParams(location.search);
  const TOKEN = qs0.get("token") || "";
  const KIOSK = location.pathname.startsWith("/kiosk");
  // rotación del kiosco: activa por defecto (30 s); ?rotar=0 la apaga, ?rotar=N ajusta
  const ROTATE_S = KIOSK ? (qs0.has("rotar") ? Number(qs0.get("rotar")) || 0 : 30) : 0;
  if (KIOSK) document.body.classList.add("kiosk");
  const CAN_HOVER = window.matchMedia("(hover: hover)").matches && !KIOSK;

  const $ = id => document.getElementById(id);
  const store = {   // localStorage tolerante (modo privado, bloqueos)
    get(k, d = null) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch {} },
  };

  // -------------------------------------------------------------------------
  // Helpers de formato
  // -------------------------------------------------------------------------
  const esc = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n >= 1099511627776) return (n / 1099511627776).toFixed(2) + " TB";
    if (n >= 1073741824) return (n / 1073741824).toFixed(2) + " GB";
    if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(0) + " KB";
    return n + " B";
  }
  // recibe BITS por segundo (live_loop manda bits/s y ntopng informa thpt.bps de
  // flujos en bits/s). Ojo: interface/data.lua 'throughput_bps' viene en BYTES/s
  // y se convierte aparte (gráfico realtime de Estado).
  function fmtMbps(bps) {
    const m = (Number(bps) || 0) / 1e6;
    return m >= 100 ? m.toFixed(0) : m >= 10 ? m.toFixed(1) : m.toFixed(2);
  }
  function fmtPps(pps) {
    pps = Number(pps) || 0;
    return pps >= 1e6 ? (pps / 1e6).toFixed(1) + "M" :
           pps >= 1e3 ? (pps / 1e3).toFixed(1) + "k" : pps.toFixed(0);
  }
  function fmtWhen(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    return d.toLocaleDateString("es-AR", { day: "2-digit", month: "2-digit" }) + " " +
           d.toLocaleTimeString("es-AR", { hour: "2-digit", minute: "2-digit" });
  }
  // "hace 5 min", "hace 3 h", "ayer 14:20", o fecha
  function fmtAgo(iso) {
    if (!iso) return "—";
    const d = new Date(iso), s = (Date.now() - d) / 1000;
    if (s < 90) return "ahora";
    if (s < 3600) return `hace ${Math.round(s / 60)} min`;
    if (s < 6 * 3600) return `hace ${Math.round(s / 3600)} h`;
    return fmtWhen(iso);
  }
  function fmtDur(s) {
    s = Number(s) || 0;
    if (s >= 3600) return Math.floor(s / 3600) + " h " + Math.floor(s % 3600 / 60) + " m";
    if (s >= 60) return Math.floor(s / 60) + " m " + (s % 60) + " s";
    return s + " s";
  }
  function timeLabel(iso, bucketS) {
    const d = new Date(iso);
    if (bucketS >= 86400) return d.toLocaleDateString("es-AR", { weekday: "short", day: "2-digit", month: "2-digit" });
    if (bucketS >= 3600)
      return d.toLocaleDateString("es-AR", { day: "2-digit", month: "2-digit" }) + " " +
             d.toLocaleTimeString("es-AR", { hour: "2-digit", minute: "2-digit" });
    return d.toLocaleTimeString("es-AR", { hour: "2-digit", minute: "2-digit" });
  }
  function pct(part, total) { return total ? 100 * Number(part) / Number(total) : 0; }

  const CAT_LABELS = {
    streaming: "Streaming", camaras: "Cámaras", social: "Social", productividad: "Productividad",
    sistema: "Sistema", p2p: "P2P", desconocido: "Desconocido",
  };
  const cssVar = name => getComputedStyle(document.body).getPropertyValue(name).trim();
  function catColor(cat) { return cssVar(`--cat-${cat}`) || "#8e8e93"; }
  // "#0a84ff" + alfa -> rgba(); los tokens de gráficos son hex de 6 dígitos
  function hexA(hex, a) {
    const m = /^#?([0-9a-f]{6})$/i.exec(String(hex).trim());
    if (!m) return hex;
    const n = parseInt(m[1], 16);
    return `rgba(${n >> 16 & 255}, ${n >> 8 & 255}, ${n & 255}, ${a})`;
  }

  // -------------------------------------------------------------------------
  // Íconos y enlaces de entidad: cualquier IP / equipo / usuario / sitio / app
  // de cualquier pantalla lleva a su ficha.
  // -------------------------------------------------------------------------
  const ico = (name, cls = "i") => `<svg class="${cls}"><use href="#i-${name}"/></svg>`;

  function isLocalIP(ip) {
    const p = String(ip).split(".").map(Number);
    if (p.length !== 4 || p.some(isNaN)) return false;
    return p[0] === 10 || (p[0] === 172 && p[1] >= 16 && p[1] <= 31) || (p[0] === 192 && p[1] === 168);
  }
  const cleanIP = ip => String(ip ?? "").split("/")[0];
  const enc = encodeURIComponent;

  const VLAN_NAMES = { 1: "Principal", 10: "Datos", 30: "Celulares", 40: "Voip", 45: "Cámaras",
                       50: "Wifi-Interna", 55: "Wifi-Invitados", 60: "Handheld", 100: "Devices" };
  // subredes conocidas -> VLAN (para cuando ntopng no informa la VLAN)
  const NETS = [["10.10.10.0", 23, 10], ["10.10.30.0", 23, 30], ["192.168.100.0", 23, 1]];
  const ip2n = ip => ip.split(".").reduce((a, o) => a * 256 + Number(o), 0);
  function vlanOf(ip) {
    ip = cleanIP(ip);
    for (const [net, bits, vlan] of NETS)
      if (Math.floor(ip2n(ip) / 2 ** (32 - bits)) === Math.floor(ip2n(net) / 2 ** (32 - bits))) return vlan;
    return null;
  }
  const vlanLabel = v => v == null ? "" : `VLAN ${v}${VLAN_NAMES[v] ? " · " + VLAN_NAMES[v] : ""}`;
  // MAC con el bit "administrada localmente": los celulares la usan como MAC privada
  function macRandom(mac) {
    const m = /^([0-9a-f]{2})/i.exec(String(mac || ""));
    return !!m && (parseInt(m[1], 16) & 2) === 2;
  }

  function ipHref(ip) {
    ip = cleanIP(ip);
    return isLocalIP(ip) ? `#/host/${ip}` : `#/ip/${ip}`;
  }
  // IP como enlace (mono). label opcional (p. ej. el nombre del equipo).
  function ipLink(ip, label) {
    ip = cleanIP(ip);
    if (!ip) return '<span class="sub">—</span>';
    return `<a class="ent ip" href="${ipHref(ip)}" data-ip="${esc(ip)}">${esc(label || ip)}</a>`;
  }
  // equipo: nombre arriba, IP (+ usuario) abajo; todo clickeable
  function hostCell(ip, hostname, user) {
    ip = cleanIP(ip);
    const top = hostname
      ? `<a class="ent" href="${ipHref(ip)}" data-ip="${esc(ip)}">${esc(hostname)}</a>`
      : ipLink(ip);
    const sub = [hostname ? ipLink(ip) : "", user ? userLink(user) : ""].filter(Boolean).join(" · ");
    return `<div class="cell-2"><span class="l1">${top}</span>${sub ? `<span class="l2">${sub}</span>` : ""}</div>`;
  }
  const userLink = u => u ? `<a class="ent user soft" href="#/usuario/${enc(u)}">${esc(u)}</a>` : "";
  const siteLink = (site, label) => site
    ? `<a class="ent" href="#/sitio/${enc(site)}">${esc(label || site)}</a>` : '<span class="sub">—</span>';
  const appLink = a => a ? `<a class="ent soft" href="#/app/${enc(a)}">${esc(a)}</a>` : '<span class="sub">—</span>';
  const catLink = c => `<a href="#/cat/${enc(c)}" class="chip cat" style="--cat:${catColor(c)}">${esc(CAT_LABELS[c] || c)}</a>`;
  const macLink = mac => mac ? `<a class="ent ip soft" href="#/dispositivos?q=${enc(mac)}">${esc(mac)}</a>` : "—";
  // IPs dentro de un texto ya escapado (mensajes de alertas) -> enlaces
  const linkifyIPs = html => String(html).replace(
    /\b((?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3})(?:\/32)?\b/g,
    (_, ip) => ipLink(ip));

  // -------------------------------------------------------------------------
  // API
  // -------------------------------------------------------------------------
  function apiUrl(path) {
    if (!TOKEN) return path;
    return path + (path.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN);
  }
  async function api(path, opts) {
    const r = await fetch(apiUrl(path), opts);
    if (r.status === 401 && !TOKEN) { showLogin(); throw new Error("no-auth"); }
    if (!r.ok) {
      let detail = `HTTP ${r.status}`;
      try { detail = (await r.json()).detail || detail; } catch {}
      throw new Error(detail);
    }
    return r.json();
  }

  // -------------------------------------------------------------------------
  // Estado global
  // -------------------------------------------------------------------------
  const state = {
    role: null, user: "",
    live: null, rt: [], lastMsgAt: 0,
    charts: [], liveHandlers: [], rtHandlers: [], refreshTimers: [],
    gen: 0,              // generación de vista: descarta renders viejos
    liveErrAt: 0,        // desde cuándo el servidor informa que ntopng no responde
    liveErrHandlers: [], // callbacks de la vista activa para "sin datos de ntopng"
    q: new URLSearchParams(),
  };

  // -------------------------------------------------------------------------
  // Charts (Chart.js con la paleta del tema activo)
  // -------------------------------------------------------------------------
  const HAS_CHARTS = typeof Chart !== "undefined";
  function themedDefaults() {
    if (!HAS_CHARTS) return;
    Chart.defaults.color = cssVar("--muted");
    Chart.defaults.borderColor = hexA(cssVar("--border"), .55);
    Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, "SF Pro Text", Inter, system-ui, sans-serif';
    Chart.defaults.font.size = 11;
    Chart.defaults.animation.duration = 650;
    Chart.defaults.animation.easing = "easeOutQuart";
    // los rellenos en degradé (CanvasGradient) no se pueden interpolar como color
    Chart.defaults.animations.colors = false;
    Chart.defaults.plugins.legend.labels.usePointStyle = true;
    Chart.defaults.plugins.legend.labels.pointStyle = "circle";
    Chart.defaults.plugins.legend.labels.boxWidth = 8;
    Chart.defaults.plugins.legend.labels.boxHeight = 8;
    const tt = Chart.defaults.plugins.tooltip;
    tt.backgroundColor = hexA(cssVar("--card") || "#1c1c1e", .96);
    tt.titleColor = cssVar("--text"); tt.bodyColor = cssVar("--text");
    tt.borderColor = hexA(cssVar("--border"), .8); tt.borderWidth = 1;
    tt.cornerRadius = 10; tt.padding = 10; tt.boxPadding = 4; tt.usePointStyle = true;
    Chart.defaults.elements.point.radius = 0;
    Chart.defaults.elements.point.hoverRadius = 4;
  }
  function mkChart(canvas, cfg) {
    if (!HAS_CHARTS || !canvas) return null;
    themedDefaults();
    const ch = new Chart(canvas, cfg);
    state.charts.push(ch);
    return ch;
  }
  // actualiza en el lugar (transición suave) si el chart ya existe en ese canvas
  function upsertChart(prev, canvas, cfg) {
    if (prev && prev.canvas === canvas && canvas?.isConnected &&
        prev.config.type === cfg.type && prev.data.datasets.length === cfg.data.datasets.length) {
      prev.data.labels = cfg.data.labels;
      cfg.data.datasets.forEach((ds, i) => Object.assign(prev.data.datasets[i], ds));
      prev.update();
      return prev;
    }
    if (prev) { try { prev.destroy(); } catch {} }
    return mkChart(canvas, cfg);
  }
  // relleno en degradé vertical (color -> transparente), estilo Apple
  function gradFill(hex, top = .32) {
    return ctx => {
      const { chart } = ctx, area = chart.chartArea;
      if (!area) return hexA(hex, top / 2);
      const g = chart.ctx.createLinearGradient(0, area.top, 0, area.bottom);
      g.addColorStop(0, hexA(hex, top));
      g.addColorStop(1, hexA(hex, 0));
      return g;
    };
  }
  const timeAxis = { ticks: { maxTicksLimit: 8, maxRotation: 0, autoSkip: true }, grid: { display: false },
                     border: { display: false } };
  const valueAxis = extra => ({ beginAtZero: true, border: { display: false },
                                grid: { color: hexA(cssVar("--border"), .45) }, ...(extra || {}) });

  // -------------------------------------------------------------------------
  // WebSocket con reconexión
  // -------------------------------------------------------------------------
  let ws, wsRetry = 1000;
  function connectWS() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws` + (TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : ""));
    ws.onopen = () => {
      wsRetry = 1000;
      ws._ka = setInterval(() => { if (ws.readyState === 1) ws.send("k"); }, 25000);
    };
    ws.onmessage = ev => {
      const msg = JSON.parse(ev.data);
      // sólo un dato válido renueva "en vivo": live_error o rt vacío significan
      // que el servidor está pero la fuente (ntopng) no (auditoría H06)
      if (msg.type === "live") {
        state.lastMsgAt = Date.now();
        state.liveErrAt = 0;
        state.live = msg;
        updateBell(msg.alerts_open);
        state.liveHandlers.forEach(h => { try { h(msg); } catch (e) { console.error(e); } });
      } else if (msg.type === "live_error") {
        if (!state.liveErrAt) state.liveErrAt = Date.now();
        state.liveErrHandlers.forEach(h => { try { h(state.lastMsgAt); } catch (e) { console.error(e); } });
      } else if (msg.type === "rt" && msg.sample) {
        state.rt.push(msg.sample);
        if (state.rt.length > 900) state.rt.shift();
        state.rtHandlers.forEach(h => { try { h(msg.sample); } catch (e) { console.error(e); } });
      }
    };
    ws.onclose = () => {
      clearInterval(ws._ka);
      setTimeout(connectWS, wsRetry);
      wsRetry = Math.min(wsRetry * 2, 15000);
    };
    ws.onerror = () => ws.close();
  }

  // El servidor manda un 'live' por vuelta de live_loop: listado completo de
  // ntopng (5-13 s) + 5 s de espera -> un dato cada 10-18 s. Umbrales acordes.
  const LIVE_WARN_S = 30, LIVE_DOWN_S = 60;
  setInterval(() => {
    const conn = $("conn"), text = $("conn-text"), banner = $("stale-banner");
    const age = (Date.now() - state.lastMsgAt) / 1000;
    const lastOk = state.lastMsgAt ? new Date(state.lastMsgAt).toLocaleTimeString("es-AR") : "nunca";
    if (state.liveErrAt) {
      conn.className = "conn down";
      text.textContent = "sin datos de ntopng";
      banner.classList.remove("hidden");
      $("stale-age").textContent = `ntopng no responde · último dato válido: ${lastOk}`;
      document.body.classList.add("stale");
    } else if (!state.lastMsgAt || age > LIVE_DOWN_S) {
      conn.className = "conn down";
      text.textContent = state.lastMsgAt ? `sin datos hace ${Math.floor(age)} s` : "conectando…";
      if (state.lastMsgAt) {
        banner.classList.remove("hidden");
        $("stale-age").textContent = `último dato: hace ${Math.floor(age)} s`;
        document.body.classList.add("stale");
      }
    } else if (age > LIVE_WARN_S) {
      conn.className = "conn warn";
      text.textContent = `hace ${Math.floor(age)} s`;
      banner.classList.add("hidden");
      document.body.classList.remove("stale");
    } else {
      conn.className = "conn ok";
      text.textContent = "en vivo";
      banner.classList.add("hidden");
      document.body.classList.remove("stale");
    }
    $("clock").textContent = new Date().toLocaleTimeString("es-AR");
  }, 1000);

  // -------------------------------------------------------------------------
  // Alertas (campana): mensajes con IPs enlazadas
  // -------------------------------------------------------------------------
  function updateBell(n) {
    const b = $("bell-badge");
    b.textContent = n > 99 ? "99+" : n;
    b.classList.toggle("hidden", !n);
  }
  function alertItem(a, admin) {
    return `
      <li class="${a.acked ? "acked" : ""}">
        <span class="sev ${esc(a.severity)}"></span>
        <div class="alert-body">
          <div>${linkifyIPs(esc(a.message))}</div>
          <div class="alert-when">${fmtWhen(a.ts)} · ${esc(a.kind)}</div>
        </div>
        ${admin && !a.acked ? `<button class="ack-btn" data-ack="${a.id}">OK</button>` : ""}
      </li>`;
  }
  async function renderAlertDrawer() {
    const onlyOpen = $("alerts-open-only").checked;
    const rows = await api(`/api/alerts?limit=80${onlyOpen ? "&only_open=true" : ""}`);
    const admin = state.role === "admin";
    $("alert-list").innerHTML = rows.map(a => alertItem(a, admin)).join("") ||
      `<li class="empty">${ico("bell")}Sin alertas</li>`;
  }
  const drawer = $("alert-drawer");
  $("bell-btn").addEventListener("click", () => {
    drawer.classList.toggle("hidden");
    if (!drawer.classList.contains("hidden")) renderAlertDrawer().catch(console.error);
  });
  $("drawer-close").addEventListener("click", () => drawer.classList.add("hidden"));
  $("alerts-open-only").addEventListener("change", () => renderAlertDrawer().catch(console.error));
  $("alert-list").addEventListener("click", async ev => {
    if (ev.target.closest("a")) { drawer.classList.add("hidden"); return; }
    const btn = ev.target.closest("[data-ack]");
    if (!btn) return;
    await api(`/api/alerts/${btn.dataset.ack}/ack`, { method: "POST" });
    renderAlertDrawer().catch(console.error);
  });

  // -------------------------------------------------------------------------
  // Tema: auto (sigue al sistema) / claro / oscuro
  // -------------------------------------------------------------------------
  const THEMES = ["auto", "light", "dark"];
  const THEME_ICON = { auto: "auto", light: "sun", dark: "moon" };
  const THEME_NAME = { auto: "automático", light: "claro", dark: "oscuro" };
  function applyTheme(mode) {
    if (mode === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = mode;
    const btn = $("theme-btn");
    btn.innerHTML = ico(THEME_ICON[mode]);
    btn.title = `Tema ${THEME_NAME[mode]} (clic para cambiar)`;
  }
  let themeMode = store.get("nm-theme") || (KIOSK ? "dark" : "auto");
  applyTheme(themeMode);
  $("theme-btn").addEventListener("click", () => {
    themeMode = THEMES[(THEMES.indexOf(themeMode) + 1) % THEMES.length];
    store.set("nm-theme", themeMode);
    applyTheme(themeMode);
    router();   // re-render: los charts toman la paleta nueva
  });
  window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
    if (themeMode === "auto") router();
  });

  // -------------------------------------------------------------------------
  // Login / rol
  // -------------------------------------------------------------------------
  function showLogin() { const d = $("dlg-login"); if (!d.open) d.showModal(); }
  const ROLE_LABELS = { admin: "Admin", viewer: "Visualizador" };

  async function detectRole() {
    try { ({ role: state.role, user: state.user } = await api("/api/me")); }
    catch { state.role = null; state.user = ""; }
    const logged = state.role === "admin" || state.role === "viewer";
    const btn = $("login-btn");
    btn.classList.toggle("anon", !logged);
    btn.innerHTML = logged
      ? `<span class="av">${esc((state.user || "?").slice(0, 1).toUpperCase())}</span><span>${esc(state.user)}</span><span class="sub">${ROLE_LABELS[state.role]}</span>`
      : "Ingresar";
    btn.title = logged ? "Cerrar sesión" : "";
    document.querySelectorAll('#nav a[data-route="config"]').forEach(a =>
      a.classList.toggle("hidden", state.role !== "admin"));
  }
  $("login-btn").addEventListener("click", async () => {
    if (state.role !== "admin" && state.role !== "viewer") return showLogin();
    if (!confirm(`¿Cerrar la sesión de ${state.user}?`)) return;
    await fetch("/api/logout", { method: "POST" });
    location.hash = "#/resumen";
    location.reload();
  });
  $("form-login").addEventListener("submit", async ev => {
    ev.preventDefault();
    const r = await fetch("/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("login-user").value.trim(), password: $("login-pass").value }),
    });
    if (r.ok) {
      $("dlg-login").close();
      $("login-err").classList.add("hidden");
      $("login-pass").value = "";
      await detectRole();
      start();
      router();
    } else {
      $("login-err").classList.remove("hidden");
    }
  });

  // -------------------------------------------------------------------------
  // Router: "#/vista/param?r=7d&tab=sitios". El estado de filtros vive en la
  // URL (se puede compartir el link). Ruta de navegación: las secciones del
  // menú la reinician; las fichas (equipo, IP, sitio…) se apilan.
  // -------------------------------------------------------------------------
  const routes = {};
  function route(name, def) { routes[name] = def; }

  const TOP = { resumen: "Resumen", estado: "Estado de red", consumo: "Consumo por equipo",
                sitios: "Sitios", conexiones: "Conexiones", usuarios: "Usuarios",
                dispositivos: "Dispositivos", reportes: "Reportes", config: "Configuración" };
  const PARENT = { host: "consumo", ip: "conexiones", sitio: "sitios", usuario: "usuarios",
                   app: "consumo", cat: "consumo" };
  let trail = [];   // [{path, qs, label}]

  function parseHash() {
    const raw = location.hash.replace(/^#\/?/, "");
    const i = raw.indexOf("?");
    const path = (i >= 0 ? raw.slice(0, i) : raw) || "resumen";
    return { path, q: new URLSearchParams(i >= 0 ? raw.slice(i + 1) : "") };
  }
  const trailHref = t => `#/${t.path}${t.qs ? "?" + t.qs : ""}`;

  function renderCrumbs() {
    const c = $("crumbs");
    c.innerHTML = trail.map((t, i) => i === trail.length - 1
      ? `<span class="cur">${esc(t.label)}</span>`
      : `<a href="${trailHref(t)}">${esc(t.label)}</a><svg class="i sep"><use href="#i-chev"/></svg>`).join("");
    $("back-btn").classList.toggle("off", trail.length < 2);
  }
  $("back-btn").addEventListener("click", () => {
    if (trail.length > 1) location.hash = trailHref(trail[trail.length - 2]);
  });

  // leer / escribir parámetros de la URL sin volver a renderizar
  const qget = (k, d = "") => state.q.get(k) ?? d;
  function qset(obj) {
    for (const [k, v] of Object.entries(obj)) {
      if (v === "" || v == null) state.q.delete(k); else state.q.set(k, v);
    }
    const { path } = parseHash();
    const qs = state.q.toString();
    history.replaceState(null, "", `#/${path}${qs ? "?" + qs : ""}`);
    if (trail.length) { trail[trail.length - 1].qs = qs; renderCrumbs(); }
  }

  // título de la vista activa: último eslabón de la ruta + pestaña del navegador
  function setTitle(label, recent) {
    if (trail.length) trail[trail.length - 1].label = label;
    renderCrumbs();
    document.title = `${label} — netmon`;
    document.querySelectorAll("#nav a").forEach(a => a.classList.toggle("active",
      a.dataset.route === (trail[0]?.path || "")));
    if (recent) pushRecent({ href: trailHref(trail[trail.length - 1]).split("?")[0], label, ...recent });
  }

  function cleanupView() {
    state.charts.forEach(c => { try { c.destroy(); } catch {} });
    state.charts = [];
    state.liveHandlers = [];
    state.liveErrHandlers = [];
    state.rtHandlers = [];
    state.refreshTimers.forEach(clearInterval);
    state.refreshTimers = [];
    hideCard();
  }

  const SKELETON = `<div class="page sk-page"><div class="sk sk-title"></div>
    <div class="sk-row">${'<div class="sk sk-card"></div>'.repeat(4)}</div><div class="sk sk-big"></div></div>`;

  let lastPath = null;
  async function router() {
    const { path, q } = parseHash();
    state.q = q;
    const [name, ...params] = path.split("/");
    const viewName = routes[name] ? name : "resumen";
    const view = routes[viewName];
    const gen = ++state.gen;

    if (TOP[viewName]) {
      trail = [{ path: viewName, qs: q.toString(), label: TOP[viewName] }];
    } else {
      const i = trail.findIndex(t => t.path === path);
      if (i >= 0) { trail = trail.slice(0, i + 1); trail[i].qs = q.toString(); }
      else {
        if (!trail.length) {
          const parent = PARENT[viewName] || "resumen";
          trail = [{ path: parent, qs: "", label: TOP[parent] }];
        }
        trail.push({ path, qs: q.toString(), label: decodeURIComponent(params[0] || "") });
      }
    }
    setTitle(trail[trail.length - 1].label);
    document.body.classList.remove("nav-open");

    cleanupView();
    const el = $("view");
    if (path !== lastPath) { el.innerHTML = SKELETON; el.scrollTop = 0; }
    lastPath = path;
    try {
      await view.render(el, params.map(decodeURIComponent), gen);
    } catch (e) {
      if (e.message !== "no-auth" && gen === state.gen)
        el.innerHTML = `<div class="page"><div class="empty">${ico("alert")}No se pudo cargar la vista (${esc(e.message)})</div></div>`;
    }
  }
  window.addEventListener("hashchange", router);
  const alive = gen => gen === state.gen;

  // monta el HTML de una vista con la animación de entrada
  function page(el, html) {
    el.innerHTML = `<div class="page enter">${html}</div>`;
    const p = el.firstElementChild;
    setTimeout(() => p.classList.remove("enter"), 900);
    initSegs(p);
    return p;
  }

  // -------------------------------------------------------------------------
  // Control segmentado (pulgar animado). seg(key, opciones, default, onChange):
  // si key no es null, el valor se guarda en la URL (?key=valor).
  // -------------------------------------------------------------------------
  function moveThumb(seg) {
    const b = seg.querySelector("button.active");
    if (!b || !b.offsetWidth) return;
    seg.style.setProperty("--x", b.offsetLeft - 2 + "px");
    seg.style.setProperty("--w", b.offsetWidth + "px");
  }
  function initSegs(root) {
    root.querySelectorAll(".seg").forEach(s => requestAnimationFrame(() => moveThumb(s)));
  }
  window.addEventListener("resize", () => document.querySelectorAll(".seg").forEach(moveThumb));
  // opciones: [valor] o [[valor, etiqueta]]
  function seg(key, options, def, onChange, cls = "") {
    const opts = options.map(o => Array.isArray(o) ? o : [o, o]);
    let cur = key ? qget(key, def) : def;
    if (!opts.some(([v]) => v === cur)) cur = def;
    const div = document.createElement("div");
    div.className = "seg " + cls;
    div.innerHTML = opts.map(([v, l]) =>
      `<button type="button" data-v="${esc(v)}" class="${v === cur ? "active" : ""}">${esc(l)}</button>`).join("");
    div.addEventListener("click", ev => {
      const b = ev.target.closest("button[data-v]");
      if (!b || b.classList.contains("active")) return;
      div.querySelectorAll("button").forEach(x => x.classList.toggle("active", x === b));
      moveThumb(div);
      if (key) qset({ [key]: b.dataset.v === def ? "" : b.dataset.v });
      onChange(b.dataset.v);
    });
    requestAnimationFrame(() => moveThumb(div));
    div.value = () => div.querySelector("button.active")?.dataset.v;
    return { el: div, value: cur };
  }
  // reemplaza un placeholder <span id=...> por un seg y devuelve el valor inicial
  function mountSeg(id, key, options, def, onChange, cls) {
    const s = seg(key, options, def, onChange, cls);
    $(id)?.replaceWith(s.el);
    return s.value;
  }

  // -------------------------------------------------------------------------
  // Tablas: ordenar por columna (clic en el encabezado) y exportar a CSV.
  // Ordenables: <table class="dt">. Valor de orden: data-v de la celda o su
  // texto. fill(tbody, html) vuelve a aplicar el orden después de refrescar.
  // -------------------------------------------------------------------------
  function sortRows(table) {
    const col = Number(table.dataset.sortCol);
    if (isNaN(col) || table.dataset.sortCol === undefined) return;
    const asc = table.dataset.sortDir === "asc";
    const tb = table.tBodies[0];
    const ncols = table.tHead.rows[0].cells.length;
    const rows = [...tb.rows].filter(r => r.cells.length === ncols);
    if (rows.length < 2) return;
    const val = r => { const c = r.cells[col]; return c?.dataset.v ?? c?.innerText.trim() ?? ""; };
    const numeric = rows.every(r => { const v = val(r); return v === "" || !isNaN(Number(v)); });
    rows.sort((a, b) => {
      const x = val(a), y = val(b);
      const d = numeric ? (Number(x) || 0) - (Number(y) || 0) : x.localeCompare(y, "es", { numeric: true });
      return asc ? d : -d;
    });
    rows.forEach(r => tb.appendChild(r));
  }
  function fill(tbody, html) {
    if (typeof tbody === "string") tbody = $(tbody);
    if (!tbody) return;
    tbody.innerHTML = html;
    const t = tbody.closest("table");
    if (t?.classList.contains("dt")) sortRows(t);
  }
  function exportCSV(table, name) {
    const clean = s => String(s).replace(/\s*[↑↓]\s*$/, "").replace(/\s+/g, " ").trim();
    const cell = c => {
      const v = c.dataset.csv ?? clean(c.innerText.replace(/\n+/g, " · "));
      return /[";\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
    };
    const head = [...table.tHead.rows[0].cells].map(c => cell(c));
    const ncols = head.length;
    const body = [...table.tBodies[0].rows].filter(r => r.cells.length === ncols)
      .map(r => [...r.cells].map(cell).join(";"));
    const blob = new Blob(["﻿" + [head.join(";"), ...body].join("\r\n")], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${name || "netmon"}_${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }
  // botón "CSV" para el card-head: exporta la tabla indicada
  const csvBtn = (tableId, name) =>
    `<button class="sm ghost btn-ico csv-btn" data-csv="${esc(tableId)}" data-name="${esc(name || tableId)}" title="Exportar la tabla a CSV (Excel)">${ico("download")}CSV</button>`;

  document.addEventListener("click", ev => {
    // exportar
    const cb = ev.target.closest("[data-csv]");
    if (cb) { const t = $(cb.dataset.csv); if (t) exportCSV(t, cb.dataset.name); return; }
    // ordenar
    const th = ev.target.closest("table.dt thead th");
    if (th && !th.classList.contains("nosort")) {
      const table = th.closest("table");
      const col = th.cellIndex;
      const same = Number(table.dataset.sortCol) === col && table.dataset.sortCol !== undefined;
      table.dataset.sortCol = col;
      table.dataset.sortDir = same && table.dataset.sortDir === "desc" ? "asc" : "desc";
      table.querySelectorAll("thead th").forEach(x => x.classList.remove("sorted", "asc"));
      th.classList.add("sorted");
      th.classList.toggle("asc", table.dataset.sortDir === "asc");
      sortRows(table);
      return;
    }
    // filas / ítems con destino
    const row = ev.target.closest("[data-href]");
    if (row && !ev.target.closest("a, button, input, select, label")) location.hash = row.dataset.href;
  });

  // -------------------------------------------------------------------------
  // Tarjeta flotante al pasar el mouse sobre una IP o un equipo
  // -------------------------------------------------------------------------
  const card = $("hovercard");
  const cardCache = new Map();
  let cardTimer = null, cardFor = null;
  function hideCard() { clearTimeout(cardTimer); cardFor = null; card.classList.remove("show"); }
  function placeCard(anchor) {
    const r = anchor.getBoundingClientRect(), w = card.offsetWidth, h = card.offsetHeight;
    let x = Math.min(Math.max(8, r.left), window.innerWidth - w - 8);
    let y = r.bottom + 8;
    if (y + h > window.innerHeight - 8) y = r.top - h - 8;
    card.style.left = x + "px"; card.style.top = y + "px";
  }
  function cardHTML(d) {
    if (d.local) {
      const v = vlanOf(d.ip);
      return `<div class="hc-t">${esc(d.hostname || "Equipo sin nombre")}</div>
        <div class="hc-ip">${esc(d.ip)}</div>
        <dl>
          ${d.user ? `<dt>Usuario</dt><dd>${esc(d.user)}</dd>` : ""}
          ${d.vendor ? `<dt>Fabricante</dt><dd>${esc(d.vendor)}</dd>` : ""}
          ${d.mac ? `<dt>MAC</dt><dd>${esc(d.mac)}${macRandom(d.mac) ? " · privada" : ""}</dd>` : ""}
          ${v != null ? `<dt>Red</dt><dd>${esc(vlanLabel(v))}</dd>` : ""}
          <dt>Hoy</dt><dd>${fmtBytes(d.today_total)} · internet ${fmtBytes(d.today_internet)}</dd>
          <dt>Visto</dt><dd>${fmtAgo(d.last_seen)}</dd>
        </dl>`;
    }
    return `<div class="hc-t">${esc(d.site || "IP externa")}</div>
      <div class="hc-ip">${esc(d.ip)}${d.domain && d.domain !== d.site ? " · " + esc(d.domain) : ""}</div>
      <dl>
        ${d.country ? `<dt>País</dt><dd>${esc(d.country)}</dd>` : ""}
        <dt>Equipos (24 h)</dt><dd>${d.hosts_24h}</dd>
        <dt>Tráfico (24 h)</dt><dd>${fmtBytes(d.bytes_24h)}</dd>
      </dl>`;
  }
  if (CAN_HOVER) {
    document.addEventListener("mouseover", ev => {
      const a = ev.target.closest("[data-ip]");
      if (!a || a.closest("#palette")) return;
      const ip = a.dataset.ip;
      if (cardFor === ip) return;
      hideCard();
      cardFor = ip;
      cardTimer = setTimeout(async () => {
        try {
          let d = cardCache.get(ip);
          if (!d || Date.now() - d._t > 60000) {
            d = await api(`/api/ipcard/${enc(ip)}`);
            d._t = Date.now();
            cardCache.set(ip, d);
          }
          if (cardFor !== ip || !a.isConnected) return;
          card.innerHTML = cardHTML(d);
          placeCard(a);
          card.classList.add("show");
        } catch {}
      }, 420);
    });
    document.addEventListener("mouseout", ev => {
      const a = ev.target.closest("[data-ip]");
      if (a && !a.contains(ev.relatedTarget)) hideCard();
    });
    document.addEventListener("scroll", hideCard, true);
  }

  // -------------------------------------------------------------------------
  // Buscador global (Ctrl+K o "/"): secciones, equipos, IPs, usuarios, sitios,
  // aplicaciones y categorías. Sin texto muestra lo visitado recientemente.
  // -------------------------------------------------------------------------
  const pal = $("palette"), palQ = $("pal-q"), palRes = $("pal-results");
  let palItems = [], palSel = 0, palTimer = null, palSeq = 0;

  function recentList() { try { return JSON.parse(store.get("nm-recent", "[]")) || []; } catch { return []; } }
  function pushRecent(item) {
    const list = recentList().filter(x => x.href !== item.href);
    list.unshift(item);
    store.set("nm-recent", JSON.stringify(list.slice(0, 8)));
  }
  const SECTION_ICONS = { resumen: "home", estado: "pulse", consumo: "chart", sitios: "globe",
    conexiones: "arrows", usuarios: "users", dispositivos: "devices", reportes: "doc", config: "sliders" };

  function openPalette() {
    if (KIOSK) return;
    pal.classList.remove("hidden");
    palQ.value = "";
    palQ.focus();
    renderPalette("");
  }
  function closePalette() { pal.classList.add("hidden"); }
  function drawPalette(groups) {
    palItems = [];
    const html = groups.filter(g => g.items.length).map(g => `
      <div class="pal-group">${esc(g.title)}</div>
      ${g.items.map(it => { palItems.push(it); const i = palItems.length - 1; return `
        <div class="pal-item" data-i="${i}">
          <span class="pi">${ico(it.icon)}</span>
          <span class="pt"><div class="a">${esc(it.a)}</div>${it.b ? `<div class="b">${esc(it.b)}</div>` : ""}</span>
          <span class="ret">↵</span>
        </div>`; }).join("")}`).join("");
    palRes.innerHTML = html || `<div class="pal-empty">Sin resultados</div>`;
    palSel = 0;
    markSel();
  }
  function markSel() {
    palRes.querySelectorAll(".pal-item").forEach(x => x.classList.toggle("sel", Number(x.dataset.i) === palSel));
    palRes.querySelector(".pal-item.sel")?.scrollIntoView({ block: "nearest" });
  }
  function localGroups(q) {
    const ql = q.toLowerCase();
    const sections = Object.entries(TOP)
      .filter(([k, l]) => (k !== "config" || state.role === "admin") && (!q || l.toLowerCase().includes(ql)))
      .map(([k, l]) => ({ icon: SECTION_ICONS[k], a: l, b: "Sección", href: `#/${k}` }));
    const cats = q ? Object.entries(CAT_LABELS).filter(([, l]) => l.toLowerCase().includes(ql))
      .map(([k, l]) => ({ icon: "tag", a: l, b: "Categoría de consumo", href: `#/cat/${k}` })) : [];
    return { sections, cats };
  }
  async function renderPalette(q) {
    q = q.trim();
    const { sections, cats } = localGroups(q);
    if (!q) {
      const rec = recentList().map(r => ({ icon: r.icon || "clock", a: r.label, b: r.sub || "", href: r.href }));
      drawPalette([{ title: "Recientes", items: rec }, { title: "Ir a", items: sections }]);
      return;
    }
    drawPalette([{ title: "Ir a", items: sections }, { title: "Categorías", items: cats }]);
    if (q.length < 2) return;
    const seq = ++palSeq;
    let res = [];
    try { res = (await api(`/api/search?q=${enc(q)}`)).results; } catch { return; }
    if (seq !== palSeq) return;
    const g = { ip: [], host: [], user: [], site: [], app: [] };
    for (const r of res) {
      if (r.type === "ip") g.ip.push({ icon: r.local ? "laptop" : "globe", a: r.ip,
        b: r.local ? "Equipo de la red" : "IP externa: quién habló con ella", href: ipHref(r.ip) });
      if (r.type === "host") g.host.push({ icon: "laptop", a: r.hostname || r.ip,
        b: [r.ip, r.user, r.vendor, r.mac].filter(Boolean).join(" · "), href: `#/host/${r.ip}` });
      if (r.type === "user") g.user.push({ icon: "user", a: r.user, b: `${r.ips} IP asociada${r.ips === 1 ? "" : "s"}`,
        href: `#/usuario/${enc(r.user)}` });
      if (r.type === "site") g.site.push({ icon: "globe", a: r.site, b: "Sitio de internet", href: `#/sitio/${enc(r.site)}` });
      if (r.type === "app") g.app.push({ icon: "app", a: r.app, b: `Aplicación · ${CAT_LABELS[r.category] || r.category}`,
        href: `#/app/${enc(r.app)}` });
    }
    drawPalette([{ title: "IP", items: g.ip }, { title: "Equipos", items: g.host },
                 { title: "Usuarios", items: g.user }, { title: "Sitios", items: g.site },
                 { title: "Aplicaciones", items: g.app }, { title: "Categorías", items: cats },
                 { title: "Ir a", items: sections }]);
  }
  function goPalette(i) {
    const it = palItems[i];
    if (!it) return;
    closePalette();
    location.hash = it.href;
  }
  palQ.addEventListener("input", () => {
    clearTimeout(palTimer);
    palTimer = setTimeout(() => renderPalette(palQ.value), 140);
  });
  palQ.addEventListener("keydown", ev => {
    if (ev.key === "ArrowDown") { palSel = Math.min(palSel + 1, palItems.length - 1); markSel(); ev.preventDefault(); }
    else if (ev.key === "ArrowUp") { palSel = Math.max(palSel - 1, 0); markSel(); ev.preventDefault(); }
    else if (ev.key === "Enter") { ev.preventDefault(); goPalette(palSel); }
  });
  palRes.addEventListener("click", ev => {
    const it = ev.target.closest(".pal-item");
    if (it) goPalette(Number(it.dataset.i));
  });
  palRes.addEventListener("mousemove", ev => {
    const it = ev.target.closest(".pal-item");
    if (it && Number(it.dataset.i) !== palSel) { palSel = Number(it.dataset.i); markSel(); }
  });
  pal.addEventListener("click", ev => { if (ev.target === pal) closePalette(); });
  $("search-btn").addEventListener("click", openPalette);

  // -------------------------------------------------------------------------
  // Teclado y barra lateral
  // -------------------------------------------------------------------------
  document.addEventListener("keydown", ev => {
    if (ev.key === "Escape") {
      if (!pal.classList.contains("hidden")) return closePalette();
      if (!drawer.classList.contains("hidden")) return drawer.classList.add("hidden");
      if (document.body.classList.contains("nav-open")) return document.body.classList.remove("nav-open");
    }
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "k") { ev.preventDefault(); openPalette(); return; }
    if (ev.target.matches("input, select, textarea") || ev.ctrlKey || ev.altKey || ev.metaKey) return;
    if (ev.key === "/") { ev.preventDefault(); openPalette(); return; }
    const navLinks = [...document.querySelectorAll("#nav a:not(.hidden)")];
    const n = Number(ev.key);
    if (n >= 1 && n <= navLinks.length) location.hash = navLinks[n - 1].getAttribute("href");
    if (ev.key === "Escape") document.body.classList.toggle("nav-collapsed");
  });
  $("collapse-btn").addEventListener("click", () => document.body.classList.toggle("nav-collapsed"));
  $("menu-btn").addEventListener("click", () => document.body.classList.toggle("nav-open"));
  document.addEventListener("click", ev => {
    if (document.body.classList.contains("nav-open") && !ev.target.closest("#sidebar, #menu-btn"))
      document.body.classList.remove("nav-open");
  });
  if (store.get("nm-collapsed") === "1") document.body.classList.add("nav-collapsed");
  new MutationObserver(() => {
    store.set("nm-collapsed", document.body.classList.contains("nav-collapsed") ? "1" : "0");
    setTimeout(() => document.querySelectorAll(".seg").forEach(moveThumb), 320);
    setTimeout(() => state.charts.forEach(c => { try { c.resize(); } catch {} }), 320);
  }).observe(document.body, { attributes: true, attributeFilter: ["class"] });

  // -------------------------------------------------------------------------
  // Kiosco: rotación Resumen <-> Estado de red
  // -------------------------------------------------------------------------
  let rotIdx = 0;
  function startKioskRotation() {
    if (!ROTATE_S) return;
    const cycle = ["resumen", "estado"];
    setInterval(() => {
      rotIdx = (rotIdx + 1) % cycle.length;
      location.hash = "#/" + cycle[rotIdx];
    }, ROTATE_S * 1000);
  }

  // -------------------------------------------------------------------------
  // Arranque
  // -------------------------------------------------------------------------
  let started = false;
  function start() {
    if (started || !state.role) return;
    started = true;
    connectWS();
    api("/api/interface/realtime").then(buf => { state.rt = buf; }).catch(() => {});
    router();
    if (KIOSK) startKioskRotation();
  }
  async function init() {
    await detectRole();
    if (state.role) start();
    else if (!TOKEN) showLogin();
  }
  document.addEventListener("DOMContentLoaded", init);

  // API pública para views.js
  return {
    api, apiUrl, esc, fmtBytes, fmtMbps, fmtPps, fmtWhen, fmtAgo, fmtDur, timeLabel, pct,
    CAT_LABELS, catColor, cssVar, hexA, mkChart, upsertChart, gradFill, timeAxis, valueAxis,
    route, setTitle, state, page, seg, mountSeg, qget, qset, fill, csvBtn, alive,
    ico, isLocalIP, ipHref, ipLink, hostCell, userLink, siteLink, appLink, catLink, macLink,
    linkifyIPs, alertItem, vlanOf, vlanLabel, VLAN_NAMES, macRandom,
    KIOSK, TOKEN,
    onLive: h => state.liveHandlers.push(h),
    onLiveError: h => state.liveErrHandlers.push(h),
    // un dato vivo sirve si llegó hace menos de 30 s y la fuente no está caída
    liveFresh: () => !!state.live && !state.liveErrAt && Date.now() - state.lastMsgAt < LIVE_WARN_S * 1000,
    STALE_S: 90,   // un target_state más viejo que esto = pinger sin datos
    onRT: h => state.rtHandlers.push(h),
    every: (ms, fn) => state.refreshTimers.push(setInterval(fn, ms)),
  };
})();
