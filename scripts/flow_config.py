#!/usr/bin/env python3
"""One local config for every Discord-flow script (Hermes Discord Flow).

Where the config lives (first one that exists wins):
  1. $HERMES_FLOW_CONFIG (a path; used by tests and the clean-install check)
  2. $HERMES_HOME/team/flow.yaml
  3. ~/.hermes/team/flow.yaml
None found: the built-in DEFAULTS below are used (the same values as config.example.yaml), so every
script can run a --dry-run on a fresh install; but when team/discord.json exists (a configured install) a missing
config is an error (FLOW_STANDAARDWAARDEN=1 overrides). A config file only has to name what differs: it is
deep-merged over DEFAULTS (lists and scalars replace, mappings merge).

Values written as "<...>" (placeholders from config.example.yaml) count as empty.
The Discord IDs: team/discord.json (written by discord_setup.py) is the base; non-empty IDs in the config
(discord.guild_id, discord.owner_id, discord.kanalen.<key>.id) override it.

CLI (for install.sh and checks):
  flow_config.py pad                 → path of the config in use ("(standaard)" when none)
  flow_config.py get <a.b.c>         → one value (JSON for mappings/lists)
  flow_config.py check               → validates the config; exit 1 with what is wrong
  flow_config.py kanalen             → "<key> <id>" per known channel
  flow_config.py als-agent-prefix    → "" (vlag agent.gebruiker leeg) of het pad van als-agent
"""
import copy
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
SCRIPTS = Path(__file__).resolve().parent

