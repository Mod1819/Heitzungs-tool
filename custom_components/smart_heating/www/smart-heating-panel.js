/* Smart Heating Control – Panel (Web-Component, kein Build-Schritt).
 * Datenquelle ausschließlich die WebSocket-API der Integration:
 *   smart_heating/subscribe liefert Konfiguration + Status live (kein Polling).
 */
const DOMAIN = "smart_heating";
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_NAMES = { mon: "Montag", tue: "Dienstag", wed: "Mittwoch", thu: "Donnerstag", fri: "Freitag", sat: "Samstag", sun: "Sonntag" };
const DAY_SHORT = { mon: "Mo", tue: "Di", wed: "Mi", thu: "Do", fri: "Fr", sat: "Sa", sun: "So" };
const COPY_GROUPS = { workdays: ["mon", "tue", "wed", "thu", "fri"], weekend: ["sat", "sun"], all: DAYS };
const SOURCE_NAMES = { schedule: "Zeitplan", manual: "Manuell", default: "Standard", window_open: "Fenster offen", window_unknown: "Fensterstatus unbekannt", window_coupled: "Gekoppelt gesperrt" };
const WINDOW_NAMES = { open: "offen", closed: "geschlossen", unknown: "unbekannt" };
const THERMO_STATUS = { ok: "OK", pending: "wird übertragen", unavailable: "nicht verfügbar", hvac_off: "ausgeschaltet", unsupported: "nicht unterstützt", not_confirmed: "nicht bestätigt", service_error: "Fehler" };
const OUTDOOR_STATUS = { ok: "OK", invalid: "ungültig", implausible: "unplausibel", not_configured: "nicht eingerichtet" };
// Spiegel von devices.NOT_A_WINDOW: diese binary_sensor sind sicher keine Fenster/Türen
const NOT_A_WINDOW = new Set(["battery", "battery_charging", "carbon_monoxide", "cold", "connectivity", "gas", "heat", "light", "moisture", "motion", "moving", "occupancy", "plug", "power", "presence", "problem", "running", "safety", "smoke", "sound", "tamper", "update", "vibration"]);
const WINDOW_LIKE = new Set(["window", "door", "opening", "garage_door"]);
const VIEWS = [["dashboard", "Übersicht"], ["rooms", "Räume"], ["profiles", "Profile"], ["schedule", "Wochenplan"], ["settings", "Einstellungen"], ["system", "System"]];

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v, d = 1) => (v === null || v === undefined || Number.isNaN(Number(v)) ? "–" : Number(v).toFixed(d).replace(".", ","));
const temp = (v) => (v === null || v === undefined ? "–" : `${num(v)} °C`);
const parseNum = (v) => { const n = parseFloat(String(v).replace(",", ".")); return Number.isFinite(n) ? n : null; };
const clock = (iso) => new Date(iso).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
const toMin = (sec) => Math.round((Number(sec) || 0) / 6) / 10; // Sekunden -> Minuten (0,1)
/** Spiegel von models.norm_time: "6", "6:00", "6.00", "630" -> "HH:MM"; sonst Original. */
const normTime = (v) => {
  const raw = String(v ?? "");
  const t = raw.trim();
  const m = /^(\d{1,2})(?:[:.,](\d{1,2}))?$/.exec(t) || /^(\d{1,2})(\d{2})$/.exec(t);
  if (!m) return raw;
  return `${String(Number(m[1])).padStart(2, "0")}:${String(Number(m[2] || 0)).padStart(2, "0")}`;
};
const TIME_OK = /^([01]\d|2[0-3]):[0-5]\d$|^24:00$/;
const minutes = (hhmm) => { const [h, m] = hhmm.split(":").map(Number); return h * 60 + m; };

/** Spiegel von targets.outdoor_reduction (Vorschau in den Einstellungen). */
function outdoorReduction(curve, t) {
  const pts = curve.points && curve.points.length ? curve.points : [[curve.start_at, 0], [curve.full_at, curve.max_reduction]];
  if (t <= pts[0][0]) return pts[0][1];
  if (t >= pts[pts.length - 1][0]) return pts[pts.length - 1][1];
  for (let i = 0; i < pts.length - 1; i++) {
    const [x0, y0] = pts[i]; const [x1, y1] = pts[i + 1];
    if (t >= x0 && t <= x1) return Math.round((y0 + ((y1 - y0) * (t - x0)) / (x1 - x0)) * 10) / 10;
  }
  return 0;
}

function tempColor(t) {
  const x = Math.max(0, Math.min(1, (t - 14) / 10)); // 14 °C blau … 24 °C rot
  return `hsl(${Math.round(210 - 210 * x)}, 70%, 52%)`;
}

class SmartHeatingPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._view = "dashboard";
    this._roomId = null;
    this._form = {};
    this._openDetails = {};
    this._data = null;
    this._draft = null;
    this._error = null;
    this._notice = null;
    this._pendingRender = false;
    this._lastRender = 0;
    this.shadowRoot.addEventListener("click", (e) => this._onClick(e));
    this.shadowRoot.addEventListener("change", (e) => this._onChange(e));
    this.shadowRoot.addEventListener("input", (e) => this._onInput(e));
    this.shadowRoot.addEventListener("toggle", (e) => { if (e.target.dataset && e.target.dataset.keep) this._openDetails[e.target.dataset.keep] = e.target.open; }, true);
    this.shadowRoot.addEventListener("focusout", (e) => { this._normSlotField(e.target); setTimeout(() => this._pendingRender && !this._focused() && this._render(), 0); });
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._subscribe();
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) { menu.hass = hass; menu.narrow = this._narrow; }
    // Ist-Temperaturen aktualisieren, gedrosselt und nie während einer Eingabe
    if (this._data && ["dashboard", "room", "system"].includes(this._view) && Date.now() - this._lastRender > 10000) this._requestRender();
  }

  get hass() { return this._hass; }
  set narrow(v) { this._narrow = v; const m = this.shadowRoot.querySelector("ha-menu-button"); if (m) m.narrow = v; }
  set panel(_p) {}

  disconnectedCallback() { if (this._unsub) { this._unsub.then((u) => u()).catch(() => {}); this._unsub = null; } }
  connectedCallback() { if (this._hass && !this._unsub) this._subscribe(); this._render(); }

  _subscribe() {
    if (!this._hass || this._unsub) return;
    this._unsub = this._hass.connection.subscribeMessage((msg) => { this._data = msg; this._requestRender(); }, { type: `${DOMAIN}/subscribe` });
    this._unsub.catch((err) => { this._unsub = null; this._error = `Verbindung zur Integration fehlgeschlagen: ${err.message || err.code || err}`; this._render(); });
  }

  _focused() {
    const a = this.shadowRoot.activeElement;
    return !!a && ["INPUT", "SELECT", "TEXTAREA"].includes(a.tagName);
  }

  _requestRender() { if (this._focused()) { this._pendingRender = true; return; } this._render(); }

  async _call(type, data = {}, notice = null) {
    try {
      const res = await this._hass.callWS({ type: `${DOMAIN}/${type}`, ...data });
      this._error = null;
      this._notice = notice;
      return res;
    } catch (err) {
      this._error = err.message || err.code || String(err);
      this._notice = null;
      this._render();
      throw err;
    }
  }

  // ---------------------------------------------------------------- Daten
  get cfg() { return this._data ? this._data.config : { rooms: {}, profiles: {}, settings: {}, overrides: {} }; }
  get status() { return this._data ? this._data.status : { rooms: {}, thermostats: {}, actions: {}, outdoor: {} }; }
  get targets() { return this._data ? this._data.targets || {} : {}; }
  f(key, fallback) { return key in this._form ? this._form[key] : fallback; }
  clearForm(prefix) { Object.keys(this._form).forEach((k) => { if (k.startsWith(prefix)) delete this._form[k]; }); }
  profileName(pid) { return pid && this.cfg.profiles[pid] ? this.cfg.profiles[pid].name : "–"; }
  stateOf(eid) { return this._hass && this._hass.states[eid]; }
  friendly(eid) { const s = this.stateOf(eid); return s && s.attributes.friendly_name ? s.attributes.friendly_name : eid; }

  roomCurrentTemp(room) {
    const sensor = room.temperature_sensor && this.stateOf(room.temperature_sensor);
    if (sensor && !Number.isNaN(parseFloat(sensor.state))) return parseFloat(sensor.state);
    const vals = room.thermostats.map((e) => this.stateOf(e)).filter((s) => s && typeof s.attributes.current_temperature === "number").map((s) => s.attributes.current_temperature);
    return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
  }

  serviceOptions() {
    const svc = (this._hass && this._hass.services) || {};
    const opts = ["notify", "telegram_bot"].flatMap((d) => Object.keys(svc[d] || {}).map((n) => `${d}.${n}`))
      .sort().map((x) => `<option value="${esc(x)}"></option>`).join("");
    return `<datalist id="dl-notify">${opts}</datalist>`;
  }

  entityOptions(id, domains, windowsOnly = false, batteryFirst = false) {
    const states = this._hass ? Object.values(this._hass.states) : [];
    let list = states.filter((s) => domains.includes(s.entity_id.split(".")[0]));
    if (windowsOnly) {
      // Batterie-, Sabotage-, Verbindungsmeldungen usw. ausblenden; echte Fenster/Türen zuerst
      list = list.filter((s) => !NOT_A_WINDOW.has(s.attributes.device_class));
      const rank = (s) => (WINDOW_LIKE.has(s.attributes.device_class) ? 0 : 1);
      list.sort((a, b) => rank(a) - rank(b) || a.entity_id.localeCompare(b.entity_id));
    } else if (batteryFirst) {
      const rank = (s) => (s.attributes.device_class === "battery" ? 0 : 1);
      list.sort((a, b) => rank(a) - rank(b) || a.entity_id.localeCompare(b.entity_id));
    } else {
      list.sort((a, b) => a.entity_id.localeCompare(b.entity_id));
    }
    const opts = list.map((s) => `<option value="${esc(s.entity_id)}">${esc(s.attributes.friendly_name || "")}</option>`).join("");
    return `<datalist id="${id}">${opts}</datalist>`;
  }

  lowBatteries(rid) {
    return (this.status.batteries || []).filter((b) => b.low && (!rid || b.rooms.includes(rid)));
  }

  // ---------------------------------------------------------------- Ereignisse
  _rdInput(el) {
    if (!el.dataset.rd || !this._rd) return;
    const [list, idx, field] = el.dataset.rd.split("|");
    const item = this._rd[list][Number(idx)];
    if (item) { item[field] = el.type === "checkbox" ? el.checked : el.value; this._rd.dirty = true; }
  }

  /** Bearbeitungsstand für Meldestufen und gekoppelte Kontakte (Minuten im UI). */
  _roomDraft(rid) {
    const room = this.cfg.rooms[rid];
    if (!this._rd || this._rd.rid !== rid || !this._rd.dirty) {
      this._rd = {
        rid, dirty: false,
        stages: room.notify_stages.map((st) => ({ after: toMin(st.after), notify: st.notify, action_entity: st.action_entity || "", cover_position: st.cover_position ?? "" })),
        coupled: room.coupled_sensors.map((c) => ({ entity_id: c.entity_id, delay: toMin(c.delay) })),
      };
    }
    return this._rd;
  }

  sensorOwner(eid) {
    const owners = Object.values(this.cfg.rooms).filter((r) => r.window_sensors.includes(eid)).map((r) => r.name);
    return owners.length ? owners.join(", ") : "keinem Raum zugeordnet";
  }

  _onInput(e) {
    const el = e.target;
    if (el.dataset.f) this._form[el.dataset.f] = el.type === "checkbox" ? el.checked : el.value;
    this._rdInput(el);
    if (el.dataset.slot && this._draft) {
      const [day, idx, field] = el.dataset.slot.split("|");
      const slot = this._draft.week[day][Number(idx)];
      slot[field] = field === "temp" ? el.value : el.value.trim();
      this._draft.dirty = true;
    }
  }

  /** Zeitfeld nach Eingabe auf HH:MM normalisieren (Eingabefeld und Entwurf). */
  _normSlotField(el) {
    if (!el.dataset || !el.dataset.slot || !this._draft) return;
    const [day, idx, field] = el.dataset.slot.split("|");
    if (field !== "start" && field !== "end") return;
    const slot = this._draft.week[day][Number(idx)];
    if (!slot) return;
    const n = normTime(el.value);
    if (n !== el.value) el.value = n;
    if (n !== slot[field]) { slot[field] = n; this._draft.dirty = true; }
  }

  async _onChange(e) {
    const el = e.target;
    if (el.dataset.f && el.type === "checkbox") this._form[el.dataset.f] = el.checked;
    if (el.type === "checkbox") this._rdInput(el);
    this._normSlotField(el);
    const act = el.dataset.change;
    if (!act) return;
    try {
      if (act === "set_active") await this._call("profiles/set_active", { profile_id: el.value || null }, "Profil aktiviert");
      if (act === "room_profile") await this._call("rooms/update", { room_id: this._roomId, profile_override_id: el.value || null }, "Profil gespeichert");
      if (act === "room_outdoor_mode") {
        this._form.outdoor_mode = el.value;
        if (el.value !== "custom") { await this._call("rooms/update", { room_id: this._roomId, outdoor_mode: el.value }, "Gespeichert"); delete this._form.outdoor_mode; }
        this._render();
      }
      if (act === "sched_profile" || act === "sched_room") {
        if (this._draft && this._draft.dirty && !confirm("Ungespeicherte Änderungen verwerfen?")) { this._render(); return; }
        const pid = act === "sched_profile" ? el.value : this._draft.pid;
        const rid = act === "sched_room" ? el.value : this._draft.rid;
        await this._loadWeek(pid, rid);
      }
    } catch (_err) { /* Fehler wird angezeigt */ }
  }

  async _onClick(e) {
    const el = e.target.closest("[data-act]");
    if (!el) return;
    const a = el.dataset.act;
    const d = el.dataset;
    this._dayHint = null;
    try {
      switch (a) {
        case "nav": this._view = d.view; this._form = {}; this._error = null; this._notice = null; if (d.view === "schedule") await this._ensureDraft(); this._render(); return;
        case "open_room": this._roomId = d.room; this._rd = null; this._view = "room"; this._form = {}; this._error = null; this._notice = null; this._render(); return;
        case "create_room": {
          const name = (this._form.new_room || "").trim();
          if (!name) { this._error = "Bitte einen Raumnamen eingeben"; break; }
          const room = await this._call("rooms/create", { name }, `Raum „${name}“ angelegt`);
          delete this._form.new_room; this._roomId = room.room_id; this._view = "room"; break;
        }
        case "delete_room": {
          const name = this.cfg.rooms[d.room] ? this.cfg.rooms[d.room].name : d.room;
          if (!confirm(`Raum „${name}“ wirklich löschen? Zeitpläne dieses Raums gehen verloren.`)) return;
          await this._call("rooms/delete", { room_id: d.room }, "Raum gelöscht"); this._view = "rooms"; break;
        }
        case "rename_room": await this._call("rooms/update", { room_id: this._roomId, name: this._form.room_name }, "Name gespeichert"); this.clearForm("room_name"); break;
        case "manual_set": {
          const t = parseNum(this.f("manual_temp", ""));
          if (t === null) { this._error = "Bitte eine Temperatur eingeben"; break; }
          await this._call("manual/set", { room_id: this._roomId, temperature: t, mode: this.f("manual_mode", "until_next") }, "Temperatur gesetzt");
          this.clearForm("manual_"); break;
        }
        case "manual_clear": await this._call("manual/clear", { room_id: this._roomId }, "Zurück zum Zeitplan"); break;
        case "add_dev": {
          const key = `add_${d.kind}`;
          const eid = (this._form[key] || "").trim();
          if (!eid) { this._error = "Bitte eine Entity auswählen"; break; }
          await this._call("rooms/assign", { room_id: this._roomId, kind: d.kind, entity_id: eid }, `${eid} zugeordnet`);
          delete this._form[key]; break;
        }
        case "bat_add": {
          const eid = (this._form.add_battery || "").trim();
          if (!eid) { this._error = "Bitte eine Entity auswählen"; break; }
          const cur = this.cfg.rooms[this._roomId].battery_entities || [];
          await this._call("rooms/update", { room_id: this._roomId, battery_entities: cur.includes(eid) ? cur : [...cur, eid] }, `${eid} als Batterie zugeordnet`);
          delete this._form.add_battery; break;
        }
        case "bat_rm": {
          const cur = this.cfg.rooms[this._roomId].battery_entities || [];
          await this._call("rooms/update", { room_id: this._roomId, battery_entities: cur.filter((x) => x !== d.entity) }, `${d.entity} entfernt`); break;
        }
        case "rm_dev": await this._call("rooms/unassign", { room_id: this._roomId, kind: d.kind, entity_id: d.entity }, `${d.entity} entfernt`); break;
        case "set_temp_sensor": await this._call("rooms/update", { room_id: this._roomId, temperature_sensor: (this._form.temp_sensor || "").trim() || null }, "Temperatursensor gespeichert"); delete this._form.temp_sensor; break;
        case "save_window_temp": {
          const room = this.cfg.rooms[this._roomId];
          const delay = parseNum(this.f("window_delay", toMin(room.window_open_delay)));
          const rd = this._roomDraft(this._roomId);
          const stages = rd.stages.map((st) => ({
            after: Math.round((parseNum(st.after) ?? NaN) * 60), notify: !!st.notify,
            action_entity: String(st.action_entity || "").trim() || null,
            cover_position: String(st.cover_position ?? "").trim() === "" ? null : parseNum(st.cover_position),
          }));
          const coupled = rd.coupled.filter((c) => String(c.entity_id || "").trim()).map((c) => ({
            entity_id: String(c.entity_id).trim(), delay: Math.round((parseNum(c.delay) ?? NaN) * 60),
          }));
          if (delay === null || stages.some((st) => !Number.isFinite(st.after) || st.after < 60) || coupled.some((c) => !Number.isFinite(c.delay))) {
            this._error = "Bitte Minuten als Zahl eingeben (Meldestufen mindestens 1 Minute)"; break;
          }
          const lines = (v) => String(v).split("\n").map((x) => x.trim()).filter(Boolean);
          await this._call("rooms/update", {
            room_id: this._roomId,
            notify_texts: lines(this.f("texts_open", (room.notify_texts || []).join("\n"))),
            notify_close_texts: lines(this.f("texts_close", (room.notify_close_texts || []).join("\n"))),
            window_open_temp: parseNum(this.f("window_temp", room.window_open_temp)),
            window_open_delay: Math.round(delay * 60),
            notify_stages: stages,
            notify_on_close: !!this.f("notify_close", room.notify_on_close),
            coupled_sensors: coupled,
          }, "Fenster-Einstellungen gespeichert");
          ["window_temp", "window_delay", "notify_close", "texts_open", "texts_close"].forEach((k) => delete this._form[k]);
          this._rd = null;
          break;
        }
        case "rd_add_stage": {
          const rd = this._roomDraft(this._roomId);
          const last = rd.stages[rd.stages.length - 1];
          rd.stages.push({ after: last ? (parseNum(last.after) || 0) + 10 : 10, notify: true, action_entity: "", cover_position: "" });
          rd.dirty = true; break;
        }
        case "rd_rm_stage": { const rd = this._roomDraft(this._roomId); rd.stages.splice(Number(d.idx), 1); rd.dirty = true; break; }
        case "rd_add_coupled": { const rd = this._roomDraft(this._roomId); rd.coupled.push({ entity_id: "", delay: 15 }); rd.dirty = true; break; }
        case "rd_rm_coupled": { const rd = this._roomDraft(this._roomId); rd.coupled.splice(Number(d.idx), 1); rd.dirty = true; break;
        }
        case "notify_test": await this._call("notify/test", {}, "Testnachricht gesendet"); break;
        case "save_room_outdoor": {
          const room = this.cfg.rooms[this._roomId];
          const c = room.outdoor_curve;
          await this._call("rooms/update", {
            room_id: this._roomId, outdoor_mode: "custom",
            outdoor_curve: { start_at: parseNum(this.f("r_start", c.start_at)), full_at: parseNum(this.f("r_full", c.full_at)), max_reduction: parseNum(this.f("r_max", c.max_reduction)) },
          }, "Außentemperatur-Einstellung gespeichert");
          this.clearForm("r_"); delete this._form.outdoor_mode; break;
        }
        case "prof_create": {
          const name = (this._form.new_profile || "").trim();
          if (!name) { this._error = "Bitte einen Profilnamen eingeben"; break; }
          await this._call("profiles/create", { name }, `Profil „${name}“ angelegt`); delete this._form.new_profile; break;
        }
        case "prof_presets": await this._call("profiles/create_presets", {}, "Jahreszeiten-Profile angelegt"); break;
        case "prof_activate": await this._call("profiles/set_active", { profile_id: d.profile }, "Profil aktiviert"); break;
        case "prof_rename": {
          const name = prompt("Neuer Name:", this.profileName(d.profile));
          if (!name) return;
          await this._call("profiles/rename", { profile_id: d.profile, name }, "Umbenannt"); break;
        }
        case "prof_copy": {
          const name = prompt("Name der Kopie:", `${this.profileName(d.profile)} (Kopie)`);
          if (!name) return;
          await this._call("profiles/copy", { profile_id: d.profile, name }, "Profil kopiert"); break;
        }
        case "prof_delete":
          if (!confirm(`Profil „${this.profileName(d.profile)}“ löschen?`)) return;
          await this._call("profiles/delete", { profile_id: d.profile }, "Profil gelöscht"); break;
        case "prof_schedule": this._view = "schedule"; await this._loadWeek(d.profile, Object.keys(this.cfg.rooms)[0]); return;
        case "slot_add": {
          const slots = this._draft.week[d.day];
          const last = slots[slots.length - 1];
          const start = last ? normTime(last.end) : "00:00";
          if (normTime(start) === "24:00") { this._dayHint = { day: d.day, text: "Tag ist bis 24:00 belegt – Ende des letzten Abschnitts verkürzen, um einen weiteren hinzuzufügen" }; break; }
          slots.push({ start, end: "24:00", temp: last ? last.temp : 20 });
          this._draft.dirty = true; break;
        }
        case "slot_rm": this._draft.week[d.day].splice(Number(d.idx), 1); this._draft.dirty = true; break;
        case "day_copy": {
          const target = this.f(`copyto_${d.day}`, "");
          if (!target) { this._error = "Bitte Ziel auswählen"; break; }
          const days = COPY_GROUPS[target] || [target];
          days.filter((x) => x !== d.day).forEach((x) => { this._draft.week[x] = this._draft.week[d.day].map((s) => ({ ...s })); });
          this._draft.dirty = true; this._notice = "Kopiert – zum Übernehmen speichern"; break;
        }
        case "week_save": {
          const week = {};
          for (const day of DAYS) week[day] = this._draft.week[day].map((s) => ({ start: normTime(s.start), end: normTime(s.end), temp: parseNum(s.temp) }));
          const res = await this._call("schedule/set_week", { profile_id: this._draft.pid, room_id: this._draft.rid, week }, "Wochenplan gespeichert");
          this._draft = { ...this._draft, week: res.week, dirty: false }; break;
        }
        case "week_reload": await this._loadWeek(this._draft.pid, this._draft.rid); return;
        case "room_copy": {
          const target = this._form.copy_room;
          if (!target) { this._error = "Bitte Zielraum auswählen"; break; }
          if (this._draft.dirty) { this._error = "Bitte zuerst speichern"; break; }
          await this._call("schedule/copy_room", { profile_id: this._draft.pid, source_room_id: this._draft.rid, target_room_ids: target === "__all" ? Object.keys(this.cfg.rooms).filter((r) => r !== this._draft.rid) : [target] }, "Zeitplan übertragen");
          delete this._form.copy_room; break;
        }
        case "settings_save": await this._saveSettings(); break;
        default: return;
      }
    } catch (_err) { /* Fehler wird angezeigt */ }
    this._render();
  }

  async _saveSettings() {
    const s = this.cfg.settings;
    const c = s.outdoor_curve;
    const pointsText = this.f("s_points", (c.points || []).map((p) => `${num(p[0])}:${num(p[1])}`).join("; "));
    let points = null;
    if (pointsText.trim()) {
      points = pointsText.split(";").map((p) => p.trim()).filter(Boolean).map((p) => p.split(":").map(parseNum));
      if (points.some((p) => p.length !== 2 || p.includes(null))) { this._error = "Stützpunkte im Format 12:0; 14:0,5; 16:1"; return; }
    }
    const sensor = (this.f("s_sensor", s.outdoor_sensor || "") || "").trim() || null;
    await this._call("settings/update", {
      outdoor_sensor: sensor,
      outdoor_enabled: !!this.f("s_enabled", s.outdoor_enabled),
      outdoor_curve: { start_at: parseNum(this.f("s_start", c.start_at)), full_at: parseNum(this.f("s_full", c.full_at)), max_reduction: parseNum(this.f("s_max", c.max_reduction)), points },
      unknown_window_policy: this.f("s_policy", s.unknown_window_policy),
      default_temp: parseNum(this.f("s_default", s.default_temp)),
      thermostat_changes: this.f("s_thermo", s.thermostat_changes) === "adopt" ? "adopt" : "ignore",
      notify_service: (this.f("s_notify", s.notify_service || "") || "").trim() || null,
      notify_entity_id: (this.f("s_notify_entity", s.notify_entity_id || "") || "").trim() || null,
    }, "Einstellungen gespeichert");
    this.clearForm("s_");
  }

  async _ensureDraft() {
    if (this._draft) return;
    const rooms = Object.keys(this.cfg.rooms);
    const profiles = Object.keys(this.cfg.profiles);
    if (!rooms.length || !profiles.length) return;
    const pid = this.cfg.settings.active_profile_id || profiles[0];
    await this._loadWeek(pid, rooms[0]);
  }

  async _loadWeek(pid, rid) {
    if (!pid || !rid) { this._draft = null; this._render(); return; }
    const res = await this._call("schedule/get", { profile_id: pid, room_id: rid });
    this._draft = { pid, rid, week: res.week, dirty: false };
    this._render();
  }

  // ---------------------------------------------------------------- Rendering
  _render() {
    this._pendingRender = false;
    this._lastRender = Date.now();
    const scrollY = this.shadowRoot.querySelector(".content") ? this.shadowRoot.querySelector(".content").scrollTop : 0;
    let body;
    if (!this._data) body = `<div class="card empty">${this._error ? esc(this._error) : "Lade Smart Heating …"}</div>`;
    else {
      const views = { dashboard: () => this._dashboard(), rooms: () => this._rooms(), room: () => this._room(), profiles: () => this._profiles(), schedule: () => this._schedule(), settings: () => this._settings(), system: () => this._system() };
      body = (views[this._view] || views.dashboard)();
    }
    const tabs = VIEWS.map(([v, n]) => `<button class="tab ${this._view === v || (v === "rooms" && this._view === "room") ? "active" : ""}" data-act="nav" data-view="${v}">${n}</button>`).join("");
    const msg = this._error ? `<div class="msg error">⚠ ${esc(this._error)}</div>` : this._notice ? `<div class="msg ok">✓ ${esc(this._notice)}</div>` : "";
    this.shadowRoot.innerHTML = `<style>${STYLE}</style>
      <div class="header"><ha-menu-button></ha-menu-button><div class="title">Heizung</div></div>
      <nav class="tabs">${tabs}</nav>
      <div class="content">${msg}${body}</div>`;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) { menu.hass = this._hass; menu.narrow = this._narrow; }
    const content = this.shadowRoot.querySelector(".content");
    if (content) content.scrollTop = scrollY;
  }

  _modeBadge(rid) {
    const r = this.status.rooms[rid];
    if (!r) return "";
    if (r.locked) return `<span class="badge warn">${esc(SOURCE_NAMES[r.reason] || r.reason)}</span>`;
    const t = this.targets[rid];
    const src = t ? t.source : r.reason;
    return `<span class="badge ${src === "manual" ? "info" : ""}">${esc(SOURCE_NAMES[src] || src)}</span>`;
  }

  _dashboard() {
    const cfg = this.cfg;
    const o = this.status.outdoor || {};
    const profileOpts = [`<option value="">– kein Profil –</option>`].concat(Object.entries(cfg.profiles).map(([pid, p]) => `<option value="${esc(pid)}" ${pid === cfg.settings.active_profile_id ? "selected" : ""}>${esc(p.name)}</option>`)).join("");
    const rooms = Object.entries(cfg.rooms);
    const cards = rooms.map(([rid, room]) => {
      const r = this.status.rooms[rid] || {};
      const cur = this.roomCurrentTemp(room);
      const t = this.targets[rid] || {};
      const issues = room.thermostats.filter((e) => this.status.thermostats[e] && !["ok", "pending"].includes(this.status.thermostats[e].status));
      return `<div class="card room ${r.locked ? "locked" : ""}" data-act="open_room" data-room="${esc(rid)}">
        <div class="room-head"><span class="room-name">${esc(room.name)}</span>${this._modeBadge(rid)}</div>
        <div class="big">${cur === null ? "–" : `${num(cur)}<small> °C</small>`}</div>
        <div class="row"><span>Soll</span><b>${temp(r.effective_temp)}</b></div>
        ${r.desired_temp !== undefined && r.desired_temp !== r.effective_temp ? `<div class="row sub"><span>gewünscht</span><span>${temp(r.desired_temp)}</span></div>` : ""}
        ${r.outdoor_reduction ? `<div class="row sub"><span>Außentemperatur</span><span>−${num(r.outdoor_reduction)} K</span></div>` : ""}
        <div class="row sub"><span>Fenster</span><span class="${r.window === "open" ? "warn-t" : r.window === "unknown" ? "err-t" : ""}">${room.window_sensors.length ? esc(WINDOW_NAMES[r.window] || "–") : "keine Kontakte"}${r.lock_pending_until ? ` · Sperre ab ${esc(clock(r.lock_pending_until))}` : ""}</span></div>
        <div class="row sub"><span>Profil</span><span>${esc(this.profileName(t.profile_id))}${t.profile_source === "room" ? " (Raum)" : ""}</span></div>
        ${this.lowBatteries(rid).length ? `<div class="row warn-t">🔋 Batterie niedrig: ${esc(this.lowBatteries(rid).map((b) => b.device).join(", "))}</div>` : ""}
        ${issues.length ? `<div class="row err-t">⚠ ${issues.length} Thermostat(e): ${esc(issues.map((e) => THERMO_STATUS[this.status.thermostats[e].status]).join(", "))}</div>` : ""}
      </div>`;
    }).join("");
    return `<div class="card bar">
        <div><span class="label">Außen</span> <b>${o.status === "ok" ? temp(o.value) : esc(OUTDOOR_STATUS[o.status] || "–")}</b>${o.status === "ok" && !o.enabled ? ' <span class="badge">Regelung aus</span>' : ""}</div>
        <label class="inline"><span class="label">Aktives Profil</span><select data-change="set_active">${profileOpts}</select></label>
        <div><span class="label">Regelung</span> <b class="${this.status.started ? "ok-t" : "warn-t"}">${this.status.started ? "aktiv" : "wartet auf HA-Start"}</b></div>
      </div>
      ${rooms.length ? `<div class="grid">${cards}</div>` : `<div class="card empty">Noch keine Räume angelegt.<br><button class="primary" data-act="nav" data-view="rooms">Raum anlegen</button></div>`}`;
  }

  _rooms() {
    const list = Object.entries(this.cfg.rooms).map(([rid, room]) => `<div class="list-row">
        <div><b>${esc(room.name)}</b><div class="sub">${room.thermostats.length} Thermostat(e) · ${room.window_sensors.length} Fensterkontakt(e)</div></div>
        <div class="actions"><button data-act="open_room" data-room="${esc(rid)}">Öffnen</button><button class="danger" data-act="delete_room" data-room="${esc(rid)}">Löschen</button></div>
      </div>`).join("");
    return `<div class="card"><h2>Neuer Raum</h2><div class="form-row">
        <input data-f="new_room" placeholder="Raumname, z. B. Wohnzimmer" value="${esc(this.f("new_room", ""))}">
        <button class="primary" data-act="create_room">Anlegen</button></div></div>
      <div class="card"><h2>Räume</h2>${list || '<div class="sub">Noch keine Räume.</div>'}</div>`;
  }

  _devList(kind, field, room) {
    const st = this.status;
    return room[field].map((eid) => {
      let info = "";
      if (kind === "thermostat") {
        const t = st.thermostats[eid];
        info = t ? `Ziel ${temp(t.target)} · Gerät ${temp(t.reported)} · <span class="${t.status === "ok" ? "ok-t" : t.status === "pending" ? "" : "err-t"}">${esc(THERMO_STATUS[t.status] || t.status)}</span>${t.rooms && t.rooms.length > 1 ? ` · geteilt mit ${t.rooms.length - 1} Raum/Räumen` : ""}` : "";
      } else {
        const s = (st.rooms[this._roomId] || {}).sensors || {};
        info = `<span class="${s[eid] === "open" ? "warn-t" : s[eid] === "unknown" ? "err-t" : ""}">${esc(WINDOW_NAMES[s[eid]] || "–")}</span>`;
      }
      return `<div class="list-row"><div><b>${esc(this.friendly(eid))}</b><div class="sub">${esc(eid)} · ${info}</div></div>
        <button class="danger" data-act="rm_dev" data-kind="${kind}" data-entity="${esc(eid)}">Entfernen</button></div>`;
    }).join("") || '<div class="sub">Keine zugeordnet.</div>';
  }

  _batteryList(room) {
    const list = room.battery_entities || [];
    return list.map((eid) => {
      const st = this.stateOf(eid);
      let info = "nicht gefunden";
      if (st) {
        if (["unavailable", "unknown"].includes(st.state)) info = "nicht verfügbar";
        else if (eid.startsWith("binary_sensor.")) info = st.state === "on" ? '<span class="err-t">niedrig</span>' : "ok";
        else info = `${esc(st.state)} ${esc(st.attributes.unit_of_measurement || "")}`.trim();
      }
      return `<div class="list-row"><div><b>${esc(this.friendly(eid))}</b><div class="sub">${esc(eid)} · ${info}</div></div>
        <button class="danger" data-act="bat_rm" data-entity="${esc(eid)}">Entfernen</button></div>`;
    }).join("") || '<div class="sub">Keine zugeordnet.</div>';
  }

  _room() {
    const rid = this._roomId;
    const room = this.cfg.rooms[rid];
    if (!room) { this._view = "rooms"; return this._rooms(); }
    const r = this.status.rooms[rid] || {};
    const t = this.targets[rid] || {};
    const manual = t.manual;
    const rd = this._roomDraft(rid);
    const profileOpts = [`<option value="">Globales Profil (${esc(this.profileName(this.cfg.settings.active_profile_id))})</option>`]
      .concat(Object.entries(this.cfg.profiles).map(([pid, p]) => `<option value="${esc(pid)}" ${pid === room.profile_override_id ? "selected" : ""}>${esc(p.name)}</option>`)).join("");
    const mode = this.f("outdoor_mode", room.outdoor_mode);
    const c = room.outdoor_curve;
    const actions = (this.status.actions[rid] || []).slice(0, 15).map((a) => `<div class="log"><span>${esc(new Date(a.time).toLocaleString("de-DE"))}</span> ${esc(a.message)}</div>`).join("") || '<div class="sub">Noch keine Aktionen.</div>';
    const cur = this.roomCurrentTemp(room);
    return `<button data-act="nav" data-view="rooms">← Räume</button>
      <div class="card"><div class="room-head"><h2>${esc(room.name)}</h2>${this._modeBadge(rid)}</div>
        <div class="stats">
          <div><span class="label">Ist</span><b>${temp(cur)}</b></div>
          <div><span class="label">Gewünscht</span><b>${temp(r.desired_temp)}</b></div>
          <div><span class="label">Effektiv</span><b>${temp(r.effective_temp)}</b></div>
          <div><span class="label">Plan</span><b>${temp(t.scheduled_temp)}</b></div>
        </div>
        ${r.locked ? `<div class="msg warn">Fenstersperre aktiv${(r.locked_by || []).length ? ` durch ${esc(r.locked_by.map((e) => this.friendly(e) + (e in (r.coupled || {}) ? " (gekoppelt)" : "")).join(", "))}` : ""} – manuelle Änderungen werden gespeichert und nach dem Schließen übernommen.</div>` : ""}
        ${r.outdoor_reduction ? `<div class="sub">Außentemperatur-Absenkung: −${num(r.outdoor_reduction)} K</div>` : ""}
        ${t.next_change ? `<div class="sub">Nächster Planwechsel: ${esc(new Date(t.next_change).toLocaleString("de-DE", { weekday: "short", hour: "2-digit", minute: "2-digit" }))}</div>` : ""}
      </div>
      <div class="card"><h3>Temperatur manuell ändern</h3><div class="form-row">
        <input type="number" step="0.5" min="4" max="35" data-f="manual_temp" value="${esc(this.f("manual_temp", r.desired_temp ?? ""))}">
        <select data-f="manual_mode"><option value="until_next" ${this.f("manual_mode", "until_next") === "until_next" ? "selected" : ""}>bis zum nächsten Zeitabschnitt</option><option value="permanent" ${this.f("manual_mode", "") === "permanent" ? "selected" : ""}>dauerhaft</option></select>
        <button class="primary" data-act="manual_set">Setzen</button>
        ${manual ? `<button data-act="manual_clear">Zurück zum Zeitplan</button>` : ""}</div>
        ${manual ? `<div class="sub">Manuell ${temp(manual.temp)} ${manual.until ? `bis ${esc(new Date(manual.until).toLocaleString("de-DE", { weekday: "short", hour: "2-digit", minute: "2-digit" }))}` : "(dauerhaft)"}</div>` : ""}
      </div>
      <div class="card"><h3>Heizprofil</h3><select data-change="room_profile">${profileOpts}</select></div>
      <div class="card"><h3>Thermostate</h3>${this._devList("thermostat", "thermostats", room)}
        <div class="form-row">${this.entityOptions("dl-climate", ["climate"])}<input list="dl-climate" data-f="add_thermostat" placeholder="climate.…" value="${esc(this.f("add_thermostat", ""))}"><button data-act="add_dev" data-kind="thermostat">Hinzufügen</button></div></div>
      <div class="card"><h3>Fensterkontakte</h3>${this._devList("window_sensor", "window_sensors", room)}
        <div class="form-row">${this.entityOptions("dl-window", ["binary_sensor", "input_boolean"], true)}<input list="dl-window" data-f="add_window_sensor" placeholder="binary_sensor.…" value="${esc(this.f("add_window_sensor", ""))}"><button data-act="add_dev" data-kind="window_sensor">Hinzufügen</button></div>
        <h3>Verhalten bei offenem Fenster</h3>
        ${r.lock_pending_until ? `<div class="msg warn">Fenster offen – Sperre greift um ${esc(clock(r.lock_pending_until))}</div>` : ""}
        <div class="form-grid">
          <label>Fenster-offen-Temperatur (°C)<input type="number" step="0.5" min="4" max="35" data-f="window_temp" value="${esc(this.f("window_temp", room.window_open_temp))}"></label>
          <label>Sperre erst nach (Minuten, 0 = sofort)<input type="number" step="0.5" min="0" max="60" data-f="window_delay" value="${esc(this.f("window_delay", toMin(room.window_open_delay)))}"></label>
        </div>
        <h3>Meldestufen</h3>
        ${this.entityOptions("dl-action", ["cover", "script", "scene"])}
        ${rd.stages.map((st, i) => `<div class="stage">
          <span class="stage-no">${i + 1}.</span>
          <label class="inline">nach<input type="number" step="1" min="1" max="1440" data-rd="stages|${i}|after" value="${esc(st.after)}">min</label>
          <label class="check small"><input type="checkbox" data-rd="stages|${i}|notify" ${st.notify ? "checked" : ""}> Nachricht</label>
          <input class="action" list="dl-action" data-rd="stages|${i}|action_entity" value="${esc(st.action_entity)}" placeholder="Aktion (optional): cover./script./scene.">
          ${String(st.action_entity || "").startsWith("cover.") ? `<label class="inline">Position<input type="number" min="0" max="100" data-rd="stages|${i}|cover_position" value="${esc(st.cover_position)}" placeholder="zu">%</label>` : ""}
          <button class="icon" data-act="rd_rm_stage" data-idx="${i}" title="Stufe entfernen">✕</button></div>`).join("") || '<div class="sub">Keine Meldestufen – keine Meldungen.</div>'}
        <button data-act="rd_add_stage">+ Meldestufe</button>
        <label class="check"><input type="checkbox" data-f="notify_close" ${this.f("notify_close", room.notify_on_close) ? "checked" : ""}> Meldung, wenn wieder geschlossen</label>
        <div class="sub">Ein Rollo wird geschlossen bzw. auf die Position gefahren und <b>nicht</b> automatisch wieder hochgefahren. Kein Rollo an Türen eintragen, durch die man hinausgeht.</div>
        ${rd.stages.some((st) => st.notify) && !this.cfg.settings.notify_service ? '<div class="sub">Kein Benachrichtigungsdienst eingestellt – es wird nur das Event <code>smart_heating_window_alert</code> ausgelöst (Einstellungen).</div>' : ""}
        <details data-keep="texts" ${this._openDetails.texts ? "open" : ""}><summary>Eigene Meldungstexte (Varianten)</summary>
          <label>Fenster zu lange offen (ein Satz pro Zeile)<textarea rows="4" data-f="texts_open" placeholder="Leer = Standardtext">${esc(this.f("texts_open", (room.notify_texts || []).join("\n")))}</textarea></label>
          <label>Wieder geschlossen (ein Satz pro Zeile)<textarea rows="4" data-f="texts_close" placeholder="Leer = Standardtext">${esc(this.f("texts_close", (room.notify_close_texts || []).join("\n")))}</textarea></label>
          <div class="sub">Pro Meldung wird zufällig ein Satz gewählt, nie zweimal derselbe hintereinander. Platzhalter: <code>{raum}</code>, <code>{fenster}</code>, <code>{minuten}</code>, <code>{stufe}</code>, <code>{temperatur}</code> (aktuelle Solltemperatur), <code>{solltemperatur}</code> (gewünschte Temperatur). Bei Stufen mit Aktion wird deren Hinweis angehängt.</div></details>
        <h3>Mitsperren durch Kontakte anderer Räume</h3>
        <div class="sub">Z. B. Küche/Flur: Wenn die Terrassentür im Wohnzimmer länger offen ist, wird dieser Raum ebenfalls abgesenkt. Meldungen kommen nur vom Raum mit dem Fenster.</div>
        ${this.entityOptions("dl-coupled", ["binary_sensor", "input_boolean"], true)}
        ${rd.coupled.map((c, i) => `<div class="stage">
          <input class="action" list="dl-coupled" data-rd="coupled|${i}|entity_id" value="${esc(c.entity_id)}" placeholder="binary_sensor.terrassentuer">
          <label class="inline">nach<input type="number" step="1" min="0" max="240" data-rd="coupled|${i}|delay" value="${esc(c.delay)}">min</label>
          <span class="sub">${c.entity_id ? `${esc(this.sensorOwner(c.entity_id))} · ${esc(WINDOW_NAMES[(r.coupled || {})[c.entity_id]] || "–")}` : ""}</span>
          <button class="icon" data-act="rd_rm_coupled" data-idx="${i}" title="Entfernen">✕</button></div>`).join("")}
        <button data-act="rd_add_coupled">+ Gekoppelter Kontakt</button>
        <div class="form-row"><button class="primary" data-act="save_window_temp">Fenster-Einstellungen speichern</button>${rd.dirty ? '<span class="sub">ungespeicherte Änderungen</span>' : ""}</div></div>
      <div class="card"><h3>Batterien (optional, manuell)</h3>
        <div class="sub">Falls die Batterie automatisch nicht gefunden wird (z. B. Gerät keinem Bereich zugeordnet), hier den Batterie-Eintrag des Fensterkontakts/Thermostats eintragen.</div>
        ${this._batteryList(room)}
        <div class="form-row">${this.entityOptions("dl-battery", ["sensor", "binary_sensor"], false, true)}<input list="dl-battery" data-f="add_battery" placeholder="sensor.… / binary_sensor.…" value="${esc(this.f("add_battery", ""))}"><button data-act="bat_add">Hinzufügen</button></div></div>
      <div class="card"><h3>Raumtemperatursensor (optional)</h3><div class="form-row">${this.entityOptions("dl-sensor", ["sensor"])}
        <input list="dl-sensor" data-f="temp_sensor" placeholder="sensor.… (leer = Thermostat-Messwert)" value="${esc(this.f("temp_sensor", room.temperature_sensor || ""))}"><button data-act="set_temp_sensor">Speichern</button></div></div>
      <div class="card"><h3>Außentemperaturregelung</h3>
        <select data-change="room_outdoor_mode"><option value="global" ${mode === "global" ? "selected" : ""}>Globale Einstellung</option><option value="custom" ${mode === "custom" ? "selected" : ""}>Eigene Kurve</option><option value="off" ${mode === "off" ? "selected" : ""}>Aus</option></select>
        ${mode === "custom" ? `<div class="form-grid">
          <label>Beginn ab (°C)<input type="number" step="0.5" data-f="r_start" value="${esc(this.f("r_start", c.start_at))}"></label>
          <label>Maximum ab (°C)<input type="number" step="0.5" data-f="r_full" value="${esc(this.f("r_full", c.full_at))}"></label>
          <label>Max. Absenkung (K)<input type="number" step="0.5" min="0" data-f="r_max" value="${esc(this.f("r_max", c.max_reduction))}"></label>
          </div><button class="primary" data-act="save_room_outdoor">Speichern</button>` : ""}
        ${!this.cfg.settings.outdoor_enabled ? '<div class="sub">Hinweis: Die Regelung ist global ausgeschaltet (Einstellungen).</div>' : ""}</div>
      <div class="card"><h3>Letzte Aktionen</h3>${actions}</div>
      <div class="card"><h3>Raum bearbeiten</h3><div class="form-row"><input data-f="room_name" value="${esc(this.f("room_name", room.name))}"><button data-act="rename_room">Umbenennen</button>
        <button class="danger" data-act="delete_room" data-room="${esc(rid)}">Raum löschen</button></div></div>`;
  }

  _profiles() {
    const active = this.cfg.settings.active_profile_id;
    const rows = Object.entries(this.cfg.profiles).map(([pid, p]) => {
      const used = Object.values(this.cfg.rooms).filter((r) => r.profile_override_id === pid).map((r) => r.name);
      return `<div class="list-row"><div><b>${esc(p.name)}</b> ${pid === active ? '<span class="badge info">aktiv</span>' : ""}
        <div class="sub">${Object.keys(p.schedules).length} Raum/Räume mit Plan${used.length ? ` · Raumprofil von: ${esc(used.join(", "))}` : ""}</div></div>
        <div class="actions">${pid !== active ? `<button class="primary" data-act="prof_activate" data-profile="${esc(pid)}">Aktivieren</button>` : ""}
        <button data-act="prof_schedule" data-profile="${esc(pid)}">Wochenplan</button>
        <button data-act="prof_rename" data-profile="${esc(pid)}">Umbenennen</button>
        <button data-act="prof_copy" data-profile="${esc(pid)}">Kopieren</button>
        <button class="danger" data-act="prof_delete" data-profile="${esc(pid)}">Löschen</button></div></div>`;
    }).join("");
    return `<div class="card"><h2>Neues Profil</h2><div class="form-row">
        <input data-f="new_profile" placeholder="z. B. Urlaub" value="${esc(this.f("new_profile", ""))}"><button class="primary" data-act="prof_create">Anlegen</button>
        <button data-act="prof_presets">Winter/Frühling/Sommer/Herbst anlegen</button></div></div>
      <div class="card"><h2>Profile</h2>${rows || '<div class="sub">Noch keine Profile.</div>'}</div>`;
  }

  _dayBar(slots) {
    slots = slots.map((s) => ({ ...s, start: normTime(s.start), end: normTime(s.end) }));
    const segs = slots.filter((s) => TIME_OK.test(s.start) && TIME_OK.test(s.end) && parseNum(s.temp) !== null).map((s) => {
      const a = minutes(s.start), b = minutes(s.end);
      if (b <= a) return "";
      return `<div class="seg" style="left:${(a / 1440) * 100}%;width:${((b - a) / 1440) * 100}%;background:${tempColor(parseNum(s.temp))}" title="${esc(s.start)}–${esc(s.end)} ${esc(num(parseNum(s.temp)))} °C">${b - a >= 120 ? esc(num(parseNum(s.temp))) : ""}</div>`;
    }).join("");
    return `<div class="daybar">${segs}<div class="ticks"><span>0</span><span>6</span><span>12</span><span>18</span><span>24</span></div></div>`;
  }

  /** Prüft die Abschnitte eines Tages: ungültige Zeit, Ende <= Start, Überschneidung. */
  _slotIssues(slots) {
    const bad = slots.map(() => ({ start: false, end: false }));
    const hints = new Set();
    const n = slots.map((s) => ({ a: normTime(s.start), b: normTime(s.end) }));
    n.forEach((x, i) => {
      if (!TIME_OK.test(x.a) || x.a === "24:00") { bad[i].start = true; hints.add("Ungültige Uhrzeit"); }
      if (!TIME_OK.test(x.b)) { bad[i].end = true; hints.add("Ungültige Uhrzeit"); }
      if (!bad[i].start && !bad[i].end && minutes(x.b) <= minutes(x.a)) { bad[i].end = true; hints.add("Ende muss nach Start liegen"); }
    });
    n.forEach((x, i) => {
      if (bad[i].start || bad[i].end) return;
      n.forEach((y, j) => {
        if (j === i || bad[j].start || bad[j].end) return;
        const ya = minutes(y.a), yb = minutes(y.b), xa = minutes(x.a);
        if ((ya < xa || (ya === xa && j < i)) && yb > xa) { bad[i].start = true; hints.add("Überschneidung mit vorherigem Abschnitt"); }
      });
    });
    return { bad, hints: [...hints] };
  }

  _schedule() {
    const rooms = Object.entries(this.cfg.rooms);
    const profiles = Object.entries(this.cfg.profiles);
    if (!rooms.length || !profiles.length) return `<div class="card empty">Für den Wochenplan werden mindestens ein Raum und ein Profil benötigt.</div>`;
    if (this._draft && (!this.cfg.rooms[this._draft.rid] || !this.cfg.profiles[this._draft.pid])) this._draft = null;
    if (!this._draft) { this._ensureDraft(); return `<div class="card empty">Lade Wochenplan …</div>`; }
    const dr = this._draft;
    const pOpts = profiles.map(([pid, p]) => `<option value="${esc(pid)}" ${pid === dr.pid ? "selected" : ""}>${esc(p.name)}</option>`).join("");
    const rOpts = rooms.map(([rid, r]) => `<option value="${esc(rid)}" ${rid === dr.rid ? "selected" : ""}>${esc(r.name)}</option>`).join("");
    const copyOpts = (day) => [`<option value="">Kopieren nach …</option>`, `<option value="workdays">Mo–Fr</option>`, `<option value="weekend">Sa–So</option>`, `<option value="all">alle Tage</option>`]
      .concat(DAYS.filter((x) => x !== day).map((x) => `<option value="${x}">${DAY_NAMES[x]}</option>`)).join("");
    const days = DAYS.map((day) => {
      const slots = dr.week[day];
      const { bad, hints } = this._slotIssues(slots);
      const hint = this._dayHint && this._dayHint.day === day ? [this._dayHint.text, ...hints] : hints;
      const rows = slots.map((s, i) => `<div class="slot">
          <input class="time${bad[i].start ? " invalid" : ""}" data-slot="${day}|${i}|start" value="${esc(s.start)}" placeholder="06:00" maxlength="5">
          <span>–</span><input class="time${bad[i].end ? " invalid" : ""}" data-slot="${day}|${i}|end" value="${esc(s.end)}" placeholder="08:30" maxlength="5">
          <input class="tnum${parseNum(s.temp) === null || parseNum(s.temp) < 4 || parseNum(s.temp) > 35 ? " invalid" : ""}" type="text" inputmode="decimal" maxlength="5" data-slot="${day}|${i}|temp" value="${esc(String(s.temp ?? "").replace(".", ","))}"><span>°C</span>
          <button class="icon" data-act="slot_rm" data-day="${day}" data-idx="${i}" title="Entfernen">✕</button></div>`).join("");
      return `<div class="day"><div class="day-head"><b>${DAY_NAMES[day]}</b>
          <span class="copy"><select data-f="copyto_${day}">${copyOpts(day)}</select><button data-act="day_copy" data-day="${day}">Kopieren</button></span></div>
        ${this._dayBar(slots)}${rows || '<div class="sub">Keine Abschnitte – es gilt die Standardtemperatur.</div>'}
        ${hint.map((h) => `<div class="hint">${esc(h)}</div>`).join("")}
        <button data-act="slot_add" data-day="${day}">+ Abschnitt</button></div>`;
    }).join("");
    const cur = this.status.rooms[dr.rid];
    const otherRooms = rooms.filter(([rid]) => rid !== dr.rid).map(([rid, r]) => `<option value="${esc(rid)}">${esc(r.name)}</option>`).join("");
    return `<div class="card"><div class="form-row">
        <label class="inline"><span class="label">Profil</span><select data-change="sched_profile">${pOpts}</select></label>
        <label class="inline"><span class="label">Raum</span><select data-change="sched_room">${rOpts}</select></label>
        ${cur ? `<span class="sub">Aktuelle Solltemperatur: <b>${temp(cur.effective_temp)}</b></span>` : ""}</div>
        <div class="sub">Zeiten im Format HH:MM, Tagesende 24:00. Lücken nutzen die Standardtemperatur (${temp(this.cfg.settings.default_temp)}).</div></div>
      ${days}
      <div class="card sticky"><div class="form-row">
        <button class="primary" data-act="week_save">${dr.dirty ? "Änderungen speichern" : "Speichern"}</button>
        <button data-act="week_reload">Verwerfen</button>
        ${otherRooms ? `<select data-f="copy_room"><option value="">Plan übertragen auf …</option><option value="__all">alle anderen Räume</option>${otherRooms}</select><button data-act="room_copy">Übertragen</button>` : ""}
      </div></div>`;
  }

  _settings() {
    const s = this.cfg.settings;
    const c = s.outdoor_curve;
    const o = this.status.outdoor || {};
    const pointsText = this.f("s_points", (c.points || []).map((p) => `${num(p[0])}:${num(p[1])}`).join("; "));
    const preview = { start_at: parseNum(this.f("s_start", c.start_at)) ?? c.start_at, full_at: parseNum(this.f("s_full", c.full_at)) ?? c.full_at, max_reduction: parseNum(this.f("s_max", c.max_reduction)) ?? c.max_reduction, points: null };
    if (pointsText.trim()) {
      const pts = pointsText.split(";").map((p) => p.trim()).filter(Boolean).map((p) => p.split(":").map(parseNum));
      if (pts.length >= 2 && pts.every((p) => p.length === 2 && !p.includes(null))) preview.points = pts;
    }
    const table = [6, 8, 10, 12, 14, 16, 18, 20, 22].map((x) => `<tr><td>${x} °C</td><td>${num(outdoorReduction(preview, x))} K</td></tr>`).join("");
    const enabled = !!this.f("s_enabled", s.outdoor_enabled);
    return `<div class="card"><h2>Außentemperaturregelung</h2>
        <label class="check"><input type="checkbox" data-f="s_enabled" ${enabled ? "checked" : ""}> Dynamische Absenkung aktiv</label>
        <label>Außentemperatursensor${this.entityOptions("dl-out", ["sensor"])}<input list="dl-out" data-f="s_sensor" value="${esc(this.f("s_sensor", s.outdoor_sensor || ""))}" placeholder="sensor.aussentemperatur"></label>
        <div class="sub">Status: ${esc(OUTDOOR_STATUS[o.status] || "–")}${o.status === "ok" ? ` · ${temp(o.value)} (verwendet ${temp(o.used)})` : o.raw !== undefined && o.raw !== null ? ` · Rohwert „${esc(o.raw)}“` : ""}</div>
        <div class="form-grid">
          <label>Absenkung beginnt ab (°C)<input type="number" step="0.5" data-f="s_start" value="${esc(this.f("s_start", c.start_at))}"></label>
          <label>Maximale Absenkung ab (°C)<input type="number" step="0.5" data-f="s_full" value="${esc(this.f("s_full", c.full_at))}"></label>
          <label>Maximale Absenkung (K)<input type="number" step="0.5" min="0" data-f="s_max" value="${esc(this.f("s_max", c.max_reduction))}"></label>
        </div>
        <label>Eigene Stützpunkte (optional, ersetzen die lineare Kurve)<input data-f="s_points" value="${esc(pointsText)}" placeholder="12:0; 14:0,5; 16:1; 18:2; 20:3"></label>
        <table class="preview"><tr><th>Außen</th><th>Absenkung</th></tr>${table}</table>
        <div class="sub">Bei ungültigem Sensorwert wird nicht abgesenkt. Manuelle Sollwerte werden nie abgesenkt, die Fenstersperre hat immer Vorrang.</div></div>
      <div class="card"><h2>Allgemein</h2>
        <label>Standardtemperatur (Lücken im Plan, kein Profil)<input type="number" step="0.5" min="4" max="35" data-f="s_default" value="${esc(this.f("s_default", s.default_temp))}"></label>
        <label>Fensterkontakt nicht verfügbar / unbekannt
          <select data-f="s_policy"><option value="open" ${this.f("s_policy", s.unknown_window_policy) === "open" ? "selected" : ""}>wie offen behandeln (sicher, empfohlen)</option>
          <option value="closed" ${this.f("s_policy", s.unknown_window_policy) === "closed" ? "selected" : ""}>wie geschlossen behandeln</option></select></label></div>
      <div class="card"><h2>Änderungen direkt am Thermostat</h2>
        <label>Wenn ein Thermostat am Gerät (oder durch eine Homematic-IP-Gruppe) verstellt wird
          <select data-f="s_thermo"><option value="ignore" ${this.f("s_thermo", s.thermostat_changes) !== "adopt" ? "selected" : ""}>ignorieren – Zeitplan/Panel gilt immer (empfohlen)</option>
          <option value="adopt" ${this.f("s_thermo", s.thermostat_changes) === "adopt" ? "selected" : ""}>als manuelle Änderung übernehmen</option></select></label>
        <div class="sub">Manuelle Werte setzt man dann im Panel. Setzt ein Gerät seinen eigenen Wert dauerhaft zurück, wird nach 3 Versuchen aufgegeben (Status „nicht bestätigt“).</div></div>
      <div class="card"><h2>Benachrichtigung (Fenster zu lange offen)</h2>
        <label>Dienst, z. B. notify.telegram oder telegram_bot.send_message${this.serviceOptions()}
          <input list="dl-notify" data-f="s_notify" value="${esc(this.f("s_notify", s.notify_service || ""))}" placeholder="leer = nur Event"></label>
        <label>Notify-Entität (nur für notify.send_message)${this.entityOptions("dl-notify-ent", ["notify"])}
          <input list="dl-notify-ent" data-f="s_notify_entity" value="${esc(this.f("s_notify_entity", s.notify_entity_id || ""))}" placeholder="optional"></label>
        <div class="form-row"><button data-act="notify_test" ${s.notify_service ? "" : "disabled"}>Testnachricht senden</button></div>
        <div class="sub">Meldezeit pro Raum in der Raumansicht. Zusätzlich wird immer das Event <code>smart_heating_window_alert</code> ausgelöst (für eigene Automationen).</div></div>
      <div class="card sticky"><button class="primary" data-act="settings_save">Einstellungen speichern</button></div>`;
  }

  _system() {
    const st = this.status;
    const o = st.outdoor || {};
    const thermos = Object.entries(st.thermostats).map(([eid, t]) => `<tr><td>${esc(this.friendly(eid))}<div class="sub">${esc(eid)}</div></td><td>${temp(t.target)}</td><td>${temp(t.reported)}</td>
        <td class="${t.status === "ok" ? "ok-t" : t.status === "pending" ? "" : "err-t"}">${esc(THERMO_STATUS[t.status] || t.status)}${t.locked ? " · Sperre" : ""}</td></tr>`).join("");
    const windows = [];
    Object.entries(st.rooms).forEach(([rid, r]) => Object.entries(r.sensors || {}).forEach(([eid, s]) => windows.push(`<tr><td>${esc(this.friendly(eid))}<div class="sub">${esc(eid)}</div></td><td>${esc((this.cfg.rooms[rid] || {}).name || rid)}</td><td class="${s === "open" ? "warn-t" : s === "unknown" ? "err-t" : ""}">${esc(WINDOW_NAMES[s])}</td></tr>`)));
    const problems = Object.values(st.thermostats).filter((t) => !["ok", "pending"].includes(t.status)).length
      + windows.filter((w) => w.includes("err-t")).length + (o.enabled && o.status !== "ok" ? 1 : 0)
      + this.lowBatteries().length;
    const roomNames = (ids) => ids.map((id) => (this.cfg.rooms[id] || {}).name || id).join(", ");
    const batteries = (st.batteries || []).map((b) => `<tr><td>${esc(b.device)}<div class="sub">${esc(b.entity_id)}</div></td><td>${esc(roomNames(b.rooms))}</td>
        <td class="${b.low ? "err-t" : "ok-t"}">${typeof b.level === "number" ? `${b.level} %` : esc(b.level)}${b.low ? " · niedrig" : ""}</td></tr>`).join("");
    return `<div class="card"><h2>Systemstatus</h2>
        <div class="stats"><div><span class="label">Regelung</span><b class="${st.started ? "ok-t" : "warn-t"}">${st.started ? "aktiv" : "wartet"}</b></div>
        <div><span class="label">Probleme</span><b class="${problems ? "err-t" : "ok-t"}">${problems}</b></div>
        <div><span class="label">Außensensor</span><b>${esc(OUTDOOR_STATUS[o.status] || "–")}</b></div>
        <div><span class="label">Räume</span><b>${Object.keys(this.cfg.rooms).length}</b></div></div></div>
      <div class="card"><h3>Thermostate</h3><div class="scroll"><table><tr><th>Gerät</th><th>Ziel</th><th>Gerät meldet</th><th>Status</th></tr>${thermos || '<tr><td colspan="4">Keine</td></tr>'}</table></div></div>
      <div class="card"><h3>Fensterkontakte</h3><div class="scroll"><table><tr><th>Kontakt</th><th>Raum</th><th>Zustand</th></tr>${windows.join("") || '<tr><td colspan="3">Keine</td></tr>'}</table></div></div>
      <div class="card"><h3>Batterien</h3><div class="sub">Geräte der eingetragenen Thermostate und Fensterkontakte sowie batteriebetriebene Geräte im selben Bereich.</div>
        <div class="scroll"><table><tr><th>Gerät</th><th>Raum</th><th>Batterie</th></tr>${batteries || '<tr><td colspan="3">Keine Batteriedaten gefunden</td></tr>'}</table></div></div>`;
  }
}

