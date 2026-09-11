#!/usr/bin/env python3
"""
Dienstplan-Sync fuer GitHub Actions.

Liest das Apache-Verzeichnislisting der Besatzungslisten auf faehre2.de,
waehlt je Kalenderwoche die aktuellste Fassung (hoechste "n. Aenderung"),
sucht darin nach einem Namen und schreibt zwei ICS-Kalenderdateien nach
docs/<SLUG>/:

  dienst.ics        -> Wochen mit Schiffszuordnung ("Dienst auf ..."). Bei
                       Dienstwochen mit Fahrplan-Daten ein Termin je Tag
                       statt einem Termin fuer die Woche, dessen
                       Beschreibung nur die Abfahrten des eigenen Schiffs
                       an diesem Tag enthaelt (aus den Fahrplan-PDFs unter
                       Dienstplan-FAL/, siehe gruppiere_abfahrten_pro_tag
                       und build_tages_vevents) - keine eigenen, farbigen
                       Kalendertermine je Abfahrt, nur eine Anmerkung am
                       jeweiligen Tages-Termin.
  frei.ics          -> Freie Tage / Urlaub / Abwesend
  voraussichtlich.ics -> unbestaetigte Vermutungen fuer die Folgewoche,
                         abgeleitet aus Nachbarspalte, Farbmarkierung und
                         fehlender Farbmarkierung (siehe build_prognosen)

Diese Dateien werden von GitHub Pages veroeffentlicht. Google Kalender
(und darueber auch die Handy-Kalender-Apps) abonnieren die Adresse per
"Von URL hinzufuegen" und aktualisieren sich danach von selbst.

Ein PDF wird nur dann heruntergeladen, wenn sich Dateiname oder
Aenderungsdatum gegenueber state.json unterscheiden.

Zugangsdaten kommen aus den Umgebungsvariablen WDR_USER / WDR_PASS
(als GitHub Actions Secrets hinterlegt), nicht aus einer lokalen Datei.
"""

import colorsys
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from html import unescape
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote

import pdfplumber
import requests

HERE = Path(__file__).resolve().parent

# Fester, zufaellig erzeugter Ordnername - macht die Pages-Adresse
# schwer zu erraten. Achtung: schuetzt NICHT gegen jemanden, der das
# Repository selbst aufruft (siehe README).
SLUG = "kal-76a4a349015c4272fe03f77806423a4b"

OUTPUT_DIR = HERE / "docs" / SLUG
STATE_PATH = HERE / "state.json"

DIENST_ICS_PATH = OUTPUT_DIR / "dienst.ics"
FREI_ICS_PATH = OUTPUT_DIR / "frei.ics"
PROGNOSE_ICS_PATH = OUTPUT_DIR / "voraussichtlich.ics"
# Alte, nicht mehr erzeugte Datei - wird geloescht statt fortgeschrieben,
# siehe main().
ALTE_ABFAHRTEN_ICS_PATH = OUTPUT_DIR / "abfahrten.ics"

BASE_URL = "https://faehre2.de/fileadmin/wdr/Schiffe/Besatzungslisten"
INDEX_URL = BASE_URL + "/"

FAHRPLAN_BASE_URL = "https://faehre2.de/fileadmin/wdr/Dienstplan-FAL"
FAHRPLAN_INDEX_URL = FAHRPLAN_BASE_URL + "/"

TARGET_NAME = os.environ.get("WDR_NAME_FRAGMENT", "Roeloffs")
WEEKS_BACK = int(os.environ.get("WDR_WEEKS_BACK", "2"))
WEEKS_AHEAD = int(os.environ.get("WDR_WEEKS_AHEAD", "8"))
# Wie lange alte Wochen im Kalender stehen bleiben, bevor sie entfallen.
PRUNE_WEEKS = int(os.environ.get("WDR_PRUNE_WEEKS", "12"))

KNOWN_RANKS = {
    "NK", "NEO", "TLM", "TWB", "TWO", "GSM", "NWB", "NWB*", "AZ", "NW",
}

# Konstanter Ersatzstempel, falls das Verzeichnislisting kein Datum liefert.
# Bewusst konstant, damit die ICS nicht bei jedem Lauf neu geschrieben wird.
FALLBACK_STAMP = "20000101T000000Z"

# Eine Zeile des Apache-Listings: href zuerst, danach das Aenderungsdatum.
# Bewusst auf das href-Attribut gematcht (prozentkodiertes ASCII) statt auf
# den Linktext - so ist das Ergebnis unabhaengig von der Zeichenkodierung
# der HTML-Seite und von Apaches Namenskuerzung im Anzeigetext.
ROW_RE = re.compile(
    r'<a href="([^"?/][^"]*\.pdf)"[^>]*>[^<]*</a>'
    r'(?:\s*</td>\s*<td[^>]*>)?\s*'
    r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})?',
    re.IGNORECASE,
)

# "38KW2026.pdf" oder "38KW2026 1. Aenderung.pdf".
# Das Muster "\S*nderung" trifft absichtlich Aenderung, Änderung und eine
# eventuell falsch dekodierte Variante gleichermassen.
FILE_RE = re.compile(
    r"^(\d{1,2})KW(\d{4})(?:[ _]+(\d+)\.\s*\S*nderung)?\.pdf$",
    re.IGNORECASE,
)


