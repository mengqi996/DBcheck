#!/usr/bin/env bash
# uninstall.sh — remove DBCheck cleanly.
#
# By default, KEEPS /opt/dbcheck/data/ (your database and Fernet key).
# Pass --purge to also delete data and the system user.

set -euo pipefail

APP_NAME="dbcheck"
APP_USER="dbcheck"
APP_DIR="/opt/${APP_NAME}"
PURGE_DATA=0

for arg in "$@"; do
    case "${arg}" in
        --purge) PURGE_DATA=1 ;;
        -h|--help)
            echo "Usage: $0 [--purge]"
            echo "  --purge    Also remove ${APP_DIR}/data and user '${APP_USER}'"
            exit 0
            ;;
        *) echo "Unknown arg: ${arg}" >&2; exit 1 ;;
    esac
done

if [[ ${EUID} -ne 0 ]]; then
    echo "ERROR: must run as root" >&2
    exit 1
fi

if systemctl list-unit-files "${APP_NAME}.service" >/dev/null 2>&1; then
    if systemctl is-active --quiet "${APP_NAME}.service"; then
        echo ">>> Stopping ${APP_NAME}.service ..."
        systemctl stop "${APP_NAME}.service"
    fi
    echo ">>> Disabling ${APP_NAME}.service ..."
    systemctl disable "${APP_NAME}.service"
fi

SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
if [[ -f "${SERVICE_FILE}" ]]; then
    echo ">>> Removing ${SERVICE_FILE} ..."
    rm -f "${SERVICE_FILE}"
    systemctl daemon-reload
fi

if [[ -d "${APP_DIR}" ]]; then
    if [[ ${PURGE_DATA} -eq 1 ]]; then
        echo ">>> Removing ${APP_DIR} (--purge) ..."
        rm -rf "${APP_DIR}"
        if id -u "${APP_USER}" >/dev/null 2>&1; then
            echo ">>> Removing user '${APP_USER}' ..."
            userdel "${APP_USER}" 2>/dev/null || true
        fi
    else
        echo ">>> Removing ${APP_DIR}/app ${APP_DIR}/venv ${APP_DIR}/logs (data kept) ..."
        rm -rf "${APP_DIR}/app" "${APP_DIR}/venv" "${APP_DIR}/logs" "${APP_DIR}/run"
        echo "    To delete data too, rerun: $0 --purge"
    fi
fi

echo ""
echo "DBCheck uninstalled."
if [[ ${PURGE_DATA} -eq 0 ]]; then
    echo "Your data is preserved at: ${APP_DIR}/data"
fi