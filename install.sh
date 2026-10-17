#!/bin/sh
IS_MIPS=0
if [ "$(uname -m)" = "mips" ]; then
   IS_MIPS=1
fi

KLIPPER_HOME="${HOME}/klipper"
KLIPPER_ENV="${HOME}/klippy-env"
KLIPPER_CONFIG_HOME="${HOME}/printer_data/config"
MOONRAKER_CONFIG_DIR="${HOME}/printer_data/config"
SRCDIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
KLIPPERSCREEN_HOME="${KLIPPERSCREEN_HOME:-}"


if [ "$IS_MIPS" -eq 1 ]; then
    KLIPPER_HOME="/usr/share/klipper"
    KLIPPER_ENV="/usr/share/klippy-env"
    KLIPPER_CONFIG_HOME="/usr/data/printer_data/config"
    MOONRAKER_CONFIG_DIR="/usr/data/printer_data/config"
fi

usage(){ echo "Usage: $0 [-u]" 1>&2; exit 1; }
# Parse command line arguments
UNINSTALL=0
while getopts "uh" arg; do
   case $arg in
       u) UNINSTALL=1;;
       h) usage;;
   esac
done

verify_ready()
{
  if [ "$IS_MIPS" -ne 1 ]; then
    if [ "$(id -u)" -eq 0 ]; then
        echo "[ERROR] This script must not run as root. Exiting."
        exit 1
    fi
  else
    echo -e "[WARNING] This script is running on a MIPS system, so we expect it to be run as root"
  fi
}

check_folders()
{
    if [ ! -d "$KLIPPER_HOME/klippy/extras/" ]; then
        echo "[ERROR] Klipper installation not found in directory \"$KLIPPER_HOME\". Exiting"
        exit 1
    fi
    echo "Klipper installation found at $KLIPPER_HOME"

    if [ ! -d "${KLIPPER_CONFIG_HOME}/" ]; then
        echo "[ERROR] Klipper configs not found in directory \"$MOONRAKER_CONFIG_DIR\". Exiting"
        exit 1
    fi
    echo "Klipper installation found at $KLIPPER_CONFIG_HOME"

    if [ ! -f "${MOONRAKER_CONFIG_DIR}/moonraker.conf" ]; then
        echo "[ERROR] Moonraker configuration not found in directory \"$MOONRAKER_CONFIG_DIR\". Exiting"
        exit 1
    fi
    echo "Moonraker configuration found at $MOONRAKER_CONFIG_DIR"
}

link_extension()
{
    echo -n "Linking extension to Klipper... "
    for extension in "${SRCDIR}"/extras/*.py; do
        [ -f "$extension" ] || continue
        extension_name=${extension##*/}
        # The screen panel belongs in KlipperScreen, not Klipper extras.
        [ "$extension_name" = acepro.py ] && continue
        ln -sfn "$extension" "${KLIPPER_HOME}/klippy/extras/${extension_name}" || return 1
    done
    echo "[OK]"
}

copy_config()
{
  echo "[WARNING] If you have custom [save_variables], place the ace_vars.cfg data in your vars file and comment out [save_variables] in ace.cfg."
  if [ -e "${KLIPPER_CONFIG_HOME}/ace.cfg" ] || [ -L "${KLIPPER_CONFIG_HOME}/ace.cfg" ]; then
      echo "[SKIPPED] Existing ace.cfg preserved"
  else
      config_temporary=$(mktemp "${KLIPPER_CONFIG_HOME}/.acecfg.XXXXXX") || return 1
      config_path_escaped=$(printf '%s\n' "$KLIPPER_CONFIG_HOME" | sed 's/[\\&|]/\\&/g') || return 1
      if ! sed "s|{config_path}|${config_path_escaped}|g" "${SRCDIR}/ace.cfg" > "$config_temporary" || \
         ! chmod 644 "$config_temporary" || \
         ! mv -f "$config_temporary" "${KLIPPER_CONFIG_HOME}/ace.cfg"; then
          rm -f "$config_temporary"
          return 1
      fi
      echo "[OK] Created ace.cfg"
  fi
  if [ -e "${KLIPPER_CONFIG_HOME}/ace_vars.cfg" ] || [ -L "${KLIPPER_CONFIG_HOME}/ace_vars.cfg" ]; then
      echo "[SKIPPED] Existing ace_vars.cfg preserved"
  else
      cp "${SRCDIR}/ace_vars.cfg" "${KLIPPER_CONFIG_HOME}/ace_vars.cfg" || return 1
      echo "[OK] Created ace_vars.cfg"
  fi
}

