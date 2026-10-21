#!/bin/bash
# The look of make install and make dev (bash 3.2: macOS's own). Install's steps send their output to a log; the
# terminal shows a spinner, then one coloured line per step, and on failure the log's last lines.
#
#   stage.sh begin LOG TITLE                     header; starts LOG afresh
#   stage.sh run LOG N/T NAME [opts] -- COMMAND   one step, COMMAND run by bash
#       --note       show COMMAND's last output line after the tick
#       --soft       a failure only warns (with that last line) and make goes on
#       --live       run it in the terminal, indented (it asks something); still logged
#       --hint TEXT  shown while it runs
#   stage.sh end LOG TITLE [COMMAND WHAT]...      footer with the total time, then the next commands
#   stage.sh header TITLE [LINE]...               just the header (make dev)
#   stage.sh say TEXT                             one highlighted line (make dev saying it runs install first)
#   stage.sh open URL [--no-open]                 wait for URL to answer, then open it in the browser (make dev);
#                                                 only says where it is over SSH, without a desktop or with --no-open
#
# 256 colours where the terminal has them, the basic eight otherwise, none when not a terminal or NO_COLOR is set.

[ -t 1 ] && [ -z "$NO_COLOR" ] && tty=1 || tty=
cols=$(tput cols 2>/dev/null || echo 80)
if [ -n "$tty" ]; then
	off=$'\033[0m' bold=$'\033[1m' dim=$'\033[2m'
	if [ "$(tput colors 2>/dev/null || echo 8)" -ge 256 ]; then
		fg() { printf '\033[38;5;%sm' $1; }
		bg() { printf '\033[48;5;%sm\033[38;5;16m' $1; }
		# Cyan through blue and violet to pink
		ramp=(51 45 39 33 63 99 135 171 207 213)
		green=42 red=203 amber=214 sky=117 pale=250 grey=244
	else
		fg() { printf '\033[%sm' $((30 + $1)); }
		bg() { printf '\033[%sm\033[30m' $((40 + $1)); }
		ramp=(6 6 6 4 4 4 5 5 5 5)
		green=2 red=1 amber=3 sky=6 pale=7 grey=7
	fi
else
	fg() { :; }
	bg() { :; }
	ramp=(0 0 0 0 0 0 0 0 0 0)
fi

took() {  # seconds -> 1m 15s
	[ $1 -lt 60 ] && echo "${1}s" || echo "$(($1 / 60))m $(($1 % 60))s"
}

short() {  # a path, as ~/... under home
	case $1 in "$HOME"/*) echo "~${1#"$HOME"}" ;; *) echo "$1" ;; esac
}

last_line() {  # the log's last line that says something: no colours, progress-bar redraws or make's own errors
	tail -c 4000 "$1" | tr '\r' '\n' | sed -e $'s/\033\\[[0-9;]*[A-Za-z]//g' -e 's/^[[:space:]]*//' -e 's/^✓ *//' |
		grep -vE '^$|^make(\[[0-9]+\])?: \*\*\*' | tail -1
}

