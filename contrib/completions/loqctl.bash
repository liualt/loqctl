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
        "") COMPREPLY=($(compgen -W "tui status metrics capabilities limits fan mode set kb doctor serve --socket --version -h --help" -- "$cur")) ;;
        metrics) COMPREPLY=($(compgen -W "--watch -h" -- "$cur")) ;;
        serve) COMPREPLY=($(compgen -W "--uid -h" -- "$cur")) ;;
        mode)
            local modes="low-power balanced performance max-power custom"
            [[ -r /sys/firmware/acpi/platform_profile_choices ]] && modes=$(< /sys/firmware/acpi/platform_profile_choices)
            COMPREPLY=($(compgen -W "$modes" -- "$cur")) ;;
        set)
            [[ $COMP_CWORD -eq $((i + 1)) ]] && COMPREPLY=($(compgen -W "ppt_pl1_spl ppt_pl2_sppt ppt_cpu_cl ppt_pl1_spl_cl ppt_pl2_sppt_cl ppt_pl3_fppt ppt_pl3_fppt_cl ppt_pl4_ipl ppt_pl4_ipl_cl ppt_pl1_apu_spl ppt_pl1_tau cpu_temp gpu_nv_ctgp gpu_nv_ppab gpu_nv_ac_offset gpu_nv_bpl gpu_nv_cpu_boost gpu_temp" -- "$cur")) ;;
        kb)
            [[ $COMP_CWORD -eq $((i + 1)) ]] && COMPREPLY=($(compgen -W "status apply off static breath wave hue brightness" -- "$cur")) ;;
    esac
}
complete -F _loqctl loqctl
