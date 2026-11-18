/* Majordome – panneau latéral (aucune dépendance externe). */

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function md(text) {
  const lines = esc(text).split("\n");
  let html = "";
  let inList = false;
  for (const raw of lines) {
    let line = raw
      .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
    if (/^- /.test(line)) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${line.slice(2)}</li>`;
      continue;
    }
    if (/^\s+↳/.test(line) && inList) {
      html = html.replace(/<\/li>$/, `<div class="sub">${line.trim()}</div></li>`);
      continue;
    }
    if (inList) { html += "</ul>"; inList = false; }
    html += line.trim() ? `<p>${line}</p>` : "";
  }
  if (inList) html += "</ul>";
  return html;
}

const SEV = {
  critical: { label: "Critique", color: "var(--error-color, #db4437)" },
  warning: { label: "Attention", color: "var(--warning-color, #ffa600)" },
  info: { label: "Info", color: "var(--info-color, #039be5)" },
};
const RISK = {
  low: { label: "Risque faible", color: "var(--success-color, #43a047)" },
  medium: { label: "Risque moyen", color: "var(--warning-color, #ffa600)" },
  high: { label: "Risque élevé", color: "var(--error-color, #db4437)" },
  blocked: { label: "Bloquée", color: "#6d4c41" },
};
const TABS = [
  ["assistant", "Assistant", "mdi:robot-happy-outline"],
  ["health", "Santé", "mdi:heart-pulse"],
  ["updates", "Mises à jour", "mdi:update"],
  ["zigbee", "Zigbee", "mdi:zigbee"],
  ["logs", "Erreurs", "mdi:text-box-search-outline"],
  ["optimize", "Optimisation", "mdi:tune-variant"],
];
const SUGGESTIONS = [
  "Fais le bilan",
  "Quelles mises à jour sont sûres ?",
  "Comment va le Zigbee ?",
  "Quelles erreurs ?",
  "Optimise",
];

const store = {
  get(k, d) { try { const v = localStorage.getItem("majordome." + k); return v ? JSON.parse(v) : d; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("majordome." + k, JSON.stringify(v)); } catch (e) { /* stockage indisponible */ } },
};

function ago(ts) {
  if (!ts) return "—";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "à l'instant";
  if (s < 3600) return `il y a ${Math.round(s / 60)} min`;
  if (s < 86400) return `il y a ${Math.round(s / 3600)} h`;
  return `il y a ${Math.round(s / 86400)} j`;
}

class MajordomePanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._tab = store.get("tab", "assistant");
    this._chat = store.get("chat", []);
    this._report = null;
    this._notes = {};
    this._zfilter = "all";
    this._pending = false;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) {
      this._renderShell();
      this._load();
      this._timer = setInterval(() => this._load(), 60000);
    }
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) { menu.hass = hass; menu.narrow = this._narrow; }
  }
  set narrow(v) {
    this._narrow = v;
    const menu = this.shadowRoot && this.shadowRoot.querySelector("ha-menu-button");
    if (menu) menu.narrow = v;
  }
  set panel(p) { this._panel = p; }
  set route(r) { this._route = r; }

  disconnectedCallback() { clearInterval(this._timer); }

  async _load(scan = false) {
    try {
      this._report = await this._hass.callWS({ type: scan ? "majordome/scan" : "majordome/report" });
      this._error = null;
    } catch (e) {
      this._error = e.message || String(e);
    }
    this._renderHeader();
    if (this._tab !== "assistant") this._renderBody();
  }

  _toast(msg, ok = true) {
    const t = this.shadowRoot.getElementById("toast");
    t.textContent = msg;
    t.className = "toast show " + (ok ? "ok" : "ko");
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => (t.className = "toast"), 6000);
  }

  // ------------------------------------------------------------------ shell
  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>${CSS}</style>
      <div class="toolbar">
        <ha-menu-button></ha-menu-button>
        <div class="title">Majordome</div>
        <div class="spacer"></div>
        <div id="hdr"></div>
        <button class="btn" data-action="scan" title="Relancer une analyse complète">
          <ha-icon icon="mdi:refresh"></ha-icon><span class="hide-narrow">Analyser</span>
        </button>
      </div>
      <nav id="tabs"></nav>
      <main id="body"></main>
      <div id="toast" class="toast"></div>`;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    menu.hass = this._hass;
    menu.narrow = this._narrow;
    this.shadowRoot.addEventListener("click", (ev) => this._onClick(ev));
    this.shadowRoot.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" && ev.target.id === "chat-input") { ev.preventDefault(); this._send(); }
    });
    this._renderTabs();
    this._renderBody();
  }

  _renderTabs() {
    const issues = (this._report && this._report.issues) || [];
    const count = (cat) => issues.filter((i) => cat.includes(i.category) && i.severity !== "info").length;
    const badges = {
      health: issues.filter((i) => i.severity !== "info").length,
      updates: ((this._report && this._report.updates) || []).length,
      zigbee: count(["zigbee"]),
      logs: count(["logs"]),
      optimize: count(["config", "system", "repairs"]),
    };
    this.shadowRoot.getElementById("tabs").innerHTML = TABS.map(
      ([id, label, icon]) => `
      <button class="tab ${this._tab === id ? "active" : ""}" data-tab="${id}">
        <ha-icon icon="${icon}"></ha-icon><span>${label}</span>
        ${badges[id] ? `<span class="badge">${badges[id]}</span>` : ""}
      </button>`
    ).join("");
  }

  _renderHeader() {
    const r = this._report;
    const el = this.shadowRoot.getElementById("hdr");
    if (!el) return;
    if (this._error) { el.innerHTML = `<span class="err">${esc(this._error)}</span>`; return; }
    if (!r || r.score == null) { el.innerHTML = `<span class="muted">Analyse en cours…</span>`; return; }
    const c = r.score >= 85 ? "var(--success-color,#43a047)" : r.score >= 60 ? "var(--warning-color,#ffa600)" : "var(--error-color,#db4437)";
    el.innerHTML = `
      ${r.busy ? `<span class="busy"><ha-icon icon="mdi:progress-clock"></ha-icon>${esc(r.busy)}</span>` : ""}
      <div class="ring" style="--c:${c};--p:${r.score}"><span>${r.score}</span></div>`;
    this._renderTabs();
  }

  // ------------------------------------------------------------------ body
  _renderBody() {
    const body = this.shadowRoot.getElementById("body");
    if (!body) return;
    const r = this._report;
    if (this._tab === "assistant") { body.innerHTML = this._assistantHtml(); this._scrollChat(); return; }
    if (!r || r.score == null) {
      body.innerHTML = `<div class="empty"><ha-icon icon="mdi:timer-sand"></ha-icon><p>Première analyse en cours… (moins d'une minute)</p></div>`;
      return;
    }
    const views = {
      health: () => this._healthHtml(r),
      updates: () => this._updatesHtml(r),
      zigbee: () => this._zigbeeHtml(r),
      logs: () => this._logsHtml(r),
      optimize: () => this._optimizeHtml(r),
    };
    body.innerHTML = views[this._tab]();
  }

  _actionBtn(a, cls = "btn") {
    return `<button class="${cls}" data-action="${esc(a.action)}" data-params="${esc(JSON.stringify(a.params || {}))}"
      ${a.confirm ? `data-confirm="${esc(a.confirm)}"` : ""}>${esc(a.label)}</button>`;
  }

  _issueHtml(i) {
    const sev = SEV[i.severity] || SEV.info;
    return `
      <div class="card issue" style="--sev:${sev.color}">
        <div class="issue-head">
          <span class="chip" style="background:${sev.color}">${sev.label}</span>
          <div class="issue-title">${esc(i.title)}</div>
        </div>
        ${i.detail ? `<div class="detail">${esc(i.detail)}</div>` : ""}
        ${i.fix ? `<div class="fix"><ha-icon icon="mdi:lightbulb-on-outline"></ha-icon><div>${esc(i.fix)}</div></div>` : ""}
        <div class="actions">
          ${(i.actions || []).map((a) => this._actionBtn(a, "btn primary")).join("")}
          ${i.link ? `<button class="btn" data-action="open" data-params="${esc(JSON.stringify({ path: i.link }))}">Ouvrir</button>` : ""}
          <button class="btn ghost" data-action="ignore" data-params="${esc(JSON.stringify({ issue_id: i.id }))}"
            data-confirm="Ne plus afficher ce point ?">Ignorer</button>
        </div>
      </div>`;
  }

  _issuesList(issues, emptyText) {
    if (!issues.length) return `<div class="card ok-card"><ha-icon icon="mdi:check-circle"></ha-icon> ${esc(emptyText)}</div>`;
    return issues.map((i) => this._issueHtml(i)).join("");
  }

  // --- Assistant
  _assistantHtml() {
    const msgs = this._chat.length
      ? this._chat
      : [{ from: "bot", text: "Bonjour ! Je suis **Majordome**. Je surveille ta maison en local, sans IA externe.\nDemande-moi un bilan, l'état du Zigbee, les mises à jour sûres, ou pourquoi un appareil ne marche pas.", actions: [] }];
    return `
      <div class="chat">
        <div class="msgs" id="msgs">
          ${msgs.map((m) => `
            <div class="msg ${m.from}">
              <div class="bubble">${md(m.text)}
                ${m.actions && m.actions.length ? `<div class="actions">${m.actions.map((a) => this._actionBtn(a, "btn primary small")).join("")}</div>` : ""}
              </div>
            </div>`).join("")}
          ${this._pending ? `<div class="msg bot"><div class="bubble typing"><span></span><span></span><span></span></div></div>` : ""}
        </div>
        <div class="suggest">${SUGGESTIONS.map((s) => `<button class="chip-btn" data-ask="${esc(s)}">${esc(s)}</button>`).join("")}
          ${this._chat.length ? `<button class="chip-btn ghost" data-action="clear_chat">Effacer</button>` : ""}</div>
        <div class="composer">
          <input id="chat-input" type="text" placeholder="Ex. : pourquoi la lampe du salon ne s'allume pas ?" autocomplete="off" />
          <button class="btn primary" data-action="send"><ha-icon icon="mdi:send"></ha-icon></button>
        </div>
      </div>`;
  }

  _scrollChat() {
    const m = this.shadowRoot.getElementById("msgs");
    if (m) m.scrollTop = m.scrollHeight;
  }

  async _send(textArg) {
    const input = this.shadowRoot.getElementById("chat-input");
    const text = (textArg ?? (input ? input.value : "")).trim();
    if (!text || this._pending) return;
    this._chat.push({ from: "user", text, actions: [] });
    this._pending = true;
    this._renderBody();
    try {
      const res = await this._hass.callWS({ type: "majordome/chat", text });
      this._chat.push({ from: "bot", text: res.text, actions: res.actions || [] });
    } catch (e) {
      this._chat.push({ from: "bot", text: "Erreur : " + (e.message || e), actions: [] });
    }
    this._pending = false;
    this._chat = this._chat.slice(-60);
    store.set("chat", this._chat);
    this._renderBody();
    const again = this.shadowRoot.getElementById("chat-input");
    if (again) again.focus();
  }

  // --- Santé
  _healthHtml(r) {
    const crit = r.issues.filter((i) => i.severity === "critical").length;
    const warn = r.issues.filter((i) => i.severity === "warning").length;
    const info = r.issues.length - crit - warn;
    const when = r.generated ? new Date(r.generated).toLocaleString("fr-FR") : "—";
    return `
      <div class="tiles">
        <div class="tile"><div class="big">${r.score}<small>/100</small></div><div class="muted">Score de santé</div></div>
        <div class="tile"><div class="big" style="color:${SEV.critical.color}">${crit}</div><div class="muted">Critiques</div></div>
        <div class="tile"><div class="big" style="color:${SEV.warning.color}">${warn}</div><div class="muted">Avertissements</div></div>
        <div class="tile"><div class="big" style="color:${SEV.info.color}">${info}</div><div class="muted">Infos</div></div>
      </div>
      <div class="muted small-line">Dernière analyse : ${esc(when)}${r.ignored_count ? ` · ${r.ignored_count} point(s) ignoré(s) <a href="#" data-action="unignore_all" data-confirm="Réafficher les points ignorés ?">réafficher</a>` : ""}</div>
      ${this._issuesList(r.issues, "Aucun problème détecté. Ta maison est en pleine forme !")}`;
  }

  // --- Mises à jour
  _updatesHtml(r) {
    const ups = r.updates || [];
    const safe = ups.filter((u) => u.risk === "low" && u.can_install);
    const hist = r.update_history || [];
    const cards = ups.map((u) => {
      const risk = RISK[u.risk] || RISK.medium;
      const notes = this._notes[u.entity_id];
      const confirm =
        u.risk === "high"
          ? `RISQUE ÉLEVÉ pour ${u.title}.\nMajordome fera une sauvegarde puis vérifiera tout après l'installation.\nAs-tu lu les notes de version ? Continuer ?`
          : `Installer ${u.title} ${u.latest} ?\nUne sauvegarde est faite avant, puis Majordome vérifie que tout répond.`;
      return `
        <div class="card update">
          <div class="upd-head">
            ${u.picture ? `<img src="${esc(u.picture)}" alt="" />` : `<ha-icon icon="mdi:package-variant"></ha-icon>`}
            <div class="grow">
              <div class="issue-title">${esc(u.title)}</div>
              <div class="muted">${esc(u.source_label)} · ${esc(u.installed)} → <b>${esc(u.latest)}</b> (${esc({ major: "majeure", minor: "mineure", patch: "correctif" }[u.jump] || "?")})</div>
            </div>
            <span class="chip" style="background:${risk.color}">${risk.label}</span>
          </div>
          ${u.reasons.length ? `<ul class="reasons">${u.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
          <div class="fix"><ha-icon icon="mdi:lightbulb-on-outline"></ha-icon><div>${esc(u.recommendation)}</div></div>
          <div class="actions">
            ${u.can_install && !u.in_progress ? `<button class="btn primary" data-action="update" data-params="${esc(JSON.stringify({ entity_id: u.entity_id }))}" data-confirm="${esc(confirm)}">Mettre à jour en sécurité</button>` : ""}
            ${u.in_progress ? `<span class="muted">Installation en cours…</span>` : ""}
            <button class="btn" data-action="notes" data-entity="${esc(u.entity_id)}">${notes !== undefined ? "Masquer les notes" : "Notes de version"}</button>
            ${u.release_url ? `<a class="btn ghost" href="${esc(u.release_url)}" target="_blank" rel="noreferrer">Page de la version</a>` : ""}
          </div>
          ${notes !== undefined ? `<pre class="notes">${esc(notes || "Pas de notes disponibles.")}</pre>` : ""}
        </div>`;
    });
    const verdictColor = { ok: "var(--success-color,#43a047)", "régression": "var(--error-color,#db4437)" };
    return `
      <div class="bar">
        <div><b>${ups.length}</b> mise(s) à jour · <b>${safe.length}</b> sans risque</div>
        ${safe.length ? `<button class="btn primary" data-action="update_all_safe" data-params="{}" data-confirm="Sauvegarde puis installation des ${safe.length} correctifs sans risque, avec vérification après chacun. Continuer ?">Installer les ${safe.length} correctifs sûrs</button>` : ""}
      </div>
      ${cards.length ? cards.join("") : `<div class="card ok-card"><ha-icon icon="mdi:check-circle"></ha-icon> Tout est à jour.</div>`}
      ${hist.length ? `<h3>Historique Majordome</h3>
        <div class="card"><table><tr><th>Date</th><th>Élément</th><th>Version</th><th>Sauvegarde</th><th>Vérification</th></tr>
        ${hist.map((h) => `<tr><td>${new Date(h.time * 1000).toLocaleString("fr-FR")}</td><td>${esc(h.title)}</td><td>${esc(h.from)} → ${esc(h.to)}</td>
          <td>${h.backup ? "✅" : "—"}</td><td><span class="chip" style="background:${verdictColor[h.verdict] || "var(--secondary-text-color)"}">${esc(h.verdict)}</span> <span class="muted">${esc(h.details || "")}</span></td></tr>`).join("")}
        </table></div>` : ""}`;
  }

  // --- Zigbee
  _zigbeeHtml(r) {
    const z = r.zigbee || {};
    const s = z.summary || {};
    const devs = z.devices || [];
    const issues = r.issues.filter((i) => i.category === "zigbee");
    const offline = devs.filter((d) => d.availability === "offline");
    const weak = devs.filter((d) => d.lqi != null && d.lqi < 40);
    const filtered = this._zfilter === "offline" ? offline : this._zfilter === "weak" ? weak : devs;
    const lqi = (v) => v == null ? "—" : `<span class="lqi" style="--w:${Math.min(100, (v / 255) * 100)}%;--c:${v < 40 ? SEV.critical.color : v < 80 ? SEV.warning.color : RISK.low.color}">${v}</span>`;
    const status = (a) => a === "online" ? `<span class="dot on"></span>en ligne` : a === "offline" ? `<span class="dot off"></span><b>hors ligne</b>` : `<span class="dot"></span>inconnu`;
    const incidents = z.incidents || [];
    return `
      <div class="tiles">
        <div class="tile"><div class="big">${s.z2m ? (s.bridge_state === "online" ? "✅" : "⚠️") : "—"}</div><div class="muted">Zigbee2MQTT ${esc(s.version || "")}</div></div>
        <div class="tile"><div class="big">${devs.length}</div><div class="muted">Appareils</div></div>
        <div class="tile"><div class="big" style="color:${offline.length ? SEV.critical.color : "inherit"}">${offline.length}</div><div class="muted">Hors ligne</div></div>
        <div class="tile"><div class="big">${s.routers ?? "—"}</div><div class="muted">Routeurs (relais)</div></div>
        <div class="tile"><div class="big">${s.channel ?? "—"}</div><div class="muted">Canal</div></div>
      </div>
      ${s.coordinator_type ? `<div class="muted small-line">Dongle : ${esc(s.coordinator_type)} · firmware ${esc(s.coordinator_revision || "?")}${s.zha ? ` · ZHA : ${esc(s.zha_state)}` : ""}</div>` : ""}
      ${this._issuesList(issues, "Réseau Zigbee en bonne santé.")}
      <div class="bar">
        <div class="seg">
          ${[["all", `Tous (${devs.length})`], ["offline", `Hors ligne (${offline.length})`], ["weak", `Signal faible (${weak.length})`]]
            .map(([k, l]) => `<button class="${this._zfilter === k ? "on" : ""}" data-zfilter="${k}">${l}</button>`).join("")}
        </div>
        ${s.z2m ? `<button class="btn" data-action="z2m_restart" data-params="{}" data-confirm="Redémarrer Zigbee2MQTT ? Les appareils Zigbee seront injoignables ~30 s.">Redémarrer Z2M</button>` : ""}
      </div>
      <div class="card scroll"><table>
        <tr><th>Appareil</th><th>Type</th><th>État</th><th>Signal</th><th>Pile</th><th>Dernier msg</th><th></th></tr>
        ${filtered.map((d) => `<tr>
          <td><b>${esc(d.name)}</b><div class="muted">${esc(d.vendor || "")} ${esc(d.model || "")} · ${esc(d.source)}</div></td>
          <td>${esc(d.type === "Router" ? "Routeur" : d.type === "EndDevice" ? "Sur pile" : d.type || "—")}</td>
          <td>${status(d.availability)}</td><td>${lqi(d.lqi)}</td>
          <td>${d.battery != null ? esc(d.battery) + " %" : "—"}</td>
          <td>${d.seen_ago != null ? ago(Date.now() / 1000 - d.seen_ago) : "—"}</td>
          <td class="row-actions">${d.source === "Zigbee2MQTT" ? `
            <button class="btn small" data-action="z2m_ping" data-params="${esc(JSON.stringify({ device: d.name }))}">Réveiller</button>
            <button class="btn small ghost" data-action="z2m_reconfigure" data-params="${esc(JSON.stringify({ device: d.name }))}" data-confirm="Reconfigurer ${esc(d.name)} ?">Reconfig.</button>` : ""}</td>
        </tr>`).join("") || `<tr><td colspan="7" class="muted">Aucun appareil dans ce filtre.</td></tr>`}
      </table></div>
      <h3>Chien de garde</h3>
      <div class="card">${incidents.length ? `<table><tr><th>Quand</th><th>Résultat</th><th>Détail</th></tr>
        ${incidents.map((i) => `<tr><td>${ago(i.time)}</td><td>${esc(i.result)}</td><td>${esc(i.message)}</td></tr>`).join("")}</table>`
        : `<div class="muted">Aucune commande perdue. Le chien de garde vérifie chaque allumage/extinction Zigbee et renvoie la commande si l'appareil n'a pas obéi.</div>`}</div>`;
  }

  // --- Erreurs
  _logsHtml(r) {
    const groups = r.logs || [];
    if (!groups.length) return `<div class="card ok-card"><ha-icon icon="mdi:check-circle"></ha-icon> Journal propre depuis le dernier démarrage.</div>`;
    return groups.map((g) => {
      const sev = SEV[g.severity] || SEV.info;
      return `
        <div class="card issue" style="--sev:${sev.color}">
          <div class="issue-head">
            <span class="chip" style="background:${sev.color}">${g.count}×</span>
            <div class="issue-title">${esc(g.title)}</div>
          </div>
          <div class="detail">${esc(g.explain)}</div>
          <div class="muted">Concerné : ${esc(g.integrations.join(", "))} · dernier ${ago(g.last)}</div>
          <div class="fix"><ha-icon icon="mdi:lightbulb-on-outline"></ha-icon><div>${esc(g.fix)}</div></div>
          <details><summary>Voir les messages</summary><pre class="notes">${esc(g.samples.join("\n\n"))}</pre></details>
        </div>`;
    }).join("");
  }

  // --- Optimisation
  _optimizeHtml(r) {
    const sys = r.system || {};
    const issues = r.issues.filter((i) => ["config", "system", "repairs"].includes(i.category));
    const backup = sys.last_backup ? new Date(sys.last_backup).toLocaleString("fr-FR") : "—";
    const chatty = sys.chatty || [];
    return `
      <div class="tiles">
        <div class="tile"><div class="big">${sys.db_size_mb != null ? sys.db_size_mb : "—"}<small> Mo</small></div><div class="muted">Base historique</div></div>
        <div class="tile"><div class="big">${sys.disk_free_gb != null ? Number(sys.disk_free_gb).toFixed(1) : "—"}<small> Go</small></div><div class="muted">Disque libre</div></div>
        <div class="tile"><div class="big small-big">${esc(backup)}</div><div class="muted">Dernière sauvegarde</div></div>
        <div class="tile"><div class="big">${sys.orphans ?? 0}</div><div class="muted">Entités fantômes</div></div>
      </div>
      <div class="actions top"><button class="btn" data-action="backup_now" data-params="{}" data-confirm="Lancer une sauvegarde complète maintenant ?">Sauvegarder maintenant</button></div>
      ${this._issuesList(issues, "Configuration propre, rien à optimiser.")}
      ${chatty.length ? `<h3>Capteurs qui écrivent le plus dans l'historique (24 h)</h3>
        <div class="card"><table><tr><th>Entité</th><th>Écritures / jour</th></tr>
        ${chatty.map((c) => `<tr><td>${esc(c.entity_id)}</td><td>${c.per_day}</td></tr>`).join("")}</table></div>` : ""}`;
  }

  // ----------------------------------------------------------------- events
  async _onClick(ev) {
    const tab = ev.target.closest("[data-tab]");
    if (tab) { this._tab = tab.dataset.tab; store.set("tab", this._tab); this._renderTabs(); this._renderBody(); return; }
    const zf = ev.target.closest("[data-zfilter]");
    if (zf) { this._zfilter = zf.dataset.zfilter; this._renderBody(); return; }
    const ask = ev.target.closest("[data-ask]");
    if (ask) { this._send(ask.dataset.ask); return; }
    const el = ev.target.closest("[data-action]");
    if (!el) return;
    ev.preventDefault();
    const act = el.dataset.action;
    let params = {};
    try { params = JSON.parse(el.dataset.params || "{}"); } catch (e) { /* ignore */ }

    if (act === "send") return this._send();
    if (act === "clear_chat") { this._chat = []; store.set("chat", []); this._renderBody(); return; }
    if (act === "open") { this._navigate(params.path); return; }
    if (act === "notes") {
      const id = el.dataset.entity;
      if (this._notes[id] !== undefined) { delete this._notes[id]; this._renderBody(); return; }
      el.textContent = "Chargement…";
      try { this._notes[id] = (await this._hass.callWS({ type: "majordome/release_notes", entity_id: id })).notes; }
      catch (e) { this._notes[id] = "Impossible de charger les notes : " + (e.message || e); }
      this._renderBody();
      return;
    }
    if (act === "scan") {
      el.disabled = true;
      this._toast("Analyse complète en cours…");
      await this._load(true);
      el.disabled = false;
      this._toast("Analyse terminée.");
      return;
    }
    if (el.dataset.confirm && !window.confirm(el.dataset.confirm)) return;
    el.disabled = true;
    const label = el.textContent;
    el.textContent = "…";
    try {
      const res = await this._hass.callWS({ type: "majordome/action", action: act, params });
      this._toast(res.message || (res.ok ? "C'est fait." : "Échec."), res.ok !== false);
      if (res.needs_restart && window.confirm("Redémarrer Home Assistant maintenant pour terminer la mise à jour ?")) {
        await this._hass.callWS({ type: "majordome/action", action: "restart_ha", params: {} });
      }
    } catch (e) {
      this._toast("Erreur : " + (e.message || e), false);
    }
    el.disabled = false;
    el.textContent = label;
    await this._load();
  }

  _navigate(path) {
    if (!path) return;
    if (/^https?:/.test(path)) { window.open(path, "_blank", "noreferrer"); return; }
    history.pushState(null, "", path);
    window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
  }
}

const CSS = `
:host { display:block; min-height:100vh; background: var(--primary-background-color); color: var(--primary-text-color);
  font-family: var(--paper-font-body1_-_font-family, Roboto, system-ui, sans-serif); --r: 12px; }
.toolbar { display:flex; align-items:center; gap:8px; height:56px; padding:0 12px; background: var(--app-header-background-color, var(--primary-color));
  color: var(--app-header-text-color, #fff); position:sticky; top:0; z-index:3; }
.toolbar .title { font-size:20px; font-weight:500; }
.spacer { flex:1; }
#hdr { display:flex; align-items:center; gap:10px; }
.busy { display:flex; align-items:center; gap:4px; font-size:13px; opacity:.9; }
.ring { width:40px; height:40px; border-radius:50%; display:grid; place-items:center;
  background: conic-gradient(var(--c) calc(var(--p) * 1%), rgba(255,255,255,.25) 0); }
.ring span { width:32px; height:32px; border-radius:50%; display:grid; place-items:center; font-weight:700; font-size:13px;
  background: var(--app-header-background-color, var(--primary-color)); }
.toolbar .btn { background: rgba(255,255,255,.15); color: inherit; border-color: transparent; }
nav { display:flex; gap:4px; overflow-x:auto; padding:8px 12px; background: var(--card-background-color); border-bottom:1px solid var(--divider-color);
  position:sticky; top:56px; z-index:2; scrollbar-width:none; }
.tab { display:flex; align-items:center; gap:6px; padding:8px 12px; border:none; border-radius:20px; background:none; color: var(--secondary-text-color);
  font: inherit; font-size:14px; cursor:pointer; white-space:nowrap; }
.tab ha-icon { --mdc-icon-size:18px; }
.tab.active { background: color-mix(in srgb, var(--primary-color) 15%, transparent); color: var(--primary-color); font-weight:500; }
.badge { background: var(--error-color, #db4437); color:#fff; border-radius:10px; font-size:11px; padding:0 6px; line-height:18px; }
main { max-width: 1000px; margin: 0 auto; padding: 16px; box-sizing:border-box; }
.card { background: var(--card-background-color); border-radius: var(--r); padding:14px 16px; margin-bottom:12px;
  box-shadow: var(--ha-card-box-shadow, 0 1px 3px rgba(0,0,0,.12)); border: var(--ha-card-border-width,0) solid var(--divider-color); }
.issue { border-left: 4px solid var(--sev); }
.issue-head, .upd-head { display:flex; align-items:center; gap:10px; }
.upd-head img { width:36px; height:36px; border-radius:8px; }
.upd-head ha-icon { --mdc-icon-size:32px; color: var(--secondary-text-color); }
.grow { flex:1; min-width:0; }
.issue-title { font-weight:500; font-size:15px; }
.chip { color:#fff; font-size:11px; font-weight:600; padding:2px 8px; border-radius:10px; white-space:nowrap; }
.detail { margin:8px 0 4px; color: var(--primary-text-color); font-size:14px; overflow-wrap:anywhere; }
.fix { display:flex; gap:8px; margin-top:8px; padding:8px 10px; border-radius:8px; font-size:14px; white-space:pre-wrap;
  background: color-mix(in srgb, var(--primary-color) 8%, transparent); overflow-wrap:anywhere; }
.fix ha-icon { --mdc-icon-size:18px; color: var(--primary-color); flex:none; }
.actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; align-items:center; }
.actions.top { margin: 0 0 12px; }
.btn { display:inline-flex; align-items:center; gap:6px; padding:7px 14px; border-radius:18px; border:1px solid var(--divider-color);
  background: var(--card-background-color); color: var(--primary-text-color); font: inherit; font-size:14px; cursor:pointer; text-decoration:none; }
.btn:hover { filter: brightness(1.08); }
.btn:disabled { opacity:.5; cursor:wait; }
.btn.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: transparent; }
.btn.ghost { background: none; border-color: transparent; color: var(--secondary-text-color); }
.btn.small { padding:4px 10px; font-size:12px; }
.btn ha-icon { --mdc-icon-size:18px; }
.muted { color: var(--secondary-text-color); font-size:13px; }
.small-line { margin: -4px 0 12px; }
.err { color: #fff; background: var(--error-color); padding:4px 8px; border-radius:8px; font-size:13px; }
.tiles { display:grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap:12px; margin-bottom:12px; }
.tile { background: var(--card-background-color); border-radius: var(--r); padding:14px; box-shadow: var(--ha-card-box-shadow, 0 1px 3px rgba(0,0,0,.12)); }
.big { font-size:28px; font-weight:600; line-height:1.2; }
.big small { font-size:13px; font-weight:400; color: var(--secondary-text-color); }
.small-big { font-size:15px; }
.ok-card { display:flex; align-items:center; gap:8px; color: var(--success-color, #43a047); }
.empty { text-align:center; padding:60px 0; color: var(--secondary-text-color); }
.empty ha-icon { --mdc-icon-size:48px; }
.bar { display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; margin-bottom:12px; }
.reasons { margin:8px 0 0; padding-left:20px; font-size:14px; }
pre.notes { white-space:pre-wrap; max-height:340px; overflow:auto; background: var(--secondary-background-color); padding:10px; border-radius:8px; font-size:12px; overflow-wrap:anywhere; }
details summary { cursor:pointer; color: var(--primary-color); font-size:13px; margin-top:8px; }
h3 { margin:20px 0 8px; font-weight:500; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th { text-align:left; color: var(--secondary-text-color); font-weight:500; padding:6px; border-bottom:1px solid var(--divider-color); }
td { padding:8px 6px; border-bottom:1px solid var(--divider-color); vertical-align:top; }
tr:last-child td { border-bottom:none; }
.scroll { overflow-x:auto; padding:6px 10px; }
.row-actions { white-space:nowrap; }
.dot { display:inline-block; width:8px; height:8px; border-radius:50%; background: var(--disabled-text-color, #999); margin-right:6px; }
.dot.on { background: var(--success-color, #43a047); } .dot.off { background: var(--error-color, #db4437); }
.lqi { display:inline-block; min-width:44px; padding:1px 6px; border-radius:6px; font-weight:600;
  background: linear-gradient(90deg, color-mix(in srgb, var(--c) 35%, transparent) var(--w), transparent var(--w)); }
.seg { display:inline-flex; border:1px solid var(--divider-color); border-radius:18px; overflow:hidden; }
.seg button { border:none; background:none; padding:6px 12px; font:inherit; font-size:13px; color: var(--primary-text-color); cursor:pointer; }
.seg button.on { background: var(--primary-color); color: var(--text-primary-color, #fff); }
.chat { display:flex; flex-direction:column; height: calc(100vh - 56px - 56px - 32px); min-height:420px; }
.msgs { flex:1; overflow-y:auto; padding:4px 2px 12px; }
.msg { display:flex; margin:8px 0; }
.msg.user { justify-content:flex-end; }
.bubble { max-width: min(680px, 88%); padding:10px 14px; border-radius:16px; background: var(--card-background-color);
  box-shadow: var(--ha-card-box-shadow, 0 1px 3px rgba(0,0,0,.12)); font-size:14px; line-height:1.45; overflow-wrap:anywhere; }
.msg.user .bubble { background: var(--primary-color); color: var(--text-primary-color, #fff); border-bottom-right-radius:4px; }
.msg.bot .bubble { border-bottom-left-radius:4px; }
.bubble p { margin:0 0 6px; } .bubble p:last-child { margin-bottom:0; }
.bubble ul { margin:4px 0 6px; padding-left:18px; } .bubble li { margin:2px 0; }
.bubble .sub { color: var(--secondary-text-color); font-size:13px; }
.bubble code { background: var(--secondary-background-color); padding:0 4px; border-radius:4px; font-size:12px; }
.typing { display:flex; gap:4px; } .typing span { width:7px; height:7px; border-radius:50%; background: var(--secondary-text-color); animation: blink 1.2s infinite; }
.typing span:nth-child(2) { animation-delay:.2s } .typing span:nth-child(3) { animation-delay:.4s }
@keyframes blink { 0%,80%,100% { opacity:.25 } 40% { opacity:1 } }
@media (prefers-reduced-motion: reduce) { .typing span { animation:none; } }
.suggest { display:flex; gap:6px; flex-wrap:wrap; padding:6px 0; }
.chip-btn { border:1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color);
  border-radius:16px; padding:5px 12px; font: inherit; font-size:13px; cursor:pointer; }
.chip-btn.ghost { border-color:transparent; color: var(--secondary-text-color); }
.composer { display:flex; gap:8px; padding-top:6px; }
.composer input { flex:1; min-width:0; padding:11px 14px; border-radius:22px; border:1px solid var(--divider-color);
  background: var(--card-background-color); color: var(--primary-text-color); font: inherit; font-size:15px; outline:none; }
.composer input:focus { border-color: var(--primary-color); }
.toast { position:fixed; left:50%; bottom:24px; transform: translate(-50%, 140%); background: #323232; color:#fff; padding:10px 18px; border-radius:10px;
  font-size:14px; transition: transform .25s ease; z-index:10; max-width: calc(100vw - 32px); }
.toast.show { transform: translate(-50%, 0); }
.toast.ko { background: var(--error-color, #db4437); }
@media (max-width: 600px) {
  main { padding: 12px; }
  .hide-narrow { display:none; }
  .tab span:not(.badge) { display:none; }
  .tab.active span:not(.badge) { display:inline; }
  th:nth-child(2), td:nth-child(2), th:nth-child(6), td:nth-child(6) { display:none; }
}
`;

customElements.define("majordome-panel", MajordomePanel);
