# Changelog

Alle wijzigingen aan Hermes Discord Flow. Datum, wat, waarom.

## 2026-10-01 — workflow-inbox

- **Wat:** kanaal WORKFLOW → #workflow-inbox met `workflow-inbox.py`: één bericht per inboxpunt ("Status: open"), afgehandeld = hetzelfde bericht doorgestreept met ✅; status in het bestand zelf, redactie vóór elk bericht; regel in het ochtendrapport. De vraagcontrole neemt alleen echte vragen aan de eigenaar mee; andere `needs_input`-blokkades meldt de bordbewaking als blokkade-voor-manager. Bordbewaking meldt kleurplaten met inloggegevens in een URL. Voorbeeldwrapper `docs/voorbeelden/hermes-testdb`.
- **Waarom:** workflowpunten zichtbaar en afvinkbaar in Discord; geen vragen meer die onterecht teruggaan naar de bouwer; testcommando's zonder overgenomen (weggelakte) wachtwoorden.

## 2026-10-01 — eerste versie

- **Wat:** de Discord-flow voor een Hermes-team als losse repo: vragen met knoppen en "Anders…" in een forum, #meldingen, #staging, #samenvatting (basis), en het ochtendrapport, de gezondheidscheck, releases, bordbewaking en opruiming (extra). Eén config (`config.example.yaml`) voor alle ID's, namen, teksten, tijden en drempels; `install.sh`; de Hermes-patches voor 0.21.4 (`v2026.9.21`); een schone-installatietest met een nep-Discord.
- **Waarom:** zodat anderen dezelfde flow op hun eigen server kunnen draaien, zonder persoonlijke gegevens of vaste waarden in de scripts.
