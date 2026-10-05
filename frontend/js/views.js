/* netmon — vistas de la SPA. Cada vista se registra con NM.route(nombre, {render}).
 * El núcleo (core.js) limpia charts/timers/suscripciones al cambiar de vista.
 * Regla de la casa: toda IP, equipo, usuario, sitio, app o categoría que se
 * muestra es un enlace a su ficha (helpers ipLink/hostCell/siteLink/... de core).
 */

"use strict";

(() => {
  const { api, apiUrl, esc, fmtBytes, fmtMbps, fmtPps, fmtWhen, fmtAgo, fmtDur, timeLabel, pct,
          CAT_LABELS, catColor, cssVar, hexA, mkChart, upsertChart, gradFill, timeAxis, valueAxis,
          route, setTitle, state, page, mountSeg, qget, qset, fill, csvBtn, alive,
          onLiveError, liveFresh, STALE_S,
          ico, isLocalIP, ipHref, ipLink, hostCell, userLink, siteLink, appLink, catLink, macLink,
          linkifyIPs, alertItem, vlanOf, vlanLabel, macRandom, KIOSK,
          onLive, onRT, every } = NM;

  // escritura tolerante: si el usuario ya cambió de vista, el elemento no existe
  const $ = id => document.getElementById(id);
  const setHTML = (id, html) => { const e = $(id); if (e) e.innerHTML = html; };
  const setText = (id, t) => { const e = $(id); if (e) e.textContent = t; };

  const enc = encodeURIComponent;
  const RANGES = [["1h", "1 h"], ["24h", "24 h"], ["7d", "7 días"], ["30d", "30 días"]];
  const RANGE_LABELS = { "5m": "últimos 5 minutos", "1h": "última hora", "24h": "últimas 24 h",
                         "7d": "últimos 7 días", "30d": "últimos 30 días" };
  const sum = (a, f) => a.reduce((s, x) => s + (Number(f(x)) || 0), 0);

  // ---------------------------------------------------------------- piezas
  function pageHead({ eyebrow = "", title, sub = "", chips = "", actions = "", avatar = "", id = "" }) {
    return `
      <header class="page-head" ${id ? `id="${id}"` : ""}>
        <div class="ph-main ${avatar ? "identity" : ""}">
          ${avatar}
          <div style="min-width:0">
            ${eyebrow ? `<div class="eyebrow">${eyebrow}</div>` : ""}
            <h1>${title}</h1>
            ${sub ? `<div class="ph-sub">${sub}</div>` : ""}
            ${chips ? `<div class="ph-chips">${chips}</div>` : ""}
          </div>
        </div>
        ${actions ? `<div class="ph-actions">${actions}</div>` : ""}
      </header>`;
  }
  const avatar = (icon, cls = "") => `<div class="avatar ${cls}">${ico(icon)}</div>`;

  function kpi({ label, icon = "", id, value = "…", sub = "", subId = "", cls = "", href = "", extra = "" }) {
    const tag = href ? "a" : "div";
    return `
      <${tag} class="card kpi ${href ? "link" : ""}" ${href ? `href="${href}"` : ""} ${extra}>
        <div class="kpi-label">${icon ? ico(icon) : ""}${label}</div>
        <div class="kpi-value ${cls}" id="${id}">${value}</div>
        <div class="kpi-sub" ${subId ? `id="${subId}"` : ""}>${sub}</div>
      </${tag}>`;
  }
  // variación contra ayer a la misma hora
  function delta(now, prev, label = "vs ayer") {
    now = Number(now) || 0; prev = Number(prev) || 0;
    if (!prev) return "";
    const p = 100 * (now - prev) / prev;
    const cls = Math.abs(p) < 3 ? "flat" : p > 0 ? "up" : "down";
    return `<span class="delta ${cls}">${p > 0.5 ? "▲" : p < -0.5 ? "▼" : "="} ${Math.abs(p).toFixed(0)}%</span><span>${label}</span>`;
  }
  const barCell = (p, color) => `
    <div class="hbar-row" style="grid-template-columns:1fr 46px;padding:0;border:none">
      <div class="hbar-track"><div class="hbar-fill" style="width:${Math.min(100, p).toFixed(1)}%${color ? ";background:" + color : ""}"></div></div>
      <div class="hbar-val">${p.toFixed(p < 10 ? 1 : 0)}%</div></div>`;
  const emptyRow = (cols, text, icon = "") =>
    `<tr><td colspan="${cols}"><div class="empty">${icon ? ico(icon) : ""}${text}</div></td></tr>`;
  // td numérico: muestra formateado, ordena/exporta por el valor crudo
  const tdB = b => `<td class="num" data-v="${Number(b) || 0}">${fmtBytes(b)}</td>`;
  const tdN = (v, txt) => `<td class="num" data-v="${Number(v) || 0}">${txt ?? v}</td>`;
  const tdT = iso => `<td class="sub" data-v="${iso ? new Date(iso).getTime() : 0}">${fmtAgo(iso)}</td>`;
  const scopeChip = s => `<span class="chip sm ${s === "interno" ? "local" : ""}">${esc(s)}</span>`;

  function rankList(items) {
    // items: [{title, sub, value, p, href, ip}]
    return `<ul class="rank">${items.map((it, i) => `
      <li data-href="${it.href}" ${it.ip ? `data-ip="${esc(it.ip)}"` : ""}>
        <span class="pos">${i + 1}</span>
        <div class="main"><div class="t">${esc(it.title)}</div>
          ${it.sub ? `<div class="s">${esc(it.sub)}</div>` : ""}
          <div class="bar"><i style="width:${Math.max(2, Math.min(100, it.p)).toFixed(1)}%;animation-delay:${i * 40}ms"></i></div></div>
        <span class="v">${it.value}</span>
      </li>`).join("")}</ul>`;
  }

  // gráfico de línea con degradé (bajada / subida / extra)
  function lineCfg(labels, series, opts = {}) {
    return {
      type: "line",
      data: { labels, datasets: series.map(s => ({
        label: s.label, data: s.data, borderColor: s.color, borderWidth: s.width || 2,
        backgroundColor: s.fill === false ? "transparent" : gradFill(s.color, s.alpha ?? .3),
        fill: s.fill !== false, tension: .38, pointRadius: 0, borderDash: s.dash || [],
        yAxisID: s.axis || "y",
      })) },
      options: { maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
        scales: { x: timeAxis, y: valueAxis(opts.y), ...(opts.scales || {}) },
        plugins: { legend: { display: series.length > 1, align: "end" },
          tooltip: { callbacks: opts.tooltip || {} } } },
    };
  }
  function barCfg(labels, data, color, opts = {}) {
    return {
      type: "bar",
      data: { labels, datasets: [{ label: opts.label || "", data, backgroundColor: hexA(color, .85),
        hoverBackgroundColor: color, borderRadius: 6, borderSkipped: false, maxBarThickness: 28 }] },
      options: { maintainAspectRatio: false, indexAxis: opts.horizontal ? "y" : "x",
        scales: opts.horizontal
          ? { x: valueAxis({ ticks: { callback: v => fmtBytes(v) } }), y: { grid: { display: false }, border: { display: false } } }
          : { x: timeAxis, y: valueAxis({ ticks: { callback: v => fmtBytes(v) } }) },
        onClick: opts.onClick, onHover: opts.onClick ? (e, els) => { e.native.target.style.cursor = els.length ? "pointer" : "default"; } : undefined,
        plugins: { legend: { display: false },
          tooltip: { callbacks: { label: c => ` ${fmtBytes(c.raw)}` } } } },
    };
  }

  // ícono según el tipo de equipo (heurística por nombre / fabricante / MAC)
  function deviceIcon(d) {
    const s = `${d.hostname || ""} ${d.vendor || ""} ${d.os || ""}`.toLowerCase();
    if (/iphone|ipad|android|galaxy|motorola|moto |redmi|xiaomi|huawei|oppo|vivo|pixel|samsung|a\d{2}|s2\d|honor|realme/.test(s) ||
        macRandom(d.mac)) return "phone";
    if (/hikvision|dahua|camara|cam\b|nvr|dvr/.test(s)) return "camera";
    if (/svr|srv|server|servidor|^dc|esx|nas|qnap/.test(s)) return "server";
    return "laptop";
  }
  const vlanChip = (vlans, ip) => {
    const list = vlans && vlans.length ? vlans : [vlanOf(ip)].filter(v => v != null);
    return list.map(v => `<span class="chip">${ico("wifi")}${esc(vlanLabel(v))}</span>`).join("");
  };

  // =========================================================================
  // RESUMEN
  // =========================================================================
  route("resumen", {
    async render(el, _p, gen) {
      setTitle("Resumen");
      const now = new Date();
      const hour = now.getHours();
      const hello = hour < 12 ? "Buen día" : hour < 20 ? "Buenas tardes" : "Buenas noches";
      const today = now.toLocaleDateString("es-AR", { weekday: "long", day: "numeric", month: "long" });
      page(el, `
        ${pageHead({ eyebrow: esc(today), title: "Resumen", sub: `${hello}. Así está la red ahora.` })}
        <div class="stack">
          <div class="grid kpi-row">
            ${kpi({ label: "Estado de red", icon: "pulse", id: "k-net", value: "—", subId: "k-net-s", sub: "esperando datos…", href: "#/estado" })}
            <div class="card kpi">
              <div class="kpi-label">${ico("bolt")}Ancho de banda ahora</div>
              <div class="kpi-split">
                <span class="dir down">↓ <span class="num" id="k-bw-d">—</span></span>
                <span class="dir up">↑ <span class="num" id="k-bw-u">—</span></span>
                <span class="unit">Mbps</span>
              </div>
              <div class="kpi-sub">promedio de los últimos segundos</div>
            </div>
            ${kpi({ label: "Consumo de hoy", icon: "chart", id: "k-today", subId: "k-today-s", href: "#/consumo?r=24h" })}
            ${kpi({ label: "Equipos activos hoy", icon: "devices", id: "k-hosts", subId: "k-hosts-s", href: "#/dispositivos" })}
            ${kpi({ label: "Alertas abiertas", icon: "bell", id: "k-alerts", subId: "k-alerts-s", extra: 'id="k-alerts-card" style="cursor:pointer"' })}
          </div>

          <div class="card">
            <div class="card-head"><div><h2>Ancho de banda</h2><div class="ch-sub">toda la red · Mbps</div></div><span id="bw-rp"></span></div>
            <div class="chart-box chart-tall" id="bw-box"><canvas id="ch-bw"></canvas></div>
          </div>

          <div class="grid three-col">
            <div class="card">
              <div class="card-head"><div><h2>Más consumo de internet</h2><div class="ch-sub">hoy, por equipo</div></div>
                <a class="sm" href="#/consumo?r=24h&amp;o=internet">Ver todos</a></div>
              <div id="o-hosts"><div class="sk" style="height:260px"></div></div>
            </div>
            <div class="card">
              <div class="card-head"><div><h2>Sitios más usados</h2><div class="ch-sub">hoy, toda la red</div></div>
                <a class="sm" href="#/sitios">Ver todos</a></div>
              <div id="o-sites"><div class="sk" style="height:260px"></div></div>
            </div>
            <div class="card">
              <div class="card-head"><div><h2>Usuarios con más consumo</h2><div class="ch-sub">hoy, internet</div></div>
                <a class="sm" href="#/usuarios">Ver todos</a></div>
              <div id="o-users"><div class="sk" style="height:260px"></div></div>
            </div>
          </div>

          <div class="grid two-col">
            <div class="card">
              <div class="card-head"><div><h2>Tráfico por categoría</h2><div class="ch-sub">clic en una porción → quién la consumió</div></div><span id="cat-rp"></span></div>
              <div class="chart-box"><canvas id="ch-cats"></canvas></div>
            </div>
            <div class="card">
              <div class="card-head"><div><h2>Ahora mismo</h2><div class="ch-sub">los 6 equipos con más tráfico en este momento</div></div></div>
              <div id="top5"><div class="empty">Esperando tráfico…</div></div>
            </div>
          </div>

          <div class="card">
            <div class="card-head"><div><h2>Alertas recientes</h2><div class="ch-sub">clic en una IP → su ficha</div></div>
              <button class="sm ghost" id="o-all-alerts">Ver todas</button></div>
            <ul class="alert-list" id="o-alerts"></ul>
          </div>
        </div>`);

      // --- ancho de banda ---------------------------------------------------
      let bwChart = null;
      const bwRange = mountSeg("bw-rp", "bw", [["1h", "1 h"], ["24h", "24 h"]], "1h", r => loadBW(r), "sm");
      async function loadBW(r = qget("bw", "1h")) {
        const data = await api(`/api/timeline?range=${r}`);
        const box = $("bw-box");
        if (!box || !alive(gen)) return;
        if (!data.points.length) {
          box.innerHTML = '<div class="empty">Recolectando datos — el gráfico se completa en ~10 min</div>';
          bwChart = null; return;
        }
        if (!box.querySelector("canvas")) box.innerHTML = '<canvas id="ch-bw"></canvas>';
        const toM = b => Number(b) * 8 / data.bucket_s / 1e6;
        bwChart = upsertChart(bwChart, $("ch-bw"), lineCfg(
          data.points.map(p => timeLabel(p.t, data.bucket_s)),
          [{ label: "Bajada", data: data.points.map(p => toM(p.down)), color: cssVar("--accent") },
           { label: "Subida", data: data.points.map(p => toM(p.up)), color: cssVar("--accent-2"), alpha: .18 }],
          { tooltip: { label: c => ` ${c.dataset.label}: ${c.raw.toFixed(1)} Mbps` } }));
      }

      // --- categorías ---------------------------------------------------------
      let catChart = null;
      mountSeg("cat-rp", "cat", [["1h", "1 h"], ["24h", "24 h"]], "1h", r => loadCats(r), "sm");
      async function loadCats(r = qget("cat", "1h")) {
        const data = await api(`/api/categories?range=${r}`);
        if (!alive(gen)) return;
        const totals = data.totals.filter(t => t.total > 0);
        const all = sum(totals, t => t.total) || 1;
        const cfg = {
          type: "doughnut",
          data: { labels: totals.map(t => CAT_LABELS[t.category] || t.category),
            datasets: [{ data: totals.map(t => Number(t.total)),
              backgroundColor: totals.map(t => catColor(t.category)),
              borderColor: cssVar("--card"), borderWidth: 3, hoverOffset: 8, borderRadius: 4 }] },
          options: { maintainAspectRatio: false, cutout: "66%",
            onClick: (_e, els) => { if (els.length) location.hash = "#/cat/" + totals[els[0].index].category; },
            onHover: (e, els) => { e.native.target.style.cursor = els.length ? "pointer" : "default"; },
            plugins: { legend: { position: "right",
              onClick: (_e, item) => { location.hash = "#/cat/" + totals[item.index].category; },
              labels: { font: { size: 12 }, padding: 12,
                generateLabels: () => totals.map((t, i) => ({
                  text: `${CAT_LABELS[t.category] || t.category}  ${(100 * t.total / all).toFixed(0)}%`,
                  fillStyle: catColor(t.category), strokeStyle: "transparent", index: i,
                  fontColor: cssVar("--text"), pointStyle: "circle" })) } },
              tooltip: { callbacks: { label: c => ` ${c.label}: ${fmtBytes(c.raw)}` } } } },
        };
        catChart = upsertChart(catChart, $("ch-cats"), cfg);
      }

      // --- vivo: estado, ancho de banda, top ahora -------------------------------
      let emaD = null, emaU = null;
      onLive(msg => {
        emaD = emaD === null ? msg.totals.down_bps : .3 * msg.totals.down_bps + .7 * emaD;
        emaU = emaU === null ? msg.totals.up_bps : .3 * msg.totals.up_bps + .7 * emaU;
        setText("k-bw-d", fmtMbps(emaD));
        setText("k-bw-u", fmtMbps(emaU));
        const targets = msg.targets || [];
        const downs = targets.filter(t => !t.up);
        const kNet = $("k-net");
        if (!kNet) return;
        // estado del pinger viejo = sin datos, no "En línea" (auditoría H05)
        const newest = Math.max(0, ...targets.map(t => t.updated_at ? new Date(t.updated_at).getTime() : 0));
        const staleS = (Date.now() - newest) / 1000;
        if (!targets.length) kNet.textContent = "—";
        else if (!newest || staleS > STALE_S) {
          kNet.textContent = "Sin datos";
          kNet.className = "kpi-value";
          setText("k-net-s", newest ? `el monitor de enlaces no mide desde ${fmtWhen(new Date(newest).toISOString())}` : "el monitor de enlaces no informó");
        } else if (downs.length) {
          kNet.textContent = "Caída";
          kNet.className = "kpi-value state-crit";
          setText("k-net-s", downs.map(t => t.target.replace("internet:", "")).join(", ") + " sin respuesta");
        } else {
          kNet.textContent = "En línea";
          kNet.className = "kpi-value state-ok";
          setText("k-net-s", targets.map(t => `${t.target.replace("internet:", "").replace("dns_interno", "DNS").replace("gateway", "Gateway")} ${t.last_rtt_ms != null ? Number(t.last_rtt_ms).toFixed(0) + " ms" : "—"}`).join(" · "));
        }
        const top = (msg.hosts || []).slice(0, 6);
        const max = Math.max(1, ...top.map(h => h.up_bps + h.down_bps));
        setHTML("top5", top.map(h => `
          <div class="hbar-row">
            <div class="hbar-label">${hostCell(h.ip, h.hostname, h.user)}</div>
            <div class="hbar-track"><div class="hbar-fill" style="animation:none;width:${(100 * (h.up_bps + h.down_bps) / max).toFixed(1)}%"></div></div>
            <div class="hbar-val">↓${fmtMbps(h.down_bps)} ↑${fmtMbps(h.up_bps)} Mbps</div>
          </div>`).join("") || '<div class="empty">Sin tráfico en este momento</div>');
      });

      // ntopng caído: los valores en vivo pasan a "sin datos" (no el último número)
      onLiveError(lastOk => {
        emaD = emaU = null;
        setText("k-bw-d", "—");
        setText("k-bw-u", "—");
        const when = lastOk ? new Date(lastOk).toLocaleTimeString("es-AR") : "—";
        setHTML("top5", `<div class="empty">${ico("alert")}Sin datos de ntopng · último dato válido ${esc(when)}</div>`);
        document.querySelector("#k-bw-d")?.closest(".kpi")?.querySelector(".kpi-sub")
          ?.replaceChildren(`sin datos de ntopng · último dato ${when}`);
      });

      // --- resumen del día ---------------------------------------------------------
      async function loadOverview() {
        const o = await api("/api/overview");
        if (!alive(gen)) return;
        const t = o.today, y = o.yesterday;
        setText("k-today", fmtBytes(t.internet));
        setHTML("k-today-s", `de internet · ${delta(t.internet, y.internet)}`);
        setText("k-hosts", t.hosts);
        setHTML("k-hosts-s", delta(t.hosts, y.hosts));
        const maxH = Math.max(1, ...o.top_hosts.map(h => h.internet));
        setHTML("o-hosts", o.top_hosts.length ? rankList(o.top_hosts.map(h => ({
          title: h.hostname || h.ip, sub: [h.hostname ? h.ip : "", h.user,
            h.equipos > 1 ? `${h.equipos} equipos usaron la IP hoy` : ""].filter(Boolean).join(" · "),
          value: fmtBytes(h.internet), p: pct(h.internet, maxH), href: `#/host/${h.ip}`, ip: h.ip }))) :
          '<div class="empty">Sin consumo todavía hoy</div>');
        const maxS = Math.max(1, ...o.top_sites.map(s => s.bytes));
        setHTML("o-sites", o.top_sites.length ? rankList(o.top_sites.map(s => ({
          title: s.site, value: fmtBytes(s.bytes), p: pct(s.bytes, maxS), href: `#/sitio/${enc(s.site)}` }))) :
          '<div class="empty">Sin sitios registrados hoy</div>');
        const maxU = Math.max(1, ...o.top_users.map(u => u.internet));
        setHTML("o-users", o.top_users.length ? rankList(o.top_users.map(u => ({
          title: u.user, sub: `${u.ips} equipo${u.ips === 1 ? "" : "s"} · total ${fmtBytes(u.total)}`,
          value: fmtBytes(u.internet), p: pct(u.internet, maxU), href: `#/usuario/${enc(u.user)}` }))) :
          '<div class="empty">Sin usuarios identificados hoy</div>');
        setHTML("k-alerts-s", `${t.alerts} hoy · ${delta(t.alerts, y.alerts)}`);
      }
      async function loadAlerts() {
        const open = await api("/api/alerts?only_open=true&limit=50");
        if (!alive(gen)) return;
        const kA = $("k-alerts");
        if (!kA) return;
        kA.textContent = open.length;
        const worst = open.some(a => a.severity === "critical") ? "crit" : open.length ? "warn" : "ok";
        kA.className = "kpi-value num state-" + worst;
        const recent = await api("/api/alerts?limit=6");
        if (!alive(gen)) return;
        setHTML("o-alerts", recent.map(a => alertItem(a, false)).join("") ||
          `<li class="empty">${ico("bell")}Sin alertas</li>`);
      }
      const openDrawer = () => $("bell-btn").click();
      $("k-alerts-card").addEventListener("click", openDrawer);
      $("o-all-alerts").addEventListener("click", openDrawer);

      // cada panel falla por separado: una fuente caída muestra "sin datos" en su
      // panel en vez de tirar toda la vista (auditoría H06)
      const noData = (ids, err) => ids.forEach(id => {
        const e = $(id); if (!e) return;
        const box = e.tagName === "CANVAS" ? e.parentElement : e;
        box.innerHTML = `<div class="empty">${ico("alert")}Sin datos (${esc(err.message)})</div>`;
      });
      const safe = (fn, ids) => (...a) => fn(...a).catch(err => { if (alive(gen)) noData(ids, err); });
      const sBW = safe(loadBW, ["bw-box"]), sCats = safe(loadCats, ["ch-cats"]),
            sOver = safe(loadOverview, ["o-hosts", "o-sites", "o-users"]), sAlerts = safe(loadAlerts, ["o-alerts"]);
      await Promise.all([sBW(bwRange), sCats(), sOver(), sAlerts()]);
      every(30000, () => { sBW(); sCats(); sAlerts(); });
      every(60000, sOver);
    },
  });

  // =========================================================================
  // CONSUMO POR EQUIPO (pestañas: Equipos | Flujos activos)
  // =========================================================================
  route("consumo", {
    async render(el, _p, gen) {
      setTitle("Consumo por equipo");
      page(el, `
        ${pageHead({ title: "Consumo por equipo", sub: "Cuánto transfirió cada equipo de la red. Clic en cualquier equipo para ver su ficha.",
                     actions: '<span id="tab-pick"></span>' })}
        <div id="tab-body"></div>`);
      const body = $("tab-body");
      let tab = mountSeg("tab-pick", "tab", [["equipos", "Equipos"], ["flujos", "Flujos activos"]], "equipos",
        v => { tab = v; (tab === "equipos" ? renderHosts : renderFlows)(); });

      // ---------------- Equipos ---------------------------------------------
      let hostRows = [], hostQ = qget("f");
      async function loadHosts() {
        hostRows = await api(`/api/top?range=${qget("r", "1h")}&limit=200`);
        if (alive(gen)) drawHosts();
      }
      function drawHosts() {
        if (tab !== "equipos" || !$("hosts-tbody")) return;
        // Mbps en vivo sólo si el dato es fresco; si no, la columna queda vacía
        const live = new Map((liveFresh() ? state.live.hosts : []).map(h => [h.ip, h]));
        let rows = hostRows.map(r => {
          const lv = live.get(r.ip) || {};
          return { ...r, hostname: r.hostname || lv.hostname || "", user: r.ad_user || lv.user || "",
                   down_bps: lv.down_bps || 0, up_bps: lv.up_bps || 0 };
        });
        if (hostQ) {
          const q = hostQ.toLowerCase();
          rows = rows.filter(r => r.ip.includes(q) || r.hostname.toLowerCase().includes(q) ||
                                  r.user.toLowerCase().includes(q));
        }
        const maxI = Math.max(1, ...rows.map(r => Number(r.bytes_internet) || 0));
        fill("hosts-tbody", rows.map(r => `
          <tr data-href="#/host/${esc(r.ip)}">
            <td data-v="${esc(r.hostname || r.ip)}">${hostCell(r.ip, r.hostname, r.user)}${r.equipos > 1
              ? `<span class="chip sm warn" title="Varios equipos usaron esta IP en el período: el consumo es de la IP, no de un solo equipo">${r.equipos} equipos</span>` : ""}</td>
            <td class="sub">${esc(vlanLabel(vlanOf(r.ip)))}</td>
            ${tdN(r.down_bps, r.down_bps ? fmtMbps(r.down_bps) : "")}
            ${tdN(r.up_bps, r.up_bps ? fmtMbps(r.up_bps) : "")}
            ${tdB(r.bytes_down)}${tdB(r.bytes_up)}${tdB(r.bytes_internet)}
            <td data-v="${Number(r.bytes_internet) || 0}" data-csv="">${barCell(pct(r.bytes_internet, maxI))}</td>
          </tr>`).join("") || emptyRow(8, "Sin resultados", "search"));
        setText("hosts-count", `${rows.length} equipos`);
      }
      function renderHosts() {
        const sortCol = qget("o") === "internet" ? 6 : 4;
        body.innerHTML = `
          <div class="card fade-in">
            <div class="toolbar">
              <div class="search-box">${ico("search")}<input type="search" id="host-q" placeholder="Filtrar por IP, equipo o usuario…" value="${esc(hostQ)}"></div>
              <span id="host-rp"></span>
              <span class="spacer"></span>
              <span class="sub" id="hosts-count"></span>
              ${csvBtn("hosts-table", "consumo_equipos")}
            </div>
            <div class="table-wrap">
              <table class="dt" id="hosts-table" data-sort-col="${sortCol}" data-sort-dir="desc">
                <thead><tr>
                  <th>Equipo</th><th>Red</th>
                  <th class="num">↓ Mbps</th><th class="num">↑ Mbps</th>
                  <th class="num ${sortCol === 4 ? "sorted" : ""}">Bajada</th><th class="num">Subida</th>
                  <th class="num ${sortCol === 6 ? "sorted" : ""}" title="Tráfico con internet (el resto es red interna)">Internet</th>
                  <th style="width:14%">Parte de internet</th>
                </tr></thead>
                <tbody id="hosts-tbody">${emptyRow(8, "Cargando…")}</tbody>
              </table>
            </div>
          </div>`;
        mountSeg("host-rp", "r", [["5m", "5 min"], ["1h", "1 h"], ["24h", "24 h"]], "1h", () => loadHosts(), "sm");
        $("host-q").addEventListener("input", ev => { hostQ = ev.target.value; qset({ f: hostQ }); drawHosts(); });
        loadHosts();
      }

      // ---------------- Flujos activos (en vivo, ntopng) ------------------------
      let flowFilters = { host: "", l7: "", port: "" };
      async function loadFlows() {
        if (tab !== "flujos" || !$("flows-tbody")) return;
        const p = new URLSearchParams({ sort: "bytes", limit: 150 });
        for (const [k, v] of Object.entries(flowFilters)) if (v) p.set(k, v);
        let rows;
        try { rows = await api("/api/flows?" + p); }
        catch { fill("flows-tbody", emptyRow(7, "ntopng no responde", "alert")); return; }
        if (!alive(gen)) return;
        fill("flows-tbody", rows.map(f => {
          const badges = (f.duration_s > 300 ? '<span class="flow-badge">LARGA</span>' : "") +
                         (f.bytes > 50 * 1048576 ? '<span class="flow-badge">VOLUMEN</span>' : "");
          return `<tr>
            <td data-v="${esc(f.cli_ip)}">${hostCell(f.cli_ip, f.cli_name && f.cli_name !== f.cli_ip ? f.cli_name : "", "")}<span class="sub">puerto ${f.cli_port}</span></td>
            <td data-v="${esc(f.srv_ip)}">${ipLink(f.srv_ip)}<span class="sub">:${f.srv_port}
              ${f.srv_country ? ` · ${esc(f.srv_country)}` : ""}${f.srv_local ? " · LAN" : ""}</span></td>
            <td>${esc(f.l4)}</td>
            <td>${f.l7 ? appLink(f.l7) : '<span class="sub">?</span>'}${badges}</td>
            ${tdN(f.duration_s, fmtDur(f.duration_s))}${tdB(f.bytes)}${tdN(f.thpt_bps, fmtMbps(f.thpt_bps))}
          </tr>`; }).join("") || emptyRow(7, "Sin flujos que coincidan"));
      }
      function renderFlows() {
        body.innerHTML = `
          <div class="card fade-in">
            <div class="toolbar">
              <input type="search" id="f-host" placeholder="IP del equipo…" style="width:170px;margin:0">
              <input type="search" id="f-l7" placeholder="Aplicación…" style="width:170px;margin:0">
              <input type="number" id="f-port" placeholder="Puerto" style="width:110px;margin:0">
              <span class="spacer"></span>
              <span class="sub">en vivo · se actualiza cada 10 s</span>
              ${csvBtn("flows-table", "flujos_activos")}
            </div>
            <div class="table-wrap">
              <table class="dt" id="flows-table" data-sort-col="5" data-sort-dir="desc">
                <thead><tr><th>Origen</th><th>Destino</th><th>L4</th><th>Aplicación</th>
                  <th class="num">Duración</th><th class="num sorted">Bytes</th><th class="num">Mbps</th></tr></thead>
                <tbody id="flows-tbody">${emptyRow(7, "Cargando flujos…")}</tbody>
              </table>
            </div>
          </div>`;
        ["f-host", "f-l7", "f-port"].forEach(id => $(id).addEventListener("input", () => {
          flowFilters = { host: $("f-host").value.trim(), l7: $("f-l7").value.trim(), port: $("f-port").value.trim() };
          loadFlows();
        }));
        loadFlows();
      }

      (tab === "equipos" ? renderHosts : renderFlows)();
      onLive(() => { if (tab === "equipos") drawHosts(); });
      every(10000, () => { if (tab === "flujos") loadFlows(); });
      every(60000, () => { if (tab === "equipos") loadHosts(); });
    },
  });

  // =========================================================================
  // FICHA DE EQUIPO
  // =========================================================================
  route("host", {
    async render(el, params, gen) {
      const ip = (params[0] || "").split("/")[0];
      // la ficha arranca con la tarjeta rápida (DB); el detalle en vivo de ntopng
      // (VLAN, SO, contactos activos) tarda varios segundos y se completa después
      const liveP = api("/api/hosts/" + enc(ip)).catch(() => null);
      let c;
      try { c = await api("/api/ipcard/" + enc(ip)); }
      catch (err) {
        setTitle(ip);
        page(el, `<div class="empty">${ico("alert")}No se pudo cargar el equipo ${esc(ip)} (${esc(err.message)}).<br><br>
          <a href="#/consumo">Volver a Consumo</a></div>`);
        return;
      }
      if (!alive(gen)) return;
      const name = c.hostname || c.ip;
      setTitle(name, { icon: "laptop", sub: `Equipo · ${c.ip}` });
      const chipsHTML = (live) => {
        const x = { ...c, ...(live || {}) };
        const seen = live?.live
          ? `<span class="chip ok"><span class="dot ok"></span>Activo ahora</span>`
          : live && live.source_ok === false
            ? `<span class="chip warn">${ico("alert")}Sin datos en vivo (ntopng no responde)</span>`
          : x.last_seen ? `<span class="chip">${ico("clock")}Visto ${esc(fmtAgo(x.last_seen))}</span>` : "";
        return [
          seen,
          x.user ? `<a class="chip accent" href="#/usuario/${enc(x.user)}">${ico("user")}${esc(x.user)}</a>` : "",
          vlanChip(live?.vlans, c.ip),
          live?.os ? `<span class="chip">${esc(live.os)}</span>` : "",
          x.vendor ? `<span class="chip">${esc(x.vendor)}</span>` : "",
          x.mac ? `<a class="chip" href="#/dispositivos?q=${enc(x.mac)}" title="Ver en Dispositivos"><span class="mono">${esc(x.mac)}</span></a>` : "",
          x.mac && macRandom(x.mac) ? `<span class="chip warn" title="El celular usa una MAC privada (aleatoria) para esta red Wi-Fi: identifica al equipo en esta red, no al aparato en general.">${ico("lock")}MAC privada</span>` : "",
        ].join("");
      };
      page(el, `
        ${pageHead({ avatar: `<span id="h-av">${avatar(deviceIcon(c))}</span>`, eyebrow: isLocalIP(ip) ? "Equipo de la red" : "Equipo remoto",
          title: esc(name), sub: c.hostname ? `<span class="mono">${esc(c.ip)}</span>` : "",
          chips: `<span class="chips" id="h-chips">${chipsHTML(null)}</span>`, actions: '<span id="h-rp"></span>' })}
        <div class="tabs"><span id="h-tabs"></span></div>
        <div id="h-body"></div>`);
      const EMPTY_LIVE = { live: false, peers: [], ports: [], countries: [], vlans: [] };
      let d = EMPTY_LIVE;
      liveP.then(live => {
        if (!live || !alive(gen)) return;
        d = live;
        setHTML("h-chips", chipsHTML(live));
        setHTML("h-av", avatar(deviceIcon({ ...c, ...live })));
      });

      const TABS = [["consumo", "Consumo"], ["sitios", "Sitios"], ["apps", "Apps"],
                    ["conexiones", "Conexiones"], ["alertas", "Alertas"], ["reporte", "Reporte"]];
      let tab = mountSeg("h-tabs", "tab", TABS, "consumo", v => { tab = v; renderTab(); });
      let range = mountSeg("h-rp", "r", RANGES, "24h", v => { range = v; renderTab(); }, "sm");
      const body = $("h-body");
      let chart = null;

      async function renderTab() {
        chart = null;
        const my = ++tabSeq;
        const isMine = () => my === tabSeq && alive(gen);
        body.innerHTML = '<div class="sk" style="height:320px;border-radius:18px"></div>';
        const fn = { consumo: tabConsumo, sitios: tabSitios, apps: tabApps, conexiones: tabConex,
                     alertas: tabAlertas, reporte: tabReporte }[tab] || tabConsumo;
        try { await fn(isMine); }
        catch (e) { if (isMine()) body.innerHTML = `<div class="empty">${ico("alert")}${esc(e.message)}</div>`; }
      }
      let tabSeq = 0;
      const show = (isMine, html) => { if (!isMine()) return false; body.innerHTML = `<div class="stack fade-in">${html}</div>`; return true; };

      // ---- Consumo -------------------------------------------------------------
      // Con DHCP la IP pasa de un equipo a otro: si en el período la usó más de
      // uno (o uno distinto del actual), se muestra quién y cuánto (auditoría H04)
      function assignmentsCard(a) {
        if (!a) return "";
        const segs = a.segments || [];
        const since = a.history_since ? new Date(a.history_since) : null;
        const rangeStart = Date.now() - ({ "1h": 1, "24h": 24, "7d": 168, "30d": 720 }[range] || 24) * 3600e3;
        const partial = !since || since.getTime() > rangeStart;
        const macs = new Set(segs.map(x => x.mac));
        if (macs.size < 2 && !(macs.size === 1 && c.mac && !macs.has(c.mac.toLowerCase()))) {
          return partial && since ? `<p class="note" style="margin:0">Historial de equipos por IP disponible desde ${fmtWhen(a.history_since)}.</p>` : "";
        }
        return `<div class="card" style="box-shadow:inset 0 0 0 1.5px var(--warn)">
          <div class="card-head"><div><h2>${ico("alert")} Esta IP la usaron ${macs.size} equipos en ${RANGE_LABELS[range]}</h2>
            <div class="ch-sub">El consumo de esta ficha es de la IP: suma a todos los equipos que la tuvieron. Abajo, cuánto le corresponde a cada uno.</div></div></div>
          <div class="table-wrap"><table class="dt">
            <thead><tr><th>Equipo</th><th>MAC</th><th>Fabricante</th><th>Desde</th><th>Hasta</th><th class="num">Consumo</th><th class="num">Internet</th></tr></thead>
            <tbody>${segs.map(x => `<tr>
              <td><b>${esc(x.hostname || "sin nombre")}</b></td>
              <td>${macLink(x.mac)}${macRandom(x.mac) ? ' <span class="chip sm warn">privada</span>' : ""}</td>
              <td class="sub">${esc(x.vendor || "")}</td>
              <td class="sub" data-v="${new Date(x.first_seen).getTime()}">${fmtWhen(x.first_seen)}</td>
              <td class="sub" data-v="${new Date(x.last_seen).getTime()}">${fmtWhen(x.last_seen)}</td>
              ${tdB(x.bytes)}${tdB(x.internet)}</tr>`).join("")}</tbody></table></div>
          ${partial ? `<p class="note">Historial disponible desde ${since ? fmtWhen(a.history_since) : "—"}: antes de esa hora no se puede saber qué equipo tenía la IP. El consumo por equipo sale de los datos por minuto (últimos 7 días).</p>` : ""}
        </div>`;
      }

      async function tabConsumo(isMine) {
        const [u, tl, hm, asg] = await Promise.all([
          api(`/api/hosts/${enc(ip)}/usage?range=${range}`),
          api(`/api/timeline?range=${range}&ip=${enc(ip)}`),
          api(`/api/hosts/${enc(ip)}/heatmap`),
          api(`/api/hosts/${enc(ip)}/assignments?range=${range}`).catch(() => null),
        ]);
        const tot = Number(u.bytes_up) + Number(u.bytes_down);
        const inet = Number(u.bytes_internet), uncl = Number(u.bytes_unclassified);
        const top = (u.domains && u.domains[0]) || null;
        const ratio = u.net_avg_total ? tot / u.net_avg_total : 0;
        const ratioTxt = !ratio ? "—" : ratio >= 1.1 ? `${ratio.toFixed(1)}×` : ratio >= .9 ? "≈ igual" : `${Math.round(ratio * 100)}%`;
        if (!show(isMine, `
          ${assignmentsCard(asg)}
          <div class="grid kpi-row">
            ${kpi({ label: "Total del período", icon: "chart", id: "u-total", value: fmtBytes(tot),
                    sub: `${RANGE_LABELS[range]}` })}
            ${kpi({ label: "Internet", icon: "globe", id: "u-inet", value: fmtBytes(inet),
                    sub: `Red interna ${fmtBytes(Math.max(tot - inet - uncl, 0))}${uncl ? ` · sin clasificar ${fmtBytes(uncl)}` : ""}` })}
            <div class="card kpi"><div class="kpi-label">${ico("arrows")}Bajada / subida</div>
              <div class="kpi-split"><span class="dir down">↓ <span class="num">${fmtBytes(u.bytes_down)}</span></span></div>
              <div class="kpi-sub"><span class="dir up">↑ ${fmtBytes(u.bytes_up)}</span></div></div>
            ${kpi({ label: "Donde más gastó", icon: "globe", id: "u-top", cls: "small",
                    value: top ? siteLink(top.name || top.domain) : "—", sub: top ? fmtBytes(top.bytes) + " de internet" : "" })}
            ${kpi({ label: "Comparado con la red", icon: "users", id: "u-cmp", value: ratioTxt,
                    sub: u.net_avg_total ? `del promedio (${fmtBytes(u.net_avg_total)}) · puesto ${u.rank} de ${u.net_hosts}` : "" })}
          </div>
          <div class="card">
            <div class="card-head"><div><h2>Tráfico en el tiempo</h2><div class="ch-sub">Mbps · ${RANGE_LABELS[range]}</div></div></div>
            <div class="chart-box chart-tall"><canvas id="ch-host"></canvas></div>
          </div>
          <div class="grid two-col">
            <div class="card"><div class="card-head"><div><h2>Por categoría</h2><div class="ch-sub">clic → quién más consumió esa categoría</div></div></div>
              <div id="u-cats"></div></div>
            <div class="card"><div class="card-head"><div><h2>Cuándo usa la red</h2><div class="ch-sub">últimos 7 días, por hora · hora local</div></div></div>
              <div id="u-heat"></div></div>
          </div>`)) return;

        const toM = b => Number(b) * 8 / tl.bucket_s / 1e6;
        chart = mkChart($("ch-host"), lineCfg(tl.points.map(p => timeLabel(p.t, tl.bucket_s)), [
          { label: "Bajada", data: tl.points.map(p => toM(p.down)), color: cssVar("--accent") },
          { label: "Subida", data: tl.points.map(p => toM(p.up)), color: cssVar("--accent-2"), alpha: .18 },
          { label: "Internet (sub + baj)", data: tl.points.map(p => toM(p.internet)), color: cssVar("--warn"), fill: false, dash: [5, 4], width: 1.6 },
        ], { tooltip: { label: c => ` ${c.dataset.label}: ${c.raw.toFixed(2)} Mbps` } }));

        const maxC = Math.max(1, ...u.categories.map(c => Number(c.bytes_up) + Number(c.bytes_down)));
        setHTML("u-cats", u.categories.map(c => {
          const t = Number(c.bytes_up) + Number(c.bytes_down);
          return `<div class="hbar-row">
            <div class="hbar-label"><div class="name"><a class="ent" href="#/cat/${enc(c.category)}">${esc(CAT_LABELS[c.category] || c.category)}</a></div>
              <div class="who">↓ ${fmtBytes(c.bytes_down)} · ↑ ${fmtBytes(c.bytes_up)}</div></div>
            <div class="hbar-track"><div class="hbar-fill" style="width:${(100 * t / maxC).toFixed(1)}%;background:${catColor(c.category)}"></div></div>
            <div class="hbar-val">${fmtBytes(t)}</div></div>`; }).join("") ||
          '<div class="empty">Sin tráfico clasificado en el período</div>');
        setHTML("u-heat", heatmap(hm.hours));
      }

      // mapa de calor 7 días x 24 horas en hora local del navegador
      function heatmap(hours) {
        const byKey = new Map();
        for (const h of hours) {
          const t = new Date(h.t);
          const k = `${t.getFullYear()}-${t.getMonth()}-${t.getDate()}|${t.getHours()}`;
          byKey.set(k, (byKey.get(k) || 0) + Number(h.total));
        }
        const max = Math.max(1, ...byKey.values());
        const days = [];
        for (let i = 6; i >= 0; i--) { const d = new Date(); d.setHours(0, 0, 0, 0); d.setDate(d.getDate() - i); days.push(d); }
        let html = '<div class="heat"><span></span>' +
          Array.from({ length: 24 }, (_, h) => `<span class="hh">${h % 3 === 0 ? h : ""}</span>`).join("");
        for (const d of days) {
          const lbl = d.toLocaleDateString("es-AR", { weekday: "short", day: "numeric" });
          html += `<span class="hl">${esc(lbl)}</span>`;
          for (let h = 0; h < 24; h++) {
            const v = byKey.get(`${d.getFullYear()}-${d.getMonth()}-${d.getDate()}|${h}`) || 0;
            const a = v ? Math.round(18 + 82 * Math.sqrt(v / max)) : 0;
            html += `<span class="c" title="${esc(lbl)} ${h}:00 — ${fmtBytes(v)}"
              ${a ? `style="background:color-mix(in srgb, var(--accent) ${a}%, var(--card-2))"` : ""}></span>`;
          }
        }
        html += "</div>";
        html += `<div class="heat-legend">menos ${[0, 25, 55, 100].map(a =>
          `<i style="background:${a ? `color-mix(in srgb, var(--accent) ${a}%, var(--card-2))` : "var(--card-2)"}"></i>`).join("")} más</div>`;
        return hours.length ? html : '<div class="empty">Sin tráfico en los últimos 7 días</div>';
      }

      // ---- Sitios ---------------------------------------------------------------
      async function tabSitios(isMine) {
        const u = await api(`/api/hosts/${enc(ip)}/usage?range=${range}&limit=100`);
        const dom = u.domains || [];
        const total = sum(dom, x => x.bytes);
        // agrupar dominios con el mismo nombre amigable
        const agg = new Map();
        for (const x of dom) {
          const k = x.name || x.domain;
          const a = agg.get(k) || { site: k, bytes: 0, domains: [] };
          a.bytes += Number(x.bytes); a.domains.push(x.domain); agg.set(k, a);
        }
        const rows = [...agg.values()].sort((a, b) => b.bytes - a.bytes);
        show(isMine, `
          <div class="card">
            <div class="card-head"><div><h2>A dónde fue el tráfico de internet</h2>
              <div class="ch-sub">${RANGE_LABELS[range]} · ${fmtBytes(total)} en ${rows.length} sitios · clic en un sitio → quién más entró</div></div>
              ${csvBtn("hs-table", `sitios_${ip}`)}</div>
            <div class="table-wrap">
              <table class="dt" id="hs-table" data-sort-col="2" data-sort-dir="desc">
                <thead><tr><th>Sitio</th><th>Dominios</th><th class="num sorted">Tráfico</th><th style="width:30%">Parte</th></tr></thead>
                <tbody>${rows.map(r => `<tr data-href="#/sitio/${enc(r.site)}">
                  <td>${siteLink(r.site)}</td><td class="sub wrap">${esc(r.domains.join(", "))}</td>
                  ${tdB(r.bytes)}<td data-csv="${pct(r.bytes, total).toFixed(1)}%">${barCell(pct(r.bytes, total))}</td></tr>`).join("") ||
                  emptyRow(4, "Sin sitios en el período (tráfico interno o cifrado sin nombre de sitio)", "globe")}</tbody>
              </table>
            </div>
            <p class="note">Los sitios se detectan por el nombre del servidor (SNI). El tráfico cifrado sin SNI no muestra sitio.</p>
          </div>`);
      }

      // ---- Apps -----------------------------------------------------------------
      async function tabApps(isMine) {
        const u = await api(`/api/hosts/${enc(ip)}/usage?range=${range}&limit=100`);
        const apps = u.apps || [];
        const total = sum(apps, a => Number(a.bytes_up) + Number(a.bytes_down));
        show(isMine, `
          <div class="card">
            <div class="card-head"><div><h2>Aplicaciones y protocolos</h2>
              <div class="ch-sub">${RANGE_LABELS[range]} · según la clasificación de ntopng (nDPI)</div></div>
              ${csvBtn("ha-table", `apps_${ip}`)}</div>
            <div class="table-wrap">
              <table class="dt" id="ha-table" data-sort-col="4" data-sort-dir="desc">
                <thead><tr><th>Aplicación</th><th>Categoría</th><th class="num">Bajada</th><th class="num">Subida</th>
                  <th class="num sorted">Total</th><th style="width:22%">Parte</th></tr></thead>
                <tbody>${apps.map(a => { const t = Number(a.bytes_up) + Number(a.bytes_down); return `
                  <tr data-href="#/app/${enc(a.app)}"><td>${appLink(a.app)}</td><td data-v="${esc(a.category)}">${catLink(a.category)}</td>
                  ${tdB(a.bytes_down)}${tdB(a.bytes_up)}${tdB(t)}
                  <td data-csv="${pct(t, total).toFixed(1)}%">${barCell(pct(t, total), catColor(a.category))}</td></tr>`; }).join("") ||
                  emptyRow(6, "Sin aplicaciones registradas en el período", "app")}</tbody>
              </table>
            </div>
          </div>`);
      }

      // ---- Conexiones -----------------------------------------------------------
      async function tabConex(isMine) {
        const [hf, inbound] = await Promise.all([
          api(`/api/hosts/${enc(ip)}/flows?range=${range}&limit=300`),
          api(`/api/ip/${enc(ip)}?range=${range}`).catch(() => null),
        ]);
        const inb = inbound && inbound.hosts.length ? inbound : null;
        if (!show(isMine, `
          <div class="card">
            <div class="card-head"><div><h2>Con quién habló este equipo</h2>
              <div class="ch-sub">${hf.flows.length ? `${hf.remotos} destinos · ${fmtBytes(hf.bytes_total)} en total · ${fmtBytes(hf.bytes_internet)} a internet · ${RANGE_LABELS[range]}` : RANGE_LABELS[range]}</div></div>
              ${csvBtn("hc-table", `conexiones_${ip}`)}</div>
            <div class="table-wrap max">
              <table class="dt" id="hc-table" data-sort-col="6" data-sort-dir="desc">
                <thead><tr><th>Destino</th><th>Sitio</th><th class="num">Puerto</th><th>App</th><th>Ámbito</th><th>Sentido</th><th class="num sorted">Tráfico</th></tr></thead>
                <tbody>${hf.flows.map(f => `<tr>
                  <td data-v="${esc(f.remote_ip)}">${f.remote_name ? hostCell(f.remote_ip, f.remote_name, "") : ipLink(f.remote_ip)}</td>
                  <td>${f.site ? siteLink(f.site) : '<span class="sub">—</span>'}</td>
                  ${tdN(f.srv_port)}<td>${appLink(f.l7)}</td><td>${scopeChip(f.scope)}</td>
                  <td class="sub">${f.direction === "entrante" ? "↓ entrante" : "↑ saliente"}</td>${tdB(f.bytes)}</tr>`).join("") ||
                  emptyRow(7, "Sin conexiones registradas en el período", "arrows")}</tbody>
              </table>
            </div>
          </div>
          ${inb ? `
          <div class="card">
            <div class="card-head"><div><h2>Quién se conectó a este equipo</h2>
              <div class="ch-sub">${inb.hosts.length} equipos · ${fmtBytes(inb.bytes_total)} · como servidor</div></div>
              ${csvBtn("hin-table", `entrantes_${ip}`)}</div>
            <div class="table-wrap max-s">
              <table class="dt" id="hin-table" data-sort-col="2" data-sort-dir="desc">
                <thead><tr><th>Equipo</th><th>Servicios</th><th class="num sorted">Tráfico</th><th>Última vez</th></tr></thead>
                <tbody>${inb.hosts.map(h => `<tr data-href="${ipHref(h.ip)}">
                  <td data-v="${esc(h.hostname || h.ip)}">${hostCell(h.ip, h.hostname, h.user)}</td>
                  <td class="sub wrap">${esc(h.services || "")}</td>${tdB(h.bytes)}${tdT(h.last)}</tr>`).join("")}</tbody>
              </table>
            </div>
          </div>` : ""}
          <div id="h-live"><div class="sk" style="height:220px;border-radius:18px"></div></div>`)) return;
        const drawLive = dd => setHTML("h-live", liveHTML(dd));
        liveP.then(live => { if (isMine()) drawLive(live || EMPTY_LIVE); });
      }

      function liveHTML(d) {
        return `
          <div class="grid two-col">
            <div class="card"><div class="card-head"><div><h2>En este momento</h2><div class="ch-sub">flujos activos según ntopng</div></div></div>
              <div class="table-wrap max-s">
                <table class="dt"><thead><tr><th>Contacto</th><th>Ubicación</th><th class="num">Bytes</th><th class="num">Flujos</th></tr></thead>
                  <tbody>${d.peers.map(p => `<tr>
                    <td data-v="${esc(p.ip)}">${p.name ? hostCell(p.ip, p.name, "") : ipLink(p.ip)}</td>
                    <td>${p.local ? '<span class="chip sm local">LAN</span>' : (p.country ? `<span class="chip sm">${esc(p.country)}</span>` : "")}</td>
                    ${tdB(p.bytes)}${tdN(p.flows)}</tr>`).join("") || emptyRow(4, d.live ? "Sin flujos activos ahora" : "El equipo no está activo ahora")}</tbody>
                </table>
              </div></div>
            <div class="card"><h2>Servicios que usa</h2>
              <div class="port-chips">${d.ports.map(p => `<span class="chip" title="${fmtBytes(p.bytes)}">
                <span class="mono">${p.port}</span>${p.l7 ? " · " + esc(p.l7) : ""} <span class="sub">(${p.flows})</span></span>`).join("") || '<span class="sub">—</span>'}</div>
              <h2 style="margin-top:20px">Países de destino</h2>
              ${d.countries.map(c => `<div class="hbar-row" style="grid-template-columns:48px 1fr auto">
                <div class="mono">${esc(c.country)}</div>
                <div class="hbar-track"><div class="hbar-fill" style="width:${pct(c.bytes, d.countries[0]?.bytes).toFixed(1)}%"></div></div>
                <div class="hbar-val">${fmtBytes(c.bytes)}</div></div>`).join("") || '<div class="sub">Sin destinos externos activos</div>'}
            </div>
          </div>`;
      }

      // ---- Alertas ------------------------------------------------------------
      async function tabAlertas(isMine) {
        const rows = await api(`/api/alerts?ip=${enc(ip)}&limit=100`);
        show(isMine, `<div class="card"><div class="card-head"><div><h2>Alertas de este equipo</h2>
            <div class="ch-sub">${rows.length} registradas</div></div></div>
          <ul class="alert-list">${rows.map(a => alertItem(a, false)).join("") ||
            `<li class="empty">${ico("bell")}Sin alertas para este equipo</li>`}</ul></div>`);
      }

      // ---- Reporte ------------------------------------------------------------
      async function tabReporte(isMine) {
        if (state.role !== "admin" && state.role !== "viewer") {
          show(isMine, `<div class="card"><div class="empty">Los reportes requieren iniciar sesión.</div></div>`); return;
        }
        if (show(isMine, `<div class="card"><h2>Reporte de auditoría de este equipo</h2>${hostReportForm(ip)}</div>`))
          wireHostReportForm(body);
      }

      await renderTab();
    },
  });

  // =========================================================================
  // IP (vista desde el destino): quién de la red habló con esta IP
  // =========================================================================
  route("ip", {
    async render(el, params, gen) {
      const ip = (params[0] || "").split("/")[0];
      setTitle(ip);
      const local = isLocalIP(ip);
      page(el, `
        ${pageHead({ avatar: avatar(local ? "server" : "globe", local ? "" : "ext"),
          eyebrow: local ? "IP de la red · como destino" : "IP externa",
          title: `<span id="ip-title">${esc(ip)}</span>`, sub: `<span class="mono">${esc(ip)}</span> <span id="ip-sub"></span>`,
          chips: `<span id="ip-chips"></span>`, actions: '<span id="ip-rp"></span>' })}
        <div class="stack">
          <div class="grid kpi-row">
            ${kpi({ label: "Tráfico con esta IP", icon: "chart", id: "ip-total", subId: "ip-total-s" })}
            ${kpi({ label: "Equipos de la red", icon: "devices", id: "ip-hosts", sub: "hablaron con ella" })}
            ${kpi({ label: "Primera vez", icon: "clock", id: "ip-first", cls: "small" })}
            ${kpi({ label: "Última vez", icon: "clock", id: "ip-last", cls: "small" })}
          </div>
          <div class="card"><div class="card-head"><div><h2>Tráfico en el tiempo</h2><div class="ch-sub" id="ip-range"></div></div></div>
            <div class="chart-box"><canvas id="ch-ip"></canvas></div></div>
          <div class="card">
            <div class="card-head"><div><h2>Equipos que hablaron con esta IP</h2><div class="ch-sub">clic → ficha del equipo</div></div>${csvBtn("ip-table", `ip_${ip}`)}</div>
            <div class="table-wrap max">
              <table class="dt" id="ip-table" data-sort-col="2" data-sort-dir="desc">
                <thead><tr><th>Equipo</th><th>Servicios (puerto/app)</th><th class="num sorted">Tráfico</th><th style="width:18%">Parte</th><th>Última vez</th></tr></thead>
                <tbody id="ip-tbody"></tbody></table>
            </div>
          </div>
          <div class="grid two-col">
            <div class="card"><h2>Servicios</h2>
              <div class="table-wrap max-s"><table class="dt"><thead><tr><th class="num">Puerto</th><th>App</th><th>Sentido</th><th class="num">Tráfico</th></tr></thead>
                <tbody id="ip-svc"></tbody></table></div></div>
            <div class="card"><h2>Sitios / dominios</h2><div id="ip-doms" class="chips"></div>
              ${local ? `<p class="note"><a href="#/host/${esc(ip)}">Ver la ficha completa de este equipo →</a></p>` : ""}</div>
          </div>
        </div>`);
      let chart = null;
      async function load(range = qget("r", "24h")) {
        const d = await api(`/api/ip/${enc(ip)}?range=${range}`);
        if (!alive(gen)) return;
        const title = d.domains[0]?.site || d.hostname || ip;
        setTitle(title, { icon: local ? "server" : "globe", sub: `IP · ${ip}` });
        setText("ip-title", title);
        setHTML("ip-sub", [d.hostname && d.hostname !== title ? esc(d.hostname) : "", d.country ? esc(d.country) : ""].filter(Boolean).map(s => "· " + s).join(" "));
        setHTML("ip-chips", d.domains.slice(0, 1).map(x => `<a class="chip accent" href="#/sitio/${enc(x.site)}">${ico("globe")}${esc(x.site)}</a>`).join("") +
          (d.country ? `<span class="chip">${esc(d.country)}</span>` : "") + (local ? '<span class="chip local">Red interna</span>' : ""));
        setText("ip-total", fmtBytes(d.bytes_total));
        setText("ip-total-s", RANGE_LABELS[range]);
        setText("ip-hosts", d.hosts.length);
        setText("ip-first", d.first ? fmtWhen(d.first) : "—");
        setText("ip-last", d.last ? fmtAgo(d.last) : "—");
        setText("ip-range", RANGE_LABELS[range]);
        chart = upsertChart(chart, $("ch-ip"), barCfg(d.series.map(p => timeLabel(p.t, d.bucket_s)),
          d.series.map(p => Number(p.bytes)), cssVar("--accent")));
        fill("ip-tbody", d.hosts.map(h => `<tr data-href="${ipHref(h.ip)}">
          <td data-v="${esc(h.hostname || h.ip)}">${hostCell(h.ip, h.hostname, h.user)}</td>
          <td class="sub wrap">${esc(h.services || "")}</td>${tdB(h.bytes)}
          <td data-csv="${pct(h.bytes, d.bytes_total).toFixed(1)}%">${barCell(pct(h.bytes, d.bytes_total))}</td>${tdT(h.last)}</tr>`).join("") ||
          emptyRow(5, "Ningún equipo de la red habló con esta IP en el período (se registran las conexiones desde el 28/09)", "arrows"));
        fill("ip-svc", d.services.map(s => `<tr>${tdN(s.srv_port)}<td>${appLink(s.l7)}</td>
          <td class="sub">${s.direction === "entrante" ? "↓ entrante" : "↑ saliente"}</td>${tdB(s.bytes)}</tr>`).join("") || emptyRow(4, "—"));
        setHTML("ip-doms", d.domains.map(x => `<a class="chip" href="#/sitio/${enc(x.site)}">${esc(x.site)}${x.site !== x.domain ? ` <span class="sub">${esc(x.domain)}</span>` : ""} · ${fmtBytes(x.bytes)}</a>`).join("") ||
          '<span class="sub">Sin nombre de sitio (tráfico sin SNI o interno)</span>');
      }
      const r0 = mountSeg("ip-rp", "r", RANGES, "24h", r => load(r), "sm");
      await load(r0);
    },
  });

  // =========================================================================
  // SITIOS (lista) y SITIO (detalle)
  // =========================================================================
  route("sitios", {
    async render(el, _p, gen) {
      setTitle("Sitios");
      page(el, `
        ${pageHead({ title: "Sitios", sub: "Adónde va el tráfico de internet de toda la red, con nombres legibles.",
                     actions: '<span id="s-rp"></span>' })}
        <div class="stack">
          <div class="card"><div class="card-head"><div><h2>Los 12 sitios con más tráfico</h2><div class="ch-sub" id="s-range"></div></div></div>
            <div class="chart-box chart-tall"><canvas id="ch-sites"></canvas></div></div>
          <div class="card">
            <div class="card-head"><div><h2>Todos los sitios</h2><div class="ch-sub">clic → qué equipos entraron</div></div>
              <div class="ch-actions"><div class="search-box" style="width:220px">${ico("search")}<input type="search" id="s-q" placeholder="Filtrar…"></div>${csvBtn("s-table", "sitios")}</div></div>
            <div class="table-wrap">
              <table class="dt" id="s-table" data-sort-col="3" data-sort-dir="desc">
                <thead><tr><th class="num">#</th><th>Sitio</th><th class="num">Equipos</th><th class="num sorted">Tráfico</th><th style="width:26%">Parte</th></tr></thead>
                <tbody id="s-tbody"></tbody></table>
            </div>
            <p class="note" id="s-cov"></p>
          </div>
        </div>`);
      let chart = null, rows = [], q = "";
      function draw() {
        const total = sum(rows, s => s.bytes);
        const list = q ? rows.filter(s => s.site.toLowerCase().includes(q) || s.domains.some(x => x.includes(q))) : rows;
        fill("s-tbody", list.map(s => `<tr data-href="#/sitio/${enc(s.site)}">
          ${tdN(rows.indexOf(s) + 1)}<td><div class="cell-2"><span class="l1">${siteLink(s.site)}</span>
            ${s.domains.length > 1 || s.domains[0] !== s.site ? `<span class="l2">${esc(s.domains.slice(0, 4).join(", "))}${s.domains.length > 4 ? "…" : ""}</span>` : ""}</div></td>
          ${tdN(s.hosts)}${tdB(s.bytes)}<td data-csv="${pct(s.bytes, total).toFixed(1)}%">${barCell(pct(s.bytes, total))}</td></tr>`).join("") ||
          emptyRow(5, "Sin sitios en el período", "globe"));
      }
      async function load(range = qget("r", "24h")) {
        const d = await api(`/api/sites-top?range=${range}&limit=100`);
        if (!alive(gen)) return;
        rows = d.sites;
        setText("s-range", `${RANGE_LABELS[range]} · cubren el ${d.coverage_pct}% del tráfico de internet`);
        setHTML("s-cov", `Los sitios con nombre cubren el <b>${d.coverage_pct}%</b> del tráfico de internet
          (${fmtBytes(d.named_bytes)} de ${fmtBytes(d.internet_bytes)}, medido desde ${fmtWhen(d.coverage_since)}):
          ${d.coverage_sni_pct}% por el nombre que informa la conexión (SNI) y ${d.coverage_dns_pct}% por las consultas
          DNS que hizo el equipo antes de conectarse. El resto no tiene nombre visible (DNS cifrado del dispositivo)
          o son conexiones chicas que no entran al registro.`);
        const top = rows.slice(0, 12);
        chart = upsertChart(chart, $("ch-sites"), barCfg(top.map(s => s.site), top.map(s => s.bytes), cssVar("--accent"),
          { horizontal: true, onClick: (_e, els) => { if (els.length) location.hash = "#/sitio/" + enc(top[els[0].index].site); } }));
        draw();
      }
      $("s-q").addEventListener("input", ev => { q = ev.target.value.trim().toLowerCase(); draw(); });
      await load(mountSeg("s-rp", "r", RANGES, "24h", r => load(r), "sm"));
    },
  });

  route("sitio", {
    async render(el, params, gen) {
      const name = params[0] || "";
      setTitle(name, { icon: "globe", sub: "Sitio" });
      page(el, `
        ${pageHead({ avatar: avatar("globe", "site"), eyebrow: "Sitio de internet", title: esc(name),
          sub: '<span id="st-doms"></span>', actions: '<span id="st-rp"></span>' })}
        <div class="stack">
          <div class="grid kpi-row">
            ${kpi({ label: "Tráfico total", icon: "chart", id: "st-total", subId: "st-total-s" })}
            ${kpi({ label: "Equipos", icon: "devices", id: "st-hosts", sub: "entraron a este sitio" })}
            ${kpi({ label: "Mayor consumidor", icon: "user", id: "st-top", cls: "small", subId: "st-top-s" })}
            ${kpi({ label: "Su parte", icon: "chart", id: "st-share", sub: "del total del sitio" })}
          </div>
          <div class="card"><div class="card-head"><div><h2>En qué horario</h2><div class="ch-sub" id="st-range"></div></div></div>
            <div class="chart-box"><canvas id="ch-site"></canvas></div></div>
          <div class="card">
            <div class="card-head"><div><h2>Equipos que entraron</h2><div class="ch-sub">clic → ficha del equipo</div></div>${csvBtn("st-table", `sitio_${name}`)}</div>
            <div class="table-wrap">
              <table class="dt" id="st-table" data-sort-col="1" data-sort-dir="desc">
                <thead><tr><th>Equipo</th><th class="num sorted">Tráfico</th><th style="width:24%">Parte</th><th>Primera vez</th><th>Última vez</th></tr></thead>
                <tbody id="st-tbody"></tbody></table>
            </div>
            <p class="note">Registro de conexiones: se toman las principales conexiones de cada minuto; puede subestimar sitios con tráfico muy chico.</p>
          </div>
        </div>`);
      let chart = null;
      async function load(range = qget("r", "24h")) {
        const d = await api(`/api/site-hosts?name=${enc(name)}&range=${range}`);
        if (!alive(gen)) return;
        setHTML("st-doms", d.domains.map(x => `<span class="mono">${esc(x.domain)}</span>`).join(" · ") || "");
        setText("st-total", fmtBytes(d.bytes_total));
        setText("st-total-s", RANGE_LABELS[range]);
        setText("st-hosts", d.hosts_total);
        const top = d.hosts[0];
        setHTML("st-top", top ? `<a class="ent" href="#/host/${esc(top.ip)}" data-ip="${esc(top.ip)}">${esc(top.hostname || top.ip)}</a>` : "—");
        setHTML("st-top-s", top ? `${top.hostname ? esc(top.ip) + " · " : ""}${fmtBytes(top.bytes)}${top.user ? " · " + userLink(top.user) : ""}` : "");
        setText("st-share", top ? `${pct(top.bytes, d.bytes_total).toFixed(0)}%` : "—");
        setText("st-range", RANGE_LABELS[range]);
        chart = upsertChart(chart, $("ch-site"), barCfg(d.series.map(p => timeLabel(p.t, d.bucket_s)),
          d.series.map(p => Number(p.bytes)), cssVar("--accent")));
        fill("st-tbody", d.hosts.map(h => `<tr data-href="#/host/${esc(h.ip)}">
          <td data-v="${esc(h.hostname || h.ip)}">${hostCell(h.ip, h.hostname, h.user)}</td>${tdB(h.bytes)}
          <td data-csv="${pct(h.bytes, d.bytes_total).toFixed(1)}%">${barCell(pct(h.bytes, d.bytes_total))}</td>
          <td class="sub" data-v="${new Date(h.first).getTime()}">${fmtWhen(h.first)}</td>${tdT(h.last)}</tr>`).join("") ||
          emptyRow(5, "Nadie entró a este sitio en el período", "globe"));
      }
      await load(mountSeg("st-rp", "r", RANGES, "24h", r => load(r), "sm"));
    },
  });

  // =========================================================================
  // USUARIOS (lista) y USUARIO (detalle)
  // =========================================================================
  const USER_NOTE = "Cada minuto de tráfico se atribuye al usuario que tenía la IP en ese momento (inicio de sesión de dominio, vigente hasta 10 h o hasta que la IP pase a otro equipo o usuario). Los celulares sin usuario de dominio no aparecen acá.";

  route("usuarios", {
    async render(el, _p, gen) {
      setTitle("Usuarios");
      page(el, `
        ${pageHead({ title: "Usuarios", sub: "Consumo por persona (usuarios de dominio), sumando sus equipos.",
                     actions: '<span id="u-rp"></span>' })}
        <div class="card">
          <div class="card-head"><div><h2>Usuarios de dominio</h2><div class="ch-sub">clic → equipos y sitios de esa persona</div></div>
            <div class="ch-actions"><div class="search-box" style="width:220px">${ico("search")}<input type="search" id="u-q" placeholder="Filtrar…"></div>${csvBtn("u-table", "usuarios")}</div></div>
          <div class="table-wrap">
            <table class="dt" id="u-table" data-sort-col="2" data-sort-dir="desc">
              <thead><tr><th>Usuario</th><th>Equipos</th><th class="num sorted">Internet</th><th class="num">Total</th><th style="width:18%">Parte de internet</th><th>Visto</th></tr></thead>
              <tbody id="u-tbody"></tbody></table>
          </div>
          <p class="note">${USER_NOTE}</p>
        </div>`);
      let rows = [], q = "";
      function draw() {
        const total = sum(rows, u => u.internet);
        const list = q ? rows.filter(u => u.user.toLowerCase().includes(q) || u.hostnames.some(h => (h || "").toLowerCase().includes(q))) : rows;
        fill("u-tbody", list.map(u => `<tr data-href="#/usuario/${enc(u.user)}">
          <td data-v="${esc(u.user)}"><div class="cell-2"><span class="l1">${userLink(u.user)}</span></div></td>
          <td class="wrap" data-v="${u.ips.length}">${u.ips.map((ip, i) => ipLink(ip, u.hostnames[i] || ip)).join(", ")}</td>
          ${tdB(u.internet)}${tdB(u.total)}<td data-csv="${pct(u.internet, total).toFixed(1)}%">${barCell(pct(u.internet, total))}</td>${tdT(u.seen)}</tr>`).join("") ||
          emptyRow(6, "Sin usuarios identificados", "users"));
      }
      async function load(range = qget("r", "24h")) {
        const d = await api(`/api/users-usage?range=${range}`);
        if (!alive(gen)) return;
        rows = d.users; draw();
      }
      $("u-q").addEventListener("input", ev => { q = ev.target.value.trim().toLowerCase(); draw(); });
      await load(mountSeg("u-rp", "r", [["24h", "24 h"], ["7d", "7 días"]], "24h", r => load(r), "sm"));
    },
  });

  route("usuario", {
    async render(el, params, gen) {
      const name = params[0] || "";
      setTitle(name, { icon: "user", sub: "Usuario de dominio" });
      page(el, `
        ${pageHead({ avatar: avatar("user", "user"), eyebrow: "Usuario de dominio", title: esc(name),
          actions: '<span id="us-rp"></span>' })}
        <div class="stack">
          <div class="grid kpi-row">
            ${kpi({ label: "Internet", icon: "globe", id: "us-inet", subId: "us-inet-s" })}
            ${kpi({ label: "Tráfico total", icon: "chart", id: "us-total", sub: "incluye red interna" })}
            ${kpi({ label: "Equipos", icon: "devices", id: "us-dev", sub: "con su sesión de dominio" })}
            ${kpi({ label: "Sitio principal", icon: "globe", id: "us-site", cls: "small", subId: "us-site-s" })}
          </div>
          <div class="grid split-2-1">
            <div class="card"><div class="card-head"><div><h2>Sus equipos</h2><div class="ch-sub">clic → ficha del equipo</div></div>${csvBtn("us-table", `usuario_${name}`)}</div>
              <div class="table-wrap"><table class="dt" id="us-table" data-sort-col="3" data-sort-dir="desc">
                <thead><tr><th>Equipo</th><th>Red</th><th>MAC</th><th class="num sorted">Internet</th><th class="num">Total</th><th>Visto</th></tr></thead>
                <tbody id="us-tbody"></tbody></table></div></div>
            <div class="card"><div class="card-head"><div><h2>Sitios</h2><div class="ch-sub">internet, todos sus equipos</div></div></div>
              <div id="us-sites"></div></div>
          </div>
          <p class="note">${USER_NOTE}</p>
        </div>`);
      async function load(range = qget("r", "24h")) {
        const d = await api(`/api/user/${enc(name)}?range=${range}`);
        if (!alive(gen)) return;
        setText("us-inet", fmtBytes(d.internet));
        setText("us-inet-s", RANGE_LABELS[range]);
        setText("us-total", fmtBytes(d.total));
        setText("us-dev", d.devices.length);
        const s0 = d.sites[0];
        setHTML("us-site", s0 ? siteLink(s0.site) : "—");
        setText("us-site-s", s0 ? fmtBytes(s0.bytes) : "");
        fill("us-tbody", d.devices.map(x => `<tr data-href="#/host/${esc(x.ip)}">
          <td data-v="${esc(x.hostname || x.ip)}">${hostCell(x.ip, x.hostname, "")}</td>
          <td class="sub">${esc(vlanLabel(vlanOf(x.ip)))}</td><td>${macLink(x.mac)}</td>
          ${tdB(x.internet)}${tdB(x.total)}${tdT(x.last_seen)}</tr>`).join("") ||
          emptyRow(6, "Sin equipos con sesión de este usuario en las últimas horas", "devices"));
        const maxS = Math.max(1, ...d.sites.map(s => s.bytes));
        setHTML("us-sites", d.sites.length ? rankList(d.sites.slice(0, 12).map(s => ({
          title: s.site, value: fmtBytes(s.bytes), p: pct(s.bytes, maxS), href: `#/sitio/${enc(s.site)}` }))) :
          '<div class="empty">Sin sitios registrados</div>');
      }
      await load(mountSeg("us-rp", "r", RANGES, "24h", r => load(r), "sm"));
    },
  });

  // =========================================================================
  // APLICACIÓN / CATEGORÍA: qué equipos generaron el consumo
  // =========================================================================
  async function drillView(el, kind, name, gen) {
    const isCat = kind === "cat";
    const title = isCat ? (CAT_LABELS[name] || name) : name;
    setTitle(title, { icon: isCat ? "tag" : "app", sub: isCat ? "Categoría" : "Aplicación" });
    page(el, `
      ${pageHead({ avatar: avatar(isCat ? "tag" : "app", "app"), eyebrow: isCat ? "Categoría de consumo" : "Aplicación",
        title: esc(title), chips: '<span id="d-cat"></span>', actions: '<span id="d-rp"></span>' })}
      <div class="stack">
        <div class="grid kpi-row">
          ${kpi({ label: "Consumo total", icon: "chart", id: "d-total", subId: "d-range" })}
          ${kpi({ label: "Equipos", icon: "devices", id: "d-hosts", sub: isCat ? "consumieron esta categoría" : "usaron esta aplicación" })}
          ${kpi({ label: "Mayor consumidor", icon: "user", id: "d-top", cls: "small", subId: "d-top-sub" })}
          ${kpi({ label: "Su parte", icon: "chart", id: "d-share", sub: "del total" })}
        </div>
        ${isCat ? `
        <div class="card"><div class="card-head"><div><h2>Aplicaciones de esta categoría</h2><div class="ch-sub">clic → equipos de esa aplicación</div></div>${csvBtn("d-apps-t", `categoria_${name}_apps`)}</div>
          <div class="table-wrap max-s"><table class="dt" id="d-apps-t" data-sort-col="2" data-sort-dir="desc">
            <thead><tr><th>Aplicación</th><th class="num">Equipos</th><th class="num sorted">Total</th><th style="width:30%">Parte</th></tr></thead>
            <tbody id="d-apps"></tbody></table></div></div>` : ""}
        <div class="card">
          <div class="card-head"><div><h2>Equipos que generaron el consumo</h2><div class="ch-sub">clic → ficha del equipo</div></div>${csvBtn("d-table", `${kind}_${name}`)}</div>
          <div class="table-wrap"><table class="dt" id="d-table" data-sort-col="4" data-sort-dir="desc">
            <thead><tr><th>Equipo</th><th>${isCat ? "En qué (apps)" : "Fabricante"}</th>
              <th class="num">Bajada</th><th class="num">Subida</th><th class="num sorted">Total</th>
              <th style="width:18%">Parte</th></tr></thead>
            <tbody id="d-tbody"></tbody></table></div>
          <p class="note" id="d-since"></p>
        </div>
      </div>`);

    async function load(range = qget("r", "7d")) {
      const q = `name=${enc(name)}&range=${range}`;
      const d = await api(isCat ? `/api/category-hosts?${q}` : `/api/app-hosts?${q}`);
      if (!alive(gen)) return;
      const color = catColor(d.category);
      const total = Number(d.bytes_total);
      setHTML("d-cat", isCat ? "" : catLink(d.category));
      setText("d-total", fmtBytes(total));
      setText("d-range", RANGE_LABELS[range]);
      setText("d-hosts", d.hosts_total);
      const top = d.hosts[0];
      const topTot = top ? Number(top.bytes_up) + Number(top.bytes_down) : 0;
      setHTML("d-top", top ? `<a class="ent" href="#/host/${esc(top.ip)}" data-ip="${esc(top.ip)}">${esc(top.hostname || top.ip)}</a>` : "—");
      setHTML("d-top-sub", top ? `${top.hostname ? esc(top.ip) + " · " : ""}${fmtBytes(topTot)}${top.ad_user ? " · " + userLink(top.ad_user) : ""}` : "");
      setText("d-share", top && total ? `${pct(topTot, total).toFixed(0)}%` : "—");
      if (isCat) {
        const appsTot = sum(d.apps, a => a.total) || 1;
        fill("d-apps", d.apps.map(a => `<tr data-href="#/app/${enc(a.app)}"><td>${appLink(a.app)}</td>
          ${tdN(a.hosts)}${tdB(a.total)}<td data-csv="${pct(a.total, appsTot).toFixed(1)}%">${barCell(pct(a.total, appsTot), color)}</td></tr>`).join("") ||
          emptyRow(4, "Sin desglose por aplicación en el período"));
      }
      fill("d-tbody", d.hosts.map(h => {
        const t = Number(h.bytes_up) + Number(h.bytes_down);
        return `<tr data-href="#/host/${esc(h.ip)}">
          <td data-v="${esc(h.hostname || h.ip)}">${hostCell(h.ip, h.hostname, h.ad_user)}</td>
          ${isCat ? `<td class="sub">${(h.apps || []).map(a => `${appLink(a.app)} ${fmtBytes(a.total)}`).join(" · ") || "—"}</td>`
                  : `<td class="sub">${esc(h.vendor) || "—"}</td>`}
          ${tdB(h.bytes_down)}${tdB(h.bytes_up)}${tdB(t)}
          <td data-csv="${pct(t, total).toFixed(1)}%">${barCell(pct(t, total), color)}</td></tr>`; }).join("") ||
        emptyRow(6, "Sin consumo en el período"));
      setText("d-since", d.apps_since ? `El desglose por aplicación se registra desde ${fmtWhen(d.apps_since)}.` : "");
    }
    await load(mountSeg("d-rp", "r", RANGES, "7d", r => load(r), "sm"));
  }
  route("app", { render: (el, params, gen) => drillView(el, "app", params[0] || "", gen) });
  route("cat", { render: (el, params, gen) => drillView(el, "cat", params[0] || "", gen) });

  // formulario de reporte por equipo (ficha del equipo y página de Reportes)
  function hostReportForm(fixedIp) {
    const today = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
    return `
      <div class="rule-row hr-form" ${fixedIp ? `data-ip="${esc(fixedIp)}"` : ""}>
        ${fixedIp ? "" : `<label>IP del equipo <input type="text" class="hr-ip" placeholder="10.10.10.x"></label>`}
        <label>Período <select class="hr-range">
          <option value="day">Día</option><option value="week">Semana (lun-dom)</option>
          <option value="custom">Rango de fechas</option></select></label>
        <label class="hr-ref-l">Fecha <input type="date" class="hr-ref" value="${today}"></label>
        <label class="hr-cus hidden">Desde <input type="date" class="hr-desde" value="${today}"></label>
        <label class="hr-cus hidden">Hasta <input type="date" class="hr-hasta" value="${today}"></label>
        <label class="hr-dev-l hidden">Equipo <select class="hr-dev"><option value="">Todos los equipos</option></select></label>
        <button class="hr-csv btn-ico">${ico("download")}CSV</button>
        <button class="hr-pdf primary btn-ico">${ico("download")}PDF</button>
      </div>
      <p class="note hr-msg">Incluye consumo hora por hora, picos de 5 minutos, en qué aplicaciones se gastó
        cada hora y a qué sitios fue el tráfico de internet. Rango máximo: 31 días.</p>`;
  }
  function wireHostReportForm(root) {
    root.querySelectorAll(".hr-form").forEach(form => {
      const sel = form.querySelector(".hr-range");
      const devL = form.querySelector(".hr-dev-l"), dev = form.querySelector(".hr-dev");
      sel.addEventListener("change", () => {
        const custom = sel.value === "custom";
        form.querySelectorAll(".hr-cus").forEach(x => x.classList.toggle("hidden", !custom));
        form.querySelector(".hr-ref-l").classList.toggle("hidden", custom);
      });
      // Si varios equipos tuvieron la IP, ofrecer elegir de cuál sacar el reporte
      async function loadDevices(ip) {
        devL.classList.add("hidden"); dev.length = 1; dev.value = "";
        if (!/^\d{1,3}(\.\d{1,3}){3}$/.test(ip)) return;
        try {
          const d = await api(`/api/hosts/${ip}/assignments?range=30d`);
          const seen = new Map();
          (d.segments || []).forEach(s => { if (s.mac && !seen.has(s.mac)) seen.set(s.mac, s.hostname || s.mac); });
          if (seen.size > 1) {
            for (const [mac, name] of seen) dev.add(new Option(`${name} (${mac})`, mac));
            devL.classList.remove("hidden");
          }
        } catch { /* sin datos de asignación: se reporta la IP completa */ }
      }
      if (form.dataset.ip) loadDevices(form.dataset.ip);
      const ipInp = form.querySelector(".hr-ip");
      if (ipInp) ipInp.addEventListener("change", () => loadDevices(ipInp.value.trim()));
      form.addEventListener("click", async ev => {
        const btn = ev.target.closest(".hr-csv, .hr-pdf");
        if (!btn) return;
        const ip = form.dataset.ip || form.querySelector(".hr-ip").value.trim();
        const msg = form.parentElement.querySelector(".hr-msg");
        if (!/^\d{1,3}(\.\d{1,3}){3}$/.test(ip)) { msg.textContent = "Ingresá una IP válida."; msg.className = "err hr-msg"; return; }
        const p = new URLSearchParams({ ip, range: sel.value, format: btn.classList.contains("hr-pdf") ? "pdf" : "csv" });
        if (sel.value === "custom") { p.set("desde", form.querySelector(".hr-desde").value); p.set("hasta", form.querySelector(".hr-hasta").value); }
        else p.set("ref", form.querySelector(".hr-ref").value);
        if (dev && dev.value) p.set("mac", dev.value);
        msg.textContent = "Generando…"; msg.className = "note hr-msg";
        const r = await fetch(apiUrl("/api/report/host?" + p.toString()));
        if (!r.ok) {
          let detail = `HTTP ${r.status}`;
          try { detail = (await r.json()).detail || detail; } catch {}
          msg.textContent = "No se pudo generar: " + detail; msg.className = "err hr-msg";
          return;
        }
        const fname = (r.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/)?.[1] || `reporte_${ip}.${p.get("format")}`;
        const a = document.createElement("a");
        a.href = URL.createObjectURL(await r.blob());
        a.download = fname;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 10000);
        msg.textContent = `Descargado: ${fname}`; msg.className = "note hr-msg";
      });
    });
  }

  // =========================================================================
  // CONEXIONES DE LA RED (registro de flujos)
  // =========================================================================
  route("conexiones", {
    async render(el, _p, gen) {
      setTitle("Conexiones");
      page(el, `
        ${pageHead({ title: "Conexiones", sub: "Con quién habla cada equipo: internet y red interna. Se excluyen las cámaras (RTSP).",
                     actions: '<span id="cx-scope"></span><span id="cx-rp"></span>' })}
        <div class="stack">
          <div class="card">
            <div class="card-head"><div><h2>Principales conexiones</h2><div class="ch-sub" id="cx-sub"></div></div>
              <div class="ch-actions"><div class="search-box" style="width:240px">${ico("search")}<input type="search" id="cx-q" placeholder="Filtrar por IP, equipo, sitio, app…"></div>${csvBtn("cx-table", "conexiones")}</div></div>
            <div class="table-wrap">
              <table class="dt" id="cx-table" data-sort-col="6" data-sort-dir="desc">
                <thead><tr><th>Equipo local</th><th>Destino</th><th>Sitio</th><th class="num">Puerto</th><th>App</th><th>Ámbito</th><th class="num sorted">Tráfico</th></tr></thead>
                <tbody id="cx-body"></tbody></table>
            </div>
          </div>
        </div>`);
      let rows = [], q = "";
      function draw() {
        const list = !q ? rows : rows.filter(f => [f.local_ip, f.local_name, f.remote_ip, f.remote_name, f.site, f.l7, String(f.srv_port)]
          .some(x => (x || "").toLowerCase().includes(q)));
        fill("cx-body", list.map(f => `<tr>
          <td data-v="${esc(f.local_name || f.local_ip)}">${hostCell(f.local_ip, f.local_name, "")}</td>
          <td data-v="${esc(f.remote_ip)}">${f.remote_name ? hostCell(f.remote_ip, f.remote_name, "") : ipLink(f.remote_ip)}</td>
          <td>${f.site ? siteLink(f.site) : '<span class="sub">—</span>'}</td>
          ${tdN(f.srv_port)}<td>${appLink(f.l7)}</td><td>${scopeChip(f.scope)}</td>${tdB(f.bytes)}</tr>`).join("") ||
          emptyRow(7, "Sin conexiones registradas en el período", "arrows"));
        setText("cx-sub", `${list.length} conexiones · ${RANGE_LABELS[qget("r", "24h")]}`);
      }
      async function load() {
        const d = await api(`/api/flows-top?range=${qget("r", "24h")}&scope=${qget("s", "")}&limit=300`);
        if (!alive(gen)) return;
        rows = d.flows; draw();
      }
      mountSeg("cx-scope", "s", [["", "Todas"], ["internet", "Internet"], ["interno", "Interno"]], "", load, "sm");
      mountSeg("cx-rp", "r", RANGES, "24h", load, "sm");
      $("cx-q").addEventListener("input", ev => { q = ev.target.value.trim().toLowerCase(); draw(); });
      await load();
    },
  });

  // =========================================================================
  // DISPOSITIVOS
  // =========================================================================
  route("dispositivos", {
    async render(el, _p, gen) {
      setTitle("Dispositivos");
      page(el, `
        ${pageHead({ title: "Dispositivos", sub: "Inventario de equipos vistos en la red (por MAC)." })}
        <div class="card">
          <div class="toolbar">
            <div class="search-box">${ico("search")}<input type="search" id="d-q" placeholder="Buscar MAC, IP, equipo, fabricante…" value="${esc(qget("q"))}"></div>
            <label class="check"><input type="checkbox" id="d-new" ${qget("nuevos") ? "checked" : ""}> solo nuevos</label>
            <span class="spacer"></span>
            <span class="sub" id="d-count"></span>
            ${csvBtn("dv-table", "dispositivos")}
          </div>
          <div class="table-wrap">
            <table class="dt" id="dv-table" data-sort-col="5" data-sort-dir="desc">
              <thead><tr><th>Equipo</th><th>MAC</th><th>Fabricante</th><th>Red</th><th>Visto 1ª vez</th><th class="sorted">Último</th><th>Estado</th><th class="nosort"></th></tr></thead>
              <tbody id="dv-tbody"></tbody>
            </table>
          </div>
        </div>`);
      let rows = [], q = qget("q").toLowerCase();
      async function load() {
        rows = await api(`/api/devices?only_new=${$("d-new")?.checked || false}`);
        if (alive(gen)) draw();
      }
      function draw() {
        const list = !q ? rows : rows.filter(d => [d.mac, d.ip, d.hostname, d.vendor, d.ad_user]
          .some(x => (x || "").toLowerCase().includes(q)));
        setText("d-count", `${list.length} dispositivos`);
        const admin = state.role === "admin";
        fill("dv-tbody", list.map(d => `
          <tr class="${d.is_known ? "" : "is-new"}" ${d.ip ? `data-href="#/host/${esc(d.ip)}"` : ""}>
            <td data-v="${esc(d.hostname || d.ip || "")}">${d.ip ? hostCell(d.ip, d.hostname, "") : esc(d.hostname || "—")}</td>
            <td><span class="mono">${esc(d.mac)}</span>${macRandom(d.mac) ? ' <span class="chip sm warn" title="MAC privada (aleatoria)">privada</span>' : ""}</td>
            <td class="sub">${esc(d.vendor || "")}</td>
            <td class="sub">${d.ip ? esc(vlanLabel(vlanOf(d.ip))) : ""}</td>
            <td class="sub" data-v="${new Date(d.first_seen).getTime()}">${fmtWhen(d.first_seen)}</td>
            ${tdT(d.last_seen)}
            <td>${d.is_known ? '<span class="chip sm">confirmado</span>' : '<span class="chip sm warn">nuevo</span>'}</td>
            <td>${admin && !d.is_known ? `<button class="ack-btn" data-mac="${esc(d.mac)}">Confirmar</button>` : ""}</td>
          </tr>`).join("") || emptyRow(8, "Sin dispositivos", "devices"));
      }
      $("d-q").addEventListener("input", ev => { q = ev.target.value.trim().toLowerCase(); qset({ q: ev.target.value.trim() }); draw(); });
      $("d-new").addEventListener("change", ev => { qset({ nuevos: ev.target.checked ? "1" : "" }); load(); });
      el.querySelector(".page").addEventListener("click", async ev => {
        const btn = ev.target.closest("[data-mac]");
        if (!btn) return;
        await api(`/api/devices/${enc(btn.dataset.mac)}/ack`, { method: "POST" });
        load();
      });
      await load();
      every(60000, load);
    },
  });

  // =========================================================================
  // ESTADO DE RED
  // =========================================================================
  route("estado", {
    async render(el, _p, gen) {
      setTitle("Estado de red");
      page(el, `
        ${pageHead({ title: "Estado de red", sub: "Disponibilidad, latencia y caídas de los enlaces monitoreados.", actions: '<span id="e-rp"></span>' })}
        <div class="stack">
          <div class="grid kpi-row" id="e-targets"></div>
          <div class="card"><div class="card-head"><div><h2>Interfaz de captura en tiempo real</h2><div class="ch-sub">Mbps y paquetes por segundo</div></div></div>
            <div class="chart-box chart-tall"><canvas id="ch-rt"></canvas></div></div>
          <div class="card"><div class="card-head"><div><h2>Latencia y pérdida</h2><div class="ch-sub">últimas 24 h</div></div></div>
            <div class="chart-box chart-tall"><canvas id="ch-lat"></canvas></div></div>
          <div class="card"><div class="card-head"><h2>Historial de caídas</h2></div>
            <div class="table-wrap max-s"><table><thead><tr><th>Cuándo</th><th>Evento</th></tr></thead>
              <tbody id="e-outages"></tbody></table></div></div>
        </div>`);

      const TARGET_LABELS = { gateway: "Gateway (WatchGuard)", dns_interno: "DNS interno" };
      async function loadTargets(range = qget("r", "24h")) {
        const rows = await api(`/api/ping/summary?range=${range}`);
        if (!alive(gen)) return;
        setHTML("e-targets", rows.map(t => {
          const st = t.state || {};
          const name = esc(TARGET_LABELS[t.target] || t.target.replace("internet:", ""));
          // sin estado reciente o sin uptime medido: "sin datos", nunca 100 % (auditoría H11)
          const fresh = st.updated_at && (Date.now() - new Date(st.updated_at)) / 1000 < STALE_S;
          if (!fresh || t.uptime_pct == null) {
            return `<div class="card kpi"><div class="kpi-label"><span class="dot"></span>${name}</div>
              <div class="kpi-value">Sin datos</div>
              <div class="kpi-sub">última medición ${st.updated_at ? fmtWhen(st.updated_at) : "—"}</div></div>`;
          }
          const up = st.up === true;
          const uptime = Number(t.uptime_pct);
          const cls = !up ? "state-crit" : uptime < 99.5 ? "state-warn" : "state-ok";
          const cover = t.expected_minutes ? t.minutes / t.expected_minutes : 1;
          const coverTxt = cover < 0.95
            ? `<span>· medido ${Math.round(t.minutes / 60)} h de ${Math.round(t.expected_minutes / 60)} h (desde ${fmtWhen(t.first_ts)})</span>` : "";
          return `<div class="card kpi">
            <div class="kpi-label"><span class="dot ${up ? (uptime < 99.5 ? "warn" : "ok") : "crit"}"></span>${name}</div>
            <div class="kpi-value ${cls}">${up ? uptime.toFixed(2) + "%" : "Caído"}</div>
            <div class="kpi-sub">
              <span>rtt <b class="num">${t.rtt_avg != null ? Number(t.rtt_avg).toFixed(1) : "—"} ms</b></span>
              <span>· pérdida <b class="num">${t.loss_avg != null ? Number(t.loss_avg).toFixed(1) : "—"}%</b></span>
              <span>· caídas <b class="num">${t.downs}</b></span>
              ${st.since ? `<span>· desde ${fmtWhen(st.since)}</span>` : ""}
              ${coverTxt}
            </div></div>`;
        }).join("") || '<div class="empty">Sin datos del pinger todavía</div>');
      }
      const r0 = mountSeg("e-rp", "r", [["24h", "24 h"], ["7d", "7 días"]], "24h", r => loadTargets(r), "sm");

      const RT_KEEP = 450;
      const buf = state.rt.slice(-RT_KEEP);
      const rtCfg = lineCfg(buf.map(s => timeLabel(s.ts, 60)), [
        { label: "Mbps", data: buf.map(s => s.bps * 8 / 1e6), color: cssVar("--accent") },
        { label: "paquetes/s", data: buf.map(s => s.pps), color: cssVar("--muted"), fill: false, dash: [4, 3], width: 1.2, axis: "y2" },
      ], { y: { title: { display: true, text: "Mbps" } },
           scales: { x: { ...timeAxis, ticks: { ...timeAxis.ticks, maxTicksLimit: 6 } },
                     y2: { position: "right", beginAtZero: true, grid: { display: false }, border: { display: false }, title: { display: true, text: "pps" } } } });
      rtCfg.options.animation = false;
      const rtChart = mkChart($("ch-rt"), rtCfg);
      onRT(sample => {
        if (!rtChart) return;
        const d = rtChart.data;
        d.labels.push(timeLabel(sample.ts, 60));
        d.datasets[0].data.push(sample.bps * 8 / 1e6);
        d.datasets[1].data.push(sample.pps);
        while (d.labels.length > RT_KEEP) { d.labels.shift(); d.datasets.forEach(ds => ds.data.shift()); }
        rtChart.update("none");
      });

      async function loadLatency() {
        const rows = await api("/api/ping?range=24h");
        if (!alive(gen)) return;
        const targets = [...new Set(rows.map(r => r.target))];
        const buckets = [...new Set(rows.map(r => r.ts))].sort();
        const colors = [cssVar("--accent"), cssVar("--accent-2"), cssVar("--muted"), cssVar("--cat-social"), cssVar("--cat-productividad")];
        const datasets = targets.map((t, i) => {
          const byTs = Object.fromEntries(rows.filter(r => r.target === t).map(r => [r.ts, r]));
          return { label: TARGET_LABELS[t] || t.replace("internet:", ""), yAxisID: "y",
                   data: buckets.map(b => byTs[b]?.rtt_avg_ms ?? null),
                   borderColor: colors[i % colors.length], pointRadius: 0, borderWidth: 1.6, tension: .3 };
        });
        datasets.push({ type: "bar", label: "Pérdida % (peor)", yAxisID: "y2",
          data: buckets.map(b => Math.max(0, ...rows.filter(r => r.ts === b).map(r => r.loss_pct || 0))),
          backgroundColor: hexA(cssVar("--crit"), .4), borderRadius: 2 });
        mkChart($("ch-lat"), { type: "line", data: { labels: buckets.map(t => timeLabel(t, 60)), datasets },
          options: { maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
            scales: { x: timeAxis, y: valueAxis({ title: { display: true, text: "ms" } }),
              y2: { position: "right", beginAtZero: true, max: 100, grid: { display: false }, border: { display: false }, title: { display: true, text: "%" } } },
            plugins: { legend: { align: "end" } } } });
      }
      async function loadOutages() {
        const rows = await api("/api/alerts?kind=link_down&limit=20");
        if (!alive(gen)) return;
        setHTML("e-outages", rows.map(a => `<tr><td class="sub">${fmtWhen(a.ts)}</td><td class="wrap">${linkifyIPs(esc(a.message))}</td></tr>`).join("") ||
          emptyRow(2, "Sin caídas registradas"));
      }
      await Promise.all([loadTargets(r0), loadLatency(), loadOutages()]);
      every(30000, () => { loadTargets(); loadOutages(); });
      every(120000, loadLatency);
    },
  });

  // =========================================================================
  // REPORTES
  // =========================================================================
  route("reportes", {
    async render(el, _p, gen) {
      setTitle("Reportes");
      if (state.role !== "admin" && state.role !== "viewer") {
        page(el, `${pageHead({ title: "Reportes" })}<div class="card"><div class="empty">Los reportes requieren iniciar sesión.
          <br><br><button class="primary" onclick="document.getElementById('login-btn').click()">Ingresar</button></div></div>`);
        return;
      }
      const yesterday = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
      page(el, `
        ${pageHead({ title: "Reportes", sub: "Panorama de consumo, gráficos por día y documentos para compartir." })}
        <div class="stack" style="margin-bottom:20px">
          <div class="card">
            <div class="card-head">
              <div><h2>Panorama de consumo</h2><div class="ch-sub" id="ov-label">cargando…</div></div>
              <span id="ov-rp"></span>
            </div>
            <div class="grid kpi-row" style="margin-bottom:16px">
              ${kpi({ label: "Consumo total", icon: "chart", id: "ov-total", subId: "ov-total-s", sub: "del período" })}
              ${kpi({ label: "Internet", icon: "globe", id: "ov-inet", subId: "ov-inet-s", sub: "tráfico con el exterior" })}
              ${kpi({ label: "Red interna", icon: "wifi", id: "ov-intra", sub: "entre equipos de la red" })}
              ${kpi({ label: "Equipos activos", icon: "devices", id: "ov-hosts", sub: "con tráfico en el período", href: "#/consumo" })}
            </div>
            <div class="chart-box chart-tall"><canvas id="ch-daily"></canvas></div>
            <p class="note" id="ov-note">Cada barra es el consumo de toda la red ese día (hora local). Clic en un día para descargar su reporte.</p>
          </div>
          <div class="grid two-col">
            <div class="card">
              <div class="card-head"><div><h2>Consumo por categoría</h2><div class="ch-sub">del período</div></div></div>
              <div class="chart-box"><canvas id="ch-ov-cats"></canvas></div>
            </div>
            <div class="card">
              <div class="card-head"><div><h2>Equipos que más consumieron</h2></div>${csvBtn("ov-top", "reporte_top_equipos")}</div>
              <div class="table-wrap max">
                <table class="dt" id="ov-top" data-sort-col="3" data-sort-dir="desc">
                  <thead><tr><th>#</th><th>Equipo</th><th class="num">Internet</th><th class="num sorted">Total</th></tr></thead>
                  <tbody id="ov-tbody">${emptyRow(4, "Cargando…")}</tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
        <div class="grid two-col">
          <div class="card">
            <h2>Top consumidores de la red</h2>
            <div class="rule-row">
              <label>Período <select id="r-range"><option value="day">Día</option><option value="week">Semana (lun-dom)</option><option value="month">Mes</option></select></label>
              <label>Fecha de referencia <input type="date" id="r-date" value="${yesterday}"></label>
              <button id="r-csv" class="btn-ico">${ico("download")}CSV</button>
              <button id="r-pdf" class="primary btn-ico">${ico("download")}PDF</button>
            </div>
            <p class="note">Incluye el top de equipos con sus principales aplicaciones, las aplicaciones de
              toda la red con los equipos que más las usaron y el consumo por categoría.
              El lunes a las 07:00 se genera solo el reporte de la semana anterior.</p>
          </div>
          <div class="card"><h2>Reporte de un equipo (auditoría)</h2>${hostReportForm(null)}</div>
        </div>
        <div class="card" style="margin-top:20px">
          <div class="card-head"><h2>Reportes generados</h2></div>
          <div class="table-wrap max"><table class="dt" data-sort-col="2" data-sort-dir="desc"><thead><tr><th>Archivo</th><th class="num">Tamaño</th><th class="sorted">Fecha</th></tr></thead>
            <tbody id="r-files"></tbody></table></div>
        </div>`);
      // ---- Panorama de consumo (en pantalla) ----------------------------------
      let dayKeys = [], dailyChart = null, ovCatChart = null;
      async function loadOverview(days) {
        const hasta = new Date().toISOString().slice(0, 10);
        const desde = new Date(Date.now() - (days - 1) * 86400000).toISOString().slice(0, 10);
        const d = await api(`/api/report/overview?desde=${desde}&hasta=${hasta}&limit=20`);
        if (!alive(gen)) return;
        const t = d.totals;
        setText("ov-label", d.label);
        setText("ov-total", fmtBytes(t.total));
        setText("ov-total-s", `${fmtBytes(Math.round(t.total / Math.max(1, d.daily.length)))}/día en promedio`);
        setText("ov-inet", fmtBytes(t.bytes_internet));
        setText("ov-inet-s", `${pct(t.bytes_internet, t.total).toFixed(0)}% del total`);
        setText("ov-intra", fmtBytes(t.bytes_internal));
        setText("ov-hosts", String(d.active_hosts));

        // consumo por día (barras) — clic en un día carga su reporte para descargar
        dayKeys = d.daily.map(x => x.day);
        const labels = d.daily.map(x => { const p = x.day.split("-"); return `${p[2]}/${p[1]}`; });
        dailyChart = upsertChart(dailyChart, $("ch-daily"),
          barCfg(labels, d.daily.map(x => x.total), cssVar("--accent"), {
            onClick: (_e, els) => {
              if (!els.length) return;
              $("r-range").value = "day";
              $("r-date").value = dayKeys[els[0].index];
              $("r-range").scrollIntoView({ behavior: "smooth", block: "center" });
            },
          }));

        // consumo por categoría (doughnut)
        const cats = d.categories.filter(c => c.total > 0);
        const all = sum(cats, c => c.total) || 1;
        ovCatChart = upsertChart(ovCatChart, $("ch-ov-cats"), {
          type: "doughnut",
          data: { labels: cats.map(c => CAT_LABELS[c.category] || c.category),
            datasets: [{ data: cats.map(c => Number(c.total)),
              backgroundColor: cats.map(c => catColor(c.category)),
              borderColor: cssVar("--card"), borderWidth: 3, hoverOffset: 8, borderRadius: 4 }] },
          options: { maintainAspectRatio: false, cutout: "66%",
            onClick: (_e, els) => { if (els.length) location.hash = "#/cat/" + cats[els[0].index].category; },
            onHover: (e, els) => { e.native.target.style.cursor = els.length ? "pointer" : "default"; },
            plugins: { legend: { position: "right",
              labels: { font: { size: 12 }, padding: 12,
                generateLabels: () => cats.map((c, i) => ({
                  text: `${CAT_LABELS[c.category] || c.category}  ${(100 * c.total / all).toFixed(0)}%`,
                  fillStyle: catColor(c.category), strokeStyle: "transparent", index: i,
                  fontColor: cssVar("--text"), pointStyle: "circle" })) } },
              tooltip: { callbacks: { label: c => ` ${c.label}: ${fmtBytes(c.raw)}` } } } },
        });

        // equipos que más consumieron
        fill("ov-tbody", d.top.map((r, i) => `
          <tr data-href="#/host/${esc(r.ip)}">
            <td class="sub" data-v="${i + 1}">${i + 1}</td>
            <td data-v="${esc(r.hostname || r.ip)}">${hostCell(r.ip, r.hostname, r.ad_user)}</td>
            ${tdB(r.bytes_internet)}${tdB(r.total)}
          </tr>`).join("") || emptyRow(4, "Sin datos en el período", "search"));
      }
      const ovSel = mountSeg("ov-rp", "ov", [["7d", "7 días"], ["30d", "30 días"]], "30d",
        v => loadOverview(v === "7d" ? 7 : 30), "sm");
      loadOverview(ovSel === "7d" ? 7 : 30);

      const dl = fmt => { location.href = apiUrl(`/api/report?range=${$("r-range").value}&ref=${$("r-date").value}&format=${fmt}`); };
      $("r-csv").addEventListener("click", () => dl("csv"));
      $("r-pdf").addEventListener("click", () => dl("pdf"));
      wireHostReportForm(el);
      const files = await api("/api/reports/list");
      fill("r-files", files.map(f => `<tr>
        <td><a class="ent" href="${apiUrl("/api/reports/file/" + enc(f.name))}">${esc(f.name)}</a></td>
        ${tdN(f.size, fmtBytes(f.size))}<td class="sub" data-v="${new Date(f.mtime).getTime()}">${fmtWhen(f.mtime)}</td></tr>`).join("") ||
        emptyRow(3, "Todavía no hay reportes generados", "doc"));
    },
  });

  // =========================================================================
  // CONFIGURACIÓN
  // =========================================================================
  route("config", {
    async render(el) {
      setTitle("Configuración");
      if (state.role !== "admin") {
        page(el, `${pageHead({ title: "Configuración" })}<div class="card"><div class="empty">La configuración requiere sesión de administrador.
          <br><br><button class="primary" onclick="document.getElementById('login-btn').click()">Ingresar</button></div></div>`);
        return;
      }
      const [rules, catmap, info] = await Promise.all([api("/api/rules"), api("/api/category-map"), api("/api/config-info")]);
      const RULE_NAMES = {
        new_device: "Dispositivo nuevo", quota_daily: "Cuota diaria por equipo",
        banned_category: "Categoría prohibida", blocklist: "IP de mala reputación",
        disk_usage: "Espacio en disco", internal_fanout: "Escaneo interno / lateral",
        upload_spike: "Subida anómala", unusual_country: "País inusual",
      };
      page(el, `
        ${pageHead({ title: "Configuración", sub: "Reglas de alerta, categorías de consumo, usuarios y estado del sistema." })}
        <div class="stack">
          <div class="grid rule-grid" id="c-rules"></div>
          <div class="grid two-col">
            <div class="card">
              <div class="card-head"><div><h2>Aplicación → categoría</h2><div class="ch-sub">últimos 7 días · clic en una categoría o app → quién la consumió</div></div>${csvBtn("c-cats-t", "categorias")}</div>
              <div class="chips" id="c-catsum" style="margin-bottom:12px"></div>
              <div class="table-wrap max"><table class="dt" id="c-cats-t"><thead><tr><th>Aplicación (nDPI)</th><th class="num">Tráfico</th><th class="nosort">Categoría</th><th class="nosort"></th></tr></thead>
                <tbody id="c-cats"></tbody></table></div>
            </div>
            <div class="card">
              <h2>Sistema</h2>
              <dl class="kv">
                <dt>Notificación mail</dt><dd>${info.smtp ? "✓ configurada" : "✗ sin configurar (netmon.env)"}</dd>
                <dt>Webhook</dt><dd>${info.webhook ? "✓ configurado" : "✗ sin configurar (netmon.env)"}</dd>
                <dt>GeoIP (países)</dt><dd>${info.geoip ? "✓ base DB-IP cargada" : "✗ falta el mmdb (netmon-feeds)"}</dd>
                <dt>Disco ${esc(info.disk.path)}</dt><dd class="${info.disk.used_pct >= 90 ? "err" : ""}">
                  ${info.disk.used_pct}% usado · ${info.disk.free_gb} GB libres de ${info.disk.total_gb} GB</dd>
                <dt>Blocklist reputación</dt><dd>${info.blocklist ? "✓ FireHOL level1" : "✗ falta el netset (netmon-feeds)"}</dd>
                <dt>ntopng</dt><dd class="mono">${esc(info.ntopng_url)}</dd>
                <dt>Redes locales</dt><dd class="mono">${esc(info.local_networks)}</dd>
                ${Object.entries(info.retention).map(([k, v]) => `<dt>Retención ${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}
              </dl>
              <p class="note">Umbrales del pinger y retenciones se ajustan en <span class="mono">/etc/netmon/netmon.env</span> (reiniciar servicios).</p>
            </div>
          </div>
          <div class="card">
            <h2>Usuarios de netmon</h2>
            <p class="note" style="margin:0 0 14px"><b>Administrador</b>: acceso a todo. <b>Visualizador</b>: ve consumos,
              gráficos y reportes; no puede modificar configuración, reglas, categorías ni usuarios.</p>
            <form id="u-form" class="rule-row" autocomplete="off">
              <label>Usuario <input type="text" id="u-name" required minlength="3" maxlength="32" autocomplete="off"></label>
              <label>Clave <input type="password" id="u-pass" required minlength="8" autocomplete="new-password"></label>
              <label>Perfil <select id="u-newrole"><option value="viewer">Visualizador</option><option value="admin">Administrador</option></select></label>
              <button class="primary">Crear usuario</button>
            </form>
            <p id="u-msg" class="note"></p>
            <div class="table-wrap"><table><thead><tr><th>Usuario</th><th>Perfil</th><th>Estado</th><th>Último ingreso</th><th></th></tr></thead>
              <tbody id="u-tbody"></tbody></table></div>
          </div>
          <div class="card">
            <div class="card-head"><div><h2>Registro de consultas</h2>
              <div class="ch-sub">quién consultó datos de qué equipo, persona o sitio (últimas 300; se guardan 180 días)</div></div>
              ${csvBtn("al-table", "registro_consultas")}</div>
            <div class="table-wrap max"><table class="dt" id="al-table" data-sort-col="0" data-sort-dir="desc">
              <thead><tr><th class="sorted">Cuándo</th><th>Usuario</th><th>Rol</th><th>Desde IP</th><th>Consulta</th></tr></thead>
              <tbody id="al-tbody"></tbody></table></div>
          </div>
        </div>`);

      function paramFields(r) {
        if (r.id === "quota_daily")
          return `<label>GB/día de internet <input type="number" min="1" step="0.5" data-p="gb" value="${Number(r.params.gb ?? 15)}"></label>
                  <label>Excluir IPs <input type="text" data-p="exclude" placeholder="10.10.10.5, …" value="${esc((r.params.exclude || []).join(", "))}" style="width:200px"></label>`;
        if (r.id === "disk_usage")
          return `<label>Aviso al % <input type="number" min="50" max="99" data-p="pct" value="${Number(r.params.pct ?? 80)}" style="width:80px"></label>
                  <label>Crítico al % <input type="number" min="50" max="99" data-p="crit_pct" value="${Number(r.params.crit_pct ?? 90)}" style="width:80px"></label>`;
        if (r.id === "internal_fanout")
          return `<label>Máx. destinos internos/h <input type="number" min="2" data-p="max_destinos" value="${Number(r.params.max_destinos ?? 50)}" style="width:90px"></label>`;
        if (r.id === "upload_spike")
          return `<label>Piso MB <input type="number" min="1" data-p="min_mb" value="${Number(r.params.min_mb ?? 500)}" style="width:90px"></label>
                  <label>× sobre promedio <input type="number" min="2" step="0.5" data-p="factor" value="${Number(r.params.factor ?? 5)}" style="width:80px"></label>`;
        if (r.id === "unusual_country")
          return `<label>Países OK <input type="text" data-p="paises_ok" value="${esc((r.params.paises_ok || []).join(","))}" style="width:130px"></label>
                  <label>Mín. MB <input type="number" min="1" data-p="min_mb" value="${Number(r.params.min_mb ?? 200)}" style="width:90px"></label>`;
        if (r.id === "banned_category")
          return `<label>Categorías <input type="text" data-p="categories" value="${esc((r.params.categories || []).join(","))}"></label>
                  <label>Mín. MB <input type="number" min="1" data-p="min_mb" value="${Number(r.params.min_mb ?? 10)}"></label>`;
        return "";
      }
      setHTML("c-rules", rules.map(r => `
        <div class="card" data-rule="${esc(r.id)}">
          <div class="card-head"><h2>${esc(RULE_NAMES[r.id] || r.id)}</h2>
            <label class="check"><input type="checkbox" class="r-en" ${r.enabled ? "checked" : ""}> activa</label></div>
          <p class="note" style="margin:0 0 12px">${esc(r.description)}</p>
          <div class="rule-row">
            <label>Severidad <select class="r-sev" style="width:120px">
              ${["info", "warning", "critical"].map(s => `<option value="${s}" ${r.severity === s ? "selected" : ""}>${s}</option>`).join("")}
            </select></label>
            ${paramFields(r)}
            <button class="r-save">Guardar</button>
          </div>
        </div>`).join(""));

      $("c-rules").addEventListener("click", async ev => {
        const btn = ev.target.closest(".r-save");
        if (!btn) return;
        const card = btn.closest("[data-rule]");
        const params = {};
        card.querySelectorAll("[data-p]").forEach(inp => {
          params[inp.dataset.p] = ["categories", "exclude", "paises_ok"].includes(inp.dataset.p)
            ? inp.value.split(",").map(s => s.trim()).filter(Boolean) : Number(inp.value);
        });
        await api("/api/rules/" + card.dataset.rule, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: card.querySelector(".r-en").checked, severity: card.querySelector(".r-sev").value, params }),
        });
        btn.textContent = "Guardado ✓";
        setTimeout(() => (btn.textContent = "Guardar"), 1500);
      });

      function drawCats() {
        fill("c-cats", catmap.apps.map(a => `
          <tr><td>${appLink(a.app)}${a.overridden ? ' <span class="chip sm">manual</span>' : ""}</td>
            ${tdB(a.bytes)}
            <td data-csv="${esc(CAT_LABELS[a.category] || a.category)}"><select data-app="${esc(a.app)}" style="margin:0;width:160px">
              ${catmap.categories.map(c => `<option value="${c}" ${a.category === c ? "selected" : ""}>${CAT_LABELS[c] || c}</option>`).join("")}
            </select></td>
            <td>${a.overridden ? `<button class="ack-btn" data-reset="${esc(a.app)}">restaurar</button>` : ""}</td>
          </tr>`).join("") || emptyRow(4, "Todavía no hay apps registradas"));
      }
      drawCats();
      Promise.all(catmap.categories.map(c =>
        api(`/api/category-hosts?name=${enc(c)}&range=7d&limit=1`).then(r => ({ c, total: Number(r.bytes_total) })).catch(() => ({ c, total: 0 }))))
        .then(list => setHTML("c-catsum", list.sort((a, b) => b.total - a.total).map(({ c, total }) =>
          `<a href="#/cat/${enc(c)}" class="chip cat" style="--cat:${catColor(c)}">${esc(CAT_LABELS[c] || c)} · ${fmtBytes(total)}</a>`).join("")));
      $("c-cats").addEventListener("change", async ev => {
        const sel = ev.target.closest("select[data-app]");
        if (!sel) return;
        await api("/api/category-map", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ app: sel.dataset.app, category: sel.value }) });
        const a = catmap.apps.find(x => x.app === sel.dataset.app);
        if (a) { a.category = sel.value; a.overridden = true; }
        drawCats();
      });
      $("c-cats").addEventListener("click", async ev => {
        const btn = ev.target.closest("[data-reset]");
        if (!btn) return;
        await api("/api/category-map/" + enc(btn.dataset.reset), { method: "DELETE" });
        catmap.apps = (await api("/api/category-map")).apps;
        drawCats();
      });

      const ROLE_OPTS = { admin: "Administrador", viewer: "Visualizador" };
      async function drawUsers() {
        const users = await api("/api/users");
        setHTML("u-tbody", users.map(u => {
          const self = u.username === state.user;
          return `<tr data-user="${esc(u.username)}">
            <td><b>${esc(u.username)}</b>${self ? ' <span class="chip sm accent">vos</span>' : ""}</td>
            <td><select class="u-role" style="margin:0;width:160px" ${self ? "disabled" : ""}>
              ${Object.entries(ROLE_OPTS).map(([k, v]) => `<option value="${k}" ${u.role === k ? "selected" : ""}>${v}</option>`).join("")}</select></td>
            <td><label class="check"><input type="checkbox" class="u-en" ${u.enabled ? "checked" : ""} ${self ? "disabled" : ""}> activo</label></td>
            <td class="sub">${u.last_login ? fmtWhen(u.last_login) : "nunca"}</td>
            <td><button class="ack-btn u-pass">cambiar clave</button> ${self ? "" : '<button class="ack-btn u-del">eliminar</button>'}</td>
          </tr>`; }).join("") || emptyRow(5, 'Todavía no hay usuarios. Mientras tanto sigue valiendo la clave de administrador del .env (usuario "admin").'));
      }
      async function userCall(path, opts) {
        const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
        if (!r.ok) { let m = `HTTP ${r.status}`; try { m = (await r.json()).detail || m; } catch {} throw new Error(m); }
      }
      const userMsg = (text, ok) => { const m = $("u-msg"); m.textContent = text; m.className = ok ? "note" : "err"; };
      $("u-form").addEventListener("submit", async ev => {
        ev.preventDefault();
        try {
          await userCall("/api/users", { method: "POST", body: JSON.stringify({
            username: $("u-name").value.trim(), password: $("u-pass").value, role: $("u-newrole").value }) });
          ev.target.reset(); userMsg("Usuario creado ✓", true); await drawUsers();
        } catch (e) { userMsg(e.message, false); }
      });
      const usersBody = $("u-tbody");
      usersBody.addEventListener("change", async ev => {
        const tr = ev.target.closest("tr[data-user]");
        if (!tr) return;
        const body = ev.target.classList.contains("u-role") ? { role: ev.target.value }
                   : ev.target.classList.contains("u-en") ? { enabled: ev.target.checked } : null;
        if (!body) return;
        try { await userCall("/api/users/" + enc(tr.dataset.user), { method: "POST", body: JSON.stringify(body) });
              userMsg(`Usuario ${tr.dataset.user} actualizado ✓`, true); }
        catch (e) { userMsg(e.message, false); }
        await drawUsers();
      });
      usersBody.addEventListener("click", async ev => {
        const tr = ev.target.closest("tr[data-user]");
        if (!tr) return;
        const name = tr.dataset.user;
        try {
          if (ev.target.classList.contains("u-pass")) {
            const pw = prompt(`Nueva clave para ${name} (mínimo 8 caracteres):`);
            if (!pw) return;
            await userCall("/api/users/" + enc(name), { method: "POST", body: JSON.stringify({ password: pw }) });
            userMsg(`Clave de ${name} cambiada ✓`, true);
          } else if (ev.target.classList.contains("u-del")) {
            if (!confirm(`¿Eliminar el usuario ${name}?`)) return;
            await userCall("/api/users/" + enc(name), { method: "DELETE" });
            userMsg(`Usuario ${name} eliminado`, true);
            await drawUsers();
          }
        } catch (e) { userMsg(e.message, false); }
      });
      await drawUsers();
      api("/api/access-log").then(rows => fill("al-tbody", rows.map(r => `<tr>
          <td class="sub" data-v="${new Date(r.ts).getTime()}">${fmtWhen(r.ts)}</td>
          <td>${esc(r.user || "—")}</td><td class="sub">${esc(r.role)}</td>
          <td class="mono">${esc(r.client_ip)}</td>
          <td class="wrap"><span class="mono">${esc(r.path)}</span>${r.query ? `<span class="sub"> ?${esc(r.query)}</span>` : ""}</td>
        </tr>`).join("") || emptyRow(5, "Sin consultas registradas todavía"))).catch(() => {});
    },
  });

  // =========================================================================
  // MANTENIMIENTO DE DATOS (oculto, solo admin) — borrado de consumos
  // No figura en la navegación; se llega por #/mantenimiento y exige admin.
  // =========================================================================
  route("mantenimiento", {
    async render(el) {
      setTitle("Mantenimiento");
      if (state.role !== "admin") {
        page(el, `${pageHead({ title: "Mantenimiento" })}<div class="card"><div class="empty">No autorizado.</div></div>`);
        return;
      }
      const today = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
      page(el, `
        ${pageHead({ title: "Mantenimiento de datos", sub: "Elegí un equipo, mirá sus sitios de consumo y marcá cuáles borrar. Se respalda y audita cada borrado." })}
        <div class="card">
          <div class="rule-row">
            <label>IP del equipo <input type="text" id="pg-ip" placeholder="10.10.10.x"></label>
            <label>Equipo <select id="pg-dev"><option value="">Todos los equipos de esa IP</option></select></label>
            <label>Desde <input type="date" id="pg-desde" value="${today}"></label>
            <label>Hasta <input type="date" id="pg-hasta" value="${today}"></label>
            <button id="pg-ver" class="btn-ico">${ico("search")}Ver consumos</button>
          </div>
          <div id="pg-result"></div>
          <p class="note" id="pg-msg">Marcá los sitios que querés borrar. Afecta las vistas Sitios y Conexiones de ese equipo; el tráfico queda respaldado en el servidor y el borrado se registra en la auditoría.</p>
        </div>

        <div class="card" style="margin-top:16px">
          <h2>Borrar TODO el consumo en un lapso</h2>
          <div class="rule-row">
            <label>Granularidad <select id="pr-gran"><option value="day">Por día</option><option value="hour">Por hora</option></select></label>
            <label>Desde <input type="date" id="pr-d0" value="${today}"></label>
            <label>Hasta <input type="date" id="pr-d1" value="${today}"></label>
            <button id="pr-prev" class="btn-ico">${ico("search")}Previsualizar</button>
          </div>
          <div id="pr-result"></div>
          <div id="pr-confirm" class="hidden" style="margin-top:12px;border-top:1px solid var(--line);padding-top:12px">
            <label style="display:block;margin-bottom:8px">Nota (motivo) <input type="text" id="pr-note" placeholder="opcional" style="width:100%;max-width:420px"></label>
            <label style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
              <input type="checkbox" id="pr-ok"> Confirmo borrar TODO el consumo del equipo en ese lapso (se respalda, no se deshace solo).</label>
            <button id="pr-del" class="btn-ico" style="background:#dc2626;color:#fff;border-color:#dc2626" disabled>${ico("x")}Borrar todo el lapso</button>
          </div>
          <p class="note" id="pr-msg">Usa la IP y el equipo de arriba. Borra TODO el consumo en el lapso (totales, apps, categorías, sitios y conexiones). Antes de borrar se respalda y se audita.</p>
        </div>`);
      const $v = id => document.getElementById(id);
      const msg = $v("pg-msg"), result = $v("pg-result");
      const ipV = () => $v("pg-ip").value.trim();
      const validIp = ip => /^\d{1,3}(\.\d{1,3}){3}$/.test(ip);

      async function loadDevices(ip) {
        const dev = $v("pg-dev"); dev.length = 1; dev.value = "";
        if (!validIp(ip)) return;
        try {
          const d = await api(`/api/hosts/${ip}/assignments?range=30d`);
          const seen = new Map();
          (d.segments || []).forEach(s => { if (s.mac && !seen.has(s.mac)) seen.set(s.mac, s.hostname || s.mac); });
          for (const [mac, name] of seen) dev.add(new Option(`${name} (${mac})`, mac));
        } catch { /* sin asignaciones */ }
      }
      $v("pg-ip").addEventListener("change", () => { loadDevices(ipV()); result.innerHTML = ""; });

      // --- Borrar TODO el consumo en un lapso (por día u hora) ---
      const prMsg = $v("pr-msg"), prResult = $v("pr-result"), prConfirm = $v("pr-confirm"),
            prOk = $v("pr-ok"), prDel = $v("pr-del");
      $v("pr-gran").addEventListener("change", ev => {
        const hour = ev.target.value === "hour";
        const d0 = $v("pr-d0"), d1 = $v("pr-d1");
        d0.type = d1.type = hour ? "datetime-local" : "date";
        const now = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString();
        if (hour) { d0.value = now.slice(0, 11) + "00:00"; d1.value = now.slice(0, 16); }
        else { d0.value = d1.value = today; }
        prConfirm.classList.add("hidden"); prResult.innerHTML = "";
      });
      prOk.addEventListener("change", () => { prDel.disabled = !prOk.checked; });
      const prQs = () => {
        const p = new URLSearchParams({ ip: ipV(), desde: $v("pr-d0").value, hasta: $v("pr-d1").value });
        if ($v("pg-dev").value) p.set("mac", $v("pg-dev").value);
        return p;
      };
      $v("pr-prev").addEventListener("click", async () => {
        prConfirm.classList.add("hidden"); prOk.checked = false; prDel.disabled = true;
        if (!validIp(ipV())) { prMsg.textContent = "Ingresá una IP válida arriba."; prMsg.className = "err"; return; }
        prMsg.textContent = "Consultando…"; prMsg.className = "note"; prResult.innerHTML = "";
        try {
          const d = await api(`/api/admin/purge/preview?${prQs().toString()}`);
          if (!d.total) { prResult.innerHTML = `<div class="empty" style="margin-top:10px">No hay consumo en ese lapso para esta IP/equipo.</div>`; prMsg.textContent = ""; return; }
          const r = d.resumen || {}, nt = Object.keys(d.por_tabla || {}).length;
          const li = (name, bytes) => `<div style="display:flex;justify-content:space-between;gap:12px"><span>${esc(name)}</span><span class="mono">${fmtBytes(bytes)}</span></div>`;
          prResult.innerHTML = `
            <div class="card" style="margin-top:10px">
              <div style="font-size:1.1em;margin-bottom:10px">Vas a borrar <b>${fmtBytes(r.total_bytes || 0)}</b> de consumo
                <span class="sub">(${d.total} registros en ${nt} tablas)</span></div>
              <div class="grid two-col">
                <div><div class="sub" style="margin-bottom:4px">Aplicaciones</div>${(r.apps || []).map(a => li(a.app, a.bytes)).join("") || "—"}</div>
                <div><div class="sub" style="margin-bottom:4px">Sitios</div>${(r.sitios || []).map(s => li(s.site, s.bytes)).join("") || "—"}</div>
              </div>
              <div class="sub" style="margin-top:10px">Categorías: ${(r.categorias || []).map(c => `${esc(CAT_LABELS[c.category] || c.category)} ${fmtBytes(c.bytes)}`).join(" · ") || "—"}</div>
            </div>`;
          prMsg.textContent = ""; prMsg.className = "note";
          prConfirm.classList.remove("hidden");
        } catch (e) { prMsg.textContent = "No se pudo previsualizar: " + (e.message || e); prMsg.className = "err"; }
      });
      $v("pr-del").addEventListener("click", async () => {
        if (!prOk.checked) return;
        prDel.disabled = true; prMsg.textContent = "Borrando…"; prMsg.className = "note";
        try {
          const body = { ...Object.fromEntries(prQs()), note: $v("pr-note").value || "", confirm: true };
          const r = await api("/api/admin/purge", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
          prResult.innerHTML = `<div class="card" style="margin-top:10px"><b>Borrado: ${r.total} filas.</b><div class="sub">Backup: <span class="mono">${esc(r.backup)}</span></div></div>`;
          prConfirm.classList.add("hidden"); prMsg.textContent = "Listo. Queda registrado en la auditoría."; prMsg.className = "note";
        } catch (e) { prMsg.textContent = "No se pudo borrar: " + (e.message || e); prMsg.className = "err"; prDel.disabled = false; }
      });

      const qs = () => {
        const p = new URLSearchParams({ ip: ipV(), desde: $v("pg-desde").value, hasta: $v("pg-hasta").value });
        if ($v("pg-dev").value) p.set("mac", $v("pg-dev").value);
        return p;
      };

      $v("pg-ver").addEventListener("click", async () => {
        if (!validIp(ipV())) { msg.textContent = "Ingresá una IP válida."; msg.className = "err"; return; }
        msg.textContent = "Consultando…"; msg.className = "note"; result.innerHTML = "";
        let d;
        try { d = await api(`/api/admin/purge/sites?${qs().toString()}`); }
        catch (e) { msg.textContent = "No se pudo consultar: " + (e.message || e); msg.className = "err"; return; }
        const sites = d.sites || [];
        if (!sites.length) { result.innerHTML = `<div class="empty" style="margin-top:10px">Sin sitios de consumo en ese rango para este equipo.</div>`; msg.textContent = ""; return; }
        const max = Math.max(1, ...sites.map(s => s.bytes));
        result.innerHTML = `
          <div class="table-wrap max" style="margin-top:12px">
            <table class="dt"><thead><tr>
              <th style="width:36px"><input type="checkbox" id="pg-all" title="Marcar todos"></th>
              <th>Sitio</th><th class="num">Tráfico</th><th style="width:16%"></th>
            </tr></thead>
            <tbody>${sites.map(s => `<tr>
              <td><input type="checkbox" class="pg-site" data-domains="${esc((s.domains || []).join(","))}"></td>
              <td>${esc(s.site)}</td>
              ${tdB(s.bytes)}
              <td data-v="${s.bytes}">${barCell(pct(s.bytes, max))}</td></tr>`).join("")}</tbody></table>
          </div>
          <div style="margin-top:12px;border-top:1px solid var(--line);padding-top:12px">
            <label style="display:block;margin-bottom:8px">Nota (motivo del borrado) <input type="text" id="pg-note" placeholder="opcional" style="width:100%;max-width:420px"></label>
            <label style="display:flex;gap:8px;align-items:center;margin-bottom:10px">
              <input type="checkbox" id="pg-ok"> Confirmo el borrado de los sitios marcados (se respalda, pero no se deshace solo).</label>
            <button id="pg-del" class="btn-ico" style="background:#dc2626;color:#fff;border-color:#dc2626" disabled>${ico("x")}Borrar seleccionados (<span id="pg-n">0</span>)</button>
          </div>`;
        msg.textContent = ""; msg.className = "note";

        const marks = () => [...result.querySelectorAll(".pg-site:checked")];
        const refresh = () => {
          const n = marks().length;
          $v("pg-n").textContent = n;
          $v("pg-del").disabled = !(n > 0 && $v("pg-ok").checked);
        };
        result.addEventListener("change", ev => {
          if (ev.target.id === "pg-all") result.querySelectorAll(".pg-site").forEach(c => { c.checked = ev.target.checked; });
          refresh();
        });

        $v("pg-del").addEventListener("click", async () => {
          const sel = marks();
          if (!sel.length || !$v("pg-ok").checked) return;
          const domains = [...new Set(sel.flatMap(c => (c.dataset.domains || "").split(",").filter(Boolean)))];
          $v("pg-del").disabled = true; msg.textContent = "Borrando…"; msg.className = "note";
          try {
            const body = { ...Object.fromEntries(qs()), sites: domains, note: $v("pg-note").value || "", confirm: true };
            const r = await api("/api/admin/purge/sites", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
            msg.textContent = `Borrado: ${r.total} registros de ${sel.length} sitio(s). Backup: ${r.backup}`; msg.className = "note";
            $v("pg-ver").click();   // recargar la lista
          } catch (e) { msg.textContent = "No se pudo borrar: " + (e.message || e); msg.className = "err"; refresh(); }
        });
      });
    },
  });
})();
