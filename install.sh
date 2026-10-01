#!/usr/bin/env bash
# Hermes Discord Flow — installatie (idempotent: je mag hem altijd opnieuw draaien).
#
# Gebruik: ./install.sh [--dry-run] [--basis] [--met-chatlog] [--geen-systemd] [--geen-cron]
#   --dry-run       laat alleen zien wat er zou gebeuren; verandert niets
#   --basis         alleen de basis: vragen, meldingen, staging, samenvatting (+ gateway-wachter)
#                   (zonder: ook de extra's: ochtendrapport, gezondheid, nachtcontrole/releases, bordbewaking, opruiming)
#   --met-chatlog   ook de nachtelijke #chatlog-vernieuwing (chatlog-rotate.py) installeren
#   --geen-systemd  geen systemd-units schrijven of aanzetten
#   --geen-cron     geen Hermes-cronjobs maken
#
# Wat het doet:
#   1. controleert Python >= 3.11 en PyYAML;
#   2. kopieert scripts/ naar $HERMES_HOME/scripts/ (een gewijzigd bestand gaat eerst naar scripts/.backup-<tijd>/);
#   3. zet config.example.yaml op $HERMES_HOME/team/flow.yaml als die nog niet bestaat (chmod 600) en controleert hem;
#   4. maakt state/, logs/, team/projects/ en de workflow-inbox (met LEESMIJ.md);
#   5. schrijft de systemd --user units (tijden uit de config) en zet de timers aan;
#   6. maakt de Hermes-cronjobs die nog niet bestaan (op naam).
# Omgevingsvariabelen: HERMES_HOME (standaard ~/.hermes), SYSTEMCTL (standaard systemctl), HERMES_BIN (standaard
# paden.hermes_bin uit de config), PYTHON (standaard python3).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
export HERMES_HOME
PYTHON="${PYTHON:-python3}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DRY=0; BASIS=0; CHATLOG=0; SYSTEMD=1; CRON=1
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --basis) BASIS=1 ;;
    --met-chatlog) CHATLOG=1 ;;
    --geen-systemd) SYSTEMD=0 ;;
    --geen-cron) CRON=0 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "onbekende optie: $a (zie --help)" >&2; exit 2 ;;
  esac
done

say() { echo "• $*"; }
doe() { # run a command, or only show it with --dry-run
  if [ "$DRY" = 1 ]; then printf '  (dry-run) %q ' "$@"; echo; else "$@"; fi
}

# 1. requirements ------------------------------------------------------------------------------------------
"$PYTHON" - <<'EOF' || { echo "Python >= 3.11 met PyYAML is nodig (apt install python3-yaml, of pip install pyyaml)" >&2; exit 1; }
import sys
assert sys.version_info >= (3, 11), sys.version
import yaml  # noqa: F401
EOF
command -v git >/dev/null || { echo "git ontbreekt" >&2; exit 1; }
say "HERMES_HOME = $HERMES_HOME"

