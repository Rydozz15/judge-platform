# AGENTS.md — Plataforma de Evaluación Ciega (MentorIA)

Guía para agentes de IA y desarrolladores que trabajen en este repositorio.

## Qué es
App FastAPI + SQLite con frontend estático para evaluación **triple-ciega** de respuestas (variantes como `naive`, `rag`, `profesional`) por jueces humanos, con rúbricas, campañas, sesiones (heartbeat), métricas y exportación. Es **autocontenida**: no depende de otros proyectos.

## Arranque
```bash
./run.sh            # crea .venv propio, instala backend/requirements.txt, sirve en :8080
./run.sh --demo     # siembra datos de prueba solo si la base no existe
./run.sh --install  # solo prepara el entorno
```
Configuración vía `.env` (ver `.env.example`): `EVAL_HOST`, `EVAL_PORT`, `EVAL_DB_PATH`, `EVAL_ADMIN_PASSWORD`, `PYTHON`.

## Mapa del código
- `backend/app.py` — Todos los endpoints. Secciones: schemas Pydantic → dependencias de seguridad (`get_current_user`, `require_admin`) → auth → flujo de evaluación (`/api/evaluation/*`) → CRUD admin (`/api/admin/*`) → métricas (`compute_fleiss_kappa`, `/api/admin/metrics`) → exportación → montaje del frontend en `/` (debe quedar **al final**).
- `backend/database.py` — Esquema y migraciones idempotentes (`CREATE TABLE IF NOT EXISTS` + `ALTER TABLE` en `try`). Tablas: `campaigns`, `judges`, `questions`, `responses`, `rubric_criteria`, `campaign_rubric`, `campaign_judges`, `evaluations`, `judge_question_order`, `judge_sessions`.
- `frontend/` — `login.html`, `evaluate.html`, `admin.html` (JS inline), `css/styles.css`. Usa Chart.js y Font Awesome por CDN.
- `examples/` — CSV de referencia para dataset y rúbrica.
- `docs/ROADMAP.md` — Plan de trabajo vigente (fases 0–6).

## Reglas
1. **Datos reales:** `data/evaluation.db` contiene evaluaciones de jueces. No borrar, sobrescribir ni correr `generate_dummy_eval_platform.py` sobre ella sin respaldo (`sqlite3 data/evaluation.db ".backup ..."`). Para pruebas usar `EVAL_DB_PATH=/tmp/x.db`.
2. **No versionar** `data/*.db`, `.env` ni `.venv/` (ya cubierto por `.gitignore`).
3. **Migraciones:** agregar columnas/tablas de forma idempotente en `init_db()`; nunca `DROP` sobre datos existentes.
4. **Ceguera:** ningún endpoint de `/api/evaluation/*` debe exponer `source_type` ni metadatos que revelen la variante al juez.
5. **Seguridad conocida:** la auth actual (`X-User-Id` + contraseñas en texto plano) es insegura; ver Fase 0 del roadmap antes de cualquier despliegue público.
6. **Métricas estadísticas:** toda métrica nueva debe validarse contra una implementación de referencia (p. ej. paquete `krippendorff`, `statsmodels`, `pingouin`) con tests.
7. **Responsive:** cualquier cambio de UI debe verificarse en anchos 360 px (móvil) y ≥ 1280 px (PC).