# Schiffe der WDR-Flotte. Positivliste: nur diese loesen eine Vorhersage aus.
SCHIFFE = [
    "NORDERAUE",
    "SCHLESWIG - HOLSTEIN",
    "UTHLANDE",
    "NORDFRIESLAND",
    "HILLIGENLEI",
]

# "32._KW_FAL.pdf" oder "38._KW_FAL_FW.pdf" (FW = vermutlich Winterfahrplan-
# Variante). Anders als die Besatzungslisten tragen die Fahrplan-Dateien
# keine Jahreszahl im Namen - die Zuordnung zum Jahr erfolgt daher ueber das
# Aenderungsdatum aus dem Verzeichnislisting, siehe find_fahrplan_eintrag().
# Toleranter als FILE_RE, weil die genaue Namenskonvention hier nicht aus
# einem echten Verzeichnislisting bestaetigt werden konnte (nur aus
# einzelnen, per Hand hochgeladenen Beispieldateien).
FAHRPLAN_FILE_RE = re.compile(
    r"^(\d{1,2})\D*KW\D*FAL\D*\.pdf$", re.IGNORECASE
)

# Schiffskuerzel in den Fahrplan-PDFs - unabhaengig von SCHIFFE_NORM, weil
# die Buchstaben nicht mit den Anfangsbuchstaben der Schiffsnamen
# uebereinstimmen (vom Nutzer bestaetigt). HILLIGENLEI kommt in den
# Fahrplaenen nicht vor.
FAHRPLAN_SCHIFF_KUERZEL = {
    "N": "NORDFRIESLAND",
    "NA": "NORDERAUE",
    "S": "SCHLESWIG - HOLSTEIN",
    "U": "UTHLANDE",
}

FAHRPLAN_ROUTEN = ["Wittdün-Wyk", "Wyk-Dagebüll", "Dagebüll-Wyk", "Wyk-Wittdün"]
FAHRPLAN_ZEIT_RE = re.compile(r"^\d{1,2}:\d{2}$")
FAHRPLAN_WOCHENTAG_DATUM_RE = re.compile(r"^([A-Za-zÄÖÜäöüß]+)(\d{2}\.\d{2}\.\d{4})$")


def norm(text):
    return "".join(c for c in (text or "").upper() if c.isalnum())


SCHIFFE_NORM = {norm(s): s for s in SCHIFFE}


def ist_schiff(kategorie):
    return norm(kategorie) in SCHIFFE_NORM


# Manche Kolleginnen und Kollegen werden in Urlaub/Freie-Tage-Bloecken mit
# der Farbe des Schiffs hinterlegt, auf dem sie voraussichtlich als
# naechstes fahren (auch als Hinweis "faehrt auf demselben Schiff weiter",
# wenn die Faerbung in der eigenen Schiffsspalte auftaucht). Der Farbton
# der Fahrplaner ist nicht pixelgenau reproduzierbar, daher Vergleich ueber
# den Hue-Winkel mit Toleranz statt exaktem RGB-Abgleich.
HUE_TOLERANCE_DEGREES = 20


def fill_rects(page):
    """Farbig gefuellte Flaechen einer Seite, ohne die duennen Rahmenlinien
    der Tabelle (die pdfplumber ebenfalls als 'rects' mit Fuellfarbe meldet)."""
    return [
        r
        for r in page.rects
        if (r["x1"] - r["x0"]) > 3
        and (r["bottom"] - r["top"]) > 3
        and r.get("non_stroking_color")
    ]


def color_at(rects, x, y):
    """Fuellfarbe am Punkt (x, y); bei Ueberlappung die kleinste (spezifischste)
    Flaeche."""
    best, best_area = None, None
    for r in rects:
        if r["x0"] <= x <= r["x1"] and r["top"] <= y <= r["bottom"]:
            area = (r["x1"] - r["x0"]) * (r["bottom"] - r["top"])
            if best_area is None or area < best_area:
                best, best_area = r, area
    return best["non_stroking_color"] if best else None


def cell_hue(color):
    """Hue-Winkel (0-360) einer Fuellfarbe, oder None bei fehlender Farbe
    oder zu geringer Saettigung (Grau/Weiss - kein auswertbarer Farbton)."""
    if not color or len(color) < 3:
        return None
    h, s, _v = colorsys.rgb_to_hsv(color[0], color[1], color[2])
    return h * 360 if s >= 0.15 else None


def hues_close(a, b, tolerance=HUE_TOLERANCE_DEGREES):
    if a is None or b is None:
        return False
    diff = abs(a - b) % 360
    return min(diff, 360 - diff) <= tolerance


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    STATE_PATH.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def iso_weeks_to_check(weeks_back, weeks_ahead):
    today = date.today()
    monday_this_week = today - timedelta(days=today.weekday())
    result = []
    for offset in range(-weeks_back, weeks_ahead + 1):
        monday = monday_this_week + timedelta(weeks=offset)
        iso_year, iso_week, _ = monday.isocalendar()
        result.append((iso_year, iso_week))
    return result


