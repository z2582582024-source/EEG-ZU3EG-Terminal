# Native development and application paths. No acquisition/stimulus side effects.
export PATH=/opt/eeg/current/bin:/opt/eeg-qt/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export TMPDIR=/var/tmp/eeg-build
export EEG_RECORDING_DIR=/var/lib/eeg-operator/recordings
export QT_QPA_FONTDIR=/usr/share/fonts
export LD_LIBRARY_PATH=/opt/eeg/current/lib:/opt/eeg-qt/lib:/usr/lib:/lib
eeg_prompt() {
    _eeg_pwd=$(pwd -P 2>/dev/null || printf '?')
    [ "$_eeg_pwd" != /root ] || _eeg_pwd='~'
    _eeg_pwd=$(printf '%s' "$_eeg_pwd" | tr -d '\001-\037\177')
    if [ "$(id -u)" = 0 ]; then _eeg_mark='#'; else _eeg_mark='$'; fi
    printf 'EEG-ZU3EG:%s%s ' "$_eeg_pwd" "$_eeg_mark"
}
# Bash and BusyBox ash both expand this command substitution before each prompt.
PS1='$(eeg_prompt)'
export PS1
umask 022
