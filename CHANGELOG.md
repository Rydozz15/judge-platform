# Changelog — Plataforma de Evaluación Ciega (`judge-platform`)

Todos los cambios relevantes del proyecto se documentan en este archivo.
El versionado sigue el esquema pre-release **`0.x.y`** (`0.1.0` base inicial $\rightarrow$ `0.2.0` versión actual para pilotos).

---

## [0.2.0] — 2026-11-18

Versión enfocada en **pilotos experimentales con jueces expertos**, incorporando análisis psicométrico de acuerdo inter-jueces en tiempo real, pruebas inferenciales, gestión operativa del piloto, rediseño UX/IA Mobile + PC y endurecimiento de ciberseguridad (OWASP Top 10).

### Added (Nuevas Funcionalidades)
- **Motor Estadístico e Inter-Rater Reliability (`backend/stats.py`)**:
  - **Alpha de Krippendorff ($\alpha$)** para escalas **ordinal**, **intervalo** y **nominal**, tolerante a datos faltantes y número variable de jueces por caso, con **intervalos de confianza al 95 % por bootstrap** ($B = 400$).
  - **AC2 de Gwet** (ponderación ordinal cuadrática, robusto frente a la paradoja del Kappa en distribuciones sesgadas).
  - **Coeficiente de Correlación Intraclase** **`ICC(2,1)`** (juez individual) e **`ICC(2,k)`** (promedio del panel de $k$ jueces) mediante ANOVA de dos vías de efectos aleatorios.
  - **Kappa de Fleiss ($\kappa$)** nominal y porcentajes de **acuerdo exacto** y **acuerdo adyacente ($\pm 1$ punto)**.
  - **Matriz Juez × Juez** con **Kappa de Cohen ponderado cuadrático ($\kappa_w$)**, % de acuerdo exacto/$\pm 1$ y diferencia media firmada ($\Delta$).
  - Desgloses automáticos de acuerdo **por Criterio de Rúbrica**, **por Variante/Fuente** y **Calibración vs. Producción**.
  - **Pruebas Inferenciales entre Variantes**: **Test no paramétrico de Friedman ($\chi^2$)** por bloques $(juez, pregunta, criterio)$ y comparaciones post-hoc pareadas de **Wilcoxon Signed-Rank** con corrección de **Holm-Bonferroni** ($p_{\text{raw}}$, $p_{\text{Holm}}$, tasa de victorias).
- **Gestión Operativa de Pilotos y Diagnóstico de Jueces**:
  - Monitoreo de cobertura global (`%`), jueces conectados en vivo (`is_online`), ritmo (`casos/hora`) y **tiempo restante estimado (ETA)** hasta completar la campaña.
  - Diagnóstico psicométrico individual por juez: sesgo de **severidad / lenidad** ($\Delta$ frente al consenso y $z$-score), acuerdo medio $\bar{\kappa}_w$ con sus pares y banderas automáticas de calidad (*Speeding / Evaluación rápida*, *Straight-lining / Baja varianza*, *Bajo acuerdo*).
  - **Consola de Auditoría de Desacuerdos**: identifica automáticamente los ítems $(pregunta, variante, criterio)$ con mayor dispersión ($\Delta$ y $SD$) entre jueces para sesiones de calibración.
- **Tiempo Real (Server-Sent Events) y Exportación Científica**:
  - Endpoint `GET /api/admin/stream` (SSE) que actualiza en vivo el dashboard de administración cuando los jueces inician sesión, envían *heartbeats* o registran evaluaciones.
  - Endpoint `GET /api/admin/export/report` para descargar el reporte estadístico completo del piloto en formato JSON estructurado.