def safe_decode_href(href):
    text = unescape(href)
    try:
        return unquote(text, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        return unquote(text, encoding="latin-1", errors="replace")


def fetch_index(session):
    """
    Liest das Verzeichnislisting einmalig.

    Rueckgabe: {(jahr, kw): (revision, href, dateiname, mtime)}
    mit jeweils der hoechsten gefundenen Revision.
    """
    resp = session.get(INDEX_URL, timeout=30)
    if resp.status_code == 401:
        sys.exit("HTTP 401: WDR_USER / WDR_PASS falsch oder abgelaufen.")
    if resp.status_code == 403:
        sys.exit("HTTP 403: Zugriff auf das Verzeichnis verweigert.")
    resp.raise_for_status()

    best = {}
    rows = ROW_RE.findall(resp.text)
    if not rows:
        sys.exit(
            "Verzeichnislisting enthaelt keine PDF-Zeilen - "
            "HTML-Struktur oder Adresse pruefen."
        )

    for href, mtime in rows:
        name = safe_decode_href(href)
        m = FILE_RE.match(name)
        if not m:
            print(f"  Hinweis: unbekanntes Dateimuster '{name}'")
            continue
        week = int(m.group(1))
        year = int(m.group(2))
        revision = int(m.group(3) or 0)
        key = (year, week)
        if key not in best or revision > best[key][0]:
            best[key] = (revision, href, name, mtime or "")

    if not best:
        sys.exit("Keine auswertbaren Dateinamen im Verzeichnislisting gefunden.")
    return best


def fetch_fahrplan_index(session):
    """
    Liest das Verzeichnislisting der Fahrplan-PDFs.

    Rueckgabe: Liste von (kw, mtime, href, dateiname). Anders als bei den
    Besatzungslisten gibt es hier kein Jahr im Dateinamen und keine erkennbare
    Revisionsnummerierung - mehrere Eintraege mit derselben KW sind moeglich
    (z.B. aus Vorjahren, falls das Verzeichnis nicht aufgeraeumt wird). Die
    Auswahl der richtigen Datei erfolgt in find_fahrplan_eintrag() ueber die
    Naehe des Aenderungsdatums zur gesuchten Kalenderwoche.
    """
    resp = session.get(FAHRPLAN_INDEX_URL, timeout=30)
    if resp.status_code == 401:
        sys.exit("HTTP 401: WDR_USER / WDR_PASS falsch oder abgelaufen (Fahrplan).")
    if resp.status_code == 403:
        sys.exit("HTTP 403: Zugriff auf das Fahrplan-Verzeichnis verweigert.")
    resp.raise_for_status()

    entries = []
    for href, mtime in ROW_RE.findall(resp.text):
        name = safe_decode_href(href)
        m = FAHRPLAN_FILE_RE.match(name)
        if not m:
            continue
        entries.append((int(m.group(1)), mtime or "", href, name))
    return entries


def find_fahrplan_eintrag(entries, jahr, kw):
    """Der Eintrag mit passender KW, dessen Aenderungsdatum am naechsten am
    Montag der gesuchten Kalenderwoche liegt."""
    kandidaten = [e for e in entries if e[0] == kw]
    if not kandidaten:
        return None
    ziel = date.fromisocalendar(jahr, kw, 1)

    def distanz(eintrag):
        try:
            mtime_datum = datetime.strptime(eintrag[1], "%Y-%m-%d %H:%M").date()
        except (ValueError, TypeError):
            return timedelta(days=9999)
        return abs(mtime_datum - ziel)

    return min(kandidaten, key=distanz)


def fahrplan_rows_from_words(words, toleranz=2.5):
    """Gruppiert Woerter einer Fahrplan-Seite zu Zeilen anhand der y-Position."""
    zeilen = []
    for wort in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if zeilen and abs(zeilen[-1][0]["top"] - wort["top"]) <= toleranz:
            zeilen[-1].append(wort)
        else:
            zeilen.append([wort])
    for zeile in zeilen:
        zeile.sort(key=lambda w: w["x0"])
    return zeilen


def fahrplan_spalte_fuer(x0, spalten_x):
    """Route, deren Spaltenanfang am naechsten an x0 liegt."""
    return min(range(len(spalten_x)), key=lambda i: abs(x0 - spalten_x[i]))


def parse_fahrplan_pdf(pdf):
    """
    Liest alle Abfahrten aus einem Fahrplan-PDF.

    Jede Seite enthaelt mehrere Tagesbloecke, erkennbar an der Zeile
    "Dienstplan KW <n> <Wochentag><Datum>", gefolgt von der Routen-
    Kopfzeile ("Wittdün-Wyk Wyk-Dagebüll Dagebüll-Wyk Wyk-Wittdün") und
    einer Tidenzeile ("HW ... NW..."), die uebersprungen wird. Die Route
    einer Abfahrt ergibt sich aus der x-Position der Uhrzeit, nicht aus der
    Reihenfolge der Woerter in der Zeile (siehe fahrplan_spalte_fuer).

    Rueckgabe: Liste von Dicts mit kw, datum (DD.MM.YYYY), zeit (HH:MM),
    schiff, route, direkt (bool), vorlaeufig (bool, aus Klammer-Notation).
    """
    ergebnisse = []
    for page in pdf.pages:
        zeilen = fahrplan_rows_from_words(page.extract_words())
        kw = None
        datum = None
        spalten_x = None
        for zeile in zeilen:
            texte = [w["text"] for w in zeile]
            if texte[:1] == ["Dienstplan"]:
                kw = texte[2] if len(texte) > 2 else None
                # Wochentag+Datum stehen mal als ein zusammenhaengender
                # Token ("Freitag31.07.2026"), mal auf mehrere Woerter
                # verteilt - deshalb Suche im zusammengefuegten Rest der
                # Zeile statt Annahme einer festen Tokenposition.
                rest = "".join(texte[3:])
                match = FAHRPLAN_WOCHENTAG_DATUM_RE.search(rest)
                datum = match.group(2) if match else None
                spalten_x = None
                continue
            # "Wittd" statt "Wittdün", um unempfindlich gegen abweichende
            # Umlaut-Kodierung zu sein.
            if texte and "Wittd" in texte[0] and len(texte) >= 4:
                spalten_x = [w["x0"] for w in zeile[:4]]
                continue
            if texte and (texte[0] == "HW" or texte[0].startswith(("HW", "NW"))):
                continue
            if not (spalten_x and kw and datum):
                continue

            k = 0
            while k < len(zeile):
                wort = zeile[k]
                vorlaeufig = False
                if wort["text"] == "(":
                    if k + 1 >= len(zeile):
                        break
                    zeit_text = zeile[k + 1]["text"]
                    zeit_x0 = wort["x0"]
                    k += 3  # "(" Uhrzeit ")"
                    vorlaeufig = True
                elif FAHRPLAN_ZEIT_RE.match(wort["text"]):
                    zeit_text = wort["text"]
                    zeit_x0 = wort["x0"]
                    k += 1
                else:
                    k += 1
                    continue
                direkt = False
                if k < len(zeile) and zeile[k]["text"] == "dir":
                    direkt = True
                    k += 1
                if k < len(zeile) and zeile[k]["text"] in FAHRPLAN_SCHIFF_KUERZEL:
                    schiff = FAHRPLAN_SCHIFF_KUERZEL[zeile[k]["text"]]
                    spalte = fahrplan_spalte_fuer(zeit_x0, spalten_x)
                    ergebnisse.append({
                        "kw": kw,
                        "datum": datum,
                        "zeit": zeit_text,
                        "schiff": schiff,
                        "route": FAHRPLAN_ROUTEN[spalte],
                        "direkt": direkt,
                        "vorlaeufig": vorlaeufig,
                    })
                    k += 1
    return ergebnisse


def gruppiere_abfahrten_pro_tag(abfahrten, schiff):
    """Abfahrten des eigenen Schiffs als reiner Text (chronologisch),
    gruppiert nach Kalendertag (Schluessel "DD.MM.YYYY") - fuer je einen
    Tages-Termin statt einem Wochen-Termin: ein Termin, der die ganze
    Woche umspannt, zeigt in Google Calendar an jedem Tag dieselbe
    (komplette) Beschreibung an, das ist hier explizit nicht gewuenscht.
    Bewusst keine eigenen Kalendertermine je Abfahrt (auf Nutzerwunsch:
    nur eine Anmerkung, keine zusaetzlichen, farbigen Termine)."""
    eigene = [a for a in abfahrten if norm(a["schiff"]) == norm(schiff)]

    def sortierschluessel(a):
        stunde, minute = a["zeit"].split(":")
        return (int(stunde), int(minute))

    pro_tag = {}
    for a in eigene:
        pro_tag.setdefault(a["datum"], []).append(a)

    ergebnis = {}
    for tag_str, eintraege in pro_tag.items():
        eintraege.sort(key=sortierschluessel)
        zeilen = []
        for a in eintraege:
            zusaetze = []
            if a["direkt"]:
                zusaetze.append("direkt")
            if a["vorlaeufig"]:
                zusaetze.append("vorl.")
            zusatz_text = f" ({', '.join(zusaetze)})" if zusaetze else ""
            zeilen.append(f"{a['zeit']} {a['route']}{zusatz_text}")
        ergebnis[tag_str] = "\n".join(zeilen)
    return ergebnis


def parse_date_range(pdf):
    text = pdf.pages[0].extract_text() or ""
    m = re.search(
        r"vom\s+(\d{2}\.\d{2}\.\d{4})\s+bis\s+(\d{2}\.\d{2}\.\d{4})", text
    )
    if not m:
        return None, None
    d_from = date(*reversed([int(p) for p in m.group(1).split(".")]))
    d_to = date(*reversed([int(p) for p in m.group(2).split(".")]))
    return d_from, d_to


def column_pairs(table):
    """
    (x0, x1) je Spaltenpaar (Rang + Name), abgeleitet aus der Zeile mit den
    meisten Zellen. Kopfzeilen taugen dafuer nicht: In den Besatzungslisten
    reicht die erkannte Kopfzeile teils nicht ueber die volle Tabellenbreite.
    """
    widest = max(table.rows, key=lambda r: sum(1 for c in r.cells if c))
    cells = widest.cells
    pairs = {}
    for i in range(0, len(cells), 2):
        left = cells[i]
        right = cells[i + 1] if i + 1 < len(cells) else None
        if left and right:
            pairs[i] = (left[0], right[2])
        elif left:
            pairs[i] = (left[0], left[2])
    return pairs


def headers_in_band(page, table, top, bottom, pairs):
    """
    Ueberschriften einer Kopfzeile ueber die x-Position der Woerter zuordnen
    statt ueber die Zellen. Die letzte Spalte einer Besatzungsliste liegt
    haeufig ausserhalb der von pdfplumber erkannten Kopfzeile; ihr Text ginge
    sonst verloren und die Woche endete als "UNBEKANNT".
    """
    x0, x1 = table.bbox[0], table.bbox[2]
    band = page.crop((x0, max(top - 1, 0), x1, min(bottom + 1, page.height)))
    found = {}
    for word in sorted(band.extract_words(), key=lambda w: w["x0"]):
        center = (word["x0"] + word["x1"]) / 2
        for index, (left, right) in pairs.items():
            if left <= center <= right:
                found[index] = (found.get(index, "") + " " + word["text"]).strip()
                break
    return found


def farbe_fuer_namen(words, rects, ship_hue, xrange, row_bbox, fragment):
    """Schiff, dessen Kopf-Farbton zur Fuellfarbe der Namenszelle passt, oder
    None. `ship_hue` ist eine {schiffsname: hue}-Zuordnung fuer die gesamte
    Seite - die Faerbung eines Namens kann sich auf ein Schiff in einem
    anderen Tabellenblock derselben Seite beziehen (z.B. Urlaubsspalte neben
    Besatzungsliste)."""
    if not xrange or not ship_hue:
        return None
    x0, x1 = xrange
    row_top, row_bottom = row_bbox[1], row_bbox[3]
    nachname = fragment.split(",")[0].strip().lower()
    own_hue = None
    for word in words:
        if (
            nachname in word["text"].lower()
            and x0 <= word["x0"] <= x1
            and row_top - 1 <= word["top"] <= row_bottom + 1
        ):
            cx = (word["x0"] + word["x1"]) / 2
            cy = (word["top"] + word["bottom"]) / 2
            own_hue = cell_hue(color_at(rects, cx, cy))
            break
    if own_hue is None:
        return None
    for schiff, hue in ship_hue.items():
        if hues_close(own_hue, hue):
            return schiff
    return None


def find_status_for_name(pdf, target_name_fragment):
    hits = []
    for page in pdf.pages:
        rects = fill_rects(page)
        words = page.extract_words()
        ship_hue = {}
        row_bands = []
        for table in page.find_tables():
            rows = table.extract()
            pairs = column_pairs(table)
            headers = {}
            for row, meta in zip(rows, table.rows):
                ncols = len(row)
                is_header = True
                any_text = False
                for i in range(0, ncols, 2):
                    even = (row[i] or "").strip()
                    odd = row[i + 1].strip() if i + 1 < ncols and row[i + 1] else ""
                    if even:
                        any_text = True
                        if odd or even.upper() in KNOWN_RANKS:
                            is_header = False
                if not any_text:
                    continue
                if is_header:
                    # Jeder Block bringt eigene Ueberschriften mit; die alten
                    # duerfen nicht stehen bleiben.
                    headers = headers_in_band(
                        page, table, meta.bbox[1], meta.bbox[3], pairs
                    )
                    for idx, header_text in headers.items():
                        if idx not in pairs or not ist_schiff(header_text):
                            continue
                        x0, x1 = pairs[idx]
                        cx = (x0 + x1) / 2
                        cy = (meta.bbox[1] + meta.bbox[3]) / 2
                        hue = cell_hue(color_at(rects, cx, cy))
                        if hue is not None:
                            ship_hue[SCHIFFE_NORM[norm(header_text)]] = hue
                    continue
                row_bands.append((row, meta, pairs, headers))

        for row, meta, pairs, headers in row_bands:
            ncols = len(row)
            for i in range(0, ncols, 2):
                name_cell = row[i + 1].strip() if i + 1 < ncols and row[i + 1] else ""
                if target_name_fragment.lower() in name_cell.lower():
                    rank_cell = (row[i] or "").strip()
                    category = headers.get(i, "UNBEKANNT")
                    links = headers.get(i - 2) if i >= 2 else None
                    farbe = farbe_fuer_namen(
                        words, rects, ship_hue, pairs.get(i), meta.bbox,
                        target_name_fragment,
                    )
                    hits.append((category, rank_cell, name_cell, links, farbe))
    return hits


def ics_escape(text):
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("\n", "\\n")
    )


