#!/usr/bin/env python3
"""
Einmalige Auswertung: Wie zuverlaessig ist die Regel

    "Wer in einer Freie-Tage-Spalte rechts neben einem Schiff steht,
     faehrt in der Folgewoche auf diesem Schiff"?

Das Skript laedt alle im Verzeichnis vorhandenen Besatzungslisten, sucht in
jeder den eigenen Namen, merkt sich die Ueberschrift der eigenen Spalte und die
der Spalte links daneben, und vergleicht dann die daraus abgeleitete Vorhersage
mit dem, was in der Folgewoche tatsaechlich stand.

Aendert nichts. Schreibt keine Dateien. Nur Ausgabe im Protokoll.

Aufruf ueber Actions -> Regelpruefung -> Run workflow.
"""

import io
import os
import sys
from datetime import date

import pdfplumber
import requests

import dienstplan_cloud_sync as ds

# Schreibweise wie in der PDF. Vergleich erfolgt normalisiert, also ohne
# Ruecksicht auf Gross-/Kleinschreibung, Leerzeichen und Bindestriche.
SCHIFFE = [
    "NORDERAUE",
    "SCHLESWIG - HOLSTEIN",
    "UTHLANDE",
    "NORDFRIESLAND",
    "HILLIGENLEI",
]


def norm(text):
    return "".join(c for c in (text or "").upper() if c.isalnum())


SCHIFFE_NORM = {norm(s): s for s in SCHIFFE}


def ist_schiff(kategorie):
    return norm(kategorie) in SCHIFFE_NORM


def treffer_mit_nachbar(pdf, fragment):
    """
    Wie find_status_for_name, liefert aber zusaetzlich die Ueberschrift des
    Spaltenpaares links daneben.
    """
    ergebnisse = []
    for page in pdf.pages:
        for table in page.find_tables():
            rows = table.extract()
            pairs = ds.column_pairs(table)
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
                        if odd or even.upper() in ds.KNOWN_RANKS:
                            is_header = False
                if not any_text:
                    continue
                if is_header:
                    headers = ds.headers_in_band(
                        page, table, meta.bbox[1], meta.bbox[3], pairs
                    )
                    continue
                for i in range(0, ncols, 2):
                    name = row[i + 1].strip() if i + 1 < ncols and row[i + 1] else ""
                    if fragment.lower() in name.lower():
                        ergebnisse.append(
                            {
                                "kategorie": headers.get(i, "UNBEKANNT"),
                                "links": headers.get(i - 2) if i >= 2 else None,
                                "spalte": i,
                            }
                        )
    return ergebnisse


def main():
    user = os.environ.get("WDR_USER")
    pw = os.environ.get("WDR_PASS")
    name = os.environ.get("WDR_NAME_FRAGMENT", ds.TARGET_NAME)
    if not user or not pw:
        sys.exit("Fehlt: WDR_USER / WDR_PASS.")

    session = requests.Session()
    session.auth = (user, pw)
    session.headers["User-Agent"] = "dienstplan-analyse/1.0"

    index = ds.fetch_index(session)
    print(f"{len(index)} Kalenderwochen im Verzeichnis, Name: {name!r}\n")

    wochen = []
    for (jahr, kw), (rev, href, dateiname, mtime) in sorted(index.items()):
        resp = session.get(f"{ds.BASE_URL}/{href}", timeout=30)
        if resp.status_code != 200 or resp.content[:4] != b"%PDF":
            print(f"  uebersprungen: {dateiname} (HTTP {resp.status_code})")
            continue
        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            von, bis = ds.parse_date_range(pdf)
            treffer = treffer_mit_nachbar(pdf, name)
        if not treffer:
            print(f"  {dateiname}: Name nicht gefunden")
            continue
        if len(treffer) > 1:
            print(f"  {dateiname}: mehrere Treffer {treffer}")
        t = treffer[0]
        wochen.append(
            {
                "kw": kw,
                "jahr": jahr,
                "datei": dateiname,
                "von": von,
                "bis": bis,
                "kategorie": t["kategorie"],
                "links": t["links"],
                "spalte": t["spalte"],
            }
        )

    wochen.sort(key=lambda w: (w["von"] or date.min, w["kw"]))

    print("\n" + "=" * 78)
    print("Was in den Listen steht")
    print("=" * 78)
    print(f"{'KW':>4}  {'Zeitraum':<23} {'Sp':>2}  {'eigene Spalte':<22} links daneben")
    for w in wochen:
        zeitraum = (
            f"{w['von']:%d.%m.} - {w['bis']:%d.%m.%Y}" if w["von"] else "unbekannt"
        )
        print(
            f"{w['kw']:>4}  {zeitraum:<23} {w['spalte']:>2}  "
            f"{w['kategorie'][:22]:<22} {w['links'] or '-'}"
        )

    print("\n" + "=" * 78)
    print("Regelpruefung: Vorhersage aus der Vorwoche gegen die Wirklichkeit")
    print("=" * 78)

    richtig = falsch = 0
    for aktuell, folge in zip(wochen, wochen[1:]):
        if ist_schiff(aktuell["kategorie"]):
            continue  # eigene Woche ist bereits ein Einsatz, keine Vorhersage
        if not ist_schiff(aktuell["links"] or ""):
            continue  # links steht kein Schiff, Regel greift nicht
        vorhersage = SCHIFFE_NORM[norm(aktuell["links"])]
        tatsaechlich = folge["kategorie"]
        ok = norm(vorhersage) == norm(tatsaechlich)
        richtig += ok
        falsch += not ok
        print(
            f"KW {aktuell['kw']} ({aktuell['kategorie']}, links {vorhersage})"
            f"  ->  Vorhersage fuer KW {folge['kw']}: {vorhersage}"
            f"  |  tatsaechlich: {tatsaechlich}  |  {'TRIFFT' if ok else 'DANEBEN'}"
        )

    gesamt = richtig + falsch
    print("\n" + "-" * 78)
    if gesamt:
        print(f"Auswertbare Wochenuebergaenge: {gesamt}")
        print(f"Vorhersage richtig: {richtig}   daneben: {falsch}")
        print(f"Trefferquote: {richtig / gesamt * 100:.0f} %")
    else:
        print("Kein auswertbarer Uebergang gefunden.")
        print("Moegliche Gruende: zu wenige Wochen im Verzeichnis, oder in keiner")
        print("Woche stand links neben der eigenen Spalte ein Schiffsname.")

    unbekannt = [w for w in wochen if w["kategorie"] == "UNBEKANNT"]
    if unbekannt:
        print(f"\nAchtung: {len(unbekannt)} Woche(n) ohne erkannte Ueberschrift:")
        for w in unbekannt:
            print(f"  KW {w['kw']} ({w['datei']}), Spalte {w['spalte']}")

    fremde = sorted(
        {
            w["links"]
            for w in wochen
            if w["links"] and not ist_schiff(w["links"]) and norm(w["links"])
        }
    )
    if fremde:
        print("\nBegriffe links neben der eigenen Spalte, die kein Schiff sind:")
        for f in fremde:
            print(f"  {f!r}")
        print("Falls hier ein Schiffsname dabei ist, fehlt er in SCHIFFE.")


if __name__ == "__main__":
    main()
