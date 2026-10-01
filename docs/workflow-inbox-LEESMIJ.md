# Workflow-inbox (manager → workflowkant)

Discord en het kanbanbord zijn alleen voor projecten. Ziet de manager een workflowprobleem of krijgt hij een workflowopdracht (scripts, config, SOUL/TEAM, cron, systemd, Discord-inrichting, Hermes zelf, server), dan maakt hij hier één bestand per punt. Geen kaart, geen melding.

- Naam: `JJJJMMDD-HHMM-<korte-slug>.md`, bijvoorbeeld `20261001-1830-nieuwe-timer.md`.
- Inhoud:
  ```
  # <korte titel>
  Wat: <wat moet er gebeuren of wat gaat er mis>
  Waarom: <gevolg voor het projectwerk>
  Wie vroeg het: <eigenaar / manager / kaart-id>
  Sinds: <datum en tijd>
  Niet vóór: <datum/tijd of voorwaarde, optioneel>
  Akkoord eigenaar: <het antwoord uit #vragen, letterlijk> (kaart <id>, <datum>)   ← alleen bij een serverstap
  ```
- Alleen toevoegen; bestaande bestanden niet wijzigen of weghalen.
- De velden `Wat:`, `Wie vroeg het:` en de titel (`# …`) worden gebruikt voor het bericht in Discord; houd ze kort en zonder geheimen (geen wachtwoorden, tokens of URL's met inloggegevens).

## In Discord: #workflow-inbox (Extra)

`workflow-inbox.py sync` (draait na elke gezondheidscheck) zet elk nieuw punt als één stil bericht in WORKFLOW → #workflow-inbox: titel, 2–3 zinnen uit `Wat:`, project (een slug uit `projecten` in de config, anders "workflow"), wie het meldde, datum, bestandsnaam en "Status: open". Bestanden die minder dan een minuut oud zijn, wachten tot de volgende run (de manager kan nog schrijven).

De status staat in het bestand zelf. Het script zet bovenaan een blok:
```
---
status: "open"
discord_message_id: "…"
discord_status: "open"
gepost_op: "2026-10-01T12:00+02:00"
---
```
Laat dat blok staan. Daardoor plaatst het script nooit iets dubbel, ook niet als het opnieuw draait.

**Afhandelen** doet wie de workflow beheert:
```
python3 ~/.hermes/scripts/workflow-inbox.py afhandelen <bestand> "<wat er is gedaan, met commit of release>"
```
Dat bewerkt hetzelfde bericht (titel en tekst doorgestreept, daaronder "✅ Afgehandeld <datum tijd>: <oplossing>", plus een ✅-reactie) en verplaatst het bestand naar `afgehandeld/`. Een bestand dat met de hand naar `afgehandeld/` is verplaatst, met een regel "Afgehandeld: <datum> — <wat>", wordt bij de volgende run ook doorgestreept geplaatst.

**Geheimen:** elk bericht gaat eerst door een redactie (inloggegevens in URL's, tokens, lange sleutels, alle waarden uit de `.env`-bestanden). Vindt die iets, dan bevat het bericht alleen de titel en de bestandsnaam.

Het ochtendrapport toont één regel: "Workflow-inbox: X open (oudste: <datum>)" of "Workflow-inbox: niets open".
