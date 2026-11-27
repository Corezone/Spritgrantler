# Spritgrantler

Spritpreise für ganz Österreich (E-Control) mit Karte, Statistik, bester Tankzeit und Tankbuch. Die App sammelt alle 30 Minuten die Preise, speichert sie in ihrer eigenen Datenbank und wertet sie aus.

## Einrichtung

Nichts zu tun. Nach dem Start erscheint Spritgrantler in der Seitenleiste. Für den Bereich „Zuhause“ nimmt die App die Zone **Zuhause** deiner Home-Assistant-Instanz.

Ist der **Mosquitto broker** installiert, meldet sich Spritgrantler automatisch als Gerät **Spritgrantler** an (Einstellungen → Geräte & Dienste → MQTT). Ohne Broker schreibt die App die Sensoren direkt, dann gehen sie nach einem Neustart von Home Assistant bis zur nächsten Sammelrunde verloren.

## Sensoren

Jeweils für Diesel (`diesel`) und Super 95 (`super`):

| Entität | Inhalt |
|---|---|
| `sensor.spritgrantler_diesel_guenstigster` | günstigster Preis in Österreich, mit Tankstelle und Navigationslink |
| `sensor.spritgrantler_diesel_guenstigster_zuhause` | günstigster Preis in 5 km um Zuhause |
| `sensor.spritgrantler_diesel_median` | Median aller erfassten Tankstellen |
| `sensor.spritgrantler_diesel_empfehlung` | jetzt tanken oder warten, Attribut `text` mit Begründung |
| `sensor.spritgrantler_diesel_tagestipp` | Plan für den Tag, z. B. „Diesel: heute 9–12 Uhr tanken“, Attribut `message` |
| `binary_sensor.spritgrantler_diesel_jetzt_guenstig` | **an**, wenn es um Zuhause so günstig ist wie selten in den letzten 14 Tagen |

Die Auswertungen brauchen Zeit: `jetzt_guenstig` ab 3 Tagen Daten, das beste Zeitfenster im Tagestipp ab 3 Tagen, der Vergleich mit anderen Wochentagen ab 7 Tagen. Vorher steht „Lernt noch“ im Text.

## Tankalarm

Eine Push-Nachricht, sobald Tanken gerade günstig ist, zum Beispiel:

> **Diesel gerade günstig in deiner Nähe**
> Diesel: 1,529 € bei AVANTI (2,3 km), 3,1 ct unter dem 14-Tage-Schnitt. Günstiger war es nur 8 % der Zeit.

mit einem Knopf zum Navigieren.

### Einschalten (ohne Node-RED)

In Spritgrantler den Tab **Info** öffnen und unter **Tankalarm** auf **An** tippen. Das gilt nur für den Benutzer, der gerade angemeldet ist – jeder schaltet ihn für sich ein.

- **Ort:** *Wo ich bin* nimmt den aktuellen Standort deiner Person in Home Assistant, *Zuhause* die Zone Zuhause.
- **Kraftstoff:** Diesel, Super oder beide.
- **Push an:** Dein Handy wird automatisch erkannt (über die Tracker deiner Person). Steht das falsche da, wähle es aus.
- **Test-Push senden** schickt sofort eine Nachricht mit dem aktuellen Stand.

Voraussetzungen: dein Home-Assistant-Benutzer ist mit einer **Person** verknüpft (Einstellungen → Personen) und die **Companion-App** ist auf dem Handy eingerichtet. Ohne verknüpfte Person gilt Zuhause.

Geprüft wird nach jeder Sammelrunde (alle 30 Minuten). Höchstens eine Push pro Kraftstoff und Tag, nicht zwischen 21 und 7 Uhr; ist es danach noch günstig, kommt sie am Morgen.

### Alternative: Node-RED

Wer die Push selbst steuern will (andere Empfänger, eigene Bedingungen), findet im Info-Tab unter **Lieber mit Node-RED?** einen fertigen Flow. Er arbeitet mit den Sensoren `binary_sensor.spritgrantler_*_jetzt_guenstig` und damit immer mit der Zone Zuhause.

#### Flow holen

Im Tab **Info** unter **Lieber mit Node-RED?** dein Handy auswählen und **Flow herunterladen** oder **In Zwischenablage kopieren**. Mit gewähltem Handy ist der Push-Dienst schon eingetragen.

Ohne Home Assistant (Docker) liegt die Vorlage im Repository unter `nodered/spritgrantler-tankalarm.json`.

#### Was du ändern musst

1. **Importieren:** In Node-RED rechts oben ☰ → Importieren, Datei wählen oder Inhalt einfügen.
2. **Server wählen:** In den Nodes *Diesel günstig*, *Super günstig*, *Diesel*, *Super* und *Push Handy* per Doppelklick deinen Home-Assistant-Server auswählen. Ohne Server laufen die Nodes nicht, im Node-RED-Log steht dann „Invalid server config“.
3. **Handy eintragen** (nur wenn du im Info-Tab keins gewählt hast): Im Node *Push Handy* bei **Action** deinen Dienst eintragen, z. B. `notify.mobile_app_mein_handy`. Die Namen findest du in Home Assistant unter Entwicklerwerkzeuge → Aktionen, Suche „notify.mobile_app“.
4. **Deploy** und in der Gruppe *Test* auf **Jetzt senden** drücken. Die Nachricht kommt auch dann, wenn es gerade nicht günstig ist.

#### Was du ändern kannst

Ganz oben im Node *Logik*:

```js
const KRAFTSTOFFE = ["Diesel", "Super"];   // nur einen Alarm? z. B. ["Diesel"]
const VON = 7, BIS = 21;                   // Nachtruhe
```

#### So verhält sich der Flow

- höchstens **eine Nachricht pro Kraftstoff und Tag**
- werden beide gleichzeitig günstig, kommt **eine** gemeinsame Nachricht
- in der **Nachtruhe** kommt nichts; ist es danach noch günstig, kommt die Nachricht um 7 Uhr
- *Tageszähler leeren* in der Gruppe *Test* hebt die Sperre für heute auf

### Wann gilt es als günstig?

Spritgrantler schaut auf die günstigste Tankstelle in 5 km um Zuhause. Der Sensor schaltet ein, wenn es in den letzten 14 Tagen höchstens 15 % der Zeit billiger war und der Preis mindestens 1 ct unter dem Schnitt liegt. Er schaltet erst wieder aus, wenn es deutlich teurer wird (über 30 %), damit er nicht flackert.

## Optionen

| Option | Bedeutung |
|---|---|
| `interval_min` | Minuten zwischen zwei Sammelrunden |
| `retention_days` | wie lange Preise gespeichert bleiben |
| `request_delay_s` | Pause zwischen den Abfragen bei E-Control |
| `center_lat`, `center_lon` | Startpunkt der Karte, falls keine Zone Zuhause gesetzt ist |
| `demo` | Beispieldaten zum Ausprobieren |