def ics_stamp(mtime):
    """'2026-08-26 05:23' -> '20260826T052300Z'."""
    try:
        return datetime.strptime(mtime, "%Y-%m-%d %H:%M").strftime(
            "%Y%m%dT%H%M%SZ"
        )
    except (ValueError, TypeError):
        return FALLBACK_STAMP


def build_vevent(iso_year, iso_week, entry):
    d_from = date.fromisoformat(entry["date_from"])
    d_to = date.fromisoformat(entry["date_to"])
    dtstart = d_from.strftime("%Y%m%d")
    dtend = (d_to + timedelta(days=1)).strftime("%Y%m%d")
    uid = f"dienstplan-{iso_year}-W{iso_week:02d}@wdr-besatzungsliste"
    stamp = ics_stamp(entry.get("mtime"))
    stand = entry.get("mtime") or "unbekannt"
    beschreibung = (
        f"KW {iso_week}/{iso_year}\\, Stand: {ics_escape(stand)}\\, "
        f"Datei: {ics_escape(entry.get('file', '?'))}"
    )
    return (
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{stamp}\r\n"
        f"LAST-MODIFIED:{stamp}\r\n"
        f"SEQUENCE:{entry.get('sequence', 0)}\r\n"
        f"DTSTART;VALUE=DATE:{dtstart}\r\n"
        f"DTEND;VALUE=DATE:{dtend}\r\n"
        f"SUMMARY:{ics_escape(entry['summary'])}\r\n"
        f"DESCRIPTION:{beschreibung}\r\n"
        "END:VEVENT\r\n"
    )


