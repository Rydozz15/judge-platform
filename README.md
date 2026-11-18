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
| `EVAL_ADMIN_PASSWORD` | `admin123` | Clave inicial de `admin` (hasheada automáticamente con PBKDF2-SHA256) |
| `EVAL_SECRET_KEY` | *(auto en `data/.secret_key`)* | Clave secreta HMAC-SHA256 para firmar tokens de sesión |
| `EVAL_CORS_ORIGINS` | `http://localhost:8080,http://127.0.0.1:8080` | Lista de orígenes CORS permitidos separados por coma |
| `PYTHON` | `python3` | Intérprete para crear `.venv` |

---

## Estructura

```
backend/
  app.py                          API FastAPI (auth HMAC, evaluación, admin, métricas, SSE, exportación)
  database.py                     Esquema SQLite, migraciones y helpers PBKDF2 / HMAC-SHA256
  stats.py                        Motor estadístico (Krippendorff, Gwet, ICC, Cohen, Friedman/Wilcoxon, piloto)
  generate_dummy_eval_platform.py Datos de demostración
  requirements.txt
frontend/                         login.html, evaluate.html, admin.html, css/styles.css
examples/                         CSV de ejemplo (dataset y rúbrica)
tests/                            Suite de pruebas unitarias, UX e integración de seguridad OWASP
data/                             Base SQLite local y .secret_key (ignorados por git)
docs/ROADMAP.md                   Hoja de ruta de versiones (v0.1 -> v0.2 -> v0.3/v0.4)
CHANGELOG.md                      Historial detallado de cambios y features futuros
render.yaml                       Blueprint de despliegue en Render
run.sh                            Arranque autocontenido
```

---

## Métricas y capacidades incluidas (`v0.2.0`)

- **Acuerdo inter-jueces en tiempo real (`backend/stats.py`):**
  - **Alpha de Krippendorff ($\alpha$)** para niveles **ordinal**, **intervalo** y **nominal**, tolerante a datos faltantes y $N$ variable de jueces + **IC 95 % por bootstrap**.
  - **AC2 de Gwet** (ponderado ordinal, resistente a la paradoja de prevalencia), **ICC(2,1) / ICC(2,k)** (acuerdo absoluto), **Kappa de Fleiss** (nominal) y **% de acuerdo exacto y dentro de $\pm 1$ punto**.
  - **Matriz Juez × Juez** con **Kappa de Cohen ponderado cuadrático ($\kappa_w$)** y diferencia media.
  - Desgloses automáticos **por criterio de rúbrica**, **por variante** y **Calibración vs Producción** (las preguntas con `is_calibration=1` se priorizan automáticamente al inicio).
- **Comparación estadística entre variantes:**
  - Media, DE, IC 95 %, Mediana e IQR por variante + **Test de Friedman ($\chi^2$)** y comparaciones post-hoc por pares de **Wilcoxon con corrección de Holm-Bonferroni**.
- **Gestión de pilotos y calidad de jueces:**
  - Avance global y por juez, jueces en línea (*heartbeat*), ritmo (`casos/hora`) y **ETA**.
  - Diagnóstico de sesgo de **severidad/lenidad ($\Delta$ y $z$-score)** y alertas automáticas (*speeder*, *straight-lining*, *bajo acuerdo*).
  - **Consola de Auditoría de Desacuerdos**: identifica e inspecciona los casos con mayor dispersión entre jueces para sesiones de calibración.
- **UX Anti-Sesgo, Tiempo Real y Experiencia Móvil + PC:**
  - Stream **SSE (`/api/admin/stream`)** con actualización en vivo en `admin.html` y sub-navegación segmentada en 4 vistas analíticas.
  - En `evaluate.html`: ocultamiento de indicadores que induzcan auto-calibración o efecto Hawthorne, navegación al **Caso Anterior** con precarga de respuestas para corregir errores, modo enfocado por pestañas (`Respuesta A / B / C`) para móviles con botones táctiles $\ge 46\text{ px}$, barra inferior fija y **autoguardado en `localStorage`**.
- **Ciberseguridad (OWASP Top 10):**
  - Autenticación obligatoria mediante tokens firmados `HMAC-SHA256`, contraseñas con `PBKDF2-HMAC-SHA256` (120.000 iteraciones + migración automática), rate-limiting contra fuerza bruta en login (`HTTP 429`), protección anti-IDOR en sesiones y envíos, sanitización contra *CSV Formula Injection* y cabeceras HTTP de seguridad.

## Pruebas automatizadas

```bash
./.venv/bin/python -m unittest discover -s tests -v
```

## Despliegue en Render y Respaldo de Datos

- **Render:** el repositorio incluye `render.yaml` (`uvicorn backend.app:app --host 0.0.0.0 --port $PORT`). Si usas SQLite en la nube, monta un **Persistent Disk** en `/var/data` y configura `EVAL_DB_PATH=/var/data/evaluation.db` (o exporta los CSV/JSON al finalizar cada sesión si usas el plan gratuito efímero).
- **Respaldo local en caliente (SQLite WAL):**
  ```bash
  sqlite3 data/evaluation.db ".backup 'data/evaluation-$(date +%F).db'"
  ```
- Consulta [`CHANGELOG.md`](CHANGELOG.md) y [`docs/ROADMAP.md`](docs/ROADMAP.md) para ver el detalle de cambios de `v0.1.0` $\rightarrow$ `v0.2.0` y los próximos features (`v0.3.0` – `v0.4.0`).

