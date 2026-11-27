"""Node-RED-Flow „Tankalarm“ als Vorlage zum Import.

Der Flow schickt eine Push-Nachricht, sobald binary_sensor.spritgrantler_*_jetzt_guenstig einschaltet:
höchstens einmal pro Kraftstoff und Tag, nicht in der Nachtruhe. Ist es nach der Nachtruhe noch
günstig, kommt die Nachricht um 7 Uhr.
"""

LOGIK = r'''// Spritgrantler Tankalarm
// Hier anpassen:
const KRAFTSTOFFE = ["Diesel", "Super"];   // nur einen Alarm? z. B. ["Diesel"]
const VON = 7, BIS = 21;                   // Nachtruhe: vor VON und ab BIS Uhr kommt nichts

const heute = new Date().toLocaleDateString("de-AT");
const gesendet = flow.get("tankalarm_gesendet") || {};

if (msg.reset) {
  flow.set("tankalarm_gesendet", {});
  node.status({ fill: "grey", shape: "ring", text: "zurückgesetzt" });
  return null;
}

const stunde = new Date().getHours();
if (!msg.test && (stunde < VON || stunde >= BIS)) {
  node.status({ fill: "yellow", shape: "ring", text: "Nachtruhe" });
  return null;
}

const ARTEN = [{ name: "Diesel", e: msg.diesel }, { name: "Super", e: msg.super }]
  .filter(a => KRAFTSTOFFE.includes(a.name));
const treffer = ARTEN.filter(a => a.e && a.e.attributes && a.e.attributes.text &&
  (msg.test || (a.e.state === "on" && gesendet[a.name] !== heute)));
if (!treffer.length) {
  node.status({ fill: "grey", shape: "dot", text: "nichts zu melden" });
  return null;
}

const knoepfe = [];
const navs = treffer.filter(a => a.e.attributes.navigation);
if (navs.length === 2 && navs[0].e.attributes.station === navs[1].e.attributes.station) {
  knoepfe.push({ action: "URI", title: "Navigieren", uri: navs[0].e.attributes.navigation });
} else {
  for (const a of navs) knoepfe.push({ action: "URI", title: a.name + " navigieren", uri: a.e.attributes.navigation });
}

if (!msg.test) {
  for (const a of treffer) gesendet[a.name] = heute;
  flow.set("tankalarm_gesendet", gesendet);
}

const app = treffer[0].e.attributes.app_path;
msg.payload = {
  data: {
    title: treffer.length === 2 ? "Diesel und Super gerade günstig" : treffer[0].name + " gerade günstig",
    message: treffer.map(a => a.name + ": " + a.e.attributes.text).join("\n\n"),
    data: Object.assign({
      tag: "spritgrantler_tankalarm",
      group: "spritgrantler",
      channel: "Tankalarm",
      importance: "high",
      notification_icon: "mdi:piggy-bank-outline",
      actions: knoepfe
    }, app ? { clickAction: app, url: app } : {})
  }
};
node.status({ fill: "green", shape: "dot", text: treffer.map(a => a.name).join(" + ") + " " +
  new Date().toLocaleTimeString("de-AT", { hour: "2-digit", minute: "2-digit" }) });
return msg;'''

INFO = """Spritgrantler Tankalarm

Schickt eine Push-Nachricht, sobald Diesel oder Super in 5 km um die Zone Zuhause so günstig ist wie selten in den letzten 14 Tagen.

Nach dem Import anpassen:
1. In allen Nodes mit Home-Assistant-Symbol den Server auswählen (Doppelklick, Feld „Server“).
2. Im Node „Push Handy“ bei „Action“ dein Handy eintragen, z. B. notify.mobile_app_mein_handy.
3. Optional im Node „Logik“ ganz oben: nur ein Kraftstoff, andere Nachtruhe.
4. Deploy, dann in der Gruppe „Test“ auf „Jetzt senden“ drücken.

Der Sensor braucht mindestens 3 Tage Preise, vorher steht in der Nachricht „Lernt noch“."""

P = "spritgrantler_ta_"


def _ev(id_, name, entity, y):
    return {"id": P + id_, "type": "server-state-changed", "z": "", "g": P + "grp", "name": name, "server": "",
            "version": 6, "outputs": 2, "exposeAsEntityConfig": "",
            "entities": {"entity": [entity], "substring": [], "regex": []},
            "outputInitially": False, "stateType": "str", "ifState": "on", "ifStateType": "str",
            "ifStateOperator": "is", "outputOnlyOnStateChange": True, "for": "0", "forType": "num",
            "forUnits": "minutes", "ignorePrevStateNull": False, "ignorePrevStateUnknown": False,
            "ignorePrevStateUnavailable": False, "ignoreCurrentStateUnknown": True,
            "ignoreCurrentStateUnavailable": True,
            "outputProperties": [{"property": "payload", "propertyType": "msg", "value": "", "valueType": "entityState"},
                                 {"property": "data", "propertyType": "msg", "value": "", "valueType": "eventData"}],
            "x": 150, "y": y, "wires": [[P + "cs_die"], []]}