- **UX / Arquitectura de Información y Diseño Responsivo (Mobile + PC)**:
  - **Flujo del Juez (`evaluate.html`)**:
    - **Mitigación de sesgos metodológicos**: reemplazo de *"Mi Puntaje Promedio"* por **"Tiempo Restante Estimado"** y **Guía Rápida de la Rúbrica** en la pantalla de bienvenida; ocultamiento del badge *"Caso de Calibración"* a los jueces (`Caso #X de Y`) para evitar el efecto Hawthorne, manteniendo la priorización automática de preguntas de calibración en el backend.
    - **Navegación hacia atrás (`Caso Anterior`)**: soporte en `GET /api/evaluation/next?question_id=...` para revisar y corregir casos ya evaluados con precarga desde el servidor (`existing_evaluations`).
    - **Modo Enfocado por Pestañas (`Respuesta A / B / C`)** en móviles (`<=768px`) y conmutable en PC, con botones táctiles $\ge 46\text{ px}$, texto de pregunta *sticky* colapsable, resaltado del botón *Siguiente Respuesta* al completar criterios, barra inferior fija y **autoguardado de borrador en `localStorage`**.
  - **Panel de Administración (`admin.html`)**:
    - Sub-navegación segmentada en 4 vistas dentro de Analítica (*Resumen & Variantes*, *Acuerdo Inter-Juez*, *Gestión del Piloto & Jueces*, *Auditoría de Desacuerdos*).
    - Checklist de preparación por campaña (*Lista para evaluar* vs. *Incompleta*), botón para **Activar / Pausar** campañas (`POST /api/admin/campaigns/{id}/toggle-active`) y carga directa de CSV desde la fila de cada campaña.
    - Selector de campaña y casilla de **Calibración** al crear preguntas manualmente, más toggle directo de calibración en el Banco de Preguntas (`PUT /api/admin/questions/{id}/calibration`).
    - Gestión de contraseñas de jueces desde el panel (`PUT /api/admin/judges/{id}/password`), descarga de plantillas CSV de ejemplo y sistema de notificaciones **Toast** no bloqueantes.
- **Suite de Tests y Despliegue Cloud**:
  - Suite automatizada `tests/test_stats_and_api.py` (7 baterías de tests unitarios, de integración UX y de seguridad).
  - Blueprint `render.yaml` para despliegue en Render.

### Fixed & Security (Auditoría de Ciberseguridad OWASP Top 10)
- **Prevención de Account Takeover (`CWE-287`)**: deshabilitado el *fallback* inseguro por cabecera `X-User-Id` en `get_current_user`; todas las rutas protegidas exigen token firmado `HMAC-SHA256` (`Authorization: Bearer <token>`).
- **Criptografía de Contraseñas y Secretos (`CWE-256` / `CWE-798`)**:
  - Hashing de contraseñas con `PBKDF2-HMAC-SHA256` (`120.000` iteraciones + salt aleatorio de 16 bytes) y migración automática en `init_db()` de todas las contraseñas en texto plano preexistentes.
  - Generación y persistencia automática de clave secreta de 256 bits en `data/.secret_key` (permisos `0600`, excluida de Git) cuando no se define `EVAL_SECRET_KEY`.