progress() {  # LOG BYTES: what the step is doing now, from the log past its first BYTES: "PERCENT<tab>WHAT",
	# PERCENT empty when it gives none. curl -# and tqdm (Hugging Face) draw bars with a percent; anything else is a line.
	tail -c +$(($2 + 1)) "$1" | tail -c 3000 | tr '\r' '\n' | sed $'s/\033\\[[0-9;]*[A-Za-z]//g' | awk '
		/^[[:space:]]*$/ || /^[#=O -]+$/ || /^make(\[[0-9]+\])?: / || /^── \[/ { next }
		{ sub(/^[[:space:]]+/, ""); sub(/[[:space:]]+$/, "") }
		match($0, /[0-9]+(\.[0-9]+)?%/) {
			pct = substr($0, RSTART, RLENGTH - 1); name = ""
			if (match($0, /^[^:|#%]+: /)) name = substr($0, 1, RLENGTH - 2)
			last = "bar"; next
		}
		{ text = $0; last = "line" }
		END { if (last == "bar") print pct "\t" (name != "" ? name : text); else print "\t" text }'
}

bar() {  # PERCENT WIDTH: a bar in the step's colour
	local full=$((${1%.*} * $2 / 100)) i=0 out=$accent
	while [ $i -lt $2 ]; do
		[ $i -eq $full ] && out="$out$(fg $grey)"
		out="${out}━"
		i=$((i + 1))
	done
	printf '%s%s %s%3s%%%s' "$out" "$off" "$(fg $pale)" "${1%.*}" "$off"
}

fit() {  # cut $2 to $1 characters (nothing on a very narrow terminal)
	if [ $1 -lt 8 ]; then echo; elif [ ${#2} -le $1 ]; then echo "$2"; else echo "${2:0:$(($1 - 1))}…"; fi
}

rule() {  # a line across, shading through the ramp
	local width=$((cols - 4)) i=0
	[ $width -gt 64 ] && width=64
	printf '  '
	while [ $i -lt $width ]; do
		printf '%s━' "$(fg ${ramp[i * 10 / width]})"
		i=$((i + 1))
	done
	printf '%s\n' "$off"
}

header() {  # TITLE [LINE]...: each ● in a line takes the next colour along the ramp, and links stand out
	printf '\n  %s◆%s %s%s%s\n' "$(fg ${ramp[8]})" "$off" "$bold" "$1" "$off"
	rule
	shift
	local line out dot=0 text=$(fg $pale) link="$bold$(fg $sky)"
	for line in "$@"; do
		out=
		while [ "${line#*●}" != "$line" ]; do
			out="$out${line%%●*}$(fg ${ramp[dot * 3 % 10]})●$off$text"
			line=${line#*●}
			dot=$((dot + 1))
		done
		printf '  %s%s%s\n' "$text" "$(printf '%s' "$out$line" | sed "s|\(https*://[^ ]*\)|$link\1$off$text|g")" "$off"
	done
	echo
}

indent() {  # stdin to stdout behind a coloured bar; a partial line (a question waiting for input) shows at once
	local c fresh=1
	while IFS= read -r -d '' -n 1 c; do
		[ -n "$fresh" ] && printf '         %s│%s ' "$accent" "$off"
		printf '%s' "$c"
		[ "$c" = $'\n' ] && fresh=1 || fresh=
	done
}

case $1 in
begin)
	mkdir -p "$(dirname "$2")"
	echo "# $(date +%s) $3" >"$2"
	header "$3" "$(printf '%slog%s %s' "$(fg $grey)" "$(fg $pale)" "$(short "$2")")"
	;;

header)
	shift
	header "$@"
	;;

say)
	printf '\n  %s▸%s %s%s%s\n' "$(fg $amber)" "$off" "$bold" "$2" "$off"
	;;

end)
	log=$2 title=$3
	shift 3
	start=$(head -1 "$log" 2>/dev/null | cut -d' ' -f2)
	echo
	rule
	printf '  %s ✓ %s %s%s%s%s' "$(bg $green)" "$off" "$bold" "$(fg $green)" "$title" "$off"
	[ -n "$start" ] && printf '  %sin %s%s' "$(fg $grey)" "$(took $(($(date +%s) - start)))" "$off"
	echo
	if [ $# -gt 0 ]; then
		printf '\n  %sNext%s\n' "$bold" "$off"
		while [ $# -ge 2 ]; do
			printf '  %s›%s %s%s%-22s%s %s%s%s\n' "$(fg ${ramp[8]})" "$off" "$bold" "$(fg $sky)" "$1" "$off" \
				"$(fg $pale)" "$2" "$off"
			shift 2
		done
	fi
	echo
	;;

open)
	url=$2
	# Up to 5 minutes: the first make web builds the web app before it serves
	for i in $(seq 600); do
		curl -s -o /dev/null --noproxy '*' --max-time 1 "$url" && up=1 && break
		sleep 0.5
	done
	[ -n "$up" ] || exit 0
	if [ "$3" = --no-open ] || [ -n "$SSH_CONNECTION" ]; then
		how=
	elif [ "$(uname -s)" = Darwin ]; then
		how=open
	elif command -v wslview >/dev/null; then
		how=wslview
	elif [ -n "$DISPLAY$WAYLAND_DISPLAY" ] && command -v xdg-open >/dev/null; then
		how=xdg-open
	fi
	[ -n "$how" ] && "$how" "$url" >/dev/null 2>&1 || how=
	printf '\n  %s READY %s %sDashboard%s  %s%s%s%s  %s%s%s\n\n' "$(bg $green)" "$off" "$bold" "$off" \
		"$bold" "$(fg $sky)" "$url" "$off" "$(fg $grey)" "${how:+opened in your browser}" "$off"
	;;

run)
	log=$2 step=$3 name=$4 note= soft= live= hint=
	mkdir -p "$(dirname "$log")"
	shift 4
	while [ $# -gt 0 ] && [ "$1" != -- ]; do
		case $1 in
		--note) note=1 ;; --soft) soft=1 ;; --live) live=1 ;; --hint) hint=$2; shift ;;
		esac
		shift
	done
	shift
	# Each step its own colour along the ramp: 1/7 cyan ... 7/7 pink
	n=${step%/*} total=${step#*/}
	[ "$total" -gt 1 ] && shade=${ramp[(n - 1) * 9 / (total - 1)]} || shade=${ramp[0]}
	accent=$(fg $shade)
	label=$(printf '  %s %s %s  %s%-20s%s' "$(bg $shade)" "$step" "$off" "$bold" "$name" "$off")
	printf '\n── [%s] %s\n' "$step" "$name" >>"$log"
	from=$(wc -l <"$log") bytes=$(wc -c <"$log")
	SECONDS=0

	if [ -n "$live" ]; then
		printf '%s%s\n' "$label" "${hint:+  $(fg $grey)$hint$off}"
		if [ -n "$tty" ]; then
			# Unbuffered, so Python's lines and its question arrive as they're written
			PYTHONUNBUFFERED=1 bash -c "$*" 2>&1 | tee -a "$log" | indent
			rc=${PIPESTATUS[0]}
		else
			bash -c "$*" 2>&1 | tee -a "$log"
			rc=${PIPESTATUS[0]}
		fi
	elif [ -n "$tty" ]; then
		(
			trap exit TERM
			frames=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
			room=$((cols - 37)) i=0 pct= what=$hint
			while :; do
				# What it's doing, read from the log three times a second; until it says anything, the hint
				if [ $((i % 3)) -eq 0 ]; then
					now=$(progress "$log" $bytes)
					pct=${now%%$'\t'*} now=${now#*$'\t'}
					[ -n "$now" ] && what=$now
				fi
				elapsed=$(took $SECONDS)
				avail=$((room - ${#elapsed} - 2))
				if [ -n "$pct" ]; then
					# The name first (without "Downloading ": the bar says so); the bar takes what's left, up to 20
					# wide, and below 6 only the percent shows
					item=${what#Downloading }
					width=$((avail - ${#item} - 9))
					[ $width -gt 20 ] && width=20
					if [ $width -ge 6 ]; then
						shown="$(fg $pale)$(fit $((avail - width - 9)) "$item")$off  $(bar "$pct" $width)"
					else
						shown="$(fg $pale)$(fit $((avail - 6)) "$item")  $(printf '%3s%%' "${pct%.*}")$off"
					fi
				else
					shown="$(fg $pale)$(fit $avail "$what")$off"
				fi
				printf '\r\033[2K%s %s%s%s %s%s%s  %s' "$label" "$accent" "${frames[i % 10]}" "$off" \
					"$(fg $grey)" "$elapsed" "$off" "$shown"
				i=$((i + 1))
				sleep 0.1
			done
		) &
		spinner=$!
		trap 'kill $spinner 2>/dev/null; wait $spinner 2>/dev/null
			printf "\r\033[2K%s %s✗ stopped%s\n" "$label" "$(fg $red)" "$off"; exit 130' INT TERM
		bash -c "$*" >>"$log" 2>&1 </dev/null
		rc=$?
		kill $spinner 2>/dev/null
		wait $spinner 2>/dev/null
		trap - INT TERM
		printf '\r\033[2K'
	else
		bash -c "$*" >>"$log" 2>&1 </dev/null
		rc=$?
	fi

	time=$(took $SECONDS)
	if [ $rc -eq 0 ]; then
		extra=
		[ -n "$note" ] && extra=$(last_line "$log")
		printf '%s %s✓%s %s%-7s%s' "$label" "$bold$(fg $green)" "$off" "$(fg $grey)" "$time" "$off"
		[ -n "$extra" ] && printf ' %s%s%s' "$(fg $sky)" "$(fit $((cols - 44)) "$extra")" "$off"
		echo
		exit 0
	fi
	if [ -n "$soft" ]; then
		printf '%s %s!%s %s%-7s%s %s%s%s\n' "$label" "$bold$(fg $amber)" "$off" "$(fg $grey)" "$time" "$off" \
			"$(fg $amber)" "$(fit $((cols - 44)) "$(last_line "$log")")" "$off"
		exit 0
	fi
	printf '%s %s✗ failed%s %safter %s%s\n\n' "$label" "$bold$(fg $red)" "$off" "$(fg $grey)" "$time" "$off"
	if [ -z "$live" ]; then
		tail -n +$((from + 1)) "$log" | tr '\r' '\n' |
			sed $'s/\033\\[[0-9;]*[A-Za-z]//g' | grep -v '^[[:space:]]*$' | tail -15 |
			while IFS= read -r line; do
				printf '         %s┃%s %s%s%s\n' "$(fg $red)" "$off" "$(fg $pale)" "$(fit $((cols - 12)) "$line")" "$off"
			done
		echo
	fi
	printf '  %s FAILED %s %sFull log%s %s\n' "$(bg $red)" "$off" "$bold" "$off" "$(short "$log")"
	printf '           Fix that, then run %s%smake install%s again: finished steps are quick the second time.\n\n' \
		"$bold" "$(fg $sky)" "$off"
	exit $rc
	;;
esac
