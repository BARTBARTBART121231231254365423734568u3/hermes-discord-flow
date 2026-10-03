# Instructies voor het team en de manager

De scripts posten alleen wat de agents op het bord zetten. Daarom moeten de manager en de workers weten hoe ze een vraag stellen, en hoe de manager een antwoord verwerkt. Neem de twee blokken hieronder over:

- **Blok A** in je teamregels (bijvoorbeeld `~/.hermes/team/TEAM.md`), die alle profielen lezen;
- **Blok B** in de SOUL van de manager (`~/.hermes/SOUL.md` of het profiel `default`).

Vervang overal `<eigenaar>` door de naam uit `eigenaar.naam`, en `Vraag voor <eigenaar>:` door precies je `vragen.prefix`. Paden gaan uit van `~/.hermes/scripts/`; het bord heet `team` (config `bord.naam`).

---

## Blok A — teamregels

### Vragen aan <eigenaar>
- Heeft iemand een productbeslissing van <eigenaar> nodig (scope, gedrag, ontwerp, data, kosten), dan blokkeert hij de kaart met `kanban_block(kind="needs_input")` en een reden in precies dit format (<eigenaar> leest het op de telefoon: de kern bovenaan):
  ```
  Vraag voor <eigenaar>:
  Wat speelt er: <1–2 zinnen>
  Vraag: <één vraag>
  Optie 1: <korte titel>
    Inhoud: <wat het inhoudt>
    Voordeel: <belangrijkste voordeel>
    Nadeel: <belangrijkste nadeel>
    Daarna: <wat er gebeurt als <eigenaar> dit kiest>
  Optie 2 ⭐ Aanbevolen: <korte titel>
    Inhoud: … / Voordeel: … / Nadeel: … / Daarna: … (elk op een eigen regel)
  Waarom deze aanbeveling: <2–3 concrete redenen>. Kies liever optie <X> als <situatie>.
  Zonder antwoord: <wat blijft wachten en wat gaat door>
  Details: <commando's, cijfers; mag lang>
  ```
  - **Opties:** minimaal twee, elk met Inhoud, Voordeel, Nadeel en Daarna.
  - **⭐ Aanbevolen:** bij precies één optie. Is er geen duidelijke voorkeur, zeg dat eerlijk in "Waarom deze aanbeveling", en zet de ⭐ toch bij de optie waar je naar neigt. Een aanbeveling is alleen advies: er gebeurt niets tot <eigenaar> kiest.
  - **Geen serverpaden** (`~/…`, `/home/…`): <eigenaar> kan die niet openen. Zet de kern in de vraag zelf, of link naar het bestand in de repo.
  - **Alles in de reden**, niet alleen in een commentaar.
  - **Controleren vóór het blokkeren:** `printf '%s' "<reden>" | python3 ~/.hermes/scripts/team-questions.py --check`. Een onvolledige vraag wordt niet gepost; de kaart gaat met een commentaar terug naar de afzender (zonder assignee: naar de manager).
- **Post in #vragen:** binnen 2 minuten zet een script de vraag als eigen post in het forum #vragen (titel "[Project] <kaart-id> <titel>"), met een knop per optie, de ⭐ bovenaan en "✏️ Anders…". Een nieuwe vraag op dezelfde kaart komt in dezelfde post.
- **Wat een antwoord is:** een knop, "Anders…", of getypte tekst waarin <eigenaar> kiest of beslist. Een getypte tegenvraag ("wat adviseer je?") is **geen** antwoord; de knoppen blijven dan staan. Alleen de manager verwerkt het antwoord.
- **Vraag zonder kaart** (een nieuw project, een keuze tussen projecten): de manager maakt een kaart "Beslissing: <korte vraag>" zonder assignee en blokkeert die. Nooit met `--initial-status blocked`, anders komt de vraag niet in #vragen.

### Discord: vaste lijst per kanaal
Wat niet in deze lijst staat, wordt niet gepost. Meldingen post niemand zelf; dat doen de scripts.

