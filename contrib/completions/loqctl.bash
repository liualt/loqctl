# SPDX-License-Identifier: GPL-3.0-or-later
# bash completion for loqctl. Static: never runs loqctl.
_loqctl() {
    local cur=${COMP_WORDS[COMP_CWORD]} prev=${COMP_WORDS[COMP_CWORD-1]} cmd="" i
    for ((i = 1; i < COMP_CWORD; i++)); do
        case ${COMP_WORDS[i]} in
            --socket) ((i++)) ;;
            -*) ;;
            *) cmd=${COMP_WORDS[i]}; break ;;
        esac
    done
    if [[ $prev == --socket ]]; then
        COMPREPLY=($(compgen -f -- "$cur"))
        return
    fi
    case $cmd in
        "") COMPREPLY=($(compgen -W "tui status metrics capabilities limits fan mode set battery toggle profile kb doctor serve --socket --version -h --help" -- "$cur")) ;;
        metrics) COMPREPLY=($(compgen -W "--watch -h" -- "$cur")) ;;
        serve) COMPREPLY=($(compgen -W "--uid -h" -- "$cur")) ;;
        battery) [[ $COMP_CWORD -eq $((i + 1)) ]] && COMPREPLY=($(compgen -W "normal conservation rapid" -- "$cur")) ;;
        toggle) [[ $COMP_CWORD -eq $((i + 1)) ]] && COMPREPLY=($(compgen -W "fn_lock camera winkey logo_light port_light" -- "$cur")) ;;
        profile) [[ $COMP_CWORD -eq $((i + 1)) ]] && COMPREPLY=($(compgen -W "list save apply" -- "$cur")) ;;
        mode)
            local modes="low-power balanced performance max-power custom" f src=""
            # Gamezone's class device first, then any other, then the legacy file (it lists "custom", which the kernel rejects).
            for f in /sys/class/platform-profile/*/choices; do
                [[ -r $f && -e ${f%choices}profile ]] || continue
                [[ -z $src ]] && src=$f
                [[ -r ${f%choices}name && $(< "${f%choices}name") == lenovo-wmi-gamezone ]] && { src=$f; break; }
            done
            [[ -z $src ]] && src=/sys/firmware/acpi/platform_profile_choices
            [[ -r $src ]] && modes=$(< "$src")
            COMPREPLY=($(compgen -W "$modes" -- "$cur")) ;;
        set)
            [[ $COMP_CWORD -eq $((i + 1)) ]] && COMPREPLY=($(compgen -W "ppt_pl1_spl ppt_pl2_sppt ppt_cpu_cl ppt_pl1_spl_cl ppt_pl2_sppt_cl ppt_pl3_fppt ppt_pl3_fppt_cl ppt_pl4_ipl ppt_pl4_ipl_cl ppt_pl1_apu_spl ppt_pl1_tau cpu_temp gpu_nv_ctgp gpu_nv_ppab gpu_nv_ac_offset gpu_nv_bpl gpu_nv_cpu_boost gpu_temp" -- "$cur")) ;;
        kb)
            local act=${COMP_WORDS[i+1]}
            if [[ $COMP_CWORD -eq $((i + 1)) ]]; then
                COMPREPLY=($(compgen -W "status apply off static breath wave hue brightness" -- "$cur"))
            elif [[ $prev != --speed && $prev != --brightness ]]; then
                case $act in
                    static|breath|hue) COMPREPLY=($(compgen -W "--speed --brightness" -- "$cur")) ;;
                    wave) COMPREPLY=($(compgen -W "ltr rtl --speed --brightness" -- "$cur")) ;;
                esac
            fi ;;
    esac
}
complete -F _loqctl loqctl
