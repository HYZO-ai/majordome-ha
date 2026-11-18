/* Majordome – carte pour tableau de bord (type: custom:majordome-card). */

(() => {
  if (customElements.get("majordome-card")) return;

  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  const find = (hass, part) =>
    Object.keys(hass.states).find((e) => e.startsWith("sensor.majordome") && e.includes(part));

  class MajordomeCard extends HTMLElement {
    setConfig(config) {
      this._config = { title: "Majordome", ...config };
      if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    }

    static getStubConfig() { return {}; }
    getCardSize() { return 3; }
    getGridOptions() { return { columns: 6, min_columns: 4, rows: 3 }; }

    set hass(hass) {
      this._hass = hass;
      const ids = {
        score: this._config.score_entity || find(hass, "score"),
        issues: this._config.issues_entity || find(hass, "probleme"),
        updates: this._config.updates_entity || find(hass, "mises_a_jour"),
        zigbee: this._config.zigbee_entity || find(hass, "zigbee"),
      };
      const st = (k) => (ids[k] ? hass.states[ids[k]] : undefined);
      const key = ["score", "issues", "updates", "zigbee"].map((k) => st(k) && st(k).last_updated).join("|");
      if (key === this._key) return;
      this._key = key;
      this._render(st("score"), st("issues"), st("updates"), st("zigbee"));
    }

    _render(score, issues, updates, zigbee) {
      if (!score) {
        this.shadowRoot.innerHTML = `<ha-card header="${esc(this._config.title)}"><div style="padding:16px">Majordome n'est pas encore installé ou n'a pas fini sa première analyse.</div></ha-card>`;
        return;
      }
      const v = Number(score.state);
      const ok = !Number.isNaN(v);
      const color = !ok ? "var(--disabled-text-color)" : v >= 85 ? "var(--success-color,#43a047)" : v >= 60 ? "var(--warning-color,#ffa600)" : "var(--error-color,#db4437)";
      const ia = (issues && issues.attributes) || {};
      const ua = (updates && updates.attributes) || {};
      const top = (ia.top || []).slice(0, 3);
      const zOff = zigbee ? Number(zigbee.state) || 0 : 0;
      this.shadowRoot.innerHTML = `
        <style>
          ha-card { padding:16px; cursor:pointer; }
          .row { display:flex; align-items:center; gap:16px; }
          .ring { width:64px; height:64px; border-radius:50%; flex:none; display:grid; place-items:center;
            background: conic-gradient(${color} ${ok ? v : 0}%, var(--divider-color) 0); }
          .ring span { width:52px; height:52px; border-radius:50%; display:grid; place-items:center; font-size:18px; font-weight:700;
            background: var(--ha-card-background, var(--card-background-color)); }
          .t { font-size:16px; font-weight:500; }
          .m { color: var(--secondary-text-color); font-size:13px; }
          .stats { display:flex; gap:14px; flex-wrap:wrap; margin-top:4px; font-size:13px; }
          .stats b { font-size:15px; }
          ul { margin:12px 0 0; padding-left:18px; font-size:13px; }
          li { margin:3px 0; }
          .crit { color: var(--error-color,#db4437); }
        </style>
        <ha-card>
          <div class="row">
            <div class="ring"><span>${ok ? v : "…"}</span></div>
            <div>
              <div class="t">${esc(this._config.title)}</div>
              <div class="stats">
                <span><b class="${ia.critical ? "crit" : ""}">${ia.critical ?? 0}</b> critique(s)</span>
                <span><b>${ia.warning ?? 0}</b> alerte(s)</span>
                <span><b>${updates ? esc(updates.state) : 0}</b> MAJ (${ua.safe ?? 0} sûres)</span>
                ${zigbee ? `<span><b class="${zOff ? "crit" : ""}">${zOff}</b> Zigbee hors ligne</span>` : ""}
              </div>
            </div>
          </div>
          ${top.length ? `<ul>${top.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : `<div class="m" style="margin-top:10px">✅ Aucun problème détecté.</div>`}
        </ha-card>`;
      this.shadowRoot.querySelector("ha-card").addEventListener("click", () => {
        history.pushState(null, "", "/majordome");
        window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
      });
    }
  }

  customElements.define("majordome-card", MajordomeCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "majordome-card",
    name: "Majordome",
    description: "Score de santé, problèmes, mises à jour et Zigbee en un coup d'œil.",
    preview: true,
  });
})();