def build_tages_vevents(iso_year, iso_week, entry):
    """Ein Termin je Kalendertag statt einem Termin fuer die ganze Woche -
    nur wenn Abfahrten-Daten vorliegen (entry['abfahrten_pro_tag']). Ein
    wochenumspannender Termin zeigt in Google Calendar an jedem Tag
    dieselbe (komplette) Beschreibung; mit Tages-Terminen sieht man an
    jedem Tag nur dessen eigene Abfahrten.

    date_to ist der letzte Tag an Bord (der Freitag der Ablösung) und
    zaehlt mit dazu - ebenso wie beim bisherigen Wochen-Termin, dessen
    DTEND bewusst auf date_to + 1 Tag gesetzt war, um genau diesen Tag als
    letzten Kalendertag noch einzuschliessen."""
    d_from = date.fromisoformat(entry["date_from"])
    d_to = date.fromisoformat(entry["date_to"])
    stamp = ics_stamp(entry.get("mtime"))
    stand = entry.get("mtime") or "unbekannt"
    pro_tag = entry.get("abfahrten_pro_tag") or {}
    events = []
    tag = d_from
    while tag <= d_to:
        uid = (
            f"dienstplan-{iso_year}-W{iso_week:02d}-{tag.strftime('%Y%m%d')}"
            "@wdr-besatzungsliste"
        )
        beschreibung = (
            f"KW {iso_week}/{iso_year}\\, Stand: {ics_escape(stand)}\\, "
            f"Datei: {ics_escape(entry.get('file', '?'))}"
        )
        tages_abfahrten = pro_tag.get(tag.strftime("%d.%m.%Y"))
        if tages_abfahrten:
            beschreibung += "\\n\\nAbfahrten:\\n" + ics_escape(tages_abfahrten)
        events.append(
            "BEGIN:VEVENT\r\n"
            f"UID:{uid}\r\n"
            f"DTSTAMP:{stamp}\r\n"
            f"LAST-MODIFIED:{stamp}\r\n"
            f"SEQUENCE:{entry.get('sequence', 0)}\r\n"
            f"DTSTART;VALUE=DATE:{tag.strftime('%Y%m%d')}\r\n"
            f"DTEND;VALUE=DATE:{(tag + timedelta(days=1)).strftime('%Y%m%d')}\r\n"
            f"SUMMARY:{ics_escape(entry['summary'])}\r\n"
            f"DESCRIPTION:{beschreibung}\r\n"
            "END:VEVENT\r\n"
        )
        tag += timedelta(days=1)
    return events


