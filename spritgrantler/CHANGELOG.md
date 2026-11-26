# Changelog

## v1.11.1 – Sensoren sofort nach dem Start
- Sensoren werden direkt beim Start aus den gespeicherten Daten gemeldet, nicht erst nach der ersten Sammelrunde (rund 10 Minuten)

## v1.11.0 – Gerät in Home Assistant
- **Spritgrantler erscheint als Gerät** in Home Assistant: alle Sensoren kommen per MQTT-Discovery mit eindeutigen IDs, ganz ohne Einrichtung
- Die Zugangsdaten zum Mosquitto-Broker holt sich die App selbst vom Supervisor
- Werte bleiben über Neustarts von Home Assistant erhalten, Sensoren lassen sich umbenennen, Bereichen zuordnen und ausblenden
- Die bisherigen Entitäts-IDs bleiben gleich, bestehende Automationen und Node-RED-Flows laufen weiter
- Ohne MQTT-Broker werden die Sensoren wie bisher direkt geschrieben
- Docker-Betrieb: Broker über `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASSWORD` einstellbar

## v1.10.0 – Tagestipp für die Früh
- **Neue Sensoren** `sensor.spritgrantler_diesel_tagestipp` und `sensor.spritgrantler_super_tagestipp` für eine Tankempfehlung am Morgen
- Bestes Zeitfenster für heute – oder ein deutlich günstigerer Tag in den nächsten drei Tagen
- Dazu Preistrend, mögliche Ersparnis auf 50 l und die günstigste Tankstelle in 5 km um Zuhause samt Navigationslink
- Schnittstelle `/api/daily_tip?fuel=DIE` zum Testen
- GitHub-Releases werden ab jetzt automatisch angelegt

## v1.9.1 – Changelog in Home Assistant
- Der Changelog ist jetzt beim Update in Home Assistant einsehbar

## v1.9.0 – Neue Oberfläche & Standard-Kraftstoff
- **Standard-Kraftstoff** unter Info → Einstellungen: damit startet die App, gespeichert je Home-Assistant-Benutzer
- **Navigation** am Desktop oben im Kopf, am Handy als feste Leiste unten
- Suche, Standort und Radius in einer gemeinsamen Karte
- Preisschilder auf der Karte überdecken sich nicht mehr, Bestpreis hervorgehoben
- Tankstellenliste zeigt den Abstand zum Bestpreis sowie Entfernung, „Am günstigsten“ und „Am nächsten“
- Empfehlungen einklappbar, Arya im Statistik-Kopf und im leeren Tankbuch

## v1.8.0 – Statistik-Zeiträume
- Statistik umschaltbar: **Woche, Monat, 3 Monate**
- Löschfrist auf 90 Tage gesenkt

## v1.7.1 – Aufgeräumte Standortwahl
- Standortwahl zeigt nur noch den eigenen Standort und Zuhause

## v1.7.0 – Daten löschen
- **Daten gezielt löschen** im Info-Tab: Preise und Statistik, eigenes Tankbuch, Kartenkacheln

## v1.6.1 – Demo-Daten aufräumen
- Demo-Daten werden automatisch entfernt, wenn der Demo-Modus aus ist

## v1.6.0 – Darkmode
- **Dunkle Darstellung:** automatisch, hell oder dunkel

## v1.5.1 – Kartenkacheln über die App
- Kartenkacheln werden über die App geladen und zwischengespeichert (OpenStreetMap-Sperre behoben)

## v1.5.0 – Sensoren in Home Assistant
- **Sensoren** für günstigsten Preis, Median und Empfehlung je Kraftstoff, nutzbar in Automationen und Node-RED
- Watchdog für die App, Entfernung beim günstigsten Preis

## v1.4.1 – Günstigster jetzt
- „Günstigster jetzt“ zeigt die Tankstelle, Antippen öffnet sie auf der Karte

## v1.4.0 – Empfehlungen
- **Empfehlungen aus der Statistik:** gutes Zeitfenster, Preistrend, verlässlich günstige Tankstellen, Sparpotenzial

## v1.3.0 – Tankbuch
- **Tankbuch** mit „Ich war tanken“: Vergleich mit Markt, günstigster Tankstelle in 5 km und bester Tageszeit
- Ersparnis-Statistik je Tankvorgang

## v1.2.1 – Neues Arya-Logo
- Gezeichnetes Arya-Logo, realistischer überarbeitet

## v1.2.0 – Standort aus Home Assistant
- Die Karte startet beim Standort des angemeldeten Home-Assistant-Benutzers

## v1.1.0 – Adresssuche & Karte
- **Adresssuche**, Standort aus Home Assistant, OpenStreetMap-Karte

## v1.0.0 – Erste Version
- Spritpreise für ganz Österreich (E-Control) als Home-Assistant-App
- Karte mit Radius, Tankstellenliste mit Navigation über Google Maps, Statistik zur besten Tankzeit