const STYLE = `
:host { display:block; height:100%; background:var(--primary-background-color); color:var(--primary-text-color); font-family:var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
.header { display:flex; align-items:center; height:56px; padding:0 8px; background:var(--app-header-background-color, var(--primary-color)); color:var(--app-header-text-color, #fff); }
.title { font-size:20px; margin-left:8px; }
.tabs { display:flex; overflow-x:auto; gap:4px; padding:8px 12px; background:var(--card-background-color); border-bottom:1px solid var(--divider-color); }
.tab { border:none; background:none; color:var(--secondary-text-color); padding:8px 12px; border-radius:16px; white-space:nowrap; font-size:14px; cursor:pointer; }
.tab.active { background:var(--primary-color); color:var(--text-primary-color, #fff); }
.content { padding:16px; max-width:1200px; margin:0 auto; box-sizing:border-box; height:calc(100% - 110px); overflow-y:auto; }
.card { background:var(--card-background-color); border-radius:var(--ha-card-border-radius, 12px); box-shadow:var(--ha-card-box-shadow, 0 1px 3px rgba(0,0,0,.15)); padding:16px; margin-bottom:16px; }
.card.bar { display:flex; flex-wrap:wrap; gap:16px 32px; align-items:center; }
.card.empty { text-align:center; color:var(--secondary-text-color); padding:32px; }
.card.sticky { position:sticky; bottom:0; z-index:1; }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(250px, 1fr)); gap:16px; }
.grid .card { margin:0; }
.room { cursor:pointer; border-left:4px solid transparent; }
.room.locked { border-left-color:var(--warning-color, #ff9800); }
.room-head { display:flex; justify-content:space-between; align-items:center; gap:8px; }
.room-name { font-size:17px; font-weight:500; }
.big { font-size:36px; font-weight:300; margin:8px 0; }
.big small { font-size:16px; color:var(--secondary-text-color); }
.row { display:flex; justify-content:space-between; gap:8px; padding:2px 0; }
.sub { color:var(--secondary-text-color); font-size:13px; }
.label { color:var(--secondary-text-color); font-size:13px; margin-right:6px; }
.badge { display:inline-block; font-size:12px; padding:2px 8px; border-radius:10px; background:var(--secondary-background-color, #eee); color:var(--primary-text-color); white-space:nowrap; }
.badge.warn { background:var(--warning-color, #ff9800); color:#fff; }
.badge.info { background:var(--primary-color); color:var(--text-primary-color, #fff); }
.ok-t { color:var(--success-color, #43a047); } .warn-t { color:var(--warning-color, #ff9800); } .err-t { color:var(--error-color, #db4437); }
h2 { font-size:18px; margin:0 0 12px; font-weight:500; } h3 { font-size:15px; margin:16px 0 8px; font-weight:500; } .card > h3:first-child { margin-top:0; }
.stats { display:grid; grid-template-columns:repeat(auto-fit, minmax(110px, 1fr)); gap:12px; margin:12px 0; }
.stats > div { display:flex; flex-direction:column; } .stats b { font-size:20px; font-weight:400; }
.list-row { display:flex; justify-content:space-between; align-items:center; gap:12px; padding:10px 0; border-bottom:1px solid var(--divider-color); flex-wrap:wrap; }
.list-row:last-child { border-bottom:none; }
.actions { display:flex; flex-wrap:wrap; gap:6px; }
.form-row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:8px 0; }
.form-row input:not([type=number]) { flex:1 1 200px; }
.form-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:12px; margin:12px 0; }
label { display:flex; flex-direction:column; gap:4px; font-size:13px; color:var(--secondary-text-color); margin:8px 0; }
label.inline { flex-direction:row; align-items:center; margin:0; }
label.check { flex-direction:row; align-items:center; gap:8px; color:var(--primary-text-color); font-size:15px; }
input, select, textarea { font:inherit; font-size:15px; padding:8px 10px; border-radius:8px; border:1px solid var(--divider-color); background:var(--card-background-color); color:var(--primary-text-color); min-width:0; }
textarea { width:100%; min-height:90px; box-sizing:border-box; resize:vertical; }
details { margin:12px 0; } summary { cursor:pointer; font-weight:500; }
input[type=number] { width:90px; } input[type=checkbox] { width:18px; height:18px; }
button { font:inherit; font-size:14px; padding:8px 14px; border-radius:8px; border:1px solid var(--divider-color); background:var(--secondary-background-color, transparent); color:var(--primary-text-color); cursor:pointer; }
button.primary { background:var(--primary-color); border-color:var(--primary-color); color:var(--text-primary-color, #fff); }
button.danger { color:var(--error-color, #db4437); }
button.icon { padding:6px 10px; }
button:disabled { opacity:.5; cursor:default; }
.stage { display:flex; flex-wrap:wrap; align-items:center; gap:8px; padding:6px 0; border-bottom:1px solid var(--divider-color); }
.stage-no { font-weight:500; min-width:20px; }
.stage input.action { flex:1 1 220px; }
.stage label.inline input { width:70px; margin:0 4px; }
label.check.small { font-size:14px; margin:0; }
code { font-size:12px; background:var(--secondary-background-color, #eee); padding:1px 4px; border-radius:4px; }
.msg { padding:10px 14px; border-radius:8px; margin-bottom:12px; }
.content > .msg { position:sticky; top:0; z-index:2; }
input.invalid { border-color:var(--error-color, #db4437); }
.hint { color:var(--error-color, #db4437); font-size:13px; margin:4px 0; }
.msg.error { background:rgba(219,68,55,.12); color:var(--error-color, #db4437); }
.msg.ok { background:rgba(67,160,71,.12); color:var(--success-color, #43a047); }
.msg.warn { background:rgba(255,152,0,.12); color:var(--warning-color, #ff9800); margin:8px 0 0; }
.day { background:var(--card-background-color); border-radius:12px; padding:12px 16px; margin-bottom:12px; box-shadow:var(--ha-card-box-shadow, 0 1px 3px rgba(0,0,0,.15)); }
.day-head { display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; }
.copy { display:flex; gap:6px; }
.daybar { position:relative; height:26px; background:var(--secondary-background-color, #eee); border-radius:6px; margin:10px 0 20px; }
.seg { position:absolute; top:0; bottom:0; border-radius:4px; color:#fff; font-size:11px; display:flex; align-items:center; justify-content:center; overflow:hidden; border-right:1px solid var(--card-background-color); box-sizing:border-box; }
.ticks { position:absolute; top:28px; left:0; right:0; display:flex; justify-content:space-between; font-size:10px; color:var(--secondary-text-color); }
.slot { display:flex; align-items:center; gap:6px; margin:6px 0; flex-wrap:wrap; }
.slot input.time { width:64px; text-align:center; } .slot input.tnum { width:72px; }
.log { font-size:13px; padding:4px 0; border-bottom:1px solid var(--divider-color); } .log span { color:var(--secondary-text-color); margin-right:6px; }
table { width:100%; border-collapse:collapse; font-size:14px; } th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--divider-color); vertical-align:top; }
table.preview { max-width:280px; margin:8px 0; } .scroll { overflow-x:auto; }
@media (max-width: 600px) { .content { padding:10px; } .card { padding:12px; } .big { font-size:30px; } .form-row > * { flex:1 1 100%; } .form-row input[type=number] { flex:1 1 30%; } .copy { width:100%; } .copy select { flex:1; } }
`;

if (!customElements.get("smart-heating-panel")) customElements.define("smart-heating-panel", SmartHeatingPanel);