# 2. scripts -----------------------------------------------------------------------------------------------
DEST="$HERMES_HOME/scripts"
STAMP="$(date +%Y%m%d-%H%M%S)"
doe mkdir -p "$DEST"
changed=0
for f in "$REPO"/scripts/*; do
  [ -f "$f" ] || continue
  name="$(basename "$f")"
  if [ -f "$DEST/$name" ] && cmp -s "$f" "$DEST/$name"; then continue; fi
  if [ -f "$DEST/$name" ]; then
    doe mkdir -p "$DEST/.backup-$STAMP"
    doe cp -p "$DEST/$name" "$DEST/.backup-$STAMP/$name"
  fi
  doe install -m 0755 "$f" "$DEST/$name"
  changed=$((changed + 1))
done
say "scripts: $changed nieuw of gewijzigd in $DEST"

# 3. config ------------------------------------------------------------------------------------------------
CFG="${HERMES_FLOW_CONFIG:-$HERMES_HOME/team/flow.yaml}"
if [ ! -f "$CFG" ]; then
  doe mkdir -p "$(dirname "$CFG")"
  doe install -m 0600 "$REPO/config.example.yaml" "$CFG"
  say "config aangemaakt: $CFG — vul guild_id, owner_id, eigenaar en projecten in"
else
  say "config bestaat al: $CFG (niet aangeraakt)"
fi
FC="$REPO/scripts/flow_config.py"
if [ -f "$CFG" ]; then export HERMES_FLOW_CONFIG="$CFG"; else export HERMES_FLOW_CONFIG="$REPO/config.example.yaml"; fi
"$PYTHON" "$FC" check
cfg() { "$PYTHON" "$FC" get "$1"; }

# 4. folders -----------------------------------------------------------------------------------------------
INBOX="$("$PYTHON" -c 'import sys; sys.path.insert(0, sys.argv[1]); import flow_config as fc; print(fc.path("workflow_inbox"))' "$REPO/scripts")"
for d in "$HERMES_HOME/state" "$HERMES_HOME/logs" "$HERMES_HOME/team/projects" "$INBOX"; do doe mkdir -p "$d"; done
if [ ! -f "$INBOX/LEESMIJ.md" ]; then doe cp "$REPO/docs/workflow-inbox-LEESMIJ.md" "$INBOX/LEESMIJ.md"; fi

# 5. systemd -----------------------------------------------------------------------------------------------
UNITS=(hermes-gateway-watch)
[ "$BASIS" = 0 ] && UNITS+=(hermes-health hermes-ochtendrapport hermes-nachtcontrole)
[ "$CHATLOG" = 1 ] && UNITS+=(hermes-chatlog-rotate hermes-chatlog-titel)
if [ "$SYSTEMD" = 1 ]; then
  doe mkdir -p "$UNIT_DIR"
  for u in "${UNITS[@]}"; do
    for kind in service timer; do
      src="$REPO/systemd/$u.$kind"
      tmp="$(mktemp)"
      "$PYTHON" - "$src" "$tmp" "$REPO/scripts" <<'EOF'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
import flow_config as fc
import shutil
text = Path(sys.argv[1]).read_text()
if fc.HOME != Path.home() / ".hermes":  # a non-default HERMES_HOME: absolute paths instead of %h/.hermes
    text = text.replace("%h/.hermes", str(fc.HOME))
want = str(fc.get("paden.hermes_python"))
python = str(fc.expand(want, Path.home())) if "/" in want else (shutil.which(want) or "/usr/bin/python3")
for token, value in {"@GEZONDHEID@": fc.get("tijden.gezondheid"), "@OCHTENDRAPPORT@": fc.get("tijden.ochtendrapport"),
                     "@NACHTCONTROLE@": fc.get("tijden.nachtcontrole"), "@CHATLOG_ROTATIE@": fc.get("tijden.chatlog_rotatie"),
                     "@TIJDZONE@": fc.get("tijdzone"), "@HERMES_PYTHON@": python}.items():
    text = text.replace(token, str(value))
Path(sys.argv[2]).write_text(text)
EOF
      if [ -f "$UNIT_DIR/$u.$kind" ] && cmp -s "$tmp" "$UNIT_DIR/$u.$kind"; then rm -f "$tmp"; continue; fi
      doe install -m 0644 "$tmp" "$UNIT_DIR/$u.$kind"
      rm -f "$tmp"
      say "unit geschreven: $u.$kind"
    done
  done
  doe "$SYSTEMCTL" --user daemon-reload
  for u in "${UNITS[@]}"; do doe "$SYSTEMCTL" --user enable --now "$u.timer"; done
else
  say "systemd overgeslagen (--geen-systemd)"
fi

# 6. Hermes cron -------------------------------------------------------------------------------------------
if [ "$CRON" = 1 ]; then
  HERMES="${HERMES_BIN:-$("$PYTHON" -c 'import sys; sys.path.insert(0, sys.argv[1]); import flow_config as fc; print(fc.hermes_bin())' "$REPO/scripts")}"
  existing="$("$HERMES" cron list 2>/dev/null || true)"
  ids="$("$PYTHON" "$FC" kanalen 2>/dev/null || true)"
  chan() { echo "$ids" | awk -v k="$1" '$1 == k {print $2}'; }
  job() { # name, schedule, script, deliver, [prompt]
    local name="$1" sched="$2" script="$3" deliver="$4" prompt="${5:-}"
    if echo "$existing" | grep -Eq "Name:[[:space:]]+$(printf '%s' "$name" | sed 's/[][\.*^$()+?{}|/]/\\&/g')[[:space:]]*$"; then
      say "cronjob bestaat al: $name"; return
    fi
    if [ -n "$prompt" ]; then
      doe "$HERMES" cron create --name "$name" --script "$script" --deliver "$deliver" "$sched" "$prompt"
    else
      doe "$HERMES" cron create --name "$name" --script "$script" --no-agent --deliver "$deliver" "$sched"
    fi
    say "cronjob gemaakt: $name ($sched)"
  }
  job "Vraag van het team" "$(cfg tijden.vragen)" team-questions.py local
  job "Staging klaar" "$(cfg tijden.staging)" staging-announce.py local
  job "Vastgelopen-check" "$(cfg tijden.vastgelopen)" kanban-stuck-check.py local
  job "Discord-opruimcontrole" "$(cfg tijden.opruimcontrole)" discord-cleanup.py local
  if [ -n "$(chan samenvatting)" ]; then
    job "Dagelijkse samenvatting" "$(cfg tijden.samenvatting)" daily-summary-data.py "discord:$(chan samenvatting)" \
      "$("$PYTHON" -c 'import sys; sys.path.insert(0, sys.argv[1]); import flow_config as fc; print(fc.text("samenvatting_prompt"))' "$REPO/scripts")"
  else
    say "LET OP: #samenvatting heeft nog geen ID — draai discord_setup.py en daarna install.sh opnieuw"
  fi
  if [ "$BASIS" = 0 ]; then
    job "Bordbewaking" "$(cfg tijden.bordbewaking)" board-guard.py local
    job "Kanban workspace cleanup" "$(cfg tijden.werkmappen_opruimen)" clean_kanban_workspaces.py local
    if [ -n "$(chan meldingen)" ]; then
      job "Schijfruimte" "$(cfg tijden.schijfruimte)" disk_space_watch.py "discord:$(chan meldingen)"
    else
      say "LET OP: #meldingen heeft nog geen ID — draai discord_setup.py en daarna install.sh opnieuw"
    fi
  fi
else
  say "cron overgeslagen (--geen-cron)"
fi

say "klaar$( [ "$DRY" = 1 ] && echo ' (dry-run: er is niets veranderd)')"
echo "Volgende stappen: zie README.md, 'Zelf installeren' (discord_setup.py, Hermes-config, testlijst)."
