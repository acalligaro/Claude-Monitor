#!/bin/bash
# Claude Code status line: dossier | branche | modele | contexte | session 5h (% + duree) | semaine 7j (%)
# locale fr_FR attend une virgule decimale: printf '%.0f' echoue sur "12.5" sans ceci
export LC_NUMERIC=C
input=$(cat)

cwd=$(echo "$input" | jq -r '.workspace.current_dir // .cwd // empty')
model=$(echo "$input" | jq -r '.model.display_name // empty')
ctx=$(echo "$input" | jq -r '.context_window.used_percentage // empty')
five=$(echo "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty')
five_reset=$(echo "$input" | jq -r '.rate_limits.five_hour.resets_at // empty')
week=$(echo "$input" | jq -r '.rate_limits.seven_day.used_percentage // empty')

# copie les limites reelles pour ~/claude-monitor (page http://claude.local) ; ecriture atomique, silencieuse
rl=$(echo "$input" | jq -c 'select(.rate_limits) | {at: (now|floor), five_hour: .rate_limits.five_hour, seven_day: .rate_limits.seven_day}' 2>/dev/null)
[ -n "$rl" ] && [ -d ~/claude-monitor ] && printf '%s' "$rl" > ~/claude-monitor/ratelimits.json.tmp && mv ~/claude-monitor/ratelimits.json.tmp ~/claude-monitor/ratelimits.json

dir="${cwd##*/}"
[ -z "$dir" ] && dir="/"

branch=""
if [ -n "$cwd" ] && git -C "$cwd" --no-optional-locks rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  branch=$(git -C "$cwd" --no-optional-locks symbolic-ref --short HEAD 2>/dev/null \
    || git -C "$cwd" --no-optional-locks rev-parse --short HEAD 2>/dev/null)
fi

# couleur selon le pourcentage: vert <50, jaune <80, rouge sinon
color() {
  local p=${1%.*}
  if [ "$p" -ge 80 ]; then printf '\033[31m'
  elif [ "$p" -ge 50 ]; then printf '\033[33m'
  else printf '\033[32m'; fi
}
R='\033[0m'
SEP=' \033[2m|\033[0m '

out=$(printf '\033[34m%s\033[0m' "$dir")
[ -n "$branch" ] && out+=$(printf "$SEP"'\033[35m%s\033[0m' "$branch")
[ -n "$model" ] && out+=$(printf "$SEP"'\033[36m%s\033[0m' "$model")
[ -n "$ctx" ] && out+=$(printf "$SEP"'ctx %b%.0f%%'"$R" "$(color "$ctx")" "$ctx")

if [ -n "$five" ]; then
  dur=""
  if [ -n "$five_reset" ]; then
    now=$(date +%s)
    left=$(( five_reset - now ))
    [ "$left" -lt 0 ] && left=0
    elapsed=$(( 18000 - left ))
    [ "$elapsed" -lt 0 ] && elapsed=0
    reset_at=$(date -r "${five_reset%.*}" +%H:%M 2>/dev/null || date -d "@${five_reset%.*}" +%H:%M 2>/dev/null)
    dur=$(printf ' %dh%02d (reset dans %dh%02d, a %s)' $((elapsed/3600)) $((elapsed%3600/60)) $((left/3600)) $((left%3600/60)) "$reset_at")
  fi
  out+=$(printf "$SEP"'5h %b%.0f%%'"$R"'%s' "$(color "$five")" "$five" "$dur")
fi
[ -n "$week" ] && out+=$(printf "$SEP"'7j %b%.0f%%'"$R" "$(color "$week")" "$week")

printf '%s' "$out"
