# SPDX-License-Identifier: GPL-3.0-or-later
# fish completion for loqctl. Static: never runs loqctl.
function __loqctl_modes
    # Gamezone's class device first, then any other, then the legacy file (it lists "custom", which the kernel rejects).
    set -l src
    for f in /sys/class/platform-profile/*/choices
        test -r $f; or continue
        set -q src[1]; or set src $f
        set -l n (string replace -r 'choices$' name $f)
        if test -r $n; and test (string trim < $n) = lenovo-wmi-gamezone
            set src $f
            break
        end
    end
    set -q src[1]; or set src /sys/firmware/acpi/platform_profile_choices
    if test -r $src
        string split ' ' -- (string trim < $src)
    else
        printf '%s\n' low-power balanced performance max-power custom
    end
end

# Positional arguments typed so far, counting the subcommand and skipping --socket and its value.
function __loqctl_argn
    set -l n 0
    set -l skip 0
    for t in (commandline -opc)[2..-1]
        if test $skip = 1
            set skip 0
        else if test "$t" = --socket
            set skip 1
        else if not string match -q -- '-*' $t
            set n (math $n + 1)
        end
    end
    echo $n
end

set -l cmds tui status metrics capabilities limits fan mode set battery kb doctor serve
complete -c loqctl -f
complete -c loqctl -l socket -r -F -d 'Connect to an existing loqctl Unix socket'
complete -c loqctl -l version -d 'Show the version'
complete -c loqctl -s h -l help -d 'Show help'
complete -c loqctl -n "not __fish_seen_subcommand_from $cmds" -a "$cmds"
complete -c loqctl -n '__fish_seen_subcommand_from metrics' -l watch -d 'Print one JSON line every SECONDS'
complete -c loqctl -n '__fish_seen_subcommand_from serve' -l uid -r -d 'UID permitted to connect'
complete -c loqctl -n '__fish_seen_subcommand_from battery; and test (__loqctl_argn) -eq 1' -a 'normal conservation rapid'
complete -c loqctl -n '__fish_seen_subcommand_from mode' -a '(__loqctl_modes)'
complete -c loqctl -n '__fish_seen_subcommand_from set; and test (__loqctl_argn) -eq 1' -a 'ppt_pl1_spl ppt_pl2_sppt ppt_cpu_cl ppt_pl1_spl_cl ppt_pl2_sppt_cl ppt_pl3_fppt ppt_pl3_fppt_cl ppt_pl4_ipl ppt_pl4_ipl_cl ppt_pl1_apu_spl ppt_pl1_tau cpu_temp gpu_nv_ctgp gpu_nv_ppab gpu_nv_ac_offset gpu_nv_bpl gpu_nv_cpu_boost gpu_temp'
complete -c loqctl -n '__fish_seen_subcommand_from kb; and test (__loqctl_argn) -eq 1' -a 'status apply off static breath wave hue brightness'
set -l kbfx '__fish_seen_subcommand_from kb; and __fish_seen_subcommand_from static breath hue wave'
complete -c loqctl -n $kbfx -l speed -r -d 'Effect speed'
complete -c loqctl -n $kbfx -l brightness -r -d 'Effect brightness'
complete -c loqctl -n "$kbfx; and __fish_seen_subcommand_from wave" -a 'ltr rtl'
