# Hermes-patches voor de Discord-vragenflow

Patches op [Hermes Agent](https://github.com/NousResearch/hermes-agent) tag **`v2026.9.21`** (Hermes 0.21.4).
Ze maken een vragenflow mogelijk waarin een kanban-agent een vraag aan de eigenaar stelt, die vraag als forumpost met
antwoordknoppen in Discord verschijnt, en het antwoord (knop of vrije tekst) als gewoon bericht terugkomt bij de agent.

Hermes valt onder de MIT-licentie (Copyright (c) 2025 Nous Research). Deze patches wijzigen die code en vallen onder
dezelfde licentie.

## Inhoud

| Map | Wanneer | Patches |
|---|---|---|
| `required/` | altijd: zonder deze vier werkt de vragenflow niet | 0001–0004 |
| `recommended/` | met `--met-aanbevolen` | leeg, zie hieronder |
| `optional/` | met `--met-optioneel` | 0001–0006, los van de vragenflow |

De volgorde is vast: eerst `required/`, dan `recommended/`, dan `optional/`, en binnen een map op nummer.
Elke patch heeft eigen tests (zie `TESTS.txt`).

## Toepassen

```bash
./apply.sh <pad-naar-hermes-checkout> --dry-run                  # proefrun in een tijdelijke worktree
./apply.sh <pad-naar-hermes-checkout>                            # alleen required/
./apply.sh <pad-naar-hermes-checkout> --met-aanbevolen --met-optioneel
```

`apply.sh` controleert dat de checkout schoon is en dat HEAD tag `v2026.9.21` is (anders stopt het; met `--force`
gaat het toch door). Daarna past het de patches toe met `git am --3way`. Gaat een patch mis, dan doet het
`git am --abort` en toont het wat je terug moet sturen (patchnaam, HEAD, git-versie, conflicterende bestanden en de
uitvoer van `git am`).

Handmatig kan ook: `git am --3way required/*.patch` (en daarna eventueel `optional/*.patch`).

## Hoe de vragenflow werkt

1. Een kanban-worker heeft een beslissing nodig en blokkeert zijn kaart met een reden die begint met
   **`Vraag voor <wie>:`**, bijvoorbeeld `Vraag voor de eigenaar: variant A of B?` (`kind="needs_input"`).
2. Een **vragenposter** (een eigen script of cronjob; dit hoort niet bij deze patches) leest de geblokkeerde kaarten,
   zet per vraag een forumpost in Discord met het label `open`, en hangt er knoppen aan via de Discord-REST-API:
   - `custom_id` **`tq:<kaart-id>:<episode>:<n>`** voor optie *n* (het knoplabel is de tekst van de optie);
   - `custom_id` **`tq:<kaart-id>:<episode>:x`** voor "✏️ Anders…" (vrije tekst).

   `<episode>` is het `id` van het meest recente event `blocked` van die kaart in `task_events`: zo hoort elke knop
   bij precies één vraag.
3. De eigenaar klikt. De Discord-adapter (patch required/0003) controleert wie er klikt, of de kaart nog op precies
   deze vraag wacht, bevestigt de klik, en stuurt het antwoord als bericht in die post naar de agent:
   `Antwoord van de eigenaar (knop): optie 2 — Variant B` of `Antwoord van de eigenaar (anders): <tekst>`.
   De knoppen verdwijnen (`✅ Gekozen: …`) en de post krijgt het label `beantwoord`.
4. De agent die de post bedient (bijvoorbeeld een manager-profiel) verwerkt het antwoord: hij deblokkeert de kaart
   (`kanban_unblock`), of zet, als de kaart nog op iets anders wacht, de reden om naar een wachtstand
   `Wacht op …` (patch required/0001). Een latere klik op dezelfde vraag wordt dan geweigerd.

## required/

### 0001 — kanban: wachtstand (`Wacht op …`) als reden van een geblokkeerde kaart

- **Wat:** `block_task()` op een kaart die al geblokkeerd is, accepteert een reden die begint met `Wacht op`
  (hoofdletterongevoelig) als pure statusupdate. Status, `block_kind` en de lusteller blijven gelijk; het event
  `blocked` krijgt `"status_update": true`, en de samenvatting van de laatste run (de bloktekst op het bord) krijgt
  de nieuwe reden. Elke andere reden op een geblokkeerde kaart blijft geweigerd.
- **Waarom nodig:** na een antwoord mag de vraag er op het bord niet meer open uitzien, en de knoppen-handler
  (0003) herkent aan `status_update` dat de vraag al beantwoord is. De lusdetectie (0002) gebruikt deze patch ook.
- **Config:** geen.
- **Tests:** `tests/hermes_cli/test_kanban_wait_status.py`.

### 0002 — kanban: lusdetectie per oorzaak; een nieuwe eigenaarsvraag is geen lus

- **Wat:**
  - Een reden die begint met `Vraag voor <wie>:` (regex `^vraag voor [^:\n]{1,40}:`, hoofdletterongevoelig) is een
    vraag aan de eigenaar (`kanban_db.is_owner_question()`). Een **nieuwe** vraag zet de lusteller terug; dezelfde
    vraag opnieuw stellen blijft een lus.
  - De lusdetectie vergelijkt de **oorzaak**: de tekst vóór `:` (tot 40 tekens, bijvoorbeeld `worker preflight`),
    anders de volledige reden. Bij eigenaarsvragen en wachtstanden is de volledige reden de oorzaak.
    Wachtstand-updates tellen niet mee.
  - Vangnet `BLOCK_ANY_CAUSE_LIMIT = 4`: vier echte blokkades sinds de laatste `specified` (zonder eigenaarsvragen
    en wachtstand-updates) gaan naar triage, ongeacht de oorzaak.
- **Waarom nodig:** upstream zet een kaart bij de tweede blokkade van hetzelfde soort in triage. Een kaart die na een
  eerdere blokkade een echte vraag stelt, verdween zo naar triage, waar de vragenposter en `kanban_unblock` haar
  niet meer bereiken.
- **Vereist:** 0001.
- **Config:** geen.
- **Tests:** `tests/hermes_cli/test_kanban_owner_question_block.py`, `tests/hermes_cli/test_kanban_block_cause.py`;
  upstream `tests/hermes_cli/test_kanban_block_kinds.py` blijft groen.

### 0003 — discord: antwoordknoppen op vragen van de eigenaar in een forumpost

- **Wat:** de adapter handelt in `on_interaction` de knoppen `tq:…` en het invulveld `tqm:<kaart>:<episode>` af op
  `custom_id` (er is geen `discord.ui.View` nodig, dus het werkt ook na een herstart van de gateway).
  - Alleen `decision_owner_id` mag antwoorden; anderen krijgen `Alleen {label} kan deze vraag beantwoorden.`
  - Eerst bevestigen (defer), dan het antwoord naar de agent, dan `✅ Ontvangen`, en pas daarna knoppen weg en
    label `beantwoord` (de labels `open`/`beantwoord`/`verwerkt` worden vervangen, andere labels blijven).
    Lukt het doorgeven niet: `⚠️ Lukt niet, typ je antwoord in de post.` en de knoppen blijven staan.
  - "Anders…" opent een invulveld (max. 1500 tekens) met `custom_id` `tqm:<kaart>:<episode>`.
  - Vóór het accepteren kijkt de handler (alleen-lezen) op het kanbanbord of de kaart nog op precies deze vraag
    wacht. Zo niet (beantwoord in de post, via een andere client of de CLI, vervangen door een nieuwere vraag, of
    een wachtstand): `Deze vraag is al beantwoord of vervangen door een nieuwere vraag…`, en er gaat niets naar de
    agent. Is het bord onleesbaar, dan wordt het antwoord geaccepteerd.
  - Het bijgewerkte vraagbericht blijft onder de 1900 tekens (Discord-limiet 2000).
- **Waarom nodig:** dit is de vragenflow zelf.
- **Vereist:** 0001 (`status_update`).
- **Config** (onder `platforms.discord`; onbekende sleutels komen in `extra`, een expliciet `extra:`-blok mag ook):

  | Sleutel | Standaard | Betekenis |
  |---|---|---|
  | `decision_owner_id` | geen (dan mag niemand antwoorden) | Discord-user-id van de eigenaar; meerdere kommagescheiden |
  | `question_owner_label` | `de eigenaar` | naam in de teksten: "Antwoord van {label} (knop): …" |
  | `question_tags` | `{open: open, beantwoord: beantwoord, verwerkt: verwerkt}` | namen van de forumlabels |
  | `question_board` | het huidige bord | kanbanbord voor de controle "wacht de kaart nog op deze vraag?" |
  | `question_texts` | Nederlandse standaardteksten | per sleutel een eigen tekst, zie hieronder |

  Sleutels van `question_texts` (met hun plaatshouders): `not_owner` ({label}), `answer_button` ({label}, {choice},
  {option}), `answer_other` ({label}, {text}), `already_answered`, `already_answered_modal`, `received`, `failed`,
  `empty`, `chosen` ({option}), `other_option` ({text}), `modal_title`, `modal_label` (die twee maximaal 45 tekens).
  Een tekst met een onbekende plaatshouder valt terug op de standaardtekst.
- **Tests:** `tests/gateway/test_discord_question_buttons.py` (24 tests met nep-interacties, zonder netwerk en zonder
  async-pytest-plugin): klik door de eigenaar, klik door een ander, "Anders…" en het invulveld, een klik op een
  vraag die al beantwoord, vervangen of in wachtstand is (met een echte tijdelijke kanban-database), maximaal 2000
  tekens, en de config-sleutels.

### 0004 — discord: 💾-melding van de self-improvement review optioneel uit Discord

- **Wat:** met `suppress_self_improvement_notice: true` stuurt `send()` meldingen als `💾 Self-improvement review: …`
  en `💾 Skill '…' updated.` niet naar Discord (geslaagde, onderdrukte verzending). Het leren zelf blijft aan; alle
  andere berichten gaan ongewijzigd.
- **Waarom nodig:** zonder deze patch kan zo'n melding tussen vraag en antwoord in de forumpost verschijnen.
- **Config:** `platforms.discord.suppress_self_improvement_notice: true` (standaard uit = upstream-gedrag).
- **Tests:** `tests/gateway/test_discord_self_improvement_notice.py`.

## recommended/

Leeg. De oorspronkelijke aanbevolen patch (goedkeuringskaarten passend maken binnen de Discord- en Telegram-limiet,
inclusief de regels met het request-ID) repareert alleen een fork-specifieke uitbreiding: goedkeuringen die aan een
request-ID gebonden zijn. Die uitbreiding zit niet in Hermes 0.21.4; daar zijn de goedkeuringskaarten al binnen de
limiet en is er niets aan te passen. `--met-aanbevolen` doet daarom niets.

## optional/

Deze patches staan los van de vragenflow en van elkaar (de volgorde is wel vast, omdat ze deels dezelfde bestanden
raken).

### 0001 — kanban: verouderde taakbranch eerst bijwerken met staging vóór coder- of reviewstart

- **Wat:** vlak voordat een worker start (ready- en reviewbaan) merget de dispatcher de HEAD van de projectcheckout
  (staging) in een schone taakbranch die die HEAD nog niet bevat. Conflict: merge afbreken en de kaart blokkeren met
  `beslissing manager: conflict in <bestanden> …`. Heeft staging nog niet-gepushte commits, dan wacht de start een
  tick. Een vuile worktree of een andere branch blijft ongemoeid; een dry run merget nooit.
- **Nut:** een worker begint altijd op de actuele staging, ook als intussen andere kaarten gemerged zijn.
- **Config:** geen.
- **Tests:** `tests/hermes_cli/test_kanban_worktree_refresh.py`.

### 0002 — kanban: een implementer reviewt nooit zijn eigen kaart

- **Wat:** vóór de start van een reviewworker gaat een kaart waarvan de assignee geen reviewprofiel is
  (`REVIEW_PROFILES = {reviewer, security, designer}`) naar `DEFAULT_REVIEW_PROFILE = "reviewer"`, als dat profiel
  bestaat. Gevraagde wijzigingen gaan nog steeds terug naar de implementer.
- **Nut:** een coder die review aanvraagt zonder `reviewer=` keurde anders zijn eigen werk goed.
- **Config:** geen; pas de twee constanten in `hermes_cli/kanban_db_dispatch.py` aan als je profielen anders heten.
- **Tests:** `tests/hermes_cli/test_kanban_review_self_guard.py`.

### 0003 — agent: instructie-vingerafdruk

- **Wat:** elk systeemprompt eindigt op `[Instructieversie <fp>]`, een hash van `SOUL.md`, de extra
  instructiebestanden en `config.yaml`. Een opgeslagen of gecachet prompt met een andere vingerafdruk wordt bij de
  volgende beurt opnieuw opgebouwd; het gesprek blijft staan.
- **Nut:** lange chatthreads en desktopsessies volgen gewijzigde instructies zonder nieuwe sessie.
- **Config** (opt-in, in de `config.yaml` van het profiel):

  ```yaml
  agent:
    instruction_fingerprint: true
    instruction_fingerprint_files: [team/TEAM.md]   # optioneel, relatief aan de Hermes-root
  ```

  Zonder `instruction_fingerprint` is het gedrag gelijk aan upstream.
- **Tests:** `tests/agent/test_instruction_fingerprint.py`.

### 0004 — kanban: branchnaam van een projectkaart is altijd een geldige git-ref

- **Wat:** de titelslug in `branch_name_for()` bevat geen `.` meer, en een naam die `git check-ref-format --branch`
  zou weigeren, valt terug op `<project>/<kaart>`.
- **Nut:** een titel als "… ca. 14 dagen" gaf na afkappen een branch op `.`, waardoor de worker zijn worktree niet kon
  maken.
- **Config:** geen.
- **Tests:** `tests/hermes_cli/test_kanban_branch_ref.py`.

### 0005 — kanban: `specify --keep-body`

- **Wat:** `hermes kanban specify <id> --keep-body` zet een triagekaart zonder LLM terug naar todo, met titel en body
  ongewijzigd, en zet de lusteller op 0 (`specify_triage_task(..., reset_block_loop=True)`, event `specified` met
  `keep_body`).
- **Nut:** na een lusdetectie kan een beheerder of manager een kaart terugzetten nadat de oorzaak is opgelost.
- **Config:** geen.
- **Tests:** `tests/hermes_cli/test_kanban_specify.py` (5 nieuwe tests).

### 0006 — kanban: kaart van een worker erft het project, ook bij `workspace_kind='worktree'`

- **Wat:** `kanban_create` door een worker met `workspace_kind="worktree"` zonder pad erft het project van de eigen
  kaart. Een worktree-kaart zonder pad, project, bordproject of `default_workdir` van het bord wordt meteen
  geweigerd.
- **Nut:** zo'n kaart kreeg anders een worktree zonder repository en faalde pas bij de start van de worker.
- **Config:** geen.
- **Tests:** `tests/tools/test_kanban_create_inherit_project.py`.

## Voorbeeldconfig voor de vragenflow

```yaml
platforms:
  discord:
    decision_owner_id: "<discord-user-id van de eigenaar>"
    question_owner_label: "de eigenaar"
    question_tags: {open: open, beantwoord: beantwoord, verwerkt: verwerkt}
    question_board: default
    suppress_self_improvement_notice: true
```