# The defaults are the values of config.example.yaml (tests/test_flow_config.py keeps them equal).
DEFAULTS = {
    "versie": 1,
    "eigenaar": {"naam": "de eigenaar"},
    "tijdzone": "Europe/Amsterdam",
    "paden": {
        "hermes_bin": "~/.local/bin/hermes",
        "hermes_python": "python3",
        "projecten_map": "~/Hermes Workspace/projects",
        "archief_root": "~",
        "workflow_inbox": "workflow-inbox",
        "incidentenlog": "",
        "discord_ids": "team/discord.json",
        "workflow_modus": "team/workflow-modus.json",
        "workflow_waits": "team/workflow-waits.json",
        "overdracht_chatlog": "team/overdracht-chatlog.md",
        "discord_archief": "~/hermes-archief-discord",
    },
    "bord": {"naam": "team"},
    "rollen": {
        "manager": "default",
        "bouwer": "coder",
        "controleurs": ["reviewer", "security", "designer"],
        "max_per_rol": {"coder": 2},
        "max_totaal": 3,
    },
    "gateway": {"unit": "hermes-gateway.service"},
    # Linux-gebruiker van de agents (gateway, workers, Hermes-cron). Leeg = alles draait als de huidige gebruiker
    # (oude gedrag). Gezet: de ops-kant spreekt de gateway, Hermes, git in projectmappen en state.db aan via als-agent.
    "agent": {"gebruiker": ""},
    "discord": {
        "guild_id": "<GUILD_ID>",
        "owner_id": "<OWNER_ID>",
        "community": True,
        "categorieen": {
            "algemeen": "GENERAL",
            "team": "HERMES AGENTS",
            "workflow": "WORKFLOW",
            "systeem": "Systeem",
        },
        "kanalen": {
            "chatlog": {"naam": "chatlog", "categorie": "algemeen", "soort": "tekst", "id": "",
                        "alleen_bot": False, "onderwerp": "Gesprekken met de manager."},
            "vragen": {"naam": "vragen", "categorie": "team", "soort": "forum", "id": "", "alleen_bot": True,
                       "onderwerp": "Vragen van het team aan {eigenaar}: één post per vraag. Klik een optie of typ "
                                    "je antwoord."},
            "meldingen": {"naam": "meldingen", "categorie": "team", "soort": "tekst", "id": "", "alleen_bot": True,
                          "onderwerp": "Alleen projectwerk: vastgelopen of mislukte kaarten, beslissing manager, "
                                       "blok/fase compleet, en storingen die het projectwerk stilleggen."},
            "staging": {"naam": "staging", "categorie": "team", "soort": "tekst", "id": "", "alleen_bot": True,
                        "onderwerp": "Eén regel per kaart die op staging staat."},
            "samenvatting": {"naam": "samenvatting", "categorie": "team", "soort": "tekst", "id": "",
                             "alleen_bot": True, "onderwerp": "Dagelijkse samenvatting (08:00)."},
            "ochtendrapport": {"naam": "ochtendrapport", "categorie": "workflow", "soort": "tekst", "id": "",
                               "alleen_bot": True,
                               "onderwerp": "Ochtendrapport van de workflow (en maandag het weekoverzicht van de "
                                            "incidenten). Eén ping per dag."},
            "gezondheid": {"naam": "gezondheid", "categorie": "workflow", "soort": "tekst", "id": "",
                           "alleen_bot": True,
                           "onderwerp": "Gezondheidsmeldingen die het projectwerk niet stilleggen; worden "
                                        "'✅ opgelost'. Stil."},
            "releases": {"naam": "releases", "categorie": "workflow", "soort": "tekst", "id": "", "alleen_bot": True,
                         "onderwerp": "Eén regel per workflow-release (releasenummer en wat er veranderde). Stil."},
            "workflow-inbox": {"naam": "workflow-inbox", "categorie": "workflow", "soort": "tekst", "id": "",
                               "alleen_bot": True,
                               "onderwerp": "Workflow-inbox: één bericht per punt; doorgestreept = afgehandeld. Stil."},
            "regels": {"naam": "regels", "categorie": "systeem", "soort": "tekst", "id": "", "alleen_bot": False,
                       "onderwerp": "Serverregels (nodig voor Community)."},
            "moderator-updates": {"naam": "moderator-updates", "categorie": "systeem", "soort": "tekst", "id": "",
                                  "alleen_bot": False,
                                  "onderwerp": "Berichten van Discord aan beheerders (nodig voor Community)."},
        },
        "tags": {"open": "open", "beantwoord": "beantwoord", "verwerkt": "verwerkt"},
    },
    "vragen": {
        "prefix": "Vraag voor de eigenaar:",
        "statussen": ["ACTIEF", "GEPAUZEERD"],
    },
    "projecten": [
        {"slug": "voorbeeld-project", "naam": "Voorbeeldproject", "repo": "~/Hermes Workspace/projects/voorbeeld-project",
         "staging_url": "", "staging_branch": "staging", "health_pad": "/health", "verify": "", "db": None},
    ],
    "modus": {"standaard": "opstart"},
    "tijden": {
        "vragen": "*/2 * * * *",
        "staging": "*/2 * * * *",
        "opruimcontrole": "*/10 * * * *",
        "bordbewaking": "*/15 * * * *",
        "werkmappen_opruimen": "15 */4 * * *",
        "schijfruimte": "every 30m",
        "samenvatting": "0 8 * * *",
        "ochtendrapport": "07:30",
        "gezondheid": "*:07,37",
        "nachtcontrole": "02:30",
        "chatlog_rotatie": "04,05,06:00",
    },
    # Abonnementen (besluit eigenaar 03-10): waarschuwen vanaf krap_procent, vanaf stop_procent (week, twee metingen)
    # starten er geen nieuwe kaarten tot de reset of tot de eigenaar "doorgaan" zegt (limiet-poort.py).
    "abonnement": {"providers": {"anthropic": "Claude", "openai-codex": "Codex"},
                   "krap_procent": 70, "stop_procent": 90},
    "drempels": {
        "schijf_min_gb": 10,
        "cpu_max_procent": 60,
        "swap_max_gb": 2.5,
        "geheugen_min_mb": 400,
        "checkin_max_uur": 2,
        "werkmappen_resten_max": 10,
        "werkmappen_totaal_max_gb": 12,
        "werkmap_gesloten_na_uur": 6,
        "review_prioriteit": 10,
        "review_wacht_min": 15,
        "opnieuw_wekken_uur": 6,
        "blokkade_wek_min": 30,
        "blokkade_vraag_uur": 2,
        "manager_opnieuw_wekken_min": 60,
        "coder_voorraad_min": 3,
        "vraag_zonder_post_min": 15,
        "ready_zonder_eigenaar_min": 30,
        "te_lang_running_uur": 3,
        "triage_min": 60,
        "todo_min": 30,
        "ready_uur": 2,
        "kleurplaat_max_regels": 300,
        "gateway_plat_na_s": 120,
    },
    "bordbewaking": {
        "toegestane_model_override": [],
        "verplichte_kleurplaat_secties": ["Voorwaarden", "Randgevallen"],
        "regels_sinds": {"vervolg": 0, "kleurplaat_secties": 0, "afhankelijkheden": 0},
    },
    "gezondheid": {
        "model_login": {"wrapper": "", "env_variabele": ""},
        "postgres_log": "",
        "ops_checkin": False,
        "werkmappen": True,
    },
    "opruimen": {
        "overslaan_repos": [], "overslaan_namen": [],
        # Discord-berichten weg na deze termijnen (besluit eigenaar 03-10; discord-cleanup.py), altijd eerst naar
        # paden.discord_archief. meldingen/gezondheid: alleen "✅ opgelost"; workflow-inbox: alleen doorgestreept;
        # vragen: posts met de tag verwerkt, dagen sinds archivering. Vastgepinde berichten nooit.
        "discord": {
            "termijnen_dagen": {"chatlog": 7, "vragen": 14, "meldingen": 3, "staging": 14, "samenvatting": 14,
                                "workflow-inbox": 7, "gezondheid": 3},
            "melding_doorgeven_dagen": 14,
            "max_per_ronde": 50,
            "max_seconden": 60,
        },
    },
    "release": {
        "controle_commando": ["{python}", "{scripts}/flow-controle.py", "--quick"],
        "sync_commando": [],
    },
    "export": {"prive_termen": [], "allowlist": []},
    "teksten": {
        "opgelost": "✅ opgelost ({tijd}) — ~~{tekst}~~",
        "vraag": {
            "knop_anders": "✏️ Anders…",
            "voetregel": "_Een aanbeveling is alleen advies: er gebeurt niets tot je klikt. Klik een optie of typ "
                         "je antwoord._",
            "waarom_kop": "💡 **Waarom deze aanbeveling:**",
            "details_kop": "**Details**",
            "vervangen": "⚠️ Vervangen door de nieuwe vraag hieronder.",
            "beantwoord_in_post": "✅ Beantwoord in de post.",
            "beantwoord_buiten": "✅ Beantwoord buiten Discord (desktop of op de kaart).",
            "advies_gewijzigd": "⚠️ **Advies gewijzigd** — zie het nieuwe bericht hieronder.",
            "nieuw_advies": "🔁 **Nieuw advies van het team**",
        },
        "staging": {
            "regel": "[{project}] ✅ {id} {titel} — {url}",
            "samenvatting": "✅ Staging klaar (samenvatting, {aantal} kaarten) — {urls}",
            "compleet": "🏁 [{project}] {niveau} {code} compleet op staging (eindcontrole {eind} klaar)",
        },
        "vastgelopen": {
            "vastgelopen": "Vastgelopen",
            "mislukt": "❌ Kaart mislukt: {id} {titel} ({project}) — {waarom}",
            "bouwer_onbeschikbaar": "⏸️ Bouwer niet beschikbaar: {aantal} kaart(en) wachten ({kaarten}). Er wordt "
                                    "geen ander model ingezet; ze gaan verder zodra de bouwer weer werkt.",
        },
        "gezondheid": {
            "blokkerend": "🔴 {label} faalt sinds {sinds}: {detail}. Projectwerk ligt stil.",
            "melding": "⚠️ {label}: {detail} (sinds {sinds})",
        },
        "gateway": {
            "onverwacht": "⚠️ Gateway onverwacht herstart: {waarom}. Weer online sinds {sinds}.",
            "weer_online": "🟢 Gateway weer online (sinds {sinds}).",
            "plat": "🔴 Gateway ligt plat sinds {sinds} (status {status}). Hermes reageert niet.",
        },
        "schijf": "⚠️ Weinig schijfruimte op {host}: nog {vrij:.1f} GB vrij van {totaal:.0f} GB ({procent:.0f}% in "
                  "gebruik) — drempel is {drempel:.0f} GB. Maak ruimte vrij of breid de disk uit.",
        "drain": {
            "actief": "⏸️ Drain actief sinds {sinds}, tot uiterlijk {tot} (geplande gateway-herstart): er starten even "
                      "geen nieuwe kaarten; lopend werk gaat door. Geen storing.",
            "klaar": "▶️ Drain klaar ({reden}): de dispatcher pakt weer kaarten op.",
            "status_regel": "⏸️ DRAIN ACTIEF sinds {sinds}, tot uiterlijk {tot}: geplande herstart door de workflowkant. "
                            "Er starten even geen nieuwe kaarten; lopend werk gaat door. Dit is GEEN storing: niet melden.",
        },
        "drain_vangnet": "⚠️ Vangnet drain: de dispatcher stond sinds {sinds} op pauze voor een geplande herstart, en "
                         "is nu automatisch weer aangezet. Kaarten worden weer opgepakt. Je hoeft niets te doen; de "
                         "workflowkant kijkt waarom de herstart niet afrondde.",
        "release": "🚀 {nr} — {notitie} (regressie {score} OK)",
        "workflow_inbox": {
            "kop": "**📥 {titel}**",
            "regel": "Project: {project} · Gemeld door: {wie} · {datum}",
            "open": "Status: open",
            "afgehandeld": "✅ Afgehandeld {wanneer}: {oplossing}",
            "verborgen": "(inhoud niet getoond: bevat mogelijk een geheim)",
            "geen_oplossing": "zie het bestand",
            "geen_project": "workflow",
        },
        "ochtendrapport": {
            "kop": "**Ochtendrapport workflow {datum}** (modus: {modus})",
            "koppen": {"gezondheid": "**Gezondheid**", "nacht": "**Nacht**", "opgelost": "**Uitgevoerd / zelf opgelost**",
                       "inbox": "**Open workflowpunten**", "week": "**Week (incidentenlogboek)**"},
            "inbox_regel": "Workflow-inbox: {aantal} open (oudste: {oudste})",
            "inbox_leeg": "Workflow-inbox: niets open",
            "backup_regel": "- 🗂️ Back-upbranches (niet-vastgelegd werk): {weg} opgeruimd sinds het vorige rapport, {bewaard} bewaard",
            "abonnement_kop": "**Abonnement**",
            "abonnement_regel": "- {naam} {label}: {procent}% gebruikt (reset {reset})",
            "abonnement_krap": "- ⚠️ {naam}: week boven {drempel}%; bij {stop}% starten er geen nieuwe kaarten meer",
            "abonnement_onbekend": "- verbruik niet op te vragen",
            "modusvraag": "❓ Beslissing in de ops-sessie: op {tot} overschakelen naar rapportmodus, of de "
                          "opstartmodus verlengen?",
            "voorstellen": {
                "model-login": "login-wrapper en .env-verwijzingen herstellen; daarna een testbeurt per model",
                "db-login": "rol en wachtwoord uit de projectcode terugzetten (niet door agents); login testen, "
                            "geblokkeerde kaarten deblokkeren",
                "pg-log": "nagaan welke sessie de rol wijzigde; herstellen zoals bij db-login",
                "git": "GitHub-login (gh auth) of netwerk controleren",
                "staging": "de deploy en de laatste merge naar staging bekijken",
                "gateway": "gateway via het drain-script herstarten",
                "dispatcher": "drain afbreken (`drain-restart.py --restore`) en controleren of er kaarten worden "
                              "opgepakt",
                "worker-restanten": "de verweesde worker-scopes stoppen",
                "resources": "zware runs spreiden of het aantal workers verlagen; bij schijf: archief opruimen",
                "ops-checkin": "de ops-sessie opnieuw starten en de uurlijkse zelfcontrole aanmaken",
            },
        },
        "productievraag": (
            "{prefix}\n"
            "Wat speelt er: Fase {code} van {project} is compleet: alle kaarten zijn klaar, de eindcontrole {eind} is "
            "afgerond en alles staat op staging ({url}, branch {branch} @ {commit}).\n"
            "Vraag: Fase {code} naar productie?\n"
            "Optie 1 ⭐ Aanbevolen: Ja, fase {code} naar productie\n"
            "  Inhoud: de manager merget precies commit {commit} van {branch} naar main en controleert productie "
            "daarna live.\n"
            "  Voordeel: gebruikers krijgen de afgeronde fase meteen.\n"
            "  Nadeel: wat op staging niet getest is, kan in productie toch anders werken.\n"
            "  Daarna: de manager zet de release in gang en meldt het resultaat; bij een probleem wordt het "
            "teruggedraaid.\n"
            "Optie 2: Eerst zelf op staging bekijken\n"
            "  Inhoud: je bekijkt de fase eerst zelf op {url}; daarna vraagt de manager opnieuw.\n"
            "  Voordeel: je ziet het eerst met eigen ogen.\n"
            "  Nadeel: de release schuift op tot je hebt gekeken.\n"
            "  Daarna: de manager stelt dezelfde vraag opnieuw zodra jij zegt dat je hebt gekeken.\n"
            "Optie 3: Nog niet, wachten tot de volgende fase\n"
            "  Inhoud: productie blijft zoals hij is; de volgende fase gaat later samen mee.\n"
            "  Voordeel: minder releases.\n"
            "  Nadeel: afgerond werk blijft langer liggen.\n"
            "  Daarna: de kaart wordt afgerond; bij de volgende complete fase komt een nieuwe vraag.\n"
            "Waarom deze aanbeveling: 1) elke kaart is gereviewd en de eindcontrole is klaar; 2) staging draait "
            "precies deze commit. Kies liever optie 2 als je iets in deze fase zelf wilt zien, of als er gevoelige "
            "onderdelen in zitten (betalingen, accounts, gezondheidsdata).\n"
            "Zonder antwoord: productie blijft zoals hij is; het werk aan de volgende fase gaat gewoon door.\n"
            "Details: eindcontrole {eind}; staging {url}; commit {commit}. Productie alleen na jouw klik, voor "
            "precies die commit."),
        "samenvatting_prompt": (
            "Hierboven staan de Kanban-gegevens van de projecten met STATUS: ACTIEF en de meting per bouwkaart. "
            "Gebruik uitsluitend die gegevens en zoek geen andere kaarten op. Dit kanaal is alleen voor projecten: "
            "noem nooit workflowzaken.\nStaat er \"GEEN ACTIEVE KAARTEN\", antwoord dan exact en alleen: Geen actief "
            "werk.\nAnders: schrijf de dagelijkse samenvatting voor {eigenaar}, in het Nederlands, maximaal 12 regels "
            "in totaal, met de kopjes **Af (gisteren)**, **Bezig**, **Vast**, en alleen als die er is **Meting**. Per "
            "kaart één korte regel met project en onderwerp; zijn het er te veel, vat dan samen (bijv. \"+4 meer bij "
            "<project>\"). Onder **Meting**: één regel met het aantal bouwkaarten en het gemiddelde aantal runs, "
            "reviewrondes en de doorlooptijd. Geen inleiding, geen afsluiting, geen andere tekst."),
    },
}