has_config_section()
{
    [ -f "$1" ] || return 1
    awk -v expected="$2" '
        /^[ \t]*\[[^]]+\]/ {
            name = $0
            sub(/^[ \t]*\[/, "", name)
            sub(/\].*$/, "", name)
            gsub(/^[ \t]+|[ \t\r]+$/, "", name)
            gsub(/[ \t]+/, " ", name)
            if (name == expected) found = 1
        }
        END { exit !found }
    ' "$1"
}

append_config_section()
{
    section_file=$1
    section_template=$2
    section_name=$3
    if has_config_section "$section_file" "$section_name"; then
        echo "[SKIPPED] Existing [$section_name] preserved"
        return 0
    fi
    [ -r "$section_template" ] || return 1
    if ! has_config_section "$section_template" "$section_name"; then
        echo "[ERROR] Template is missing [$section_name]: $section_template"
        return 1
    fi
    section_backup="${section_file}.bak"
    # Preserve a user's config symlink when adding the section to its target.
    if [ -L "$section_file" ]; then
        section_file=$(readlink -f "$section_file") || return 1
        [ -f "$section_file" ] || return 1
    fi
    section_existing="$section_file"
    section_temporary=$(mktemp "${section_file%/*}/.acescreen.XXXXXX") || return 1
    if [ -f "$section_file" ]; then
        if ! cp -p "$section_file" "$section_temporary"; then
            rm -f "$section_temporary"
            return 1
        fi
    else
        section_existing=/dev/null
        chmod 644 "$section_temporary" || { rm -f "$section_temporary"; return 1; }
    fi
    # Insert before KlipperScreen generated settings, or append at EOF.
    if ! awk '
        FNR == NR { template = template $0 "\n"; next }
        function insert_section() {
            if (!inserted) {
                if (has_content) printf "\n"
                printf "%s", template
                inserted = 1
            }
        }
        /^#~# --- Do not edit below this line[.]/ { insert_section() }
        { print; has_content = 1 }
        END { insert_section() }
    ' "$section_template" "$section_existing" > "$section_temporary"; then
        rm -f "$section_temporary"
        return 1
    fi
    if [ -f "$section_file" ]; then
        if ! cp -p "$section_file" "$section_backup"; then
            rm -f "$section_temporary"
            return 1
        fi
    fi
    mv -f "$section_temporary" "$section_file" || { rm -f "$section_temporary"; return 1; }
    echo "[OK] Added [$section_name]"
}

find_klipperscreen()
{
    if [ -z "$KLIPPERSCREEN_HOME" ]; then
        for screen_candidate in "${HOME}/KlipperScreen" /usr/share/KlipperScreen /usr/data/KlipperScreen; do
            if [ -d "${screen_candidate}/panels" ]; then
                KLIPPERSCREEN_HOME="$screen_candidate"
                break
            fi
        done
    fi
    if [ -z "$KLIPPERSCREEN_HOME" ] || [ ! -d "${KLIPPERSCREEN_HOME}/panels" ]; then
        echo "[ERROR] KlipperScreen not found. Set KLIPPERSCREEN_HOME to its installation directory."
        return 1
    fi
}

install_screen_config()
{
    find_klipperscreen || return 1
    if [ ! -r "${SRCDIR}/templates/acepro.css" ]; then
        echo "[ERROR] Missing panel stylesheet: ${SRCDIR}/templates/acepro.css"
        return 1
    fi
    screen_panel="${KLIPPERSCREEN_HOME}/panels/acepro.py"
    if [ -f "$screen_panel" ] && [ ! -L "$screen_panel" ]; then
        cp -p "$screen_panel" "${screen_panel}.bak" || return 1
    fi
    ln -sfn "${SRCDIR}/extras/acepro.py" "$screen_panel" || return 1

    screen_macros="${KLIPPER_CONFIG_HOME}/ace_screen.cfg"
    if [ -f "$screen_macros" ]; then
        cp -p "$screen_macros" "${screen_macros}.bak" || return 1
    fi
    screen_temporary=$(mktemp "${KLIPPER_CONFIG_HOME}/.acescreen.XXXXXX") || return 1
    if ! cp -p "${SRCDIR}/ace_screen.cfg" "$screen_temporary" || \
       ! mv -f "$screen_temporary" "$screen_macros"; then
        rm -f "$screen_temporary"
        return 1
    fi
    echo "[OK] Replaced ace_screen.cfg; previous file saved as ace_screen.cfg.bak if present."

    screen_menu="${KLIPPER_CONFIG_HOME}/KlipperScreen.conf"
    if [ ! -f "$screen_menu" ] && [ -f "${KLIPPER_CONFIG_HOME}/klipperscreen.conf" ]; then
        screen_menu="${KLIPPER_CONFIG_HOME}/klipperscreen.conf"
    fi
    append_config_section "$screen_menu" "${SRCDIR}/templates/KlipperScreen.txt" \
        'menu __main acescreen' || return 1
    echo "Include ace_screen.cfg AFTER ace.cfg in printer.cfg."
}

