# MentorIA · Plataforma de Evaluación Ciega

Plataforma web para que **jueces expertos evalúen, a ciegas, distintas variantes de respuesta** (p. ej. LLM sin contexto, RAG y experto humano) a un mismo conjunto de preguntas, usando una rúbrica configurable. Pensada para **pilotos y estudios de evaluación** con análisis estadístico y de acuerdo entre jueces.

- **Backend:** FastAPI + SQLite (sin servicios externos).
- **Frontend:** HTML/CSS/JS estático servido por el mismo backend.
- **Autocontenida:** no depende de otros proyectos; solo requiere Python ≥ 3.10.

---

## Inicio rápido

```bash
cp .env.example .env        # ajusta EVAL_ADMIN_PASSWORD y el puerto
./run.sh                    # crea .venv, instala dependencias e inicia en :8080
# ./run.sh --demo           # (opcional) base nueva con datos de prueba
```

| Vista | URL |
|---|---|
| Login | `http://localhost:8080/login.html` |
| Evaluación (jueces) | `http://localhost:8080/evaluate.html` |
| Administración | `http://localhost:8080/admin.html` |
| API docs (OpenAPI) | `http://localhost:8080/docs` |

En una base nueva se crea el usuario `admin` con la contraseña de `EVAL_ADMIN_PASSWORD` (por defecto `admin123`; **cámbiala**). Con `--demo` también se crean `juez1` / `juez2` (clave `juez123`).

---

## Flujo de un piloto

1. **Admin → Campañas:** crear la campaña (nombre, descripción, fecha límite).
2. **Admin → Rúbrica:** crear criterios o importar un CSV ([`examples/rubrica_ejemplo.csv`](examples/rubrica_ejemplo.csv)).
3. **Admin → Jueces:** crear las cuentas de los jueces.
4. **Admin → Carga de dataset:** subir el CSV de preguntas/respuestas ([`examples/dataset_ejemplo.csv`](examples/dataset_ejemplo.csv)).
5. **Jueces:** ingresan y evalúan; el orden de las respuestas se aleatoriza por juez y se ocultan las etiquetas de origen.
6. **Admin → Métricas / Exportar:** seguimiento y exportación CSV (general y formato largo para ANOVA).

### Formato del dataset (CSV, UTF-8)

| Columna | Obligatoria | Descripción |
|---|---|---|
| `pregunta` | Sí | Texto de la pregunta. |
| `is_calibration` | No | `1` si es ítem de calibración (todos los jueces lo evalúan para medir acuerdo). |
| *cualquier otra* | ≥ 1 | Cada columna adicional es una **variante** de respuesta; el nombre de la columna es la etiqueta interna (oculta al juez). |

### Formato de la rúbrica (CSV)

`nombre` (obligatoria), `descripcion`, `min_score`, `max_score`, `peso`, y opcionalmente `descripcion_N` para describir el anclaje del puntaje `N`.

---

## Configuración (`.env`)

| Variable | Defecto | Uso |
|---|---|---|
| `EVAL_HOST` | `0.0.0.0` | Interfaz de escucha |
| `EVAL_PORT` | `8080` | Puerto |
| `EVAL_DB_PATH` | `./data/evaluation.db` | Ruta de la base SQLite |
| `EVAL_ADMIN_PASSWORD` | `admin123` | Clave inicial de `admin` (solo al crear la base) |
| `PYTHON` | `python3` | Intérprete para crear `.venv` |

---

## Estructura

```
backend/
  app.py                          API FastAPI (auth, evaluación, admin, métricas, exportación)
  database.py                     Esquema SQLite y migraciones ligeras
  generate_dummy_eval_platform.py Datos de demostración
  requirements.txt
frontend/                         login.html, evaluate.html, admin.html, css/
examples/                         CSV de ejemplo (dataset y rúbrica)
data/                             Base SQLite local (ignorada por git)
docs/ROADMAP.md                   Plan de mejoras
run.sh                            Arranque autocontenido
```

---

## Estado y limitaciones conocidas

> ⚠️ **Seguridad:** la versión actual es apta solo para uso local / red de confianza. La autenticación se basa en un header `X-User-Id` sin token firmado y las contraseñas se guardan en texto plano. **No exponer a Internet** hasta completar la Fase 0 del [ROADMAP](docs/ROADMAP.md).

- Acuerdo entre jueces: solo **Kappa de Fleiss** (nominal). Krippendorff α, Kappa ponderado e ICC están planificados.
- El panel de métricas no se actualiza en tiempo real.
- Diseño parcialmente responsive (sin breakpoints para móvil).

Ver el plan completo en [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Respaldo de datos

La base vive en `data/evaluation.db` (modo WAL). Para respaldar en caliente:

```bash
sqlite3 data/evaluation.db ".backup 'data/evaluation-$(date +%F).db'"
```