def next_week_key(key):
    """'2026-W39' -> '2026-W40', ueber den Jahreswechsel hinweg."""
    year, week = key.split("-W")
    try:
        monday = date.fromisocalendar(int(year), int(week), 1) + timedelta(days=7)
    except ValueError:
        return None
    y, w, _ = monday.isocalendar()
    return f"{y}-W{w:02d}"


def build_prognose_vevent(key, entry, schiff, grund):
    d_from = date.fromisoformat(entry["date_to"])
    d_to = d_from + timedelta(days=7)
    folge = next_week_key(key)
    uid = f"prognose-{folge}@wdr-besatzungsliste"
    stamp = ics_stamp(entry.get("mtime"))
    quelle = key.replace("-W", "/KW ")
    if grund == "farbe":
        hinweis = (
            f"Eigener Name in {ics_escape(quelle)} farblich wie "
            f"{ics_escape(schiff)} hinterlegt."
        )
    else:
        hinweis = (
            f"In {ics_escape(quelle)} stand {ics_escape(schiff)} links "
            "neben der eigenen Spalte."
        )
    return (
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{stamp}\r\n"
        f"LAST-MODIFIED:{stamp}\r\n"
        f"SEQUENCE:{entry.get('sequence', 0)}\r\n"
        f"DTSTART;VALUE=DATE:{d_from.strftime('%Y%m%d')}\r\n"
        f"DTEND;VALUE=DATE:{(d_to + timedelta(days=1)).strftime('%Y%m%d')}\r\n"
        f"SUMMARY:{ics_escape('Voraussichtlich Dienst auf ' + schiff)}\r\n"
        "STATUS:TENTATIVE\r\n"
        "TRANSP:OPAQUE\r\n"
        f"DESCRIPTION:Unbestaetigte Vermutung. {hinweis}\r\n"
        "END:VEVENT\r\n"
    )


def build_prognose_frei_vevent(key, entry):
    d_from = date.fromisoformat(entry["date_to"])
    d_to = d_from + timedelta(days=7)
    folge = next_week_key(key)
    uid = f"prognose-frei-{folge}@wdr-besatzungsliste"
    stamp = ics_stamp(entry.get("mtime"))
    quelle = key.replace("-W", "/KW ")
    kategorie = entry.get("category", "") or "?"
    return (
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{stamp}\r\n"
        f"LAST-MODIFIED:{stamp}\r\n"
        f"SEQUENCE:{entry.get('sequence', 0)}\r\n"
        f"DTSTART;VALUE=DATE:{d_from.strftime('%Y%m%d')}\r\n"
        f"DTEND;VALUE=DATE:{(d_to + timedelta(days=1)).strftime('%Y%m%d')}\r\n"
        f"SUMMARY:{ics_escape('Voraussichtlich frei')}\r\n"
        "STATUS:TENTATIVE\r\n"
        "TRANSP:TRANSPARENT\r\n"
        f"DESCRIPTION:Unbestaetigte Vermutung. In {ics_escape(quelle)} Dienst "
        f"auf {ics_escape(kategorie)} ohne Farbmarkierung des eigenen Namens "
        "- voraussichtlich abgeloest.\r\n"
        "END:VEVENT\r\n"
    )


