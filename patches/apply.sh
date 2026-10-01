#!/usr/bin/env bash
# Apply the Hermes Discord-flow patches to a clean Hermes checkout (tag v2026.9.21, Hermes 0.21.4).
#
# Usage: apply.sh <hermes-checkout> [--met-aanbevolen] [--met-optioneel] [--dry-run] [--force]
#
#   required/     always applied (the Discord question flow needs all of them)
#   recommended/  with --met-aanbevolen
#   optional/     with --met-optioneel
#   --dry-run     apply everything in a temporary worktree, report, and throw it away
#   --force       continue although HEAD is not tag v2026.9.21
set -euo pipefail

BASE_TAG="v2026.9.21"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<'USAGE'
Past de Hermes-patches voor de Discord-vragenflow toe op een schone Hermes-checkout (tag v2026.9.21).

Gebruik: apply.sh <hermes-checkout> [--met-aanbevolen] [--met-optioneel] [--dry-run] [--force]

  required/          wordt altijd toegepast (de vragenflow heeft ze allemaal nodig)
  recommended/       alleen met --met-aanbevolen
  optional/          alleen met --met-optioneel
  --dry-run          alles proberen in een tijdelijke worktree; je checkout blijft ongewijzigd
  --force            toch doorgaan als HEAD niet tag v2026.9.21 is
USAGE
}

checkout=""
with_recommended=0
with_optional=0
dry_run=0
force=0
for arg in "$@"; do
  case "$arg" in
    --met-aanbevolen) with_recommended=1 ;;
    --met-optioneel) with_optional=1 ;;
    --dry-run) dry_run=1 ;;
    --force) force=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "Onbekende optie: $arg" >&2; usage >&2; exit 2 ;;
    *)
      if [[ -n "$checkout" ]]; then
        echo "Geef maar één Hermes-checkout op." >&2; exit 2
      fi
      checkout="$arg" ;;
  esac
done
if [[ -z "$checkout" ]]; then
  usage >&2
  exit 2
fi

if ! git -C "$checkout" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Geen git-checkout: $checkout" >&2
  exit 2
fi
checkout="$(git -C "$checkout" rev-parse --show-toplevel)"

# --- checks -----------------------------------------------------------------------------------
gitdir="$(git -C "$checkout" rev-parse --git-dir)"
case "$gitdir" in /*) ;; *) gitdir="$checkout/$gitdir" ;; esac
if [[ -d "$gitdir/rebase-apply" || -d "$gitdir/rebase-merge" ]]; then
  echo "Er loopt nog een 'git am' of rebase in $checkout. Rond die eerst af (of: git am --abort)." >&2
  exit 1
fi
if [[ -n "$(git -C "$checkout" status --porcelain --untracked-files=no)" ]]; then
  echo "De checkout heeft niet-gecommitte wijzigingen. Commit of stash die eerst:" >&2
  git -C "$checkout" status --short --untracked-files=no >&2
  exit 1
fi
head="$(git -C "$checkout" rev-parse HEAD)"
base="$(git -C "$checkout" rev-parse -q --verify "refs/tags/$BASE_TAG^{commit}" 2>/dev/null || true)"
if [[ "$head" != "$base" ]]; then
  if [[ -z "$base" ]]; then
    echo "Let op: tag $BASE_TAG bestaat niet in deze checkout (git fetch --tags?)." >&2
  else
    echo "Let op: HEAD ($head) is niet tag $BASE_TAG ($base)." >&2
  fi
  if [[ "$force" -ne 1 ]]; then
    echo "De patches zijn gemaakt en getest op $BASE_TAG. Toch doorgaan? Draai opnieuw met --force." >&2
    exit 2
  fi
  echo "--force: ik ga toch door." >&2
fi

# --- patch list -------------------------------------------------------------------------------
dirs=(required)
[[ "$with_recommended" -eq 1 ]] && dirs+=(recommended)
[[ "$with_optional" -eq 1 ]] && dirs+=(optional)
patches=()
for d in "${dirs[@]}"; do
  found=0
  while IFS= read -r p; do
    patches+=("$p"); found=1
  done < <(find "$HERE/$d" -maxdepth 1 -name '*.patch' -type f 2>/dev/null | LC_ALL=C sort)
  if [[ "$found" -eq 0 ]]; then
    echo "($d/: geen patches, overgeslagen)"
  fi
done
if [[ "${#patches[@]}" -eq 0 ]]; then
  echo "Geen patches gevonden in ${dirs[*]}." >&2
  exit 1
fi

# git am needs a committer identity; fall back to a neutral one when none is configured.
ident=()
if [[ -z "$(git -C "$checkout" config user.email || true)" ]]; then
  ident=(-c "user.name=Hermes Discord Flow" -c "user.email=noreply@example.invalid")
fi

target="$checkout"
tmp=""
cleanup() {
  if [[ -n "$tmp" ]]; then
    git -C "$checkout" worktree remove --force "$tmp" >/dev/null 2>&1 || true
    rm -rf "$tmp"
  fi
}
trap cleanup EXIT
if [[ "$dry_run" -eq 1 ]]; then
  tmp="$(mktemp -d)"
  git -C "$checkout" worktree add --quiet --detach "$tmp/wt" HEAD
  tmp="$tmp/wt"
  target="$tmp"
  echo "Proefrun in een tijdelijke worktree; je checkout blijft ongewijzigd."
fi

# --- apply --------------------------------------------------------------------------------------
log="$(mktemp)"
n=0
for p in "${patches[@]}"; do
  n=$((n + 1))
  rel="${p#"$HERE"/}"
  if ! git ${ident[@]+"${ident[@]}"} -C "$target" am --3way --quiet "$p" >"$log" 2>&1; then
    conflicts="$(git -C "$target" diff --name-only --diff-filter=U 2>/dev/null | paste -sd, - | sed 's/,/, /g' || true)"
    git ${ident[@]+"${ident[@]}"} -C "$target" am --abort >/dev/null 2>&1 || true
    echo
    echo "MISLUKT bij patch $n van ${#patches[@]}: $rel"
    echo "De checkout staat weer zoals vóór deze patch (git am --abort)."
    if [[ "$n" -gt 1 ]]; then
      echo "de $((n - 1)) eerdere patch(es) uit deze run zijn wel toegepast$( [[ "$dry_run" -eq 1 ]] && echo ' (alleen in de proefrun)')."
      echo "Terug naar de beginstand: git -C \"$target\" reset --hard $head"
    fi
    echo
    echo "Stuur dit terug:"
    echo "  - de naam van de patch: $rel"
    echo "  - HEAD vóór de run: $head (tag $BASE_TAG: ${base:-ontbreekt})"
    echo "  - git-versie: $(git --version)"
    echo "  - bestanden met een conflict: ${conflicts:-geen (zie de uitvoer hieronder)}"
    echo "  - de uitvoer van git am:"
    sed 's/^/      /' "$log"
    rm -f "$log"
    exit 1
  fi
  echo "ok  $rel"
done
rm -f "$log"
echo
if [[ "$dry_run" -eq 1 ]]; then
  echo "Proefrun geslaagd: alle ${#patches[@]} patches passen op $head."
else
  echo "Klaar: ${#patches[@]} patches toegepast op $checkout."
  echo "Draai nu de tests uit TESTS.txt."
fi
