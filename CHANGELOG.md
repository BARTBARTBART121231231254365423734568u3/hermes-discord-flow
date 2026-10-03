# Changelog

Alle wijzigingen aan Hermes Discord Flow. Datum, wat, waarom.

## 2026-10-03 — één bordbewaking

- **Wat:** `kanban-stuck-check.py` is opgegaan in `board-guard.py` (cronjob "Bordbewaking" elke 15 min, één wekker per project met alle punten, nu in de basis; de job "Vastgelopen-check" mag weg). Eén regel voor elke blokkade: een vraag aan de eigenaar telt niet mee; zonder voortgang na 30 min de manager wekken met een draaiboek, na 2 uur een vraag van de manager in #vragen (geen meldingen meer na 2 of 6 uur). Wachtkringen tussen kaarten worden vanzelf doorbroken (reviewer vóór security), een kaart die op een afgehandeld workflow-inboxpunt wacht gaat verder, en bij minder dan 3 startbare coderkaarten plant de manager de eerstvolgende 3. `drain-restart.py` herstart niet terwijl een managerrun of plankaart buiten een eigen scope loopt. Config: `drempels.blokkade_wek_min`, `blokkade_vraag_uur`, `coder_voorraad_min`, `opnieuw_wekken_uur` (vervangen `escalatie_uur`, `manager_escalatie_uur`, `beslissing_manager_wacht_min`, `vastgelopen_uur`); `tijden.vastgelopen` en de teksten `bordbewaking.*` en `vastgelopen.beslissing_manager` vervallen.
- **Waarom:** werk lag stil op een kring tussen reviewer en security, op al afgehandelde inboxpunten en op een lege codervoorraad; twee scripts deden half hetzelfde.

## 2026-10-01 — workflow-inbox

- **Wat:** kanaal WORKFLOW → #workflow-inbox met `workflow-inbox.py`: één bericht per inboxpunt ("Status: open"), afgehandeld = hetzelfde bericht doorgestreept met ✅; status in het bestand zelf, redactie vóór elk bericht; regel in het ochtendrapport. De vraagcontrole neemt alleen echte vragen aan de eigenaar mee; andere `needs_input`-blokkades meldt de bordbewaking als blokkade-voor-manager. Bordbewaking meldt kleurplaten met inloggegevens in een URL. Voorbeeldwrapper `docs/voorbeelden/hermes-testdb`.
- **Waarom:** workflowpunten zichtbaar en afvinkbaar in Discord; geen vragen meer die onterecht teruggaan naar de bouwer; testcommando's zonder overgenomen (weggelakte) wachtwoorden.

## 2026-10-01 — eerste versie

- **Wat:** de Discord-flow voor een Hermes-team als losse repo: vragen met knoppen en "Anders…" in een forum, #meldingen, #staging, #samenvatting (basis), en het ochtendrapport, de gezondheidscheck, releases, bordbewaking en opruiming (extra). Eén config (`config.example.yaml`) voor alle ID's, namen, teksten, tijden en drempels; `install.sh`; de Hermes-patches voor 0.21.4 (`v2026.9.21`); een schone-installatietest met een nep-Discord.
- **Waarom:** zodat anderen dezelfde flow op hun eigen server kunnen draaien, zonder persoonlijke gegevens of vaste waarden in de scripts.
