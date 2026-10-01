# Checklist: eerste echte installatie

Vink af in deze volgorde. Lukt een stap niet, stop dan en stuur terug wat onderaan staat.

## Vooraf
- [ ] Hermes draait op tag `v2026.9.21` (0.21.4): `git -C <hermes-checkout> describe --tags` geeft `v2026.9.21`.
- [ ] Python ≥ 3.11 met PyYAML: `python3 -c "import sys, yaml; print(sys.version)"`.
- [ ] `loginctl show-user $USER -p Linger` geeft `Linger=yes`.
- [ ] De gateway draait: `systemctl --user is-active hermes-gateway.service` geeft `active`.

## Discord-bot
- [ ] Bot aangemaakt, **Message Content Intent** aan.
- [ ] Bot uitgenodigd met scopes `bot` + `applications.commands` en rechten **`328833551472`** (of tijdelijk Administrator).
- [ ] `DISCORD_BOT_TOKEN` staat in `~/.hermes/.env` (niet in git, niet in een chat geplakt).
- [ ] Ontwikkelaarsmodus aan; server-ID en je eigen gebruikers-ID gekopieerd.

## Patches
- [ ] `./patches/apply.sh <hermes-checkout> --met-aanbevolen` eindigt zonder fout.
- [ ] De tests uit `patches/TESTS.txt` zijn groen.
- [ ] Hermes opnieuw geïnstalleerd vanuit die checkout en de gateway herstart.

## Installatie
- [ ] `./install.sh --dry-run` toont geen fouten.
- [ ] `./install.sh --basis --geen-cron` gedraaid.
- [ ] `~/.hermes/team/flow.yaml` ingevuld (guild_id, owner_id, eigenaar.naam, vragen.prefix, projecten) en `python3 ~/.hermes/scripts/flow_config.py check` zegt `config OK`.
- [ ] `python3 ~/.hermes/scripts/discord_setup.py --dry-run`, daarna zonder `--dry-run`: de categorieën en kanalen staan in Discord, #vragen is een forum met de labels open/beantwoord/verwerkt.
- [ ] Discord-deel van `~/.hermes/config.yaml` overgenomen uit `docs/hermes-config-discord.yaml` (ID's uit `~/.hermes/team/discord.json`), gateway herstart.
- [ ] `./install.sh` opnieuw (met of zonder `--basis`): `hermes cron list` toont "Vraag van het team", "Staging klaar", "Vastgelopen-check", "Discord-opruimcontrole" en "Dagelijkse samenvatting".
- [ ] Blok A en B uit `docs/manager-instructies.md` staan in TEAM.md en de SOUL van de manager.
- [ ] Minstens één projectbestand met `STATUS: ACTIEF`.

## Testen (README, "Testlijst")
- [ ] Testvraag met knoppen verschijnt binnen 2 minuten in #vragen, met ping, ⭐-knop bovenaan en "✏️ Anders…".
- [ ] Klik op de ⭐-knop: "✅ Gekozen", label **beantwoord**, de manager reageert in de post.
- [ ] Tweede testvraag: **Anders…** met eigen tekst komt bij de manager aan.
- [ ] `python3 ~/.hermes/scripts/discord_post.py meldingen "Testmelding"` geeft een ping in #meldingen.
- [ ] `python3 ~/.hermes/scripts/staging-announce.py --dry-run` geeft geen fout (een echte staging-regel volgt bij de eerste gemergde kaart).
- [ ] Extra's aan: `python3 ~/.hermes/scripts/hermes-ochtendrapport.py --dry-run` werkt, en **de volgende ochtend** staat het ochtendrapport in #ochtendrapport.

## Als iets niet lukt, stuur dit terug
1. Welke stap, en wat je zag (een schermafbeelding van Discord mag).
2. De uitvoer van het commando dat faalde (kopieer de laatste 30 regels).
3. `python3 ~/.hermes/scripts/flow_config.py check` en `python3 ~/.hermes/scripts/flow_config.py kanalen`.
4. Bij een vraag die niet verschijnt: `python3 ~/.hermes/scripts/team-questions.py --dry-run`.
5. Bij een knop die niet werkt: `journalctl --user -u hermes-gateway.service --since "-30 min" | grep -i -E "question|interaction|tq:"`.
6. `git -C <hermes-checkout> log --oneline v2026.9.21..HEAD` (welke patches er in zitten).

**Stuur nooit** je `.env`, `auth.json`, het bot-token of `flow.yaml` mee. Draai bij twijfel eerst `python3 ~/.hermes/scripts/scan_personal.py <bestand>` op wat je wilt sturen.