install_requirements()
{
    echo -n "Install requirements... "
    "${KLIPPER_ENV}/bin/pip" install -r "${SRCDIR}/requirements.txt"
    echo "[OK]"
}

uninstall()
{
    if [ -f "${KLIPPER_HOME}/klippy/extras/ace.py" ]; then
        echo -n "Uninstalling... "
        rm -f "${KLIPPER_HOME}/klippy/extras/ace.py"
        rm -f "${KLIPPER_HOME}/klippy/extras/ace_protocol.py"
        rm -f "${KLIPPER_HOME}/klippy/extras/ace_screen.py"
        if find_klipperscreen && [ -L "${KLIPPERSCREEN_HOME}/panels/acepro.py" ]; then
            if grep -Fq 'Standalone KlipperScreen panel; installed as panels/acepro.py.' "${KLIPPERSCREEN_HOME}/panels/acepro.py"; then
                rm -f "${KLIPPERSCREEN_HOME}/panels/acepro.py"
            fi
        fi
        echo "[OK]"
        echo "Remove the [update_manager AceScreen] section from moonraker.conf and [include ace_screen.cfg] / [include ace.cfg] from printer.cfg."
    else
        echo "ace.py not found in \"${KLIPPER_HOME}/klippy/extras/\". Is it installed?"
        echo "[FAILED]"
    fi
}

restart_moonraker()
{
    echo -n "Restarting Moonraker... "
    sudo systemctl restart moonraker
    sleep 1
    echo "[OK]"
}

start_moonraker() {
  echo -n "Starting Moonraker... "
  /etc/init.d/S56moonraker_service start
  sleep 1
  echo "[OK]"
}

stop_moonraker() {
  echo -n "Stopping Moonraker... "
  /etc/init.d/S56moonraker_service stop
  sleep 1
  echo "[OK]"
}

start_klipper() {
  echo -n "Starting Klipper... "
  if [ "$IS_MIPS" -eq 1 ]; then
    /etc/init.d/S55klipper_service start
  else
    sudo systemctl start klipper
  fi
  echo "[OK]"
}

restart_klipperscreen()
{
    echo "Restarting KlipperScreen..."
    if [ "$(id -u)" -eq 0 ]; then
        systemctl restart KlipperScreen
    else
        sudo systemctl restart KlipperScreen
    fi
    screen_restart_status=$?
    if [ "$screen_restart_status" -ne 0 ]; then
        echo "[ERROR] Unable to restart KlipperScreen."
        return "$screen_restart_status"
    fi
    echo "[OK]"
}


stop_klipper() {
  echo -n "Stopping Klipper... "
  if [ "$IS_MIPS" -eq 1 ]; then
    /etc/init.d/S55klipper_service stop
  else
    sudo systemctl stop klipper
  fi
  echo "[OK]"
}

add_updater()
{
    echo -n "Adding update manager to moonraker.conf... "
    if has_config_section "${MOONRAKER_CONFIG_DIR}/moonraker.conf" 'update_manager AceScreen'; then
        echo "[SKIPPED] Existing section preserved"
        return 0
    fi
    append_config_section "${MOONRAKER_CONFIG_DIR}/moonraker.conf" \
        "${SRCDIR}/templates/moonraker_update.txt" 'update_manager AceScreen' || return 1
    if [ "$IS_MIPS" -eq 1 ]; then
        stop_moonraker
        start_moonraker
    else
        restart_moonraker
    fi

}


verify_ready
check_folders
if [ "$UNINSTALL" -ne 1 ]; then
    find_klipperscreen || exit 1
fi
stop_klipper

if [ "$UNINSTALL" -ne 1 ]; then
    if ! link_extension || ! copy_config || ! install_screen_config || ! add_updater; then
        echo "[ERROR] Unable to complete installation."
        start_klipper
        exit 1
    fi
else
    uninstall
fi

start_klipper
if [ "$UNINSTALL" -ne 1 ]; then
    restart_klipperscreen || exit 1
fi
