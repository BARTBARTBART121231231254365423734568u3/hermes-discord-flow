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
"""
import copy
import json
import os
import re
import sys
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
        "vastgelopen": "*/5 * * * *",
        "opruimcontrole": "*/10 * * * *",
        "bordbewaking": "*/30 * * * *",
        "werkmappen_opruimen": "15 */4 * * *",
        "schijfruimte": "every 30m",
        "samenvatting": "0 8 * * *",
        "ochtendrapport": "07:30",
        "gezondheid": "*:07,37",
        "nachtcontrole": "02:30",
        "chatlog_rotatie": "04,05,06:00",
    },
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
        "escalatie_uur": 6,
        "manager_escalatie_uur": 2,
        "manager_opnieuw_wekken_min": 60,
        "beslissing_manager_wacht_min": 30,
        "vraag_zonder_post_min": 15,
        "ready_zonder_eigenaar_min": 30,
        "te_lang_running_uur": 3,
        "vastgelopen_uur": 6,
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
    "opruimen": {"overslaan_repos": [], "overslaan_namen": []},
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
            "beslissing_manager": "Beslissing manager nodig",
            "mislukt": "❌ Kaart mislukt: {id} {titel} ({project}) — {waarom}",
            "bouwer_onbeschikbaar": "⏸️ Bouwer niet beschikbaar: {aantal} kaart(en) wachten ({kaarten}). Er wordt "
                                    "geen ander model ingezet; ze gaan verder zodra de bouwer weer werkt.",
        },
        "bordbewaking": {
            "escalatie": "🧭 Bordbewaking: {id} {titel} ({project}) — {waarom}. De manager kreeg dit {uren} uur "
                         "geleden en het staat er nog.",
            "escalatie_manager": "🧭 Kaart {id} {titel} ({project}) wacht al {uren} uur op een beslissing van de "
                                 "manager en ligt stil.\n1. Open #chatlog: {link}\n2. Typ: `Pak eerst de blokkade van "
                                 "{id} op, vóór je planwerk.`\n3. Daarna hoort de manager te antwoorden wat hij "
                                 "besluit, en staat de kaart niet meer op geblokkeerd.\nAntwoord hier alleen met "
                                 "\"gelukt\" of \"niet gelukt\".",
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


def hermes_bin() -> str:
    return str(expand(get("paden.hermes_bin"), Path.home()))


def tz() -> ZoneInfo:
    return ZoneInfo(get("tijdzone") or "UTC")


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


def threshold(key):
    return get(f"drempels.{key}")


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
    elif cmd == "kanalen":
        for k, v in channel_ids().items():
            print(k, v)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