- **Protección contra Fuerza Bruta (`CWE-307`)**: limitador de tasa en `/api/auth/login` (`8` intentos fallidos por `IP + usuario` cada `5 minutos` $\rightarrow$ `HTTP 429`).
- **Prevención de IDOR (`CWE-639`)**: validación de propiedad de sesión (`WHERE id = ? AND judge_id = ?`) en `/api/evaluation/session/heartbeat` y validación de pertenencia `response_id -> question_id` + campaña activa en `/api/evaluation/submit`.
- **Protección contra CSV Formula Injection (`CWE-1236`)**: sanitización de celdas (`=`, `+`, `-`, `@`, `\t`, `\r`) en todas las exportaciones CSV (`/api/admin/export` y `/api/admin/export/anova`).
- **Prevención de DoS y XSS (`CWE-400` / `CWE-79` / `CWE-693`)**: límite de `10 MB` en subidas CSV (`HTTP 413`), tope de suscriptores SSE simultáneos, cabeceras HTTP de seguridad (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`), orígenes CORS restringibles por `EVAL_CORS_ORIGINS` y eliminación de interpolación de strings en atributos `onclick` inline.
- **Corrección de bug en Kappa de Calibración**: `get_admin_metrics` ya no sobrescribe `calibration_fleiss_kappa` con el Kappa global.

---

## [0.1.0] — 2026-11-17

Extracción y empaquetado inicial de la plataforma de evaluación triple-ciega como repositorio independiente y autocontenido.

### Added
- Backend base en FastAPI (`backend/app.py`, `backend/database.py`) con SQLite local (`data/evaluation.db`).
- Flujo básico de evaluación ciega con orden aleatorio por juez (`judge_question_order`) y soporte para múltiples variantes (`naive`, `rag`, `professional`, etc.).
- CRUD administrativo de campañas, preguntas, criterios de rúbrica y jueces, con importación/exportación CSV básica.
- Script de arranque autocontenido `run.sh`, `.env.example`, `.gitignore` y datasets de ejemplo en `examples/`.

---

## Próximos Elementos y Features Futuros (`Roadmap v0.3.0 – v0.4.0`)

### planeado para `v0.3.0` — Experiencia de Evaluación Avanzada y Persistencia Cloud
1. **Soporte Dual SQLite / PostgreSQL (Cloud-Ready)**:
   - Adaptador opcional para conectar a **PostgreSQL** (Neon / Supabase / Render Postgres) o **Turso (libSQL)** mediante `DATABASE_URL`, permitiendo despliegues *serverless* o en planes gratuitos efímeros sin riesgo de pérdida de datos al reiniciar el contenedor.
2. **Renderizado Seguro de Markdown y Bloques de Código/Citas en Respuestas**:
   - Soporte opcional (con sanitización DOMPurify) para que las respuestas de los modelos RAG y expertos humanos muestren listas, negritas y citas académicas formateadas sin revelar qué variante las generó.
3. **Asignación Balanceada de Subconjuntos (Incomplete Block Design)**:
   - Permitir que en datasets grandes (ej. $100+$ preguntas) cada juez evalúe un bloque de $M$ preguntas con un porcentaje configurable de **solapamiento obligatorio** (ej. 20 % de preguntas compartidas de calibración + bloques balanceados).
4. **Modo de Desempate / Ranking Preferencial (*Pairwise / Best-Worst Scaling*)**:
   - Opción configurable por campaña para que, además de la escala Likert 1–5 por criterio, el juez indique cuál de las respuestas ($A, B, C$) prefiere globalmente o marque empate, habilitando modelos **Bradley-Terry / Elo** entre variantes.
5. **Vista Previa Interactiva (Dry-Run) al Importar CSV**:
   - Previsualizador en el modal de subida CSV que muestre las columnas detectadas, número de preguntas nuevas vs. duplicadas y advertencias de celdas vacías antes de confirmar la inserción.

### Planeado para `v0.4.0` — Automatización Científica y Gestión de Paneles de Jueces
1. **Exportación Directa a Tablas LaTeX / APA y Notebooks Reproducibles**:
   - Generación en un clic de tablas en formato **LaTeX (`booktabs`)** y **Markdown/APA** listas para pegar en la tesis o paper (con $\alpha$ de Krippendorff, IC 95 %, medias $\pm$ DE por variante y $p$-valores corregidos de Friedman/Wilcoxon).
2. **Invitación de Jueces por Enlace Único (*Magic Links*) y Consentimiento Informado**:
   - Generación de enlaces de acceso firmados de un solo clic por juez (para evitar que tengan que teclear usuario/contraseña en móvil) y pantalla opcional de aceptación de consentimiento informado / instrucciones metodológicas al iniciar el piloto.
3. **Detección de Fatiga Intrasesión y Pausas Sugeridas**:
   - Análisis de deriva temporal de latencia y varianza a medida que el juez avanza (ej. tras 15 casos seguidos), sugiriendo una pausa breve cuando se detecta caída abrupta en el tiempo de lectura.
4. **Congelamiento e Inmutabilidad de Campañas Cerradas (*Audit Trail*)**:
   - Estado `Cerrada / Sellada` con hash `SHA-256` del dataset y de las evaluaciones exportadas para garantizar trazabilidad e integridad científica de los resultados publicados.
5. **Tema Claro / Oscuro Conmutable (Alto Contraste Lectura)**:
   - Selector de tema visual en `evaluate.html` para jueces que prefieran fondo claro tipo papel durante sesiones largas de lectura.