_CACHE = {}


def _merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def config_path():
    """The config file in use, or None (built-in defaults)."""
    env = os.environ.get("HERMES_FLOW_CONFIG")
    if env:
        return Path(env).expanduser()
    for p in (HOME / "team" / "flow.yaml", Path.home() / ".hermes" / "team" / "flow.yaml"):
        if p.is_file():
            return p
    return None


def load(reload=False) -> dict:
    """The merged config (DEFAULTS + the local file)."""
    if "cfg" in _CACHE and not reload:
        return _CACHE["cfg"]
    path = config_path()
    data = {}
    if path is None and (HOME / "team" / "discord.json").is_file() and not os.environ.get("FLOW_STANDAARDWAARDEN"):
        # A configured install without its flow config would silently run on the example values (wrong owner name,
        # question prefix, projects): refuse instead. FLOW_STANDAARDWAARDEN=1 allows it on purpose.
        raise RuntimeError(f"flow-config ontbreekt ({HOME / 'team' / 'flow.yaml'}), terwijl team/discord.json bestaat; "
                           "kopieer config.example.yaml en vul hem in (of zet HERMES_FLOW_CONFIG)")
    if path is not None:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("PyYAML ontbreekt: installeer python3-yaml (of pip install pyyaml)") from exc
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError as exc:
            raise RuntimeError(f"config {path} bestaat niet (HERMES_FLOW_CONFIG)") from exc
        except yaml.YAMLError as exc:
            raise RuntimeError(f"config {path} is geen geldige YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"config {path}: verwacht een mapping bovenaan")
    cfg = _merge(DEFAULTS, data)
    _CACHE["cfg"] = cfg
    return cfg


def get(dotted, default=None):
    cur = load()
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def is_set(value) -> bool:
    """False for None, "" and "<PLACEHOLDER>" values."""
    if value is None:
        return False
    s = str(value).strip()
    return bool(s) and not (s.startswith("<") and s.endswith(">"))


def expand(path_value, base=None) -> Path:
    """``~`` expanded; a relative path is relative to ``base`` (default: HERMES_HOME)."""
    p = Path(os.path.expanduser(str(path_value)))
    return p if p.is_absolute() else (base or HOME) / p


def path(key) -> Path:
    """A path from ``paden.<key>`` (relative = under HERMES_HOME)."""
    return expand(get(f"paden.{key}"))


def optional_path(key):
    """Like path(), but None when the value is empty (an optional file)."""
    value = get(f"paden.{key}")
    return expand(value) if is_set(value) else None


def discord_archief() -> Path:
    """Archiefmap van verwijderde Discord-berichten. ``~`` is de thuismap van de agent-gebruiker als de vlag aan staat:
    de opruiming (als de agent) en akkoord-check.py (als de ops-gebruiker, leest via de agent) zien zo dezelfde map."""
    value = str(get("paden.discord_archief"))
    if value.startswith("~") and agent_gebruiker() and not ben_agent():
        import pwd
        value = pwd.getpwnam(agent_gebruiker()).pw_dir + value[1:]
    return expand(value, Path.home())


def hermes_bin() -> str:
    return str(expand(get("paden.hermes_bin"), Path.home()))


def tz() -> ZoneInfo:
    return ZoneInfo(get("tijdzone") or "UTC")


def nu() -> datetime:
    """Nu in de tijdzone van de flow (standaard Europe/Amsterdam; de server staat op UTC): voor weergave en
    kalendervensters ("vandaag", "gisteren"). Rekenen met epoch-seconden blijft time.time()."""
    return datetime.fromtimestamp(time.time(), tz())


def tijd(ts=None, fmt="%d-%m %H:%M") -> str:
    """Epoch-seconden (None = nu) als tekst in de tijdzone van de flow."""
    return datetime.fromtimestamp(time.time() if ts is None else ts, tz()).strftime(fmt)


def log(pad, line, echo=False, schrijf=True):
    """Gedeelde logregel: ``<ISO-tijd in de flow-tijdzone> <line>`` achteraan ``pad`` (map wordt aangemaakt);
    ``echo``: ook op stdout; ``schrijf=False`` (bijv. een droogloop): niets naar het bestand."""
    if schrijf:
        pad.parent.mkdir(parents=True, exist_ok=True)
        with pad.open("a") as fh:
            fh.write(f"{nu().isoformat(timespec='seconds')} {line}\n")
    if echo:
        print(line)


def last_line(text, limit=None) -> str:
    """De laatste niet-lege regel van ``text`` (gestript, hoogstens ``limit`` tekens), of "(geen uitvoer)"."""
    lines = [l for l in (text or "").splitlines() if l.strip()]
    return lines[-1].strip()[:limit] if lines else "(geen uitvoer)"


def check_compile(files, limit=None, prefix=None):
    """``(ok, detail)``: py_compile van ``files`` met de bytecode in een tijdelijke map (controle 1 van de
    nachtelijke checks)."""
    import py_compile
    import tempfile
    bad = []
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        for f in files:
            try:
                py_compile.compile(str(f), cfile=str(Path(tmp) / (f.stem + ".pyc")), doraise=True)
            except py_compile.PyCompileError as e:
                bad.append(f"{f.name}: {last_line(str(e), limit)}")
    return not bad, f"{len(files) - len(bad)}/{len(files)} scripts compileren" + (f"; FOUT {'; '.join(bad)}" if bad else "")


def owner_name() -> str:
    return str(get("eigenaar.naam") or "de eigenaar")


def question_prefix() -> str:
    return str(get("vragen.prefix") or "Vraag voor de eigenaar:").strip()


def text(dotted, **values) -> str:
    """A message template from ``teksten.<dotted>``, formatted with ``values`` (plus eigenaar)."""
    tpl = get(f"teksten.{dotted}")
    if tpl is None:
        raise KeyError(f"tekst teksten.{dotted} ontbreekt in de config")
    return str(tpl).format(eigenaar=owner_name(), **values)


def tag(status) -> str:
    """Forum tag name for a status key (open, beantwoord, verwerkt)."""
    return str(get(f"discord.tags.{status}") or status)


def status_tags() -> tuple:
    return tuple(tag(s) for s in ("open", "beantwoord", "verwerkt"))


def projects() -> dict:
    """``{slug: entry}`` from ``projecten`` (paths expanded; db None when not checked)."""
    out = {}
    for p in get("projecten") or []:
        if not isinstance(p, dict) or not is_set(p.get("slug")):
            continue
        entry = dict(p)
        entry["repo"] = expand(p.get("repo") or "", Path.home()) if is_set(p.get("repo")) else None
        entry["db"] = (p.get("db") or {}).get("bestand") if isinstance(p.get("db"), dict) else p.get("db")
        out[p["slug"]] = entry
    return out


# Projecten en borden (voorheen team_projects.py): gedeelde alleen-lezen helpers voor de team-cronscripts.
# Active project = a file in ~/.hermes/team/projects/ (not _TEMPLATE.md) with a line
# "STATUS: ACTIEF"; no STATUS line means not active. It is linked to Kanban via the projects.db slug (= file name):
# cards match on project_id or on a workspace path under the project's folder.
#
# Boards: every non-archived board under ~/.hermes/kanban/boards/ (the same set the dispatcher walks),
# except ``default``, which is the archive of the old setup.
PROJECTS_DIR = HOME / "team" / "projects"


def _ro(path: Path):
    # Eén Hermes-eigenaar (03-10): via connect_ro (na de overstap via de agent, nooit direct).
    import sqlite3
    conn = connect_ro(path)
    conn.row_factory = sqlite3.Row
    return conn


def active_projects(statuses=("ACTIEF",)) -> dict:
    """``{slug: {"name", "ids", "paths", "status", "file"}}`` for every project file whose STATUS is in ``statuses``."""
    slugs = {}
    wanted = "|".join(re.escape(s) for s in statuses)
    for f in sorted(PROJECTS_DIR.glob("*.md")):
        if f.name.startswith("_"):
            continue
        m = re.search(rf"^\s*STATUS:\s*({wanted})\s*$", f.read_text(encoding="utf-8", errors="replace"), re.M | re.I)
        if not m:
            continue
        slugs[f.stem] = {"name": f.stem, "ids": set(), "paths": [], "status": m.group(1).upper(), "file": f}
    db = HOME / "projects.db"
    if slugs and db.exists():
        conn = _ro(db)
        for row in conn.execute("SELECT p.slug, p.id, p.name, f.path FROM projects p "
                                "LEFT JOIN project_folders f ON f.project_id = p.id"):
            if row["slug"] in slugs:
                entry = slugs[row["slug"]]
                entry["name"] = row["name"] or entry["name"]
                entry["ids"].add(row["id"])
                if row["path"]:
                    entry["paths"].append(row["path"].rstrip("/") + "/")
    return slugs


def project_of(task, projects: dict):
    """Slug of the active project a card belongs to, else None."""
    workspace = (task["workspace_path"] or "").rstrip("/") + "/"
    for slug, p in projects.items():
        if task["project_id"] in p["ids"] or any(workspace.startswith(path) for path in p["paths"]):
            return slug
    return None


def boards() -> list:
    """``[(slug, kanban.db path)]`` for every non-archived board except ``default``."""
    found = []
    root = HOME / "kanban" / "boards"
    for d in sorted(root.iterdir(), key=lambda p: p.name.lower()) if root.is_dir() else []:
        if not d.is_dir() or d.name.startswith("_") or d.name == "default":
            continue
        if not ((d / "board.json").exists() or (d / "kanban.db").exists()):
            continue
        try:
            meta = json.loads((d / "board.json").read_text(encoding="utf-8")) if (d / "board.json").exists() else {}
        except (OSError, ValueError):
            meta = {}
        if meta.get("archived") or not (d / "kanban.db").exists():
            continue
        found.append((d.name, d / "kanban.db"))
    return found


def kanban(db_path: Path):
    return _ro(db_path)


ALS_AGENT = os.environ.get("HERMES_ALS_AGENT") or "/usr/local/bin/als-agent"


def agent_gebruiker() -> str:
    """De Linux-gebruiker van de agents (``agent.gebruiker``), of "" (vlag uit: oude gedrag)."""
    value = get("agent.gebruiker")
    return str(value).strip() if is_set(value) else ""


def ben_agent() -> bool:
    """True als dit proces al als de agent-gebruiker draait (bijv. Hermes-cron in de gateway)."""
    import pwd
    return pwd.getpwuid(os.getuid()).pw_name in {agent_gebruiker() or AGENT_STANDAARD, AGENT_STANDAARD}


AGENT_STANDAARD = os.environ.get("HERMES_AGENT_USER") or "hermes-agent"


def van_agent(pad) -> bool:
    """True als ``pad`` van de agent-gebruiker is (vanaf Z3 van de overstap, ook als de vlag nog leeg is)."""
    import pwd
    try:
        return pwd.getpwuid(Path(pad).stat().st_uid).pw_name in {agent_gebruiker() or AGENT_STANDAARD, AGENT_STANDAARD}
    except (OSError, KeyError):
        return False


def via_agent(pad=None) -> bool:
    """Eén Hermes-eigenaar (besluit eigenaar 03-10): de ops-kant raakt Hermes-bestanden alleen via de agent zodra
    de vlag aan staat of het bestand al van de agent is. Nooit direct, ook niet alleen-lezen: SQLite maakt anders
    -wal/-shm als de ops-gebruiker aan en de agent kan daarna niet meer schrijven (bewezen 03-10)."""
    if ben_agent():
        return False
    if pad is not None:  # alleen bestanden van de agent; eigen kopieën van de ops-kant (tests in /tmp) blijven direct
        return van_agent(pad)
    return bool(agent_gebruiker()) or van_agent(HOME / "state.db")


def als_agent(cmd) -> list:
    """``cmd`` als de agent: ongewijzigd bij een lege vlag of als we de agent al zijn, anders via als-agent.
    Let op: als-agent (sudo) neemt de omgeving niet over; geef variabelen mee met ``["env", "X=1", …]``."""
    if not agent_gebruiker() or ben_agent():
        return list(cmd)
    return [ALS_AGENT, *cmd]


def als_agent_voor(pad, cmd) -> list:
    """``cmd`` als de agent alleen als ``pad`` van de agent is (één Hermes-eigenaar); eigen paden van de ops-gebruiker
    (bijv. testrepo's in /tmp) blijven direct, ook als de vlag aan staat."""
    if ben_agent() or not van_agent(pad):
        return list(cmd)
    return [ALS_AGENT, *cmd]


def systemctl_gateway(*args) -> list:
    """``systemctl --user …`` in de user-manager waar de gateway draait (die van de agent als de vlag aan staat)."""
    return als_agent(["systemctl", "--user", *args])


def systemctl_managers() -> list:
    """Prefixen voor alle user-managers die de ops-kant bewaakt: die van zichzelf en (vlag aan) die van de agent."""
    eigen = ["systemctl", "--user"]
    return [eigen] + ([systemctl_gateway()] if agent_gebruiker() and not ben_agent() else [])


class _AgentRow(tuple):
    """Een rij die op positie én op kolomnaam werkt (zoals sqlite3.Row)."""

    def __new__(cls, waarden, kolommen):
        rij = super().__new__(cls, waarden)
        rij._kolommen = list(kolommen)
        return rij

    def __getitem__(self, sleutel):
        if isinstance(sleutel, str):
            return tuple.__getitem__(self, self._kolommen.index(sleutel))
        return tuple.__getitem__(self, sleutel)

    def keys(self):
        return list(self._kolommen)


class _AgentRows:
    def __init__(self, rows, kolommen=None):
        self._rows = [_AgentRow(r, kolommen) if kolommen else tuple(r) for r in rows]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _AgentSQL:
    """Alleen-lezen SQLite via als-agent (scripts/agent_sql.py): voor bestanden die Hermes op 600 zet (state.db).
    ``row_factory = sqlite3.Row`` mag gezet worden: rijen werken altijd op positie én kolomnaam."""

    row_factory = None

    def __init__(self, db):
        self.db = str(db)

    def execute(self, sql, params=()):
        import subprocess
        r = subprocess.run([ALS_AGENT, "python3", str(SCRIPTS / "agent_sql.py"), self.db, sql,
                            json.dumps(list(params)), "--met-kolommen"], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            import sqlite3
            raise sqlite3.OperationalError(f"agent_sql: exit {r.returncode}: {r.stderr.strip()[-200:]}")
        uit = json.loads(r.stdout or "{}")
        return _AgentRows(uit.get("rijen", []), uit.get("kolommen"))

    def close(self):
        pass


def connect_ro(db, timeout=10):
    """Alleen-lezen verbinding met een SQLite-bestand van Hermes. Is het (of Hermes) van de agent: altijd via
    als-agent (via_agent), nooit direct. Anders direct (oude stand). Rijen zijn tuples (geen row_factory)."""
    import sqlite3
    if via_agent(db):
        return _AgentSQL(db)
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=timeout)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return conn
    except sqlite3.Error:
        if not agent_gebruiker() or ben_agent():
            raise
        return _AgentSQL(db)


def kopieer_db(src, dst):
    """Consistente kopie van een live SQLite-database (backup-API, WAL inbegrepen) naar ``dst`` (van de ops-gebruiker).
    Is de bron van de agent (via_agent): de agent maakt de kopie in een tijdelijke gedeelde map; de ops-gebruiker
    opent de live database nooit zelf (besluit eigenaar 03-10)."""
    import shutil
    import sqlite3
    import subprocess
    import tempfile
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not via_agent(src):
        s, d = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30), sqlite3.connect(dst)
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()
        return
    tmp = Path(tempfile.mkdtemp(prefix="hermes-dbkopie-"))
    try:
        subprocess.run(["chgrp", "hermes-team", str(tmp)], check=True)
        tmp.chmod(0o2770)
        doel = tmp / "kopie.db"
        code = ("import sqlite3,sys; s=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True,timeout=30); "
                "d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()")
        r = subprocess.run([ALS_AGENT, "sh", "-c", 'umask 007; exec python3 -c "$1" "$2" "$3"', "kopie", code,
                            str(src), str(doel)], capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise sqlite3.OperationalError(f"kopie via agent mislukt: {r.stderr.strip()[-200:]}")
        shutil.copyfile(doel, dst)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def verbruik(provider):
    """``[(label, procent, resets_at)]`` van een abonnement via ``hermes usage --json`` (wrapper → de agent);
    ``None`` als het niet op te vragen is."""
    import subprocess
    try:
        r = subprocess.run([hermes_bin(), "usage", "--provider", provider, "--json"], capture_output=True, text=True,
                           timeout=90)
        data = json.loads(r.stdout[r.stdout.find("{"):])
    except Exception:  # noqa: BLE001
        return None
    if data.get("unavailable_reason"):
        return None
    return [(w.get("label", "?"), float(w.get("used_percent") or 0), w.get("resets_at")) for w in data.get("windows") or []]


def abonnementen() -> dict:
    """``{provider: naam}`` uit ``abonnement.providers``."""
    prov = get("abonnement.providers")
    if isinstance(prov, dict) and prov:
        return {str(k): str(v) for k, v in prov.items()}
    return {}


def channel_ids() -> dict:
    """``{"guild", "owner", <channel key>: id}``: team/discord.json overlaid with the IDs set in the config."""
    ids = {}
    f = path("discord_ids")
    if f.is_file():
        ids.update(json.loads(f.read_text(encoding="utf-8")))
    d = get("discord") or {}
    if is_set(d.get("guild_id")):
        ids["guild"] = str(d["guild_id"])
    if is_set(d.get("owner_id")):
        ids["owner"] = str(d["owner_id"])
    for key, ch in (d.get("kanalen") or {}).items():
        if isinstance(ch, dict) and is_set(ch.get("id")):
            ids[key] = str(ch["id"])
    return {k: v for k, v in ids.items() if is_set(v)}


def validate() -> list:
    """What is wrong with the config (empty list = fine)."""
    problems = []
    cfg = load()
    unknown = sorted(set(cfg) - set(DEFAULTS))
    if unknown:
        problems.append("onbekende sleutel(s) bovenaan: " + ", ".join(unknown))
    try:
        tz()
    except Exception:  # noqa: BLE001
        problems.append(f"tijdzone '{cfg.get('tijdzone')}' bestaat niet")
    if not re.match(r"(?i)^vraag voor [^:\n]{1,40}:$", question_prefix()):
        problems.append("vragen.prefix moet de vorm 'Vraag voor <wie>:' hebben (de fork-patches herkennen die)")
    for key, ch in (cfg["discord"].get("kanalen") or {}).items():
        if ch.get("categorie") not in cfg["discord"]["categorieen"]:
            problems.append(f"kanaal {key}: categorie '{ch.get('categorie')}' staat niet in discord.categorieen")
        if ch.get("soort") not in ("tekst", "forum"):
            problems.append(f"kanaal {key}: soort moet 'tekst' of 'forum' zijn")
    for key in ("vragen", "meldingen", "staging", "samenvatting"):
        if key not in cfg["discord"]["kanalen"]:
            problems.append(f"kanaal {key} ontbreekt in discord.kanalen (hoort bij de basis)")
    slugs = [p.get("slug") for p in cfg.get("projecten") or [] if isinstance(p, dict)]
    if len(slugs) != len(set(slugs)):
        problems.append("projecten: dubbele slug")
    for name in ("controle_commando", "sync_commando"):
        if not isinstance(cfg["release"].get(name), list):
            problems.append(f"release.{name} moet een lijst zijn (leeg = overslaan)")
    for dotted in ("opgelost", "release", "staging.regel", "vastgelopen.mislukt", "gezondheid.melding"):
        try:
            text(dotted, tijd="", tekst="", nr="", notitie="", score="", project="", id="", titel="", url="",
                 waarom="", label="", detail="", sinds="")
        except (KeyError, IndexError, ValueError) as exc:
            problems.append(f"teksten.{dotted}: onbekende plaatshouder {exc}")
    return problems


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "pad":
        print(config_path() or "(standaard)")
    elif cmd == "get" and len(argv) == 2:
        value = get(argv[1])
        if value is None:
            print(f"onbekende sleutel {argv[1]}", file=sys.stderr)
            return 1
        print(json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value)
    elif cmd == "check":
        problems = validate()
        print("config OK" + (f" ({config_path()})" if config_path() else " (standaardwaarden)") if not problems
              else "config FOUT:\n- " + "\n- ".join(problems))
        return 1 if problems else 0
    elif cmd == "als-agent-prefix":
        print(" ".join(als_agent([])))  # leeg bij vlag uit; anders het pad van als-agent (voor shellscripts)
    elif cmd == "kanalen":
        for k, v in channel_ids().items():
            print(k, v)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
