#!/usr/bin/env bash
# install.sh — install DBCheck as a systemd service.
#
# Target: CentOS / RHEL / Rocky / Alma 8+
# Run as root:  sudo ./install.sh
#
# After install, copy your existing dbcheck.db and .fernet_key into
# /opt/dbcheck/data/ and restart the service. See deploy/DEPLOY.md.

set -euo pipefail

# ============================================================
# Configuration
# ============================================================
APP_NAME="dbcheck"
APP_USER="dbcheck"
APP_GROUP="dbcheck"
APP_DIR="/opt/${APP_NAME}"
PYTHON_MIN_MAJOR=3
PYTHON_MIN_MINOR=8
PYTHON_BIN="${PYTHON_BIN:-}"

# ============================================================
# Pre-flight
# ============================================================
if [[ ${EUID} -ne 0 ]]; then
    echo "ERROR: must run as root (use sudo)" >&2
    exit 1
fi

find_python() {
    local candidate

    if [[ -n "${PYTHON_BIN}" ]]; then
        command -v "${PYTHON_BIN}" >/dev/null 2>&1 || return 1
        command -v "${PYTHON_BIN}"
        return 0
    fi

    for candidate in python3.12 python3.11 python3.10 python3.9 python3.8 python3; do
        if command -v "${candidate}" >/dev/null 2>&1; then
            command -v "${candidate}"
            return 0
        fi
    done

    return 1
}

if ! PYTHON_BIN="$(find_python)"; then
    echo "ERROR: Python >= ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR} is required, but no python3 executable was found." >&2
    echo "Install Python 3.8+ first, then rerun with PYTHON_BIN=python3.11 ./install.sh if needed." >&2
    exit 1
fi

PY_VER="$("${PYTHON_BIN}" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if ! "${PYTHON_BIN}" -c "import sys; sys.exit(0 if sys.version_info >= (${PYTHON_MIN_MAJOR}, ${PYTHON_MIN_MINOR}) else 1)"; then
    echo "ERROR: Python >= ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR} required, found ${PY_VER} at $(command -v "${PYTHON_BIN}")" >&2
    echo "Install a newer Python and rerun, for example: PYTHON_BIN=python3.11 ./install.sh" >&2
    exit 1
fi

echo ">>> Using Python ${PY_VER}: $(command -v "${PYTHON_BIN}")"

# ============================================================
# Resolve source code path
# ============================================================
# This script is deploy/install.sh, so the project root is its parent.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"
BACKEND_SRC="${PROJECT_ROOT}/backend"
FRONTEND_SRC="${PROJECT_ROOT}/index.html"

if [[ ! -d "${BACKEND_SRC}" ]]; then
    echo "ERROR: backend source not found at ${BACKEND_SRC}" >&2
    echo "       Did you rsync the whole project to the server first?" >&2
    exit 1
fi
if [[ ! -f "${FRONTEND_SRC}" ]]; then
    echo "ERROR: frontend index.html not found at ${FRONTEND_SRC}" >&2
    exit 1
fi

# ============================================================
# System user
# ============================================================
if ! id -u "${APP_USER}" >/dev/null 2>&1; then
    echo ">>> Creating system user '${APP_USER}' ..."
    useradd --system --home-dir "${APP_DIR}" --shell /sbin/nologin "${APP_USER}"
fi

# ============================================================
# Directory layout (single root, /opt/dbcheck)
# ============================================================
echo ">>> Creating directory layout under ${APP_DIR} ..."
install -d -m 755 -o root        -g root       "${APP_DIR}"
install -d -m 750 -o "${APP_USER}" -g "${APP_GROUP}" "${APP_DIR}/app"
install -d -m 700 -o "${APP_USER}" -g "${APP_GROUP}" "${APP_DIR}/data"
install -d -m 755 -o "${APP_USER}" -g "${APP_GROUP}" "${APP_DIR}/logs"
install -d -m 755 -o "${APP_USER}" -g "${APP_GROUP}" "${APP_DIR}/run"
install -d -m 755 -o "${APP_USER}" -g "${APP_GROUP}" "${APP_DIR}/venv"

