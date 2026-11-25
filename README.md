# Spritgrantler

Grantelt über jeden Cent zu viel: Spritpreise für ganz Österreich live auf der Karte, mit Statistik und Empfehlung für die beste Tankzeit.
Datenquelle ist die offizielle API des E-Control-Spritpreisrechners. Einen API-Key braucht man nicht.

**Funktionen**
- Karte (OpenStreetMap) mit Preis-Pins und einstellbarem Radius (1–50 km)
- Darkmode: automatisch nach Geräteeinstellung oder fest hell/dunkel (Info-Tab)
- Adresssuche (OpenStreetMap, auf Österreich beschränkt)
- Standort automatisch von der Person des angemeldeten Home-Assistant-Benutzers (über Ingress, kein eigener Login nötig); alternativ Zuhause, andere Personen oder Tippen bzw. Ziehen auf der Karte
- Liste der günstigsten Tankstellen mit Navigation in Google Maps
- Diesel und Super 95 umschaltbar
- Statistik mit Umschalter **Woche / Monat / Jahr**: Empfehlung „Jetzt tanken / Warte bis X Uhr“, Preis nach Uhrzeit, Heatmap Wochentag × Uhrzeit, günstigster Wochentag, Überblick mit Spanne und Ersparnis, letzte 48 Stunden, Verlauf (im Jahr wochenweise)
- Detailansicht je Tankstelle mit Preisverlauf und Öffnungszeiten
- **Empfehlungen** aus den eigenen Daten: nächstes günstiges Zeitfenster (Wochentag × Uhrzeit), günstigster Termin der Woche, Preistendenz (steigend, fallend, stabil), verlässlich günstige Stammtankstellen im Umkreis, persönlicher Tipp aus dem Tankbuch und Sparpotenzial im Tagesverlauf
- **Tankbuch** pro Home-Assistant-Benutzer: „Ich war tanken“ speichert Liter und Preis und vergleicht mit dem Median zum Zeitpunkt, der günstigsten Tankstelle in 5 km und der besten Zeit an derselben Tankstelle am selben Tag. Dazu Gesamtersparnis, Ø bezahlter Preis und Ersparnis je Tankvorgang. Tankbuch-Einträge werden nicht automatisch gelöscht.
- Daten werden nach 365 Tagen automatisch gelöscht (einstellbar)

## Installation als Home-Assistant-App

**Variante A, lokal:** Den Ordner `spritgrantler/` nach `/addons/spritgrantler` auf den HA-Host kopieren (Samba- oder SSH-App).
Danach unter *Einstellungen → Apps → App-Store → ⋮ → Nach Updates suchen* erscheint „Spritgrantler“ unter *Lokale Apps*.

**Variante B, GitHub:** Das Repo als `Corezone/spritgrantler` pushen und die URL unter *App-Store → ⋮ → Repositories* hinzufügen.

Dann installieren, starten und „In Seitenleiste anzeigen“ aktivieren. Die Oberfläche läuft über Ingress, ein eigener Port ist nicht nötig.

## Betrieb ohne Home Assistant (Docker-LXC)

```bash
docker compose up -d --build
# → http://<IP>:8099
```

## Optionen

| Option | Standard | Bedeutung |
|---|---|---|
| `region` | `Österreich` | `Österreich`, `Wien`, ein Bundesland oder Bezirkscodes wie `901,902` |
| `interval_min` | `30` | Abfrageintervall. Für nur Wien gehen auch 15 |
| `retention_days` | `365` | Löschfrist |
| `request_delay_s` | `2.5` | Pause zwischen einzelnen API-Anfragen |
| `center_lat` / `center_lon` | Stephansplatz | Startmitte der Karte |
| `demo` | `false` | Beispieldaten, zum Ausprobieren ohne API |

## Wie die Daten gesammelt werden

- Die API liefert je Abfrage nur die **günstigsten** Tankstellen mit Preis. Spritgrantler fragt deshalb jeden Bezirk der Region einzeln ab. Für ganz Österreich sind das rund 230 Anfragen pro Runde, die mit Pause dazwischen etwa 10 Minuten dauern.
- Zusätzlich holt die Karte beim Öffnen die nächstgelegenen Tankstellen live, höchstens alle 5 Minuten pro Ort.
- Es werden nur **Preisänderungen** gespeichert. Ein gleichbleibender Preis verlängert nur den bestehenden Eintrag.
- Falls die Bezirksliste nicht abrufbar ist, nutzt Spritgrantler ein Punkteraster rund um die Kartenmitte.
- Nicht zu oft abfragen: In der Community wird berichtet, dass zu kurze Intervalle zu einer Sperre für einen Tag führen können.

## Datenmenge

Bei ganz Österreich, 30 Minuten Intervall und 365 Tagen Speicherung wächst die Datenbank auf etwa 300–500 MB. Nur Wien liegt bei etwa 60–120 MB im Jahr.

## Sensoren in Home Assistant

Nach jeder Sammelrunde legt die App diese Sensoren an bzw. aktualisiert sie (Diesel und Super):

| Sensor | Inhalt |
|---|---|
| `sensor.spritgrantler_diesel_guenstigster` | günstigster Preis in Österreich, Attribute: Tankstelle, Adresse, Navigationslink |
| `sensor.spritgrantler_diesel_guenstigster_zuhause` | günstigster Preis in 5 km um die Zone Zuhause, mit Entfernung |
| `sensor.spritgrantler_diesel_median` | Median der erfassten Tankstellen |
| `sensor.spritgrantler_diesel_empfehlung` | z. B. „Jetzt tanken“ oder „Warte bis 9 Uhr“, Attribut `text` mit Begründung |

Für Super heißen sie `sensor.spritgrantler_super_…`. Die Sensoren haben eine Verlaufsstatistik und eignen sich direkt für Automationen oder Node-RED, z. B. eine Push-Nachricht, wenn `…_guenstigster_zuhause` unter einen Wunschpreis fällt. Nach einem Neustart von Home Assistant erscheinen sie wieder mit der nächsten Sammelrunde.

## API-Endpunkte

`/api/stations?lat=&lon=&radius_km=&fuel=DIE|SUP` · `/api/summary?fuel=` · `/api/stats?fuel=&days=` · `/api/station/{id}?fuel=` · `/api/info`