def build_prognosen(state):
    events = []
    for key, entry in sorted(state.items()):
        if not entry.get("date_to"):
            continue
        folge = next_week_key(key)
        if not folge:
            continue
        folge_entry = state.get(folge, {})
        if folge_entry.get("date_from"):
            continue  # echte Liste vorhanden

        farbe = entry.get("farbe_schiff")
        if ist_schiff(farbe or ""):
            schiff = SCHIFFE_NORM[norm(farbe)]
            events.append(build_prognose_vevent(key, entry, schiff, "farbe"))
            continue

        kategorie = entry.get("category", "") or ""
        if ist_schiff(kategorie):
            # Dienst diese Woche, aber kein Farbhinweis auf Fortsetzung:
            # voraussichtlich Abloesung -> naechste Woche frei. Gilt nicht
            # fuer Az (Auszubildende bleiben fest einem Schiff zugeteilt und
            # rotieren nicht woechentlich).
            if (entry.get("rang") or "").upper() != "AZ":
                events.append(build_prognose_frei_vevent(key, entry))
            continue

        links = entry.get("nachbar_links")
        if not ist_schiff(links or ""):
            continue
        schiff = SCHIFFE_NORM[norm(links)]
        events.append(build_prognose_vevent(key, entry, schiff, "nachbar"))
    return events


def add_placeholder_weeks(state, weeks_to_check):
    for jahr, kw in weeks_to_check:
        key = f"{jahr}-W{kw:02d}"
        if key not in state:
            state[key] = {
                "category": None,
                "summary": None,
                "date_from": None,
                "date_to": None,
                "file": None,
                "mtime": None,
                "nachbar_links": None,
                "farbe_schiff": None,
                "rang": None,
                "sequence": 0,
            }
    return state


def wrap_calendar(vevents, name):
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Dienstplan Sync//DE\r\n"
        "CALSCALE:GREGORIAN\r\n"
        "METHOD:PUBLISH\r\n"
        f"X-WR-CALNAME:{ics_escape(name)}\r\n"
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H\r\n"
        "X-PUBLISHED-TTL:PT6H\r\n"
        + "".join(vevents)
        + "END:VCALENDAR\r\n"
    )


def category_to_summary(category):
    upper = category.upper()
    if "FREIE TAGE" in upper:
        return "Freie Tage"
    if "URLAUB" in upper:
        return "Urlaub"
    if "ABWESEND" in upper:
        return "Abwesend"
    if upper == "UNBEKANNT":
        return "Besatzungsliste: Spalte unklar (bitte PDF pruefen)"
    return f"Dienst auf {category}"


def is_dienst(summary):
    """Unklare Faelle bewusst zum Dienst zaehlen - dort fallen sie auf."""
    return summary.startswith("Dienst auf ") or summary.startswith("Besatzungsliste:")


def prune_state(state, weeks):
    """Entfernt Eintraege, deren Woche laenger als `weeks` zurueckliegt."""
    cutoff = date.today() - timedelta(weeks=weeks)
    for key in list(state):
        date_to = state[key].get("date_to")
        if date_to and date.fromisoformat(date_to) < cutoff:
            del state[key]
            print(f"{key}: aus dem Kalender entfernt (aelter als {weeks} Wochen)")