def _cs(id_, name, entity, prop, y, wire):
    return {"id": P + id_, "type": "api-current-state", "z": "", "g": P + "grp", "name": name, "server": "",
            "version": 3, "outputs": 1, "halt_if": "", "halt_if_type": "str", "halt_if_compare": "is",
            "entity_id": entity, "state_type": "str", "blockInputOverrides": True,
            "outputProperties": [{"property": prop, "propertyType": "msg", "value": "", "valueType": "entity"}],
            "for": "0", "forType": "num", "forUnits": "minutes", "override_topic": False,
            "state_location": "payload", "override_payload": "msg", "entity_location": "data",
            "override_data": "msg", "x": 400, "y": y, "wires": [[wire]]}


def _inj(id_, g, name, props, x, y, crontab=""):
    return {"id": P + id_, "type": "inject", "z": "", "g": g, "name": name, "props": props, "repeat": "",
            "crontab": crontab, "once": False, "onceDelay": 0.1, "topic": "", "x": x, "y": y,
            "wires": [[P + "cs_die"]]}


def tankalarm_flow(notify: str | None = None) -> list:
    """Flow als Liste von Nodes; notify z. B. 'mobile_app_pixel' oder 'notify.mobile_app_pixel'."""
    action = notify or "notify.mobile_app_DEIN_HANDY"
    if not action.startswith("notify."):
        action = "notify." + action
    service = action.split(".", 1)[1]
    return [
        {"id": P + "grp", "type": "group", "z": "", "name": "Tankalarm", "style": {"fill": "#dff3f3", "label": True},
         "nodes": [P + n for n in ("ev_die", "ev_sup", "in_7", "cs_die", "cs_sup", "fn", "push", "dbg", "info")],
         "x": 34, "y": 39, "w": 772, "h": 262},
        _ev("ev_die", "Diesel günstig", "binary_sensor.spritgrantler_diesel_jetzt_guenstig", 80),
        _ev("ev_sup", "Super günstig", "binary_sensor.spritgrantler_super_jetzt_guenstig", 140),
        _inj("in_7", P + "grp", "7 Uhr", [], 150, 200, "00 07 * * *"),
        _cs("cs_die", "Diesel", "binary_sensor.spritgrantler_diesel_jetzt_guenstig", "diesel", 80, P + "cs_sup"),
        _cs("cs_sup", "Super", "binary_sensor.spritgrantler_super_jetzt_guenstig", "super", 140, P + "fn"),
        {"id": P + "fn", "type": "function", "z": "", "g": P + "grp", "name": "Logik", "func": LOGIK, "outputs": 1,
         "timeout": 0, "noerr": 0, "initialize": "", "finalize": "", "libs": [], "x": 400, "y": 260,
         "wires": [[P + "push", P + "dbg"]]},
        {"id": P + "push", "type": "api-call-service", "z": "", "g": P + "grp", "name": "Push Handy", "server": "",
         "version": 7, "debugenabled": False, "action": action, "floorId": [], "areaId": [], "deviceId": [],
         "entityId": [], "labelId": [], "data": "", "dataType": "jsonata", "mergeContext": "",
         "mustacheAltTags": False, "outputProperties": [], "queue": "none", "blockInputOverrides": False,
         "domain": "notify", "service": service, "x": 680, "y": 80, "wires": [[]]},
        {"id": P + "dbg", "type": "debug", "z": "", "g": P + "grp", "name": "Debug", "active": True,
         "tosidebar": True, "console": False, "tostatus": False, "complete": "payload", "targetType": "msg",
         "statusVal": "", "statusType": "auto", "x": 680, "y": 140, "wires": []},
        {"id": P + "info", "type": "comment", "z": "", "g": P + "grp", "name": "Anleitung (Doppelklick)", "info": INFO,
         "x": 680, "y": 200, "wires": []},
        {"id": P + "test", "type": "group", "z": "", "name": "Test", "style": {"fill": "#e3f3d3", "label": True},
         "nodes": [P + "in_test", P + "in_reset"], "x": 834, "y": 39, "w": 232, "h": 142},
        _inj("in_test", P + "test", "Jetzt senden", [{"p": "test", "v": "true", "vt": "bool"}], 950, 80),
        _inj("in_reset", P + "test", "Tageszähler leeren", [{"p": "reset", "v": "true", "vt": "bool"}], 950, 140),
    ]