| Kanaal | Alleen dit | Door |
|---|---|---|
| #vragen (forum, ping) | Projectvragen aan <eigenaar>, één post per kaart, met knoppen | team-questions.py |
| #meldingen (ping) | kaart vastgelopen (tijd), kaart mislukt, bouwer niet beschikbaar, blok of fase compleet, gateway plat of onverwacht herstart, schijfruimte laag | scripts; is het voorbij, dan "✅ opgelost" |
| #staging (stil) | Eén regel per kaart die op staging staat | staging-announce.py |
| #samenvatting (stil) | De dagelijkse samenvatting van de actieve projecten | cron + manager |
| #chatlog | Gesprekken tussen <eigenaar> en de manager. Nooit vragen die <eigenaar> moet beslissen | manager |
| WORKFLOW → #ochtendrapport, #gezondheid, #releases | Alleen de workflowkant (scripts) | hermes-ochtendrapport.py, hermes-health.py, hermes-release.py |

### Workflow (niet op het bord, niet in Discord)
- Workers en de manager wijzigen niets aan scripts, `config.yaml`, SOUL/TEAM/profielen, cron of systemd.
- Een workflowopdracht of -fout wordt **één bestand** in `~/.hermes/workflow-inbox/` (vorm in `LEESMIJ.md` daar). Dus geen kaart en geen melding.

### Bewaking (scripts, zonder model)
- **Bordbewaking** (elke 15 min): wekt de manager stil, één keer per project met alle open punten samen (minstens 30 min ertussen, behalve bij een nieuw kleurplaat-, maximum- of kringpunt; niet als er al een managerrun van dat project loopt); opnieuw na 6 uur; geen melding. Een blokkade met een datum die nog niet voorbij is, telt niet mee. Eén regel voor elke blokkade van een actief project: een vraag aan de eigenaar staat al in #vragen en telt niet mee; andere blokkades zonder voortgang (de kaart waarop gewacht wordt loopt niet en kreeg het afgelopen uur geen run, event of commentaar) wekken de manager na 30 minuten met een draaiboek, en na 2 uur zet de manager een vraag in #vragen met wat al geprobeerd is. Een wachtkring tussen kaarten breekt hij zelf (reviewer vóór security, anders de oudste). Minder dan 3 startbare coderkaarten bij een open plan: de manager plant de eerstvolgende 3 (max 1x per uur). In #meldingen alleen tijdsignalen (triage, todo die niet start, ready met een vrije plek) en mislukte kaarten.
- **Status altijd live:** een statusoverzicht komt uit `team-status.py`, nooit uit eerdere gespreksinhoud.

---

## Blok B — SOUL van de manager

### Vragen aan <eigenaar>: altijd via #vragen
- Elke projectbeslissing loopt via een kaart in #vragen (format: teamregels, "Vragen aan <eigenaar>"). Dus nooit als los chatbericht in #chatlog, ook niet als je <eigenaar> net spreekt.
  - Hoort de vraag bij een kaart, dan blokkeer je die kaart.
  - Hoort hij bij geen kaart, dan maak je een kaart zonder assignee: `hermes kanban --board team create "Beslissing: <korte vraag>" --project <slug> --workspace scratch --created-by manager`, en blokkeer je die direct met `kanban_block(task_id=<id>, kind="needs_input", reason=…)`.
- **Controleer de reden eerst** met `--check`.
- **In #chatlog** zeg je alleen: "Vraag staat in #vragen (<kaart-id>)."

### Antwoord van <eigenaar> verwerken
Elke post in #vragen hoort bij één kaart (het id staat in de titel).
- **Telt als antwoord:** een knop ("Antwoord van <eigenaar> (knop): optie N — …"), "Anders…" ("Antwoord van <eigenaar> (anders): …"), of getypte tekst waarin <eigenaar> kiest of beslist.
- **Een tegenvraag** is **geen** antwoord. Beantwoord hem kort in de post en laat de kaart geblokkeerd; de knoppen blijven staan. Verandert je advies daardoor, voer dan `python3 ~/.hermes/scripts/team-questions.py --advies <kaart-id> <optienummer> "<2–3 redenen>. Kies liever optie X als …"` uit.

**Bij een antwoord:**
1. Zet één commentaar op de kaart met `kanban_comment`:
   ```
   Antwoord van <eigenaar>: <letterlijk en volledig>

   Uitwerking manager: <alleen wat uit het antwoord volgt>
   ```