def main():
    username = os.environ.get("WDR_USER")
    password = os.environ.get("WDR_PASS")
    if not username or not password:
        sys.exit("Fehlt: Umgebungsvariablen WDR_USER / WDR_PASS (GitHub Secrets).")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    state = load_state()
    changed = False

    session = requests.Session()
    session.auth = (username, password)
    session.headers["User-Agent"] = "dienstplan-sync/2.0"

    index = fetch_index(session)
    print(f"Verzeichnislisting: {len(index)} Kalenderwochen gefunden\n")

    weeks_to_check = list(iso_weeks_to_check(WEEKS_BACK, WEEKS_AHEAD))
    for iso_year, iso_week in weeks_to_check:
        key = f"{iso_year}-W{iso_week:02d}"
        found = index.get((iso_year, iso_week))
        if not found:
            print(f"KW {iso_week}/{iso_year}: noch nicht verfuegbar")
            continue

        revision, href, filename, mtime = found
        prev = state.get(key)

        # Unveraendert? Dann kein Download.
        if prev and prev.get("file") == filename and prev.get("mtime") == mtime:
            print(f"KW {iso_week}/{iso_year}: unveraendert ({prev['summary']})")
            continue

        resp = session.get(f"{BASE_URL}/{href}", timeout=30)
        if resp.status_code != 200 or resp.content[:4] != b"%PDF":
            print(f"  Warnung: {filename} -> HTTP {resp.status_code}, uebersprungen")
            continue

        with pdfplumber.open(BytesIO(resp.content)) as pdf:
            d_from, d_to = parse_date_range(pdf)
            hits = find_status_for_name(pdf, TARGET_NAME)

        if not hits:
            print(
                f"KW {iso_week}/{iso_year} ({filename}): "
                f"'{TARGET_NAME}' nicht gefunden"
            )
            continue
        if len(hits) > 1:
            other = ", ".join(h[0] for h in hits[1:])
            print(f"  Hinweis: mehrere Treffer, weitere Kategorien: {other}")
        if not d_from or not d_to:
            print(
                f"  Warnung: KW {iso_week}/{iso_year} ({filename}) - "
                "Datumszeile im PDF nicht gefunden, kein Kalendereintrag"
            )

        category, rank, name_cell, links, farbe = hits[0]
        entry = {
            "date_from": d_from.isoformat() if d_from else None,
            "date_to": d_to.isoformat() if d_to else None,
            "category": category,
            "summary": category_to_summary(category),
            "file": filename,
            "mtime": mtime,
            "revision": revision,
            "nachbar_links": links,
            "farbe_schiff": farbe,
            "rang": rank,
            "sequence": (prev.get("sequence", 0) + 1) if prev else 0,
        }
        state[key] = entry
        changed = True
        rev_text = "Erstfassung" if revision == 0 else f"{revision}. Aenderung"
        print(
            f"KW {iso_week}/{iso_year}: {entry['summary']} "
            f"({rev_text}, Stand {mtime or 'unbekannt'})"
        )

    prune_state(state, PRUNE_WEEKS)

    abfahrten_wochen_text = 0
    dienst_wochen = [
        (int(key.split("-W")[0]), int(key.split("-W")[1]), entry)
        for key, entry in state.items()
        if ist_schiff(entry.get("category", "") or "")
        and (int(key.split("-W")[0]), int(key.split("-W")[1])) in weeks_to_check
    ]
    if dienst_wochen:
        fahrplan_index = fetch_fahrplan_index(session)
        print(f"\nFahrplan-Verzeichnislisting: {len(fahrplan_index)} Dateien gefunden")
        fahrplan_cache = {}
        for iso_year, iso_week, entry in dienst_wochen:
            gefunden = find_fahrplan_eintrag(fahrplan_index, iso_year, iso_week)
            if not gefunden:
                print(f"  KW {iso_week}/{iso_year}: kein Fahrplan gefunden")
                entry.pop("abfahrten_pro_tag", None)
                continue
            _, mtime, href, filename = gefunden
            if filename not in fahrplan_cache:
                resp = session.get(f"{FAHRPLAN_BASE_URL}/{href}", timeout=30)
                if resp.status_code != 200 or resp.content[:4] != b"%PDF":
                    print(
                        f"  Warnung: Fahrplan {filename} -> "
                        f"HTTP {resp.status_code}, uebersprungen"
                    )
                    continue
                with pdfplumber.open(BytesIO(resp.content)) as pdf:
                    fahrplan_cache[filename] = parse_fahrplan_pdf(pdf)
            abfahrten = fahrplan_cache[filename]
            pro_tag = gruppiere_abfahrten_pro_tag(abfahrten, entry["category"])
            entry["abfahrten_pro_tag"] = pro_tag
            anzahl = sum(text.count("\n") + 1 for text in pro_tag.values())
            if not abfahrten:
                print(
                    f"  Warnung: {filename} lieferte gar keine Abfahrten - "
                    "Seitenstruktur (Kopfzeile/Datum) vermutlich abweichend, "
                    "PDF-Aufbau pruefen"
                )
            elif not pro_tag:
                andere_schiffe = sorted({a["schiff"] for a in abfahrten})
                print(
                    f"  Warnung: {filename} hat Abfahrten, aber keine fuer "
                    f"'{entry['category']}' (gefunden: {', '.join(andere_schiffe)})"
                )
            else:
                abfahrten_wochen_text += 1
            print(
                f"  KW {iso_week}/{iso_year}: {anzahl} Abfahrten auf "
                f"{len(pro_tag)} Tage verteilt als Anmerkung "
                f"({entry['category']}, aus {filename})"
            )

    if ALTE_ABFAHRTEN_ICS_PATH.exists():
        ALTE_ABFAHRTEN_ICS_PATH.unlink()

    dienst_events, frei_events = [], []
    for key, entry in sorted(state.items()):
        if not entry.get("date_from") or not entry.get("date_to"):
            continue
        iso_year, iso_week = key.split("-W")
        if entry.get("abfahrten_pro_tag"):
            vevents = build_tages_vevents(int(iso_year), int(iso_week), entry)
        else:
            vevents = [build_vevent(int(iso_year), int(iso_week), entry)]
        target = dienst_events if is_dienst(entry["summary"]) else frei_events
        target.extend(vevents)

    DIENST_ICS_PATH.write_text(
        wrap_calendar(dienst_events, "Dienst"), encoding="utf-8", newline=""
    )
    FREI_ICS_PATH.write_text(
        wrap_calendar(frei_events, "Frei"), encoding="utf-8", newline=""
    )
    state = add_placeholder_weeks(state, weeks_to_check)
    prognose_events = build_prognosen(state)
    PROGNOSE_ICS_PATH.write_text(
        wrap_calendar(prognose_events, "Voraussichtlich"), encoding="utf-8", newline=""
    )
    save_state(state)

    print(
        f"\nDienst-Termine: {len(dienst_events)}, Frei-Termine: {len(frei_events)}, "
        f"Vermutungen: {len(prognose_events)}, "
        f"Wochen mit Abfahrten-Anmerkung: {abfahrten_wochen_text}"
    )
    print(f"changed={str(changed).lower()}")


if __name__ == "__main__":
    main()