# ============================================================
# Copy code
# ============================================================
echo ">>> Copying backend code to ${APP_DIR}/app/ ..."
# rsync without --delete so we don't accidentally wipe data/ inside app/
rsync -a --exclude='venv' --exclude='__pycache__' --exclude='*.pyc' \
    "${BACKEND_SRC}/" "${APP_DIR}/app/"
chown -R "${APP_USER}:${APP_GROUP}" "${APP_DIR}/app"
chmod 750 "${APP_DIR}/app"

echo ">>> Copying frontend index.html to ${APP_DIR}/ ..."
FRONTEND_DST="${APP_DIR}/index.html"
if [[ "$(readlink -f "${FRONTEND_SRC}")" == "$(readlink -f "${FRONTEND_DST}")" ]]; then
    echo ">>> Frontend already in place; normalizing permissions ..."
    chown "${APP_USER}:${APP_GROUP}" "${FRONTEND_DST}"
    chmod 644 "${FRONTEND_DST}"
else
    install -m 644 -o "${APP_USER}" -g "${APP_GROUP}" \
        "${FRONTEND_SRC}" "${FRONTEND_DST}"
fi

# ============================================================
# Python venv + dependencies
# ============================================================
echo ">>> Creating Python venv ..."
sudo -u "${APP_USER}" "${PYTHON_BIN}" -m venv "${APP_DIR}/venv"

echo ">>> Installing requirements (this may take a minute) ..."
sudo -u "${APP_USER}" "${APP_DIR}/venv/bin/pip" install --quiet --upgrade pip wheel
sudo -u "${APP_USER}" "${APP_DIR}/venv/bin/pip" install --quiet -r "${APP_DIR}/app/requirements.txt"

# ============================================================
# Environment file
# ============================================================
ENV_FILE="${APP_DIR}/.env"
if [[ ! -f "${ENV_FILE}" ]]; then
    echo ">>> Creating .env from example ..."
    install -m 640 -o "${APP_USER}" -g "${APP_GROUP}" \
        "${SCRIPT_DIR}/${APP_NAME}.env.example" "${ENV_FILE}"
fi

# ============================================================
# systemd unit
# ============================================================
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
echo ">>> Installing systemd unit to ${SERVICE_FILE} ..."
install -m 644 "${SCRIPT_DIR}/${APP_NAME}.service" "${SERVICE_FILE}"

# ============================================================
# Enable + start
# ============================================================
systemctl daemon-reload
systemctl enable "${APP_NAME}.service"
if systemctl is-active --quiet "${APP_NAME}.service"; then
    echo ">>> Restarting existing service ..."
    systemctl restart "${APP_NAME}.service"
else
    echo ">>> Starting service ..."
    systemctl start "${APP_NAME}.service"
fi

# ============================================================
# Firewall
# ============================================================
if command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active --quiet firewalld; then
    if ! firewall-cmd --query-port=8000/tcp >/dev/null 2>&1; then
        echo ">>> Opening port 8000/tcp in firewalld ..."
        firewall-cmd --permanent --add-port=8000/tcp
        firewall-cmd --reload
    fi
fi

# ============================================================
# Summary
# ============================================================
sleep 1
HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
HOST_IP="${HOST_IP:-<server-ip>}"

cat <<EOF

============================================================
DBCheck installed successfully.
  App dir:    ${APP_DIR}
  Data dir:   ${APP_DIR}/data  (chmod 700, owner ${APP_USER})
  Config:     ${ENV_FILE}
  Service:    systemctl status ${APP_NAME}
  Logs:       journalctl -u ${APP_NAME} -f
  URL:        http://${HOST_IP}:8000
============================================================

NEXT STEPS — migrate your existing data:

  # from your Mac, push the database files:
  scp backend/dbcheck.db backend/.fernet_key \\
      user@server:${APP_DIR}/data/

  # back on the server, fix ownership and restart:
  sudo chown ${APP_USER}:${APP_GROUP} ${APP_DIR}/data/*
  sudo chmod 600 ${APP_DIR}/data/*
  sudo systemctl restart ${APP_NAME}

Then open http://${HOST_IP}:8000/ in a browser.

EOF
