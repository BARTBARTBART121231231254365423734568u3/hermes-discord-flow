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

Wie de workflow beheert, leest deze map regelmatig (het ochtendrapport en `team-status.py` tonen de open punten) en verplaatst een afgehandeld punt naar `afgehandeld/` met een regel "Afgehandeld: <datum> — <wat>".
