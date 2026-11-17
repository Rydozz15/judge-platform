#!/usr/bin/env bash
# run.sh — Levanta la Plataforma de Evaluación de forma autocontenida.
#
# Uso:
#   ./run.sh            # crea .venv si falta, instala dependencias e inicia el servidor
#   ./run.sh --demo     # además siembra datos de prueba SI la base aún no existe
#   ./run.sh --install  # solo crea/actualiza el entorno y sale
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'

DEMO=0; INSTALL_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --demo) DEMO=1 ;;
    --install) INSTALL_ONLY=1 ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo -e "${RED}Opción desconocida: $arg${NC}"; exit 1 ;;
  esac
done

# 1. Cargar .env (sin sobrescribir variables ya definidas en la terminal)
if [ -f .env ]; then
  while IFS='=' read -r key value || [ -n "$key" ]; do
    [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
    key="$(echo "$key" | xargs)"; value="$(echo "$value" | xargs)"
    [ -z "${!key:-}" ] && export "$key=$value"
  done < .env
else
  echo -e "${YELLOW}[AVISO] No existe .env; usando valores por defecto (ver .env.example).${NC}"
fi

HOST="${EVAL_HOST:-0.0.0.0}"
PORT="${EVAL_PORT:-8080}"
PYTHON_BIN="${PYTHON:-python3}"
VENV_DIR="$ROOT_DIR/.venv"

# 2. Entorno virtual propio (se recrea si quedó roto, p. ej. tras actualizar Python del sistema)
if [ -x "$VENV_DIR/bin/python" ] && ! "$VENV_DIR/bin/python" -c "import sys" >/dev/null 2>&1; then
  echo -e "${YELLOW}[AVISO] .venv inválido; se recreará.${NC}"
  rm -rf "$VENV_DIR"
fi
if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo -e "${GREEN}[INFO] Creando entorno virtual con $PYTHON_BIN ...${NC}"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

REQ="backend/requirements.txt"
STAMP="$VENV_DIR/.requirements.sha"
REQ_SHA="$(sha256sum "$REQ" | cut -d' ' -f1)"
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$REQ_SHA" ] \
   || ! "$VENV_DIR/bin/python" -c "import fastapi, uvicorn, multipart" >/dev/null 2>&1; then
  echo -e "${GREEN}[INFO] Instalando dependencias...${NC}"
  "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
  "$VENV_DIR/bin/python" -m pip install --quiet -r "$REQ"
  echo "$REQ_SHA" > "$STAMP"
fi

[ "$INSTALL_ONLY" -eq 1 ] && { echo -e "${GREEN}[OK] Entorno listo.${NC}"; exit 0; }

# 3. Base de datos
DB_FILE="${EVAL_DB_PATH:-$ROOT_DIR/data/evaluation.db}"
if [ ! -f "$DB_FILE" ] && [ "$DEMO" -eq 1 ]; then
  echo -e "${GREEN}[INFO] Base inexistente: sembrando datos de demostración...${NC}"
  (cd backend && "$VENV_DIR/bin/python" generate_dummy_eval_platform.py)
elif [ ! -f "$DB_FILE" ]; then
  echo -e "${GREEN}[INFO] Se creará una base vacía en $DB_FILE (usuario 'admin').${NC}"
fi

echo -e "${BLUE}------------------------------------------------------${NC}"
echo -e "${GREEN}Plataforma de Evaluación en http://localhost:${PORT}/login.html${NC}"
echo -e "${GREEN}Panel admin:              http://localhost:${PORT}/admin.html${NC}"
echo -e "${BLUE}------------------------------------------------------${NC}"

cd backend
exec "$VENV_DIR/bin/uvicorn" app:app --host "$HOST" --port "$PORT" --reload