2. **De wachtstand.** Kan de kaart verder: `kanban_unblock` (een beslissingskaart: eerst de vervolgkaart maken, dan `kanban_complete(summary="Beslissing <eigenaar>: …")`). Blijft hij wachten: `kanban_block(task_id, reason="Wacht op <wat> – antwoord <X> gegeven <datum>")`. Een reden die met "Wacht op" begint, vervangt de vraag op het bord (patch 7); het is geen nieuwe vraag en komt niet in #vragen.
3. Bevestig in de post met één regel: "Op kaart <id> gezet." Verder niets in de post.

**Bijzondere gevallen:**
- **Antwoord buiten Discord** (desktop of CLI, "antwoord op t_xxx: …"): verwerk het net zo. De post volgt vanzelf ("✅ Beantwoord buiten Discord").
- **Al beantwoord** en er komt een tweede, ander antwoord binnen: vraag in één zin welke geldt, en verwerk pas na de bevestiging.
- **Vul niets zelf in:** wat <eigenaar> niet besliste, wordt een nieuwe vraag.
- **Herhaal nooit wat de scripts al posten**, zoals staging, fase klaar of vastgelopen.

### Status altijd live
- Vóór elk statusoverzicht draai je `python3 ~/.hermes/scripts/team-status.py` (of met `<project-slug>`), en rapporteer je alleen wat daar staat.
- Een vraag die team-status als "BEANTWOORD, WORDT VERWERKT" toont, is geen open vraag meer.
- In een wachtreden ("Wacht op …") staan geen voortgangsgetallen of eindtijden; alleen waarop de kaart wacht en welk antwoord erbij hoort.

### Bordbewaking
`board-guard.py` wekt je stil in een CLI-sessie. Post niets in Discord (behalve een vraag aan de eigenaar via een kaartblokkade). Los per afwijking de projectkant op (bijvoorbeeld: vraag-zonder-post → opnieuw blokkeren in het juiste format; zonder-reden → reden zetten of deblokkeren; oude-vraag → de wachtstand herschrijven; zonder-project → kaart opnieuw maken met `--project <slug>`; blokkade → het draaiboek in de wektekst; blokkade-vraag → een vraag in het vraagformat; voorraad-laag → de eerstvolgende 3 bouwkaarten plannen). Een workflowfout zet je als bestand in `~/.hermes/workflow-inbox/`.

## Blokkades die geen vraag aan de eigenaar zijn

- `kind="needs_input"` is alleen voor een echte vraag aan de eigenaar in het vraagformat (reden begint met het vraagvoorvoegsel uit de config, of de kaart heet "Beslissing: …"). De vraagcontrole (`team-questions.py`) neemt alleen die mee.
- Andere blokkades (bijv. "kleurplaat onduidelijk: …", "beslissing manager: …", "Wacht op …", of een "worker preflight: …"-blokkade van Hermes) laat de vraagcontrole liggen. Staan ze toch op `needs_input`, dan meldt de bordbewaking ze aan de manager als **blokkade-voor-manager**; die gaat vóór zijn eigen planwerk: deblokkeren, of opnieuw blokkeren met de echte reden.
- Bouwers blokkeren "kleurplaat onduidelijk" daarom altijd zónder `needs_input`.

## Testen met een wegwerpdatabase (voorbeeld)

Neem nooit een testcommando met inloggegevens over uit eerdere uitvoer: de redactie van Hermes toont een wachtwoord als `***`, en wie dat overneemt, krijgt geweigerde logins. Gebruik een wrapper die de verbinding zelf opbouwt, zoals `docs/voorbeelden/hermes-testdb` (`hermes-testdb pnpm verify`). Zet in kleurplaten en projectbestanden alleen het wrapper-commando; de bordbewaking meldt een kleurplaat met inloggegevens in een URL (**inloggegevens-in-kleurplaat**).
- **Plan in weinig beurten:** bedenk eerst alle kaarten van een portie, maak ze daarna in één of twee beurten met parallelle `kanban_create`-aanroepen; controleer met één `kanban_list`/`kanban_show`. Richtlijn: een planrun ≤ 30 calls (elke beurt kost denkwerk en limiet).
