/* Rendertest des Panels mit jsdom und einem ECHTEN Snapshot aus smart_heating/subscribe.
 * Ausführen:  SH_SNAPSHOT=snapshot.json NODE_PATH=<node_modules mit jsdom> node --test tests/js/
 * (Snapshot erzeugt tests/test_panel.py::test_subscribe_live_updates, wenn SH_SNAPSHOT gesetzt ist.)
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const SNAP = JSON.parse(fs.readFileSync(process.env.SH_SNAPSHOT, "utf-8"));
const CODE = fs.readFileSync(path.join(__dirname, "../../custom_components/smart_heating/www/smart-heating-panel.js"), "utf-8");
const tick = () => new Promise((r) => setTimeout(r, 0));

function setup({ fail = null } = {}) {
  const dom = new JSDOM("<!doctype html><body></body>", { runScripts: "outside-only", pretendToBeVisual: true });
  const w = dom.window;
  w.confirm = () => true;
  w.prompt = () => "Neu";
  w.eval(CODE);
  const calls = [];
  let push = null;
  const states = {
    "climate.wz": { entity_id: "climate.wz", state: "heat", attributes: { friendly_name: "Thermostat WZ", current_temperature: 19.5, temperature: 5 } },
    "climate.neu": { entity_id: "climate.neu", state: "heat", attributes: { friendly_name: "Neu" } },
    "binary_sensor.wz_f": { entity_id: "binary_sensor.wz_f", state: "on", attributes: { friendly_name: "Fenster WZ" } },
    "sensor.wz_temp": { entity_id: "sensor.wz_temp", state: "20.4", attributes: {} },
    "sensor.aussen": { entity_id: "sensor.aussen", state: "16", attributes: {} },
  };
  const hass = {
    states,
    callWS: async (msg) => {
      calls.push(JSON.parse(JSON.stringify(msg)));
      if (fail && msg.type.endsWith(fail.type)) throw { code: "invalid_format", message: fail.message };
      if (msg.type === "smart_heating/schedule/get") return { week: { mon: [], tue: [], wed: [], thu: [], fri: [], sat: [], sun: [], ...(SNAP.config.profiles[msg.profile_id].schedules[msg.room_id] || {}) } };
      if (msg.type === "smart_heating/schedule/set_week") return { week: msg.week };
      if (msg.type === "smart_heating/rooms/create") return { room_id: "neu" };
      return {};
    },
    connection: { subscribeMessage: (cb) => { push = cb; setTimeout(() => cb(SNAP), 0); return Promise.resolve(() => { push = null; }); } },
  };
  const el = w.document.createElement("smart-heating-panel");
  w.document.body.appendChild(el);
  el.hass = hass;
  const root = el.shadowRoot;
  const q = (sel) => root.querySelector(sel);
  const click = async (sel) => { const n = q(sel); assert.ok(n, `Element fehlt: ${sel}`); n.dispatchEvent(new w.MouseEvent("click", { bubbles: true })); await tick(); await tick(); };
  const type = (sel, value) => { const n = q(sel); assert.ok(n, `Eingabe fehlt: ${sel}`); n.value = value; n.dispatchEvent(new w.Event("input", { bubbles: true })); };
  const choose = async (sel, value) => { const n = q(sel); n.value = value; n.dispatchEvent(new w.Event("change", { bubbles: true })); await tick(); await tick(); };
  return { w, el, root, q, click, type, choose, calls, hass, push: (d) => push(d) };
}

test("Dashboard zeigt Räume, Fenstersperre, Ist- und Sollwerte", async () => {
  const t = setup(); await tick(); await tick();
  const html = t.root.innerHTML;
  assert.match(html, /Wohnzimmer/);
  assert.match(html, /Fenster offen/);            // Sperrgrund
  assert.match(html, /20,4/);                      // Ist aus Raumsensor
  assert.match(html, /5,0 °C/);                    // effektiv gesperrt
  assert.match(html, /21,0 °C/);                   // gewünscht
  assert.match(html, /−1,5 K/);                    // Außentemperatur-Absenkung
  assert.match(html, /Regelung/);
});

test("XSS: Raumname mit HTML wird nur als Text angezeigt", async () => {
  const t = setup(); await tick(); await tick();
  for (const v of ["dashboard", "rooms", "profiles", "system"]) {
    await t.click(`[data-act=nav][data-view=${v}]`);
    assert.equal(t.root.querySelector("img"), null, `img-Element in Ansicht ${v}`);
  }
  assert.match(t.root.innerHTML + t.w.document.body.innerHTML, /&lt;img|Thermostat/);
  assert.equal(t.w.__xss, undefined);
});

test("Alle Ansichten rendern ohne Fehler", async () => {
  const t = setup(); await tick(); await tick();
  const expect = { rooms: /Neuer Raum/, profiles: /Winter/, schedule: /Montag/, settings: /Außentemperatursensor/, system: /Systemstatus/ };
  for (const [v, re] of Object.entries(expect)) {
    await t.click(`[data-act=nav][data-view=${v}]`);
    await tick();
    assert.match(t.root.innerHTML, re, v);
  }
  await t.click("[data-act=nav][data-view=dashboard]");
  await t.click("[data-act=open_room][data-room=wz]");
  assert.match(t.root.innerHTML, /Fenstersperre aktiv/);
  assert.match(t.root.innerHTML, /Thermostat WZ/);
});

test("Raum anlegen ruft rooms/create und öffnet den Raum", async () => {
  const t = setup(); await tick(); await tick();
  await t.click("[data-act=nav][data-view=rooms]");
  t.type("[data-f=new_room]", "Bad");
  await t.click("[data-act=create_room]");
  assert.deepEqual(t.calls.at(-1), { type: "smart_heating/rooms/create", name: "Bad" });
});

test("Manuelle Temperatur, Gerät zuordnen, Profil pro Raum", async () => {
  const t = setup(); await tick(); await tick();
  await t.click("[data-act=open_room][data-room=wz]");
  t.type("[data-f=manual_temp]", "22.5"); // Zahlenfeld liefert immer Punkt
  await t.click("[data-act=manual_set]");
  assert.deepEqual(t.calls.at(-1), { type: "smart_heating/manual/set", room_id: "wz", temperature: 22.5, mode: "until_next" });
  t.type("[data-f=add_thermostat]", "climate.neu");
  await t.click("[data-act=add_dev][data-kind=thermostat]");
  assert.deepEqual(t.calls.at(-1), { type: "smart_heating/rooms/assign", room_id: "wz", kind: "thermostat", entity_id: "climate.neu" });
  await t.choose("[data-change=room_profile]", "sommer");
  assert.deepEqual(t.calls.at(-1), { type: "smart_heating/rooms/update", room_id: "wz", profile_override_id: "sommer" });
});

test("Wochenplan: Abschnitt bearbeiten, Tag kopieren, speichern", async () => {
  const t = setup(); await tick(); await tick();
  await t.click("[data-act=nav][data-view=schedule]");
  await tick();
  assert.match(t.root.innerHTML, /Montag/);
  t.type('[data-slot="mon|1|temp"]', "22");
  t.type("[data-f=copyto_mon]", "workdays");
  await t.click('[data-act=day_copy][data-day=mon]');
  await t.click('[data-act=slot_add][data-day=sat]');
  await t.click("[data-act=week_save]");
  const save = t.calls.at(-1);
  assert.equal(save.type, "smart_heating/schedule/set_week");
  assert.equal(save.week.mon[1].temp, 22);
  assert.deepEqual(save.week.fri, save.week.mon);
  assert.deepEqual(save.week.sat, [{ start: "00:00", end: "24:00", temp: 20 }]);
  assert.deepEqual(save.week.sun, []);
});

test("Einstellungen: Stützpunkte im deutschen Zahlenformat werden korrekt übergeben", async () => {
  const t = setup(); await tick(); await tick();
  await t.click("[data-act=nav][data-view=settings]");
  t.type("[data-f=s_points]", "12:0; 14:0,5; 16:1; 18:2; 20:3");
  await t.click("[data-act=settings_save]");
  const c = t.calls.at(-1);
  assert.equal(c.type, "smart_heating/settings/update");
  assert.deepEqual(c.outdoor_curve.points, [[12, 0], [14, 0.5], [16, 1], [18, 2], [20, 3]]);
  assert.equal(c.outdoor_sensor, "sensor.aussen");
});

test("Fehler vom Backend wird angezeigt", async () => {
  const t = setup({ fail: { type: "manual/set", message: "Wert außerhalb des Bereichs" } }); await tick(); await tick();
  await t.click("[data-act=open_room][data-room=wz]");
  t.type("[data-f=manual_temp]", "99");
  await t.click("[data-act=manual_set]");
  assert.match(t.root.querySelector(".msg.error").textContent, /außerhalb des Bereichs/);
});

test("Live-Update überschreibt keine laufende Eingabe", async () => {
  const t = setup(); await tick(); await tick();
  await t.click("[data-act=open_room][data-room=wz]");
  assert.ok(t.q(".msg.warn"));                                  // Sperrhinweis sichtbar
  const input = t.q("[data-f=manual_temp]");
  input.focus();
  t.type("[data-f=manual_temp]", "23");
  const snap = JSON.parse(JSON.stringify(SNAP));
  snap.status.rooms.wz.locked = false;
  t.push(snap);
  await tick();
  assert.equal(t.q("[data-f=manual_temp]").value, "23");
  assert.strictEqual(t.q("[data-f=manual_temp]"), input);   // nicht neu gerendert
  input.blur();
  input.dispatchEvent(new t.w.FocusEvent("focusout", { bubbles: true }));
  await tick(); await tick();
  assert.equal(t.q(".msg.warn"), null);                         // Sperrhinweis weg -> nachgeholt
  assert.equal(t.q("[data-f=manual_temp]").value, "23");        // Eingabe bleibt erhalten
});

test("Raum: Verzögerung und Meldung in Minuten speichern (Sekunden an API)", async () => {
  const t = setup(); await tick(); await tick();
  await t.click("[data-act=open_room][data-room=wz]");
  assert.match(t.root.innerHTML, /Sperre erst nach/);
  t.type("[data-f=window_delay]", "7.5");
  await t.click("[data-act=rd_add_stage]");            // v1.2: Meldezeit = erste Meldestufe
  await t.click("[data-act=save_window_temp]");
  const c = t.calls.at(-1);
  assert.equal(c.type, "smart_heating/rooms/update");
  assert.equal(c.window_open_delay, 450);
  assert.equal(c.notify_stages[0].after, 600);
  assert.equal(c.notify_on_close, true);
  assert.equal(c.window_open_temp, 5);
});

test("Übersicht und Raum zeigen ausstehende Sperre", async () => {
  const t = setup(); await tick(); await tick();
  const snap = JSON.parse(JSON.stringify(SNAP));
  Object.assign(snap.status.rooms.wz, { locked: false, effective_temp: 19.5, lock_pending_until: "2026-11-02T06:25:00+00:00" });
  t.push(snap); await tick();
  assert.match(t.root.innerHTML, /Sperre ab 07:25/);
  await t.click("[data-act=open_room][data-room=wz]");
  assert.match(t.root.innerHTML, /Sperre greift um 07:25/);
});

test("Einstellungen: Benachrichtigungsdienst speichern, Test erst mit Dienst", async () => {
  const t = setup(); await tick(); await tick();
  t.hass.services = { notify: { telegram: {}, send_message: {} }, telegram_bot: { send_message: {} } };
  await t.click("[data-act=nav][data-view=settings]");
  assert.ok(t.q("[data-act=notify_test]").disabled);
  assert.ok(t.root.innerHTML.includes('value="telegram_bot.send_message"'));
  t.type("[data-f=s_notify]", "notify.telegram");
  await t.click("[data-act=settings_save]");
  const c = t.calls.at(-1);
  assert.equal(c.notify_service, "notify.telegram");
  assert.equal(c.notify_entity_id, null);
});

test("Meldestufen und gekoppelte Kontakte bearbeiten und speichern", async () => {
  const t = setup(); await tick(); await tick();
  await t.click("[data-act=open_room][data-room=wz]");
  assert.match(t.root.innerHTML, /Keine Meldestufen/);
  await t.click("[data-act=rd_add_stage]");
  await t.click("[data-act=rd_add_stage]");
  t.type('[data-rd="stages|0|after"]', "5");
  t.type('[data-rd="stages|1|after"]', "30");
  t.type('[data-rd="stages|1|action_entity"]', "cover.bad");
  const cb = t.q('[data-rd="stages|1|notify"]');
  cb.checked = false; cb.dispatchEvent(new t.w.Event("change", { bubbles: true }));
  await t.click("[data-act=rd_add_coupled]");                     // Rendern behält Entwurf
  assert.ok(t.q('[data-rd="stages|1|cover_position"]'));          // Position erst bei cover.*
  t.type('[data-rd="coupled|0|entity_id"]', "binary_sensor.terrasse");
  t.type('[data-rd="coupled|0|delay"]', "15");
  assert.match(t.root.innerHTML, /ungespeicherte Änderungen/);
  await t.click("[data-act=save_window_temp]");
  const c = t.calls.at(-1);
  assert.equal(c.type, "smart_heating/rooms/update");
  assert.deepEqual(c.notify_stages, [
    { after: 300, notify: true, action_entity: null, cover_position: null },
    { after: 1800, notify: false, action_entity: "cover.bad", cover_position: null },
  ]);
  assert.deepEqual(c.coupled_sensors, [{ entity_id: "binary_sensor.terrasse", delay: 900 }]);
  assert.equal("notify_after" in c, false);
});

test("Ungültige Meldestufe wird im Panel abgefangen", async () => {
  const t = setup(); await tick(); await tick();
  await t.click("[data-act=open_room][data-room=wz]");
  await t.click("[data-act=rd_add_stage]");
  t.type('[data-rd="stages|0|after"]', "0");
  const before = t.calls.length;
  await t.click("[data-act=save_window_temp]");
  assert.equal(t.calls.length, before);
  assert.match(t.q(".msg.error").textContent, /mindestens 1 Minute/);
});

test("Gekoppelte Sperre wird mit Ursache angezeigt", async () => {
  const t = setup(); await tick(); await tick();
  const snap = JSON.parse(JSON.stringify(SNAP));
  Object.assign(snap.status.rooms.wz, { locked: true, reason: "window_coupled", window: "closed",
    locked_by: ["binary_sensor.wz_f"], coupled: { "binary_sensor.wz_f": "open" } });
  t.push(snap); await tick();
  assert.match(t.root.innerHTML, /Gekoppelt gesperrt/);
  await t.click("[data-act=open_room][data-room=wz]");
  assert.match(t.q(".msg.warn").textContent, /Fenster WZ \(gekoppelt\)/);
});

test("Fensterkontakt-Auswahl blendet Batterie-Einträge aus, Fenster zuerst", async () => {
  const t = setup(); await tick(); await tick();
  Object.assign(t.hass.states, {
    "binary_sensor.wz_fenster_low_battery": { entity_id: "binary_sensor.wz_fenster_low_battery", state: "off", attributes: { device_class: "battery" } },
    "binary_sensor.a_tuer": { entity_id: "binary_sensor.a_tuer", state: "off", attributes: { device_class: "door" } },
    "binary_sensor.z_fenster": { entity_id: "binary_sensor.z_fenster", state: "off", attributes: { device_class: "window" } },
  });
  await t.click("[data-act=open_room][data-room=wz]");
  const opts = [...t.root.querySelectorAll("#dl-window option")].map((o) => o.value);
  assert.ok(!opts.includes("binary_sensor.wz_fenster_low_battery"));
  assert.deepEqual(opts.slice(0, 2), ["binary_sensor.a_tuer", "binary_sensor.z_fenster"]);
  assert.ok(opts.includes("binary_sensor.wz_f"));                 // ohne Klasse bleibt wählbar
  const coupled = [...t.root.querySelectorAll("#dl-coupled option")].map((o) => o.value);
  assert.ok(!coupled.includes("binary_sensor.wz_fenster_low_battery"));
});

test("Niedrige Batterie in Übersicht und System", async () => {
  const t = setup(); await tick(); await tick();
  const snap = JSON.parse(JSON.stringify(SNAP));
  snap.status.batteries = [
    { entity_id: "binary_sensor.trv1_low_battery", device: "Heizkörper Fenster", rooms: ["wz"], level: "niedrig", low: true },
    { entity_id: "sensor.fenster_battery", device: "Fensterkontakt WZ", rooms: ["wz"], level: 85, low: false },
  ];
  t.push(snap); await tick();
  assert.match(t.root.innerHTML, /🔋 Batterie niedrig: Heizkörper Fenster/);
  await t.click("[data-act=nav][data-view=system]");
  assert.match(t.root.innerHTML, /Batterien/);
  assert.match(t.root.innerHTML, /85 %/);
  assert.match(t.root.innerHTML, /niedrig · niedrig/);          // Binärwert + Warnung
});

test("Wochenplan: Zeit \"6:00\" wird beim Speichern zu \"06:00\"", async () => {
  const t = setup(); await tick(); await tick();
  await t.click("[data-act=nav][data-view=schedule]");
  await tick();
  await t.click("[data-act=slot_add][data-day=sat]");
  const idx = t.root.querySelectorAll('[data-slot^="sat|"][data-slot$="|start"]').length - 1;
  t.type(`[data-slot="sat|${idx}|start"]`, "6:00");
  t.type(`[data-slot="sat|${idx}|end"]`, "8");
  await t.choose(`[data-slot="sat|${idx}|end"]`, "8");
  assert.equal(t.q(`[data-slot="sat|${idx}|start"]`).value, "6:00");  // Roh-Eingabe bleibt ohne change
  await t.click("[data-act=week_save]");
  const save = t.calls.at(-1);
  assert.equal(save.type, "smart_heating/schedule/set_week");
  assert.equal(save.week.sat[idx].start, "06:00");
  assert.equal(save.week.sat[idx].end, "08:00");
});

test("Wochenplan: ungültige Zeit wird markiert und Hinweis angezeigt", async () => {
  const t = setup(); await tick(); await tick();
  await t.click("[data-act=nav][data-view=schedule]");
  await tick();
  await t.click("[data-act=slot_add][data-day=sun]");
  t.type('[data-slot="sun|0|start"]', "25:99");
  t.type('[data-slot="sun|0|end"]', "7");
  t.root.querySelector('[data-slot="sun|0|temp"]').dispatchEvent(new t.w.Event("focusout", { bubbles: true }));
  await t.click("[data-act=slot_add][data-day=mon]"); // löst Re-Render aus
  assert.ok(t.q('[data-slot="sun|0|start"]').classList.contains("invalid"));
  assert.match(t.root.innerHTML, /Ungültige Uhrzeit/);
  assert.ok(!t.q('[data-slot="sun|0|temp"]').classList.contains("invalid"));
});

test("Raum: Batterie manuell hinzufügen und entfernen", async () => {
  const t = setup(); await tick(); await tick();
  Object.assign(t.hass.states, {
    "binary_sensor.fk_bat": { entity_id: "binary_sensor.fk_bat", state: "on", attributes: { device_class: "battery", friendly_name: "FK Batterie" } },
  });
  await t.click("[data-act=open_room][data-room=wz]");
  assert.match(t.root.innerHTML, /Batterien \(optional, manuell\)/);
  const opts = [...t.root.querySelectorAll("#dl-battery option")].map((o) => o.value);
  assert.equal(opts[0], "binary_sensor.fk_bat");                   // device_class battery zuerst
  t.type("[data-f=add_battery]", "binary_sensor.fk_bat");
  await t.click("[data-act=bat_add]");
  const call = t.calls.at(-1);
  assert.equal(call.type, "smart_heating/rooms/update");
  assert.equal(call.room_id, "wz");
  assert.ok(call.battery_entities.includes("binary_sensor.fk_bat"));
});

test("Wochenplan: Abschnitt hinzufügen bei 24:00 zeigt Hinweis statt Fehler", async () => {
  const t = setup(); await tick(); await tick();
  await t.click("[data-act=nav][data-view=schedule]");
  await tick();
  const count = () => t.root.querySelectorAll('[data-slot^="sat|"][data-slot$="|start"]').length;
  await t.click("[data-act=slot_add][data-day=sat]");   // letzter Abschnitt endet danach bei 24:00
  const before = count();
  await t.click("[data-act=slot_add][data-day=sat]");
  assert.match(t.root.innerHTML, /Tag ist bis 24:00 belegt/);
  assert.equal(t.root.querySelector(".msg.error"), null);
  assert.equal(count(), before);
});

test("Einstellungen: Thermostat-Änderungen ignorieren/übernehmen wird gespeichert", async () => {
  const t = setup(); await tick(); await tick();
  await t.click("[data-act=nav][data-view=settings]");
  assert.match(t.root.innerHTML, /Änderungen direkt am Thermostat/);
  await t.click("[data-act=settings_save]");
  assert.equal(t.calls.at(-1).thermostat_changes, "ignore");
  t.type("[data-f=s_thermo]", "adopt");
  await t.click("[data-act=settings_save]");
  const c = t.calls.at(-1);
  assert.equal(c.type, "smart_heating/settings/update");
  assert.equal(c.thermostat_changes, "adopt");
});

test("Raum: eigene Meldungstexte werden zeilenweise als Arrays gespeichert", async () => {
  const t = setup(); await tick(); await tick();
  await t.click("[data-act=open_room][data-room=wz]");
  assert.match(t.root.innerHTML, /Eigene Meldungstexte \(Varianten\)/);
  assert.equal(t.q("[data-f=texts_open]").tagName, "TEXTAREA");
  t.type("[data-f=texts_open]", "  Fenster in {raum} zu!  \n\nNoch offen: {fenster}\n");
  t.type("[data-f=texts_close]", "Alles zu.");
  await t.click("[data-act=save_window_temp]");
  const c = t.calls.at(-1);
  assert.equal(c.type, "smart_heating/rooms/update");
  assert.deepEqual(c.notify_texts, ["Fenster in {raum} zu!", "Noch offen: {fenster}"]);
  assert.deepEqual(c.notify_close_texts, ["Alles zu."]);
});
