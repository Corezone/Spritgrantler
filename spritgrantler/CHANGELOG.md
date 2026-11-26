# Changelog

## 1.10.0 – 2026-11-26
- Neu: Sensoren `sensor.spritgrantler_diesel_tagestipp` und `sensor.spritgrantler_super_tagestipp` für eine Tankempfehlung in der Früh
  - bestes Zeitfenster für heute, oder ein deutlich günstigerer Tag in den nächsten drei Tagen
  - Preistrend, mögliche Ersparnis auf 50 l und die günstigste Tankstelle in 5 km um Zuhause samt Navigationslink
- Neu: Schnittstelle `/api/daily_tip?fuel=DIE` zum Testen

## 1.9.1 – 2026-11-26
- Changelog ist jetzt in Home Assistant beim Update einsehbar

## 1.9.0 – 2026-11-26
- Neu: Standard-Kraftstoff unter Info → Einstellungen. Damit startet die App, gespeichert je Home-Assistant-Benutzer
- Navigation am Desktop oben im Kopf, am Handy als feste Leiste unten
- Suche, Standort und Radius in einer gemeinsamen Karte
- Preisschilder auf der Karte überdecken sich nicht mehr, Bestpreis hervorgehoben
- Tankstellenliste zeigt den Abstand zum Bestpreis sowie Entfernung, „Am günstigsten“ und „Am nächsten“
- Empfehlungen einklappbar, Arya im Statistik-Kopf und im leeren Tankbuch
- Markierung „jetzt“ in den Diagrammen ruhiger dargestellt

## 1.8.0 – 2026-11-25
- Löschfrist auf 90 Tage gesenkt
- Statistik umschaltbar: Woche, Monat, 3 Monate

## 1.7.1 – 2026-11-25
- Standortwahl zeigt nur noch den eigenen Standort und Zuhause

## 1.7.0 – 2026-11-25
- Daten gezielt löschen im Info-Tab (Preise, eigenes Tankbuch, Kartenkacheln)

## 1.6.1 – 2026-11-25
- Demo-Daten werden automatisch entfernt, wenn der Demo-Modus aus ist

## 1.6.0 – 2026-11-25
- Dunkle Darstellung (automatisch, hell, dunkel)

## 1.5.1 – 2026-11-25
- Kartenkacheln werden über die App geladen (OpenStreetMap-Sperre behoben)

## 1.5.0 – 2026-11-25
- Sensoren in Home Assistant, Watchdog, Entfernung beim günstigsten Preis

## 1.4.1 – 2026-11-24
- „Günstigster jetzt“ zeigt die Tankstelle, Antippen öffnet sie auf der Karte

## 1.4.0 – 2026-11-24
- Empfehlungen aus der Statistik

## 1.3.0 – 2026-11-24
- Tankbuch mit „Ich war tanken“ und Ersparnis-Statistik

## 1.2.1 – 2026-11-24
- Gezeichnetes Arya-Logo, realistischer überarbeitet

## 1.2.0 – 2026-11-24
- Standort des angemeldeten Home-Assistant-Benutzers

## 1.1.0 – 2026-11-24
- Adresssuche, Standort aus Home Assistant, OpenStreetMap-Karte

## 1.0.0 – 2026-11-24
- Erste Version: Spritpreise für ganz Österreich als Home-Assistant-App
