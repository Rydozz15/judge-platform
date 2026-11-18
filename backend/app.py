import asyncio
import logging
import json
import os
import random
import csv
import io
import time
from collections import defaultdict
from typing import List, Dict, Optional, Any
from fastapi import FastAPI, HTTPException, Header, Depends, Query, status, UploadFile, File, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pathlib import Path

from database import (
    init_db,
    get_db_connection,
    hash_password,
    verify_password,
    create_auth_token,
    verify_auth_token,
)
from stats import (
    compute_mean,
    compute_median,
    compute_variance,
    compute_sd,
    compute_iqr,
    compute_ci95_mean,
    interpret_agreement,
    compute_krippendorff_alpha,
    bootstrap_krippendorff_ci,
    compute_fleiss_kappa as stats_fleiss_kappa,
    compute_gwet_ac2,
    compute_percentage_agreements,
    compute_pairwise_judge_matrix,
    compute_icc_two_way_random,
    compute_variant_inferential_tests,
    compute_judge_diagnostics,
    compute_disagreement_audit,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("evaluation_app")

# Inicializar DB SQLite
init_db()

app = FastAPI(
    title="MentorIA Blind Evaluation Platform API",
    description="API para la evaluación triple-ciega de respuestas (Naive, RAG, Experto Humano)",
    version="0.2.0"
)

_raw_origins = os.getenv("EVAL_CORS_ORIGINS", "http://localhost:8080,http://127.0.0.1:8080")
_allowed_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-User-Id"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# ---------------- SECURITY CONFIG & RATE LIMITING ----------------

MAX_CSV_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_SSE_SUBSCRIBERS = 32
LOGIN_WINDOW_SEC = 300  # 5 minutos
MAX_FAILED_LOGINS = 8
_failed_login_attempts: Dict[str, List[float]] = defaultdict(list)


def _check_rate_limit(key: str):
    now = time.time()
    attempts = [t for t in _failed_login_attempts.get(key, []) if now - t < LOGIN_WINDOW_SEC]
    _failed_login_attempts[key] = attempts
    if len(attempts) >= MAX_FAILED_LOGINS:
        raise HTTPException(
            status_code=429,
            detail="Demasiados intentos de inicio de sesión fallidos. Espera 5 minutos antes de reintentar.",
        )


def _record_failed_login(key: str):
    now = time.time()
    attempts = [t for t in _failed_login_attempts.get(key, []) if now - t < LOGIN_WINDOW_SEC]
    attempts.append(now)
    _failed_login_attempts[key] = attempts


def _clear_failed_logins(key: str):
    _failed_login_attempts.pop(key, None)


def sanitize_csv_cell(val: Any) -> Any:
    """
    Previene CSV Formula Injection (CWE-1236) neutralizando celdas de texto
    que comiencen con '=', '+', '-', '@', tabulador o retorno de carro.
    """
    if val is None:
        return ""
    if isinstance(val, (int, float)):
        return val
    s = str(val)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


# ---------------- REAL-TIME SSE BROADCASTER ----------------

_sse_subscribers: List[asyncio.Queue] = []


def broadcast_realtime_event(event_type: str, payload: Optional[Dict[str, Any]] = None):
    msg = {
        "event": event_type,
        "timestamp": int(time.time()),
        "data": payload or {},
    }
    for q in list(_sse_subscribers):
        try:
            q.put_nowait(msg)
        except Exception:
            pass


# ---------------- PYDANTIC SCHEMAS ----------------

class LoginRequest(BaseModel):
    username: str
    password: str

class UserResponse(BaseModel):
    id: int
    username: str
    display_name: str
    role: str
    token: Optional[str] = None

class JudgeCreate(BaseModel):
    username: str
    password: str
    display_name: str
    role: str = "judge"

class ResponseCreate(BaseModel):
    response_text: str
    source_type: str
    source_metadata: Optional[str] = None

class QuestionCreate(BaseModel):
    question_text: str
    campaign_id: Optional[int] = None
    is_calibration: int = 0
    responses: List[ResponseCreate]

class PasswordResetRequest(BaseModel):
    password: str

class CalibrationToggleRequest(BaseModel):
    is_calibration: int

class CriterionCreate(BaseModel):
    name: str
    description: Optional[str] = None
    min_score: int = 1
    max_score: int = 5
    weight: float = 1.0
    score_descriptions: Optional[str] = None

class CampaignCreate(BaseModel):
    name: str
    description: Optional[str] = None
    deadline: Optional[str] = None
    rubric_ids: List[int] = []

class AssignQuestionsRequest(BaseModel):
    question_ids: List[int]

class RemoveQuestionsRequest(BaseModel):
    question_ids: List[int]

class SessionStartRequest(BaseModel):
    campaign_id: int

class SessionStartResponse(BaseModel):
    session_id: int
    status: str

class SessionHeartbeatRequest(BaseModel):
    session_id: int

class ScoreRating(BaseModel):
    criterion_id: int
    score: int

class SingleResponseRating(BaseModel):
    response_id: int
    ratings: List[ScoreRating]
    comment: Optional[str] = None
    latency_ms: Optional[int] = None

class EvaluationSubmit(BaseModel):
    question_id: int
    evaluations: List[SingleResponseRating]


# ---------------- SECURITY DEPENDENCIES ----------------

def get_current_user(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    token: Optional[str] = Query(None),
):
    resolved_user_id: Optional[int] = None

    # 1. Validar exclusivamente token firmado HMAC-SHA256 (Bearer o query param para SSE EventSource)
    raw_token = None
    if authorization and isinstance(authorization, str) and authorization.lower().startswith("bearer "):
        raw_token = authorization[7:].strip()
    elif token and isinstance(token, str):
        raw_token = token.strip()

    if raw_token:
        resolved_user_id = verify_auth_token(raw_token)

    # 2. Fallback X-User-Id DESHABILITADO por defecto (solo permitido si EVAL_ALLOW_INSECURE_HEADER_AUTH=1)
    if (
        resolved_user_id is None
        and x_user_id
        and os.getenv("EVAL_ALLOW_INSECURE_HEADER_AUTH") == "1"
    ):
        try:
            resolved_user_id = int(x_user_id)
        except ValueError:
            resolved_user_id = None

    if resolved_user_id is None:
        raise HTTPException(
            status_code=401,
            detail="No autenticado: se requiere un token de sesión válido (Authorization: Bearer).",
        )
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, display_name, role, is_active FROM judges WHERE id = ?", (resolved_user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row or not row["is_active"]:
        raise HTTPException(status_code=401, detail="Usuario no autorizado o inactivo.")
    
    return dict(row)

def require_admin(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Acceso denegado: Se requiere rol de administrador.")
    return current_user

# ---------------- AUTH ENDPOINTS ----------------

@app.post("/api/auth/login", response_model=UserResponse)
def login(req: LoginRequest, request: Request = None):
    client_ip = request.client.host if (request and getattr(request, "client", None)) else "local"
    rate_key = f"{client_ip}:{req.username.strip().lower()}"
    _check_rate_limit(rate_key)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, username, password, display_name, role, is_active FROM judges WHERE username = ?",
        (req.username.strip(),)
    )
    row = cursor.fetchone()
    conn.close()
    
    if not row or not verify_password(req.password, row["password"]):
        _record_failed_login(rate_key)
        raise HTTPException(status_code=400, detail="Usuario o contraseña incorrectos.")
    if not row["is_active"]:
        raise HTTPException(status_code=400, detail="Esta cuenta de juez se encuentra inactiva.")

    _clear_failed_logins(rate_key)
    token = create_auth_token(row["id"], row["role"])
    return UserResponse(
        id=row["id"],
        username=row["username"],
        display_name=row["display_name"],
        role=row["role"],
        token=token,
    )

# ---------------- EVALUATION FLOW ENDPOINTS ----------------

@app.post("/api/evaluation/session/start", response_model=SessionStartResponse)
def start_judge_session(req: SessionStartRequest, current_user: dict = Depends(get_current_user)):
    judge_id = current_user["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Insert new session
    cursor.execute(
        "INSERT INTO judge_sessions (judge_id, campaign_id, started_at, last_active) VALUES (?, ?, datetime('now'), datetime('now'))",
        (judge_id, req.campaign_id)
    )
    session_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    broadcast_realtime_event("session_started", {"judge_id": judge_id, "campaign_id": req.campaign_id})
    return SessionStartResponse(session_id=session_id, status="started")

@app.post("/api/evaluation/session/heartbeat")
def judge_session_heartbeat(req: SessionHeartbeatRequest, current_user: dict = Depends(get_current_user)):
    session_id = req.session_id
    judge_id = current_user["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Verificar propiedad de la sesión (evita IDOR entre jueces)
    cursor.execute(
        "SELECT campaign_id, started_at, last_active, session_duration_sec FROM judge_sessions WHERE id = ? AND judge_id = ?",
        (session_id, judge_id),
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Sesión no encontrada o no pertenece al usuario actual.")
        
    # Calculate time delta since last active in seconds (acotado a máx 60s por latido para evitar saltos por pestañas dormidas)
    cursor.execute("""
        UPDATE judge_sessions 
        SET last_active = datetime('now'),
            session_duration_sec = session_duration_sec + MIN(60, MAX(0, strftime('%s', 'now') - strftime('%s', last_active)))
        WHERE id = ? AND judge_id = ?
    """, (session_id, judge_id))
    conn.commit()
    conn.close()
    
    broadcast_realtime_event("session_heartbeat", {"judge_id": judge_id, "campaign_id": row["campaign_id"]})
    return {"status": "ok"}

@app.get("/api/evaluation/next")
def get_next_evaluation(
    campaign_id: Optional[int] = None,
    question_id: Optional[int] = None,
    current_user: dict = Depends(get_current_user),
):
    judge_id = current_user["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if campaign_id is None:
        cursor.execute(
            """
            SELECT c.id FROM campaigns c
            WHERE c.is_active = 1 AND EXISTS (SELECT 1 FROM questions q WHERE q.campaign_id = c.id AND q.is_active = 1)
            ORDER BY c.id DESC LIMIT 1
            """
        )
        camp_row = cursor.fetchone()
        if not camp_row:
            cursor.execute("SELECT id FROM campaigns WHERE is_active = 1 ORDER BY id DESC LIMIT 1")
            camp_row = cursor.fetchone()
        campaign_id = camp_row["id"] if camp_row else 1
        
    # 1. Obtener todas las preguntas activas para esta campaña (priorizando preguntas de calibración al inicio)
    cursor.execute(
        "SELECT id, question_text, is_calibration FROM questions WHERE is_active = 1 AND campaign_id = ? ORDER BY is_calibration DESC, order_index ASC, id ASC",
        (campaign_id,)
    )
    all_questions = cursor.fetchall()
    
    # 2. Obtener preguntas completadas por este juez
    cursor.execute(
        """
        SELECT q.id FROM judge_question_order o
        JOIN questions q ON o.question_id = q.id
        WHERE o.judge_id = ? AND o.is_completed = 1 AND q.campaign_id = ?
        """,
        (judge_id, campaign_id)
    )
    completed_ids = {row["id"] for row in cursor.fetchall()}
    
    # Seleccionar la pregunta objetivo: o la explícitamente pedida (para volver/corregir), o la primera pendiente
    target_question = None
    target_idx = 0
    if question_id is not None:
        for idx, q in enumerate(all_questions):
            if q["id"] == question_id:
                target_question = q
                target_idx = idx
                break

    if target_question is None:
        for idx, q in enumerate(all_questions):
            if q["id"] not in completed_ids:
                target_question = q
                target_idx = idx
                break
            
    progress = {
        "completed": len(completed_ids),
        "total": len(all_questions),
        "current_index": (target_idx + 1) if target_question else len(all_questions),
    }
    
    if not target_question:
        conn.close()
        return {"status": "completed", "progress": progress}
        
    q_id = target_question["id"]
    prev_question_id = all_questions[target_idx - 1]["id"] if target_idx > 0 else None
    next_question_id = all_questions[target_idx + 1]["id"] if target_idx + 1 < len(all_questions) else None
    
    # 3. Obtener o crear orden aleatorio de respuestas para este juez y pregunta
    cursor.execute(
        "SELECT response_order FROM judge_question_order WHERE judge_id = ? AND question_id = ?",
        (judge_id, q_id)
    )
    order_row = cursor.fetchone()
    
    if not order_row:
        cursor.execute("SELECT id FROM responses WHERE question_id = ?", (q_id,))
        response_rows = cursor.fetchall()
        response_ids = [r["id"] for r in response_rows]
        random.shuffle(response_ids)
        response_order_str = json.dumps(response_ids)
        cursor.execute(
            """
            INSERT INTO judge_question_order (judge_id, question_id, response_order, is_completed)
            VALUES (?, ?, ?, 0)
            """,
            (judge_id, q_id, response_order_str)
        )
        conn.commit()
    else:
        response_ids = json.loads(order_row["response_order"])
        
    # 4. Cargar los textos de las respuestas en el orden aleatorio fijado
    shuffled_responses = []
    for idx, r_id in enumerate(response_ids):
        cursor.execute("SELECT id, response_text FROM responses WHERE id = ?", (r_id,))
        r_row = cursor.fetchone()
        if r_row:
            label = chr(65 + idx) if idx < 26 else f"V{idx+1}"
            shuffled_responses.append({
                "label": label,
                "id": r_row["id"],
                "text": r_row["response_text"]
            })
            
    # 5. Obtener rúbrica activa asociada a la campaña
    cursor.execute(
        """
        SELECT c.id, c.name, c.description, c.min_score, c.max_score, c.score_descriptions FROM rubric_criteria c
        JOIN campaign_rubric cr ON c.id = cr.criterion_id
        WHERE c.is_active = 1 AND cr.campaign_id = ?
        ORDER BY c.order_index ASC
        """,
        (campaign_id,)
    )
    criteria = [dict(row) for row in cursor.fetchall()]

    # 6. Si el juez ya había evaluado esta pregunta (ej. al volver al caso anterior), devolver sus puntajes previos
    existing_evaluations: Dict[str, Any] = {}
    if response_ids:
        placeholders = ",".join("?" for _ in response_ids)
        cursor.execute(
            f"SELECT response_id, criterion_id, score, comment FROM evaluations WHERE judge_id = ? AND response_id IN ({placeholders})",
            [judge_id] + response_ids,
        )
        for erow in cursor.fetchall():
            rid_str = str(erow["response_id"])
            if rid_str not in existing_evaluations:
                existing_evaluations[rid_str] = {"ratings": {}, "comment": erow["comment"] or ""}
            existing_evaluations[rid_str]["ratings"][str(erow["criterion_id"])] = erow["score"]
            if erow["comment"]:
                existing_evaluations[rid_str]["comment"] = erow["comment"]
    
    conn.close()
    
    return {
        "status": "pending",
        "question_id": q_id,
        "question_text": target_question["question_text"],
        "is_calibration": bool(target_question["is_calibration"]),
        "is_already_evaluated": q_id in completed_ids,
        "prev_question_id": prev_question_id,
        "next_question_id": next_question_id,
        "responses": shuffled_responses,
        "rubric": criteria,
        "existing_evaluations": existing_evaluations,
        "progress": progress
    }

@app.post("/api/evaluation/submit")
def submit_evaluation(req: EvaluationSubmit, current_user: dict = Depends(get_current_user)):
    judge_id = current_user["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Validar que la pregunta exista, esté activa y pertenezca a una campaña activa
    cursor.execute(
        """
        SELECT q.campaign_id, q.is_active as q_active, COALESCE(c.is_active, 1) as c_active
        FROM questions q
        LEFT JOIN campaigns c ON q.campaign_id = c.id
        WHERE q.id = ?
        """,
        (req.question_id,),
    )
    q_row = cursor.fetchone()
    if not q_row or not q_row["q_active"]:
        conn.close()
        raise HTTPException(status_code=404, detail="La pregunta indicada no existe o está inactiva.")
    if not q_row["c_active"]:
        conn.close()
        raise HTTPException(status_code=400, detail="La campaña asociada se encuentra pausada.")
    campaign_id = q_row["campaign_id"]

    # Verificar que todos los response_id enviados pertenezcan realmente a req.question_id (evita IDOR cruzado)
    cursor.execute("SELECT id FROM responses WHERE question_id = ?", (req.question_id,))
    valid_response_ids = {r["id"] for r in cursor.fetchall()}

    cursor.execute("SELECT id, min_score, max_score FROM rubric_criteria")
    crit_bounds = {r["id"]: (r["min_score"], r["max_score"]) for r in cursor.fetchall()}

    for r_rating in req.evaluations:
        if r_rating.response_id not in valid_response_ids:
            conn.close()
            raise HTTPException(
                status_code=400,
                detail=f"La respuesta {r_rating.response_id} no pertenece a la pregunta {req.question_id}.",
            )
        if r_rating.comment and len(r_rating.comment) > 4000:
            conn.close()
            raise HTTPException(status_code=400, detail="El comentario excede el máximo de 4000 caracteres.")
        for score_item in r_rating.ratings:
            bounds = crit_bounds.get(score_item.criterion_id, (1, 5))
            if score_item.score < bounds[0] or score_item.score > bounds[1]:
                conn.close()
                raise HTTPException(
                    status_code=400,
                    detail=f"Puntaje {score_item.score} fuera de rango permitido ({bounds[0]}–{bounds[1]})."
                )

    try:
        # Guardar cada evaluación de respuesta
        for r_rating in req.evaluations:
            safe_latency = None
            if r_rating.latency_ms is not None:
                safe_latency = max(0, min(int(r_rating.latency_ms), 86_400_000))
            for score_item in r_rating.ratings:
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO evaluations (judge_id, response_id, criterion_id, score, comment, latency_ms)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        judge_id,
                        r_rating.response_id,
                        score_item.criterion_id,
                        score_item.score,
                        r_rating.comment,
                        safe_latency
                    )
                )
        
        # Marcar pregunta como completada para el juez
        cursor.execute(
            """
            UPDATE judge_question_order
            SET is_completed = 1
            WHERE judge_id = ? AND question_id = ?
            """,
            (judge_id, req.question_id)
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        logger.error(f"Error submitting evaluation: {e}")
        raise HTTPException(status_code=500, detail="Error interno al registrar la evaluación.")
        
    conn.close()
    broadcast_realtime_event(
        "evaluation_submitted",
        {"judge_id": judge_id, "question_id": req.question_id, "campaign_id": campaign_id},
    )
    return {"status": "success", "message": "Evaluación guardada correctamente."}


# ---------------- ADMIN CRUD: JUDGES ----------------

@app.get("/api/admin/judges", dependencies=[Depends(require_admin)])
def list_judges():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, display_name, role, is_active, created_at FROM judges ORDER BY id DESC")
    rows = cursor.fetchall()
    result = []
    for r in rows:
        jd = dict(r)
        cursor.execute(
            """
            SELECT c.id, c.name FROM campaigns c
            JOIN campaign_judges cj ON c.id = cj.campaign_id
            WHERE cj.judge_id = ?
            ORDER BY c.id DESC
            """,
            (jd["id"],),
        )
        jd["campaigns"] = [dict(c) for c in cursor.fetchall()]
        cursor.execute(
            "SELECT COUNT(DISTINCT r.question_id) FROM evaluations e JOIN responses r ON e.response_id = r.id WHERE e.judge_id = ?",
            (jd["id"],),
        )
        jd["evaluated_questions_count"] = cursor.fetchone()[0] or 0
        result.append(jd)
    conn.close()
    return result

@app.post("/api/admin/judges", dependencies=[Depends(require_admin)])
def create_judge(judge: JudgeCreate):
    import sqlite3
    if judge.role not in ("judge", "admin"):
        raise HTTPException(status_code=400, detail="Rol inválido: debe ser 'judge' o 'admin'.")
    if not judge.username or len(judge.username.strip()) < 2:
        raise HTTPException(status_code=400, detail="Nombre de usuario inválido.")
    if not judge.password or len(judge.password.strip()) < 4:
        raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 4 caracteres.")
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO judges (username, password, display_name, role) VALUES (?, ?, ?, ?)",
            (judge.username.strip(), hash_password(judge.password.strip()), judge.display_name.strip(), judge.role)
        )
        new_judge_id = cursor.lastrowid
        # Si es juez evaluador, asociarlo por defecto a las campañas activas y crearle orden aleatorio
        if judge.role == "judge":
            cursor.execute("SELECT id FROM campaigns WHERE is_active = 1")
            active_camps = [r["id"] for r in cursor.fetchall()]
            for cid in active_camps:
                cursor.execute(
                    "INSERT OR IGNORE INTO campaign_judges (campaign_id, judge_id) VALUES (?, ?)",
                    (cid, new_judge_id),
                )
                cursor.execute("SELECT id FROM questions WHERE campaign_id = ? AND is_active = 1", (cid,))
                for qrow in cursor.fetchall():
                    cursor.execute("SELECT id FROM responses WHERE question_id = ?", (qrow["id"],))
                    rids = [rr["id"] for rr in cursor.fetchall()]
                    if rids:
                        random.shuffle(rids)
                        cursor.execute(
                            "INSERT OR IGNORE INTO judge_question_order (judge_id, question_id, response_order, is_completed) VALUES (?, ?, ?, 0)",
                            (new_judge_id, qrow["id"], json.dumps(rids)),
                        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="El nombre de usuario ya está registrado.")
    conn.close()
    return {"status": "success", "message": "Juez creado y asignado exitosamente."}

@app.put("/api/admin/judges/{judge_id}/password", dependencies=[Depends(require_admin)])
def reset_judge_password(judge_id: int, req: PasswordResetRequest):
    if not req.password or len(req.password.strip()) < 3:
        raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 3 caracteres.")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM judges WHERE id = ?", (judge_id,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")
    cursor.execute(
        "UPDATE judges SET password = ? WHERE id = ?",
        (hash_password(req.password.strip()), judge_id),
    )
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Contraseña actualizada correctamente."}

@app.delete("/api/admin/judges/{judge_id}", dependencies=[Depends(require_admin)])
def delete_judge(judge_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT username FROM judges WHERE id = ?", (judge_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Juez no encontrado.")
    if row["username"] == "admin":
        conn.close()
        raise HTTPException(status_code=400, detail="No se puede eliminar el administrador principal.")
        
    cursor.execute("DELETE FROM judges WHERE id = ?", (judge_id,))
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Juez eliminado correctamente."}

# ---------------- ADMIN CRUD: QUESTIONS & RESPONSES ----------------

@app.get("/api/admin/questions", dependencies=[Depends(require_admin)])
def list_questions(campaign_id: Optional[int] = None):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    where_sql = "WHERE q.campaign_id = ?" if campaign_id is not None else ""
    params = [campaign_id] if campaign_id is not None else []

    cursor.execute(
        f"""
        SELECT q.id, q.campaign_id, c.name as campaign_name, q.question_text, q.order_index, q.is_calibration, q.is_active
        FROM questions q
        LEFT JOIN campaigns c ON q.campaign_id = c.id
        {where_sql}
        ORDER BY q.is_calibration DESC, q.order_index ASC, q.id DESC
        """,
        params,
    )
    questions_rows = cursor.fetchall()
    
    result = []
    for q in questions_rows:
        cursor.execute("SELECT id, response_text, source_type FROM responses WHERE question_id = ?", (q["id"],))
        resp_rows = cursor.fetchall()
        result.append({
            "id": q["id"],
            "campaign_id": q["campaign_id"],
            "campaign_name": q["campaign_name"],
            "question_text": q["question_text"],
            "order_index": q["order_index"],
            "is_calibration": bool(q["is_calibration"]),
            "is_active": bool(q["is_active"]),
            "responses": [dict(r) for r in resp_rows]
        })
        
    conn.close()
    return result

@app.post("/api/admin/questions", dependencies=[Depends(require_admin)])
def create_question(q_req: QuestionCreate):
    if len(q_req.responses) < 1:
        raise HTTPException(status_code=400, detail="Se debe proveer al menos 1 respuesta.")
        
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        campaign_id = q_req.campaign_id
        if campaign_id is None:
            cursor.execute("SELECT id FROM campaigns WHERE is_active = 1 ORDER BY id DESC LIMIT 1")
            crow = cursor.fetchone()
            if crow:
                campaign_id = crow["id"]

        cursor.execute(
            "INSERT INTO questions (question_text, campaign_id, is_calibration) VALUES (?, ?, ?)",
            (q_req.question_text, campaign_id, 1 if q_req.is_calibration else 0),
        )
        question_id = cursor.lastrowid
        
        resp_ids = []
        for resp in q_req.responses:
            cursor.execute(
                """
                INSERT INTO responses (question_id, response_text, source_type, source_metadata)
                VALUES (?, ?, ?, ?)
                """,
                (question_id, resp.response_text, resp.source_type, resp.source_metadata)
            )
            resp_ids.append(cursor.lastrowid)

        # Generar orden aleatorio para todos los jueces
        cursor.execute("SELECT id FROM judges WHERE role = 'judge'")
        for jrow in cursor.fetchall():
            jid = jrow["id"]
            if campaign_id is not None:
                cursor.execute(
                    "INSERT OR IGNORE INTO campaign_judges (campaign_id, judge_id) VALUES (?, ?)",
                    (campaign_id, jid),
                )
            shuffled = list(resp_ids)
            random.shuffle(shuffled)
            cursor.execute(
                "INSERT OR IGNORE INTO judge_question_order (judge_id, question_id, response_order, is_completed) VALUES (?, ?, ?, 0)",
                (jid, question_id, json.dumps(shuffled)),
            )

        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        logger.error(f"Error creating question: {e}")
        raise HTTPException(status_code=500, detail="Error interno al guardar la pregunta.")
        
    conn.close()
    return {"status": "success", "message": "Pregunta y respuestas creadas correctamente."}

@app.put("/api/admin/questions/{question_id}/calibration", dependencies=[Depends(require_admin)])
def toggle_question_calibration(question_id: int, req: CalibrationToggleRequest):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM questions WHERE id = ?", (question_id,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Pregunta no encontrada.")
    cursor.execute(
        "UPDATE questions SET is_calibration = ? WHERE id = ?",
        (1 if req.is_calibration else 0, question_id),
    )
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.delete("/api/admin/questions/{question_id}", dependencies=[Depends(require_admin)])
def delete_question(question_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM questions WHERE id = ?", (question_id,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Pregunta no encontrada.")
        
    cursor.execute("DELETE FROM questions WHERE id = ?", (question_id,))
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Pregunta eliminada correctamente."}

# ---------------- ADMIN CRUD: RUBRIC ----------------

@app.get("/api/admin/rubric", dependencies=[Depends(require_admin)])
def list_rubric():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, description, min_score, max_score, weight, order_index, is_active, score_descriptions FROM rubric_criteria ORDER BY order_index ASC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/admin/rubric", dependencies=[Depends(require_admin)])
def create_rubric_criterion(req: CriterionCreate):
    import sqlite3
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO rubric_criteria (name, description, min_score, max_score, weight, score_descriptions)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (req.name, req.description, req.min_score, req.max_score, req.weight, req.score_descriptions)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Ya existe un criterio con ese nombre.")
    conn.close()
    return {"status": "success", "message": "Criterio de rúbrica creado correctamente."}

@app.put("/api/admin/rubric/{criterion_id}", dependencies=[Depends(require_admin)])
def update_rubric_criterion(criterion_id: int, req: CriterionCreate):
    import sqlite3
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM rubric_criteria WHERE id = ?", (criterion_id,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Criterio de rúbrica no encontrado.")
        
    try:
        cursor.execute(
            """
            UPDATE rubric_criteria 
            SET name = ?, description = ?, min_score = ?, max_score = ?, weight = ?, score_descriptions = ?
            WHERE id = ?
            """,
            (req.name, req.description, req.min_score, req.max_score, req.weight, req.score_descriptions, criterion_id)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Ya existe un criterio con ese nombre.")
    conn.close()
    return {"status": "success", "message": "Criterio de rúbrica actualizado correctamente."}

@app.delete("/api/admin/rubric/{criterion_id}", dependencies=[Depends(require_admin)])
def delete_rubric_criterion(criterion_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM rubric_criteria WHERE id = ?", (criterion_id,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Criterio de rúbrica no encontrado.")
        
    cursor.execute("DELETE FROM rubric_criteria WHERE id = ?", (criterion_id,))
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Criterio de rúbrica eliminado correctamente."}

# ---------------- ADMIN CRUD: CAMPAIGNS ----------------

@app.get("/api/admin/campaigns", dependencies=[Depends(require_admin)])
def list_campaigns():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, description, deadline, is_active, created_at FROM campaigns ORDER BY id DESC")
    campaigns_rows = cursor.fetchall()
    
    result = []
    for camp in campaigns_rows:
        cid = camp["id"]
        cursor.execute(
            """
            SELECT c.id, c.name FROM rubric_criteria c
            JOIN campaign_rubric cr ON c.id = cr.criterion_id
            WHERE cr.campaign_id = ?
            """,
            (cid,)
        )
        criteria = [{"id": r["id"], "name": r["name"]} for r in cursor.fetchall()]

        cursor.execute(
            "SELECT COUNT(*), SUM(CASE WHEN is_calibration = 1 THEN 1 ELSE 0 END) FROM questions WHERE campaign_id = ? AND is_active = 1",
            (cid,),
        )
        qrow = cursor.fetchone()
        questions_count = int(qrow[0] or 0)
        calibration_count = int(qrow[1] or 0)

        cursor.execute(
            """
            SELECT COUNT(DISTINCT j.id) FROM judges j
            WHERE j.role = 'judge' AND (
                EXISTS (SELECT 1 FROM campaign_judges cj WHERE cj.campaign_id = ? AND cj.judge_id = j.id)
                OR EXISTS (SELECT 1 FROM judge_question_order o JOIN questions q ON o.question_id = q.id WHERE o.judge_id = j.id AND q.campaign_id = ?)
            )
            """,
            (cid, cid),
        )
        judges_count = int(cursor.fetchone()[0] or 0)

        cursor.execute(
            """
            SELECT COUNT(*) FROM evaluations e
            JOIN responses r ON e.response_id = r.id
            JOIN questions q ON r.question_id = q.id
            WHERE q.campaign_id = ?
            """,
            (cid,),
        )
        evaluations_count = int(cursor.fetchone()[0] or 0)
        
        result.append({
            "id": cid,
            "name": camp["name"],
            "description": camp["description"],
            "deadline": camp["deadline"],
            "is_active": bool(camp["is_active"]),
            "created_at": camp["created_at"],
            "criteria": criteria,
            "questions_count": questions_count,
            "calibration_count": calibration_count,
            "judges_count": judges_count,
            "evaluations_count": evaluations_count,
            "is_ready": questions_count > 0 and len(criteria) > 0 and judges_count > 0,
        })
        
    conn.close()
    return result

@app.post("/api/admin/campaigns/{campaign_id}/toggle-active", dependencies=[Depends(require_admin)])
def toggle_campaign_active(campaign_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT is_active FROM campaigns WHERE id = ?", (campaign_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Campaña no encontrada.")
    new_state = 0 if row["is_active"] else 1
    cursor.execute("UPDATE campaigns SET is_active = ? WHERE id = ?", (new_state, campaign_id))
    conn.commit()
    conn.close()
    return {"status": "success", "is_active": bool(new_state)}

@app.post("/api/admin/campaigns", dependencies=[Depends(require_admin)])
def create_campaign(req: CampaignCreate):
    import sqlite3
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO campaigns (name, description, deadline, is_active)
            VALUES (?, ?, ?, 1)
            """,
            (req.name, req.description, req.deadline)
        )
        campaign_id = cursor.lastrowid
        
        # Link criteria
        for crit_id in req.rubric_ids:
            cursor.execute(
                """
                INSERT OR IGNORE INTO campaign_rubric (campaign_id, criterion_id)
                VALUES (?, ?)
                """,
                (campaign_id, crit_id)
            )
        # Asociar jueces activos por defecto para que la campaña no nazca sin jueces
        cursor.execute("SELECT id FROM judges WHERE role = 'judge'")
        for jrow in cursor.fetchall():
            cursor.execute(
                "INSERT OR IGNORE INTO campaign_judges (campaign_id, judge_id) VALUES (?, ?)",
                (campaign_id, jrow["id"]),
            )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Ya existe una campaña con ese nombre.")
    conn.close()
    return {"status": "success", "message": "Campaña creada correctamente."}


@app.put("/api/admin/campaigns/{campaign_id}", dependencies=[Depends(require_admin)])
def update_campaign(campaign_id: int, req: CampaignCreate):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM campaigns WHERE id = ?", (campaign_id,))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Campaña no encontrada.")
        
    try:
        cursor.execute(
            """
            UPDATE campaigns
            SET name = ?, description = ?, deadline = ?
            WHERE id = ?
            """,
            (req.name, req.description, req.deadline, campaign_id)
        )
        
        # Sync criteria (delete old links and insert new)
        cursor.execute("DELETE FROM campaign_rubric WHERE campaign_id = ?", (campaign_id,))
        for crit_id in req.rubric_ids:
            cursor.execute(
                """
                INSERT OR IGNORE INTO campaign_rubric (campaign_id, criterion_id)
                VALUES (?, ?)
                """,
                (campaign_id, crit_id)
            )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Ya existe una campaña con ese nombre.")
    conn.close()
    return {"status": "success", "message": "Campaña actualizada correctamente."}

@app.delete("/api/admin/campaigns/{campaign_id}", dependencies=[Depends(require_admin)])
def delete_campaign(campaign_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM campaigns WHERE id = ?", (campaign_id,))
    camp = cursor.fetchone()
    if not camp:
        conn.close()
        raise HTTPException(status_code=404, detail="Campaña no encontrada.")
        
    if camp["name"] == "Campaña General":
        conn.close()
        raise HTTPException(status_code=400, detail="No se puede eliminar la campaña por defecto.")
        
    cursor.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Campaña eliminada correctamente."}

# ---------------- ANALYTICS & RESULTS ----------------

@app.get("/api/admin/results", dependencies=[Depends(require_admin)])
def get_evaluation_results(campaign_id: Optional[int] = None):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    where_clause = ""
    params = []
    if campaign_id is not None:
        where_clause = "JOIN questions q ON r.question_id = q.id WHERE q.campaign_id = ?"
        params.append(campaign_id)
        
    # 1. Promedio general de puntuación por tipo de fuente (Naive, RAG, Profesional)
    cursor.execute(
        f"""
        SELECT 
            r.source_type,
            AVG(e.score) as avg_score,
            COUNT(DISTINCT e.judge_id) as n_judges,
            COUNT(e.id) as n_evaluations
        FROM evaluations e
        JOIN responses r ON e.response_id = r.id
        {where_clause}
        GROUP BY r.source_type
        """,
        params
    )
    overall = [dict(row) for row in cursor.fetchall()]
    
    # 2. Promedio desglosado por Criterio y Tipo de Fuente
    where_clause_2 = ""
    if campaign_id is not None:
        where_clause_2 = "WHERE q.campaign_id = ?"
        
    cursor.execute(
        f"""
        SELECT 
            c.name as criterion_name,
            r.source_type,
            AVG(e.score) as avg_score
        FROM evaluations e
        JOIN responses r ON e.response_id = r.id
        JOIN questions q ON r.question_id = q.id
        JOIN rubric_criteria c ON e.criterion_id = c.id
        {where_clause_2}
        GROUP BY c.id, r.source_type
        ORDER BY c.order_index ASC, r.source_type ASC
        """,
        params
    )
    by_criterion = [dict(row) for row in cursor.fetchall()]
    
    conn.close()
    return {
        "overall": overall,
        "by_criterion": by_criterion
    }

@app.get("/api/admin/export", dependencies=[Depends(require_admin)])
def export_evaluations_csv():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute(
        """
        SELECT 
            j.display_name as judge_name,
            q.id as question_id,
            q.question_text,
            r.source_type,
            c.name as criterion_name,
            e.score,
            e.comment,
            e.created_at
        FROM evaluations e
        JOIN judges j ON e.judge_id = j.id
        JOIN responses r ON e.response_id = r.id
        JOIN questions q ON r.question_id = q.id
        JOIN rubric_criteria c ON e.criterion_id = c.id
        ORDER BY j.id, q.id, r.source_type, c.order_index ASC
        """
    )
    rows = cursor.fetchall()
    conn.close()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    writer.writerow([
        "juez", "pregunta_id", "pregunta_texto", "tipo_fuente", "criterio", "puntuacion", "comentario", "fecha"
    ])
    
    for r in rows:
        writer.writerow([
            sanitize_csv_cell(r["judge_name"]),
            r["question_id"],
            sanitize_csv_cell(r["question_text"]),
            sanitize_csv_cell(r["source_type"]),
            sanitize_csv_cell(r["criterion_name"]),
            r["score"],
            sanitize_csv_cell(r["comment"]),
            sanitize_csv_cell(r["created_at"]),
        ])
        
    output.seek(0)
    
    response = StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv"
    )
    response.headers["Content-Disposition"] = "attachment; filename=resultados_evaluacion_ciega.csv"
    return response

def compute_fleiss_kappa(evaluations: List[dict]) -> float:
    from collections import defaultdict
    subject_scores = defaultdict(list)
    for e in evaluations:
        subject = (e["response_id"], e["criterion_id"])
        subject_scores[subject].append(int(e["score"]))
        
    if not subject_scores:
        return 0.0
        
    valid_subjects = {k: v for k, v in subject_scores.items() if len(v) >= 2}
    if not valid_subjects:
        scores_set = {e["score"] for e in evaluations}
        if len(scores_set) == 1:
            return 1.0
        return 0.0
        
    N = len(valid_subjects)
    raters_counts = [len(scores) for scores in valid_subjects.values()]
    n = sum(raters_counts) / len(raters_counts)
    
    if n <= 1:
        return 0.0
        
    table = []
    for subject, scores in valid_subjects.items():
        counts = [0]*5
        for s in scores:
            if 1 <= s <= 5:
                counts[s-1] += 1
        table.append(counts)
        
    p = [0.0]*5
    total_ratings = N * n
    for j in range(5):
        p[j] = sum(table[i][j] for i in range(N)) / total_ratings
        
    P = []
    for i in range(N):
        n_i = sum(table[i])
        if n_i <= 1:
            P.append(0.0)
            continue
        sum_sq = sum(c**2 for c in table[i])
        P_i = (sum_sq - n_i) / (n_i * (n_i - 1))
        P.append(P_i)
        
    P_bar = sum(P) / N
    P_e_bar = sum(p_j**2 for p_j in p)
    
    denominator = 1.0 - P_e_bar
    if abs(denominator) < 1e-9:
        if abs(P_bar - 1.0) < 1e-9:
            return 1.0
        return 0.0
        
    kappa = (P_bar - P_e_bar) / denominator
    return round(kappa, 4)

def compute_median(lst):
    if not lst:
        return 0.0
    sorted_lst = sorted(lst)
    n = len(sorted_lst)
    if n % 2 == 1:
        return float(sorted_lst[n // 2])
    else:
        return float((sorted_lst[n // 2 - 1] + sorted_lst[n // 2]) / 2.0)

@app.post("/api/admin/upload", dependencies=[Depends(require_admin)])
async def upload_csv(campaign_id: Optional[int] = None, file: UploadFile = File(...)):
    contents = await file.read(MAX_CSV_UPLOAD_BYTES + 1)
    if len(contents) > MAX_CSV_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="El archivo CSV supera el límite permitido de 10 MB.")
    try:
        decoded = contents.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            decoded = contents.decode("latin-1")
        except Exception:
            raise HTTPException(status_code=400, detail="Encoding incorrecto. Por favor use UTF-8.")
            
    reader = csv.reader(io.StringIO(decoded))
    try:
        headers = next(reader)
    except StopIteration:
        raise HTTPException(status_code=400, detail="El archivo CSV está vacío.")
        
    if "pregunta" not in headers:
        raise HTTPException(status_code=400, detail="Columna 'pregunta' requerida no encontrada.")
        
    variant_headers = [h for h in headers if h not in ["pregunta", "is_calibration"]]
    if not variant_headers:
        raise HTTPException(status_code=400, detail="Se requiere al menos una columna de respuesta variante.")
        
    rows_parsed = 0
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Verificar si la campaña existe cuando se especifica campaign_id
    if campaign_id is not None:
        cursor.execute("SELECT id FROM campaigns WHERE id = ?", (campaign_id,))
        if not cursor.fetchone():
            conn.close()
            raise HTTPException(status_code=404, detail="La campaña seleccionada no existe.")
        
    import sqlite3
    try:
        for row in reader:
            if not row:
                continue
            if len(row) < len(headers):
                raise HTTPException(status_code=400, detail="Fila con menor cantidad de columnas que las cabeceras.")
                
            pregunta_idx = headers.index("pregunta")
            question_text = row[pregunta_idx].strip()
            if not question_text:
                continue
                
            if campaign_id is not None:
                cursor.execute("SELECT id FROM questions WHERE question_text = ? AND campaign_id = ?", (question_text, campaign_id))
            else:
                cursor.execute("SELECT id FROM questions WHERE question_text = ? AND campaign_id IS NULL", (question_text,))
            q_row = cursor.fetchone()
            if q_row:
                continue
                
            is_calib_val = 0
            if "is_calibration" in headers:
                calib_idx = headers.index("is_calibration")
                if calib_idx < len(row):
                    try:
                        is_calib_val = int(row[calib_idx].strip())
                    except ValueError:
                        pass
                        
            cursor.execute("INSERT INTO questions (question_text, campaign_id, is_calibration) VALUES (?, ?, ?)", (question_text, campaign_id, is_calib_val))
            question_id = cursor.lastrowid
            
            for v_header in variant_headers:
                v_idx = headers.index(v_header)
                response_text = row[v_idx].strip()
                cursor.execute(
                    """
                    INSERT INTO responses (question_id, response_text, source_type)
                    VALUES (?, ?, ?)
                    """,
                    (question_id, response_text, v_header)
                )
                
            cursor.execute("SELECT id FROM judges WHERE role = 'judge'")
            judge_ids = [r["id"] for r in cursor.fetchall()]
            
            for j_id in judge_ids:
                if campaign_id is not None:
                    cursor.execute(
                        "INSERT OR IGNORE INTO campaign_judges (campaign_id, judge_id) VALUES (?, ?)",
                        (campaign_id, j_id),
                    )
                cursor.execute("SELECT id FROM responses WHERE question_id = ?", (question_id,))
                resp_rows = cursor.fetchall()
                resp_ids = [r["id"] for r in resp_rows]
                random.shuffle(resp_ids)
                order_str = json.dumps(resp_ids)
                
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO judge_question_order (judge_id, question_id, response_order, is_completed)
                    VALUES (?, ?, ?, 0)
                    """,
                    (j_id, question_id, order_str)
                )
                
            rows_parsed += 1
            
        conn.commit()
    except HTTPException as he:
        conn.rollback()
        raise he
    except Exception as e:
        conn.rollback()
        logger.error(f"Error parsing CSV upload: {e}")
        raise HTTPException(status_code=400, detail="Error al procesar el archivo CSV. Verifique el formato de las columnas.")
    finally:
        conn.close()
        
    broadcast_realtime_event("dataset_uploaded", {"campaign_id": campaign_id, "parsed_count": rows_parsed})
    return {
        "status": "success",
        "message": f"CSV importado correctamente. {rows_parsed} preguntas ingresadas.",
        "parsed_count": rows_parsed
    }

@app.post("/api/admin/rubric/import-csv", dependencies=[Depends(require_admin)])
async def import_rubric_csv(campaign_id: Optional[int] = None, file: UploadFile = File(...)):
    contents = await file.read(MAX_CSV_UPLOAD_BYTES + 1)
    if len(contents) > MAX_CSV_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="El archivo CSV supera el límite permitido de 10 MB.")
    try:
        decoded = contents.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            decoded = contents.decode("latin-1")
        except Exception:
            raise HTTPException(status_code=400, detail="Encoding incorrecto. Por favor use UTF-8.")
            
    reader = csv.reader(io.StringIO(decoded))
    try:
        headers = next(reader)
    except StopIteration:
        raise HTTPException(status_code=400, detail="El archivo CSV está vacío.")
        
    headers = [h.strip().lower() for h in headers]
    if "nombre" not in headers:
        raise HTTPException(status_code=400, detail="Columna 'nombre' requerida no encontrada.")
        
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if campaign_id is not None:
        cursor.execute("SELECT id FROM campaigns WHERE id = ?", (campaign_id,))
        if not cursor.fetchone():
            conn.close()
            raise HTTPException(status_code=404, detail="La campaña seleccionada no existe.")
            
    rows_parsed = 0
    try:
        for row in reader:
            if not row:
                continue
            if len(row) < len(headers):
                continue
                
            nombre_idx = headers.index("nombre")
            name = row[nombre_idx].strip()
            if not name:
                continue
                
            desc = ""
            if "descripcion" in headers:
                desc = row[headers.index("descripcion")].strip()
            elif "descripcion_general" in headers:
                desc = row[headers.index("descripcion_general")].strip()
                
            min_score = 1
            if "min_score" in headers:
                try:
                    min_score = int(row[headers.index("min_score")].strip())
                except ValueError:
                    pass
                    
            max_score = 5
            if "max_score" in headers:
                try:
                    max_score = int(row[headers.index("max_score")].strip())
                except ValueError:
                    pass
                    
            weight = 1.0
            if "peso" in headers:
                try:
                    weight = float(row[headers.index("peso")].strip())
                except ValueError:
                    pass
            elif "weight" in headers:
                try:
                    weight = float(row[headers.index("weight")].strip())
                except ValueError:
                    pass
                    
            score_descs = {}
            for idx, h in enumerate(headers):
                if h.startswith("descripcion_"):
                    parts = h.split("_")
                    if len(parts) >= 2 and parts[1].isdigit():
                        val_str = parts[1]
                        val_desc = row[idx].strip()
                        if val_desc:
                            score_descs[val_str] = val_desc
                            
            score_descriptions_json = json.dumps(score_descs, ensure_ascii=False) if score_descs else None
            
            cursor.execute("SELECT id FROM rubric_criteria WHERE name = ?", (name,))
            existing = cursor.fetchone()
            
            if existing:
                criterion_id = existing["id"]
                cursor.execute(
                    """
                    UPDATE rubric_criteria
                    SET description = ?, min_score = ?, max_score = ?, weight = ?, score_descriptions = ?
                    WHERE id = ?
                    """,
                    (desc, min_score, max_score, weight, score_descriptions_json, criterion_id)
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO rubric_criteria (name, description, min_score, max_score, weight, score_descriptions)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (name, desc, min_score, max_score, weight, score_descriptions_json)
                )
                criterion_id = cursor.lastrowid
                
            if campaign_id is not None:
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO campaign_rubric (campaign_id, criterion_id)
                    VALUES (?, ?)
                    """,
                    (campaign_id, criterion_id)
                )
                
            rows_parsed += 1
            
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Error importing rubric CSV: {e}")
        raise HTTPException(status_code=400, detail=f"Error al procesar el archivo CSV de rúbrica: {str(e)}")
    finally:
        conn.close()
        
    broadcast_realtime_event("rubric_imported", {"campaign_id": campaign_id, "parsed_count": rows_parsed})
    return {
        "status": "success",
        "message": f"Rúbrica importada correctamente. {rows_parsed} criterios ingresados/actualizados.",
        "parsed_count": rows_parsed
    }

@app.get("/api/admin/campaigns/{campaign_id}/questions", dependencies=[Depends(require_admin)])
def list_campaign_questions(campaign_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, question_text, order_index, is_calibration, is_active FROM questions WHERE campaign_id = ? ORDER BY is_calibration DESC, order_index ASC, id DESC", (campaign_id,))
    questions_rows = cursor.fetchall()
    conn.close()
    return [dict(q) for q in questions_rows]

@app.post("/api/admin/campaigns/{campaign_id}/questions/assign", dependencies=[Depends(require_admin)])
def assign_questions_to_campaign(campaign_id: int, req: AssignQuestionsRequest):
    conn = get_db_connection()
    cursor = conn.cursor()
    for q_id in req.question_ids:
        cursor.execute("UPDATE questions SET campaign_id = ? WHERE id = ?", (campaign_id, q_id))
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Preguntas asignadas correctamente."}

@app.post("/api/admin/campaigns/{campaign_id}/rubrics/assign", dependencies=[Depends(require_admin)])
async def assign_campaign_rubric(campaign_id: int, request: Request):
    data = await request.json()
    criterion_id = data.get("criterion_id")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO campaign_rubric (campaign_id, criterion_id) VALUES (?, ?)", (campaign_id, criterion_id))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.post("/api/admin/campaigns/{campaign_id}/rubrics/remove", dependencies=[Depends(require_admin)])
async def remove_campaign_rubric(campaign_id: int, request: Request):
    data = await request.json()
    criterion_id = data.get("criterion_id")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM campaign_rubric WHERE campaign_id = ? AND criterion_id = ?", (campaign_id, criterion_id))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.post("/api/admin/campaigns/{campaign_id}/questions/remove", dependencies=[Depends(require_admin)])
def remove_questions_from_campaign(campaign_id: int, req: RemoveQuestionsRequest):
    conn = get_db_connection()
    cursor = conn.cursor()
    for q_id in req.question_ids:
        cursor.execute("UPDATE questions SET campaign_id = NULL WHERE id = ?", (q_id,))
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Preguntas desasociadas de la campaña."}

@app.get("/api/admin/variants", dependencies=[Depends(require_admin)])
def list_unique_variants():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT source_type FROM responses WHERE source_type IS NOT NULL")
    rows = cursor.fetchall()
    conn.close()
    return [r["source_type"] for r in rows]

@app.get("/api/admin/campaigns/{campaign_id}/judges", dependencies=[Depends(require_admin)])
def get_campaign_judges(campaign_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Jueces asignados
    cursor.execute("""
        SELECT j.id, j.username, j.display_name 
        FROM judges j
        INNER JOIN campaign_judges cj ON j.id = cj.judge_id
        WHERE cj.campaign_id = ? AND j.role = 'judge'
    """, (campaign_id,))
    assigned = [dict(r) for r in cursor.fetchall()]
    
    # Jueces no asignados
    cursor.execute("""
        SELECT j.id, j.username, j.display_name 
        FROM judges j
        WHERE j.role = 'judge' AND j.id NOT IN (
            SELECT judge_id FROM campaign_judges WHERE campaign_id = ?
        )
    """, (campaign_id,))
    unassigned = [dict(r) for r in cursor.fetchall()]
    
    conn.close()
    return {"assigned": assigned, "unassigned": unassigned}

@app.post("/api/admin/campaigns/{campaign_id}/judges/assign", dependencies=[Depends(require_admin)])
async def assign_campaign_judge(campaign_id: int, request: Request):
    data = await request.json()
    judge_id = data.get("judge_id")
    if not judge_id:
        raise HTTPException(status_code=400, detail="Falta el ID del juez")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO campaign_judges (campaign_id, judge_id) VALUES (?, ?)", (campaign_id, judge_id))
    conn.commit()
    conn.close()
    return {"status": "success"}

@app.post("/api/admin/campaigns/{campaign_id}/judges/remove", dependencies=[Depends(require_admin)])
async def remove_campaign_judge(campaign_id: int, request: Request):
    data = await request.json()
    judge_id = data.get("judge_id")
    if not judge_id:
        raise HTTPException(status_code=400, detail="Falta el ID del juez")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM campaign_judges WHERE campaign_id = ? AND judge_id = ?", (campaign_id, judge_id))
    conn.commit()
    conn.close()
    return {"status": "success"}

# ---------------- EVALUATION ENDPOINTS (JUECES) ----------------

@app.get("/api/evaluation/campaigns/active")
def get_active_campaigns(current_user: dict = Depends(get_current_user)):
    judge_id = current_user["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT c.id, c.name, c.description 
        FROM campaigns c
        LEFT JOIN campaign_judges cj ON c.id = cj.campaign_id
        WHERE c.is_active = 1 AND (
            cj.judge_id = ?
            OR NOT EXISTS (SELECT 1 FROM campaign_judges cj2 WHERE cj2.campaign_id = c.id)
            OR EXISTS (
                SELECT 1 FROM judge_question_order o
                JOIN questions q ON o.question_id = q.id
                WHERE o.judge_id = ? AND q.campaign_id = c.id
            )
        )
        AND EXISTS (SELECT 1 FROM questions q2 WHERE q2.campaign_id = c.id AND q2.is_active = 1)
        ORDER BY c.id DESC
    """, (judge_id, judge_id))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/api/evaluation/metrics")
def get_judge_metrics(campaign_id: Optional[int] = None, current_user: dict = Depends(get_current_user)):
    judge_id = current_user["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if campaign_id is None:
        cursor.execute(
            """
            SELECT c.id FROM campaigns c
            WHERE c.is_active = 1 AND EXISTS (SELECT 1 FROM questions q WHERE q.campaign_id = c.id AND q.is_active = 1)
            ORDER BY c.id DESC LIMIT 1
            """
        )
        camp_row = cursor.fetchone()
        if not camp_row:
            cursor.execute("SELECT id FROM campaigns WHERE is_active = 1 ORDER BY id DESC LIMIT 1")
            camp_row = cursor.fetchone()
        campaign_id = camp_row["id"] if camp_row else 1
        
    cursor.execute("SELECT COUNT(*) FROM questions WHERE is_active = 1 AND campaign_id = ?", (campaign_id,))
    total_questions = cursor.fetchone()[0]
    
    cursor.execute(
        """
        SELECT COUNT(DISTINCT o.question_id) FROM judge_question_order o
        JOIN questions q ON o.question_id = q.id
        WHERE o.judge_id = ? AND o.is_completed = 1 AND q.campaign_id = ?
        """,
        (judge_id, campaign_id)
    )
    completed_questions = cursor.fetchone()[0]
    pending_questions = max(0, total_questions - completed_questions)
    
    cursor.execute(
        """
        SELECT AVG(max_lat) FROM (
            SELECT r.question_id, MAX(e.latency_ms) as max_lat FROM evaluations e
            JOIN responses r ON e.response_id = r.id
            JOIN questions q ON r.question_id = q.id
            WHERE e.judge_id = ? AND q.campaign_id = ? AND e.latency_ms IS NOT NULL
            GROUP BY r.question_id
        )
        """,
        (judge_id, campaign_id)
    )
    avg_lat = cursor.fetchone()[0]
    avg_latency_sec = round((avg_lat / 1000.0), 1) if avg_lat is not None else 0.0
    
    cursor.execute(
        """
        SELECT AVG(score) FROM evaluations e
        JOIN responses r ON e.response_id = r.id
        JOIN questions q ON r.question_id = q.id
        WHERE e.judge_id = ? AND q.campaign_id = ?
        """,
        (judge_id, campaign_id)
    )
    avg_score_val = cursor.fetchone()[0]
    avg_score = round(avg_score_val, 2) if avg_score_val is not None else 0.0

    # Obtener rúbrica activa de la campaña para mostrar guía previa al juez
    cursor.execute(
        """
        SELECT c.id, c.name, c.description, c.min_score, c.max_score, c.score_descriptions
        FROM rubric_criteria c
        JOIN campaign_rubric cr ON c.id = cr.criterion_id
        WHERE c.is_active = 1 AND cr.campaign_id = ?
        ORDER BY c.order_index ASC
        """,
        (campaign_id,),
    )
    rubric = [dict(r) for r in cursor.fetchall()]

    # Si aún no tiene tiempo promedio propio, estimar ~60s por caso pendiente
    sec_per_case = avg_latency_sec if avg_latency_sec > 0 else 60.0
    estimated_remaining_sec = int(round(pending_questions * sec_per_case))
    
    conn.close()
    
    return {
        "completed": completed_questions,
        "total": total_questions,
        "pending": pending_questions,
        "avg_latency_sec": avg_latency_sec,
        "estimated_remaining_sec": estimated_remaining_sec,
        "avg_score": avg_score,
        "rubric": rubric,
    }



def _compute_agreement_bundle(evals: List[Dict[str, Any]], include_bootstrap: bool = False) -> Dict[str, Any]:
    """
    Calcula el paquete de métricas de acuerdo sobre una lista de evaluaciones.
    """
    alpha_ord = compute_krippendorff_alpha(evals, level="ordinal")
    alpha_int = compute_krippendorff_alpha(evals, level="interval")
    alpha_nom = compute_krippendorff_alpha(evals, level="nominal")
    f_kappa = stats_fleiss_kappa(evals)
    gwet = compute_gwet_ac2(evals)
    pct = compute_percentage_agreements(evals)
    icc = compute_icc_two_way_random(evals)

    ord_ci = bootstrap_krippendorff_ci(evals, level="ordinal") if include_bootstrap else None
    int_ci = bootstrap_krippendorff_ci(evals, level="interval") if include_bootstrap else None

    unit_counts: Dict[ Any, int] = defaultdict(int)
    raters = set()
    for e in evals:
        if e.get("score") is not None:
            unit_counts[(int(e["response_id"]), int(e["criterion_id"]))] += 1
            raters.add(int(e["judge_id"]))
    n_pairable_units = sum(1 for c in unit_counts.values() if c >= 2)

    return {
        "krippendorff_ordinal": alpha_ord,
        "krippendorff_ordinal_ci95": list(ord_ci) if ord_ci else None,
        "krippendorff_ordinal_interpretation": interpret_agreement(alpha_ord, "krippendorff"),
        "krippendorff_interval": alpha_int,
        "krippendorff_interval_ci95": list(int_ci) if int_ci else None,
        "krippendorff_nominal": alpha_nom,
        "fleiss_kappa": f_kappa,
        "fleiss_interpretation": interpret_agreement(f_kappa, "kappa"),
        "gwet_ac2": gwet,
        "gwet_interpretation": interpret_agreement(gwet, "kappa"),
        "icc_2_1": icc["icc_2_1"],
        "icc_2_k": icc["icc_2_k"],
        "icc_n_targets": icc["n_targets"],
        "icc_n_judges": icc["n_judges"],
        "exact_agreement_pct": pct["exact_pct"],
        "within_one_pct": pct["within_one_pct"],
        "n_pairable_units": n_pairable_units,
        "n_raters": len(raters),
        "n_evaluations": len(evals),
    }


@app.get("/api/admin/metrics", dependencies=[Depends(require_admin)])
def get_admin_metrics(campaign_id: Optional[int] = None):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. Cargar todas las evaluaciones enriquecidas con metadatos de pregunta, respuesta, criterio y juez
    where_clause = "WHERE q.campaign_id = ?" if campaign_id is not None else ""
    params = [campaign_id] if campaign_id is not None else []

    cursor.execute(
        f"""
        SELECT 
            e.id,
            e.judge_id,
            j.username as judge_username,
            j.display_name as judge_name,
            e.response_id,
            r.source_type,
            r.response_text,
            r.question_id,
            q.question_text,
            q.is_calibration,
            q.campaign_id,
            e.criterion_id,
            c.name as criterion_name,
            e.score,
            e.comment,
            e.latency_ms,
            e.created_at
        FROM evaluations e
        JOIN responses r ON e.response_id = r.id
        JOIN questions q ON r.question_id = q.id
        JOIN rubric_criteria c ON e.criterion_id = c.id
        JOIN judges j ON e.judge_id = j.id
        {where_clause}
        """,
        params,
    )
    evaluations = [dict(r) for r in cursor.fetchall()]

    # Subconjuntos de calibración y producción
    evals_calib = [e for e in evaluations if e.get("is_calibration")]
    evals_prod = [e for e in evaluations if not e.get("is_calibration")]

    q_filter_sql = "WHERE campaign_id = ? AND is_active = 1" if campaign_id is not None else "WHERE is_active = 1"
    cursor.execute(f"SELECT COUNT(*), SUM(CASE WHEN is_calibration = 1 THEN 1 ELSE 0 END) FROM questions {q_filter_sql}", params)
    q_counts_row = cursor.fetchone()
    total_questions = int(q_counts_row[0] or 0)
    calibration_questions = int(q_counts_row[1] or 0)
    has_calibration = calibration_questions > 0

    # Acuerdo global, calibración y producción
    global_agreement = _compute_agreement_bundle(evaluations, include_bootstrap=True)
    calib_agreement = _compute_agreement_bundle(evals_calib, include_bootstrap=False) if has_calibration else None
    prod_agreement = _compute_agreement_bundle(evals_prod, include_bootstrap=False) if evals_prod else None

    # Mantener compatibilidad con campo legacy fleiss_kappa y exponer calibration_fleiss_kappa sin sobrescribirlo
    kappa_global = global_agreement["fleiss_kappa"] if global_agreement["fleiss_kappa"] is not None else 0.0
    kappa_calibration = (
        calib_agreement["fleiss_kappa"]
        if (calib_agreement and calib_agreement["fleiss_kappa"] is not None)
        else kappa_global
    )

    # Desglose de acuerdo por criterio de rúbrica
    by_crit_evals: Dict[Tuple[int, str], List[Dict[str, Any]]] = defaultdict(list)
    for e in evaluations:
        by_crit_evals[(int(e["criterion_id"]), str(e["criterion_name"]))].append(e)

    agreement_by_criterion = []
    for (cid, cname), c_evals in sorted(by_crit_evals.items(), key=lambda x: x[0][0]):
        bundle = _compute_agreement_bundle(c_evals, include_bootstrap=False)
        c_scores = [float(x["score"]) for x in c_evals if x.get("score") is not None]
        agreement_by_criterion.append({
            "criterion_id": cid,
            "criterion_name": cname,
            "krippendorff_ordinal": bundle["krippendorff_ordinal"],
            "krippendorff_interval": bundle["krippendorff_interval"],
            "fleiss_kappa": bundle["fleiss_kappa"],
            "gwet_ac2": bundle["gwet_ac2"],
            "exact_pct": bundle["exact_agreement_pct"],
            "within_one_pct": bundle["within_one_pct"],
            "mean_score": round(compute_mean(c_scores), 2) if c_scores else 0.0,
            "sd_score": round(compute_sd(c_scores), 2) if len(c_scores) > 1 else 0.0,
            "count": len(c_scores),
        })

    # Desglose de acuerdo por fuente/variante
    by_src_evals: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for e in evaluations:
        by_src_evals[str(e["source_type"])].append(e)

    agreement_by_source = []
    for src_name, s_evals in sorted(by_src_evals.items()):
        bundle = _compute_agreement_bundle(s_evals, include_bootstrap=False)
        agreement_by_source.append({
            "source_type": src_name,
            "krippendorff_ordinal": bundle["krippendorff_ordinal"],
            "krippendorff_interval": bundle["krippendorff_interval"],
            "fleiss_kappa": bundle["fleiss_kappa"],
            "gwet_ac2": bundle["gwet_ac2"],
            "exact_pct": bundle["exact_agreement_pct"],
            "within_one_pct": bundle["within_one_pct"],
            "count": len(s_evals),
        })

    # Lista de jueces (para matriz juez x juez y diagnósticos de piloto)
    if campaign_id is not None:
        cursor.execute(
            """
            SELECT DISTINCT j.id, j.username, j.display_name, j.role, j.is_active
            FROM judges j
            WHERE j.role = 'judge' AND (
                EXISTS (SELECT 1 FROM campaign_judges cj WHERE cj.campaign_id = ? AND cj.judge_id = j.id)
                OR EXISTS (SELECT 1 FROM judge_question_order o JOIN questions q ON o.question_id = q.id WHERE o.judge_id = j.id AND q.campaign_id = ?)
                OR EXISTS (SELECT 1 FROM evaluations e JOIN responses r ON e.response_id = r.id JOIN questions q ON r.question_id = q.id WHERE e.judge_id = j.id AND q.campaign_id = ?)
                OR NOT EXISTS (SELECT 1 FROM campaign_judges cj2 WHERE cj2.campaign_id = ?)
            )
            ORDER BY j.id ASC
            """,
            (campaign_id, campaign_id, campaign_id, campaign_id),
        )
    else:
        cursor.execute("SELECT id, username, display_name, role, is_active FROM judges WHERE role = 'judge' ORDER BY id ASC")
    judges_list = [dict(r) for r in cursor.fetchall()]
    judges_map = {int(j["id"]): j for j in judges_list}

    pairwise_judges = compute_pairwise_judge_matrix(evaluations, judges_map=judges_map)
    global_agreement["calibration"] = calib_agreement
    global_agreement["production"] = prod_agreement
    global_agreement["by_criterion"] = agreement_by_criterion
    global_agreement["by_source"] = agreement_by_source
    global_agreement["pairwise_judges"] = pairwise_judges

    # Latencias por pregunta evaluada (evitando triplicar por criterio)
    question_latencies_ms = []
    seen_q_eval = set()
    for e in evaluations:
        if e.get("latency_ms") is not None:
            key = (e["judge_id"], e["question_id"])
            if key not in seen_q_eval:
                seen_q_eval.add(key)
                question_latencies_ms.append(float(e["latency_ms"]))

    avg_latency = round(compute_mean(question_latencies_ms), 1) if question_latencies_ms else 0.0
    median_latency = round(compute_median(question_latencies_ms), 1) if question_latencies_ms else 0.0

    # 2. Sesiones de Jueces
    session_params = [campaign_id] if campaign_id is not None else []
    session_where = "WHERE s.campaign_id = ?" if campaign_id is not None else ""
    cursor.execute(
        f"""
        SELECT 
            j.id as judge_id,
            j.display_name,
            j.username,
            s.started_at,
            s.last_active,
            s.completed_at,
            s.session_duration_sec,
            CAST(strftime('%s', 'now') - strftime('%s', s.last_active) AS INTEGER) as seconds_since_active,
            (SELECT COUNT(DISTINCT q.id) FROM evaluations e JOIN responses r ON e.response_id = r.id JOIN questions q ON r.question_id = q.id WHERE e.judge_id = j.id {("AND q.campaign_id = ?" if campaign_id else "")}) as evaluated_questions
        FROM judges j
        JOIN judge_sessions s ON j.id = s.judge_id
        {session_where}
        ORDER BY s.last_active DESC
        """,
        ([campaign_id] + session_params) if campaign_id else session_params
    )
    sessions = [dict(r) for r in cursor.fetchall()]

    # 3. Métricas detalladas por fuente/variante + tests inferenciales (Friedman / Wilcoxon)
    source_scores: Dict[str, List[float]] = defaultdict(list)
    for e in evaluations:
        if e.get("score") is not None:
            source_scores[str(e["source_type"])].append(float(e["score"]))

    source_metrics = []
    for src, scores in sorted(source_scores.items()):
        avg = compute_mean(scores)
        var = compute_variance(scores, sample=True)
        sd = compute_sd(scores, sample=True)
        med = compute_median(scores)
        iqr = compute_iqr(scores)
        ci_lo, ci_hi = compute_ci95_mean(scores)
        dist = {str(k): sum(1 for s in scores if int(round(s)) == k) for k in range(1, 6)}
        source_metrics.append({
            "source_type": src,
            "avg_score": round(avg, 2),
            "sd_score": round(sd, 2),
            "median_score": round(med, 2),
            "iqr_score": round(iqr, 2),
            "variance": round(var, 3),
            "ci95_low": ci_lo,
            "ci95_high": ci_hi,
            "count": len(scores),
            "distribution": dist,
        })

    inferential = compute_variant_inferential_tests(evaluations)

    # 4. Diagnósticos de gestión del piloto y auditoría de desacuerdos
    judge_diagnostics = compute_judge_diagnostics(
        evaluations=evaluations,
        judges_list=judges_list,
        sessions_list=sessions,
        total_campaign_questions=total_questions,
        pairwise_matrix=pairwise_judges,
    )
    disagreement_audit = compute_disagreement_audit(evaluations, top_n=15)

    n_active_judges = len(judges_list)
    expected_q_evals = total_questions * n_active_judges
    completed_q_evals = sum(jd["completed_questions"] for jd in judge_diagnostics)
    completion_pct = round((completed_q_evals / expected_q_evals) * 100.0, 1) if expected_q_evals > 0 else 0.0
    online_judges_count = sum(1 for jd in judge_diagnostics if jd["is_online"])

    # Ritmo (preguntas por hora de sesión activa o por latencia mediana) y ETA
    total_active_sec = sum(jd["active_duration_sec"] for jd in judge_diagnostics)
    if total_active_sec > 30 and completed_q_evals > 0:
        pace_q_per_hour = round((completed_q_evals / (total_active_sec / 3600.0)), 1)
    elif median_latency > 0 and completed_q_evals > 0:
        pace_q_per_hour = round(3600.0 / (median_latency / 1000.0), 1)
    else:
        pace_q_per_hour = None

    remaining_q_evals = max(0, expected_q_evals - completed_q_evals)
    eta_hours = (
        round(remaining_q_evals / pace_q_per_hour, 1)
        if (pace_q_per_hour and pace_q_per_hour > 0 and remaining_q_evals > 0)
        else (0.0 if remaining_q_evals == 0 else None)
    )

    pilot_management = {
        "total_questions": total_questions,
        "calibration_questions": calibration_questions,
        "total_judges": n_active_judges,
        "online_judges_count": online_judges_count,
        "expected_question_evaluations": expected_q_evals,
        "completed_question_evaluations": completed_q_evals,
        "completion_pct": completion_pct,
        "pace_questions_per_hour": pace_q_per_hour,
        "estimated_hours_remaining": eta_hours,
        "judge_diagnostics": judge_diagnostics,
        "disagreement_audit": disagreement_audit,
    }

    # 5. Justificaciones / comentarios cualitativos
    source_params = [campaign_id] if campaign_id is not None else []
    cursor.execute(
        f"""
        SELECT r.source_type, e.comment, e.score, c.name as criterion_name, j.display_name as judge_name, q.question_text 
        FROM evaluations e
        JOIN responses r ON e.response_id = r.id
        JOIN questions q ON r.question_id = q.id
        JOIN rubric_criteria c ON e.criterion_id = c.id
        JOIN judges j ON e.judge_id = j.id
        WHERE e.comment IS NOT NULL AND e.comment != '' {("AND q.campaign_id = ?" if campaign_id else "")}
        ORDER BY e.id DESC
        LIMIT 40
        """,
        source_params
    )
    comments = [dict(r) for r in cursor.fetchall()]
    
    # 6. Fecha límite (deadline)
    deadline = None
    days_left = None
    if campaign_id is not None:
        cursor.execute("SELECT deadline FROM campaigns WHERE id = ?", (campaign_id,))
        camp_row = cursor.fetchone()
        if camp_row:
            deadline = camp_row["deadline"]
            if deadline:
                try:
                    cursor.execute("SELECT CAST(julianday(?) - julianday('now') AS INTEGER)", (deadline,))
                    days_row = cursor.fetchone()
                    if days_row:
                        days_left = days_row[0]
                except Exception:
                    pass
                    
    conn.close()
    
    return {
        "fleiss_kappa": kappa_global,
        "calibration_fleiss_kappa": kappa_calibration,
        "average_latency_ms": avg_latency,
        "median_latency_ms": median_latency,
        "total_evaluations": len(evaluations),
        "sessions": sessions,
        "source_metrics": source_metrics,
        "justifications": comments,
        "deadline": deadline,
        "days_left": days_left,
        "agreement": global_agreement,
        "inferential": inferential,
        "pilot_management": pilot_management,
    }


@app.get("/api/admin/stream")
async def stream_admin_events(request: Request, current_user: dict = Depends(require_admin)):
    """
    Endpoint Server-Sent Events (SSE) para actualizar en tiempo real el dashboard
    de administración cuando los jueces envían evaluaciones o latidos de sesión.
    """
    if len(_sse_subscribers) >= MAX_SSE_SUBSCRIBERS:
        # Evict oldest subscriber queue to avoid connection exhaustion
        try:
            _sse_subscribers.pop(0)
        except IndexError:
            pass
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    _sse_subscribers.append(queue)

    async def event_generator():
        try:
            yield f"data: {json.dumps({'event': 'connected', 'timestamp': int(time.time())})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if queue in _sse_subscribers:
                _sse_subscribers.remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/admin/export/report", dependencies=[Depends(require_admin)])
def export_statistical_report(campaign_id: Optional[int] = None):
    """
    Exporta un reporte JSON completo con todas las métricas de acuerdo (Krippendorff,
    Fleiss, Gwet, ICC, matriz juez x juez), pruebas de Friedman/Wilcoxon y diagnósticos
    del piloto.
    """
    report_data = get_admin_metrics(campaign_id=campaign_id)
    content = json.dumps(report_data, indent=2, ensure_ascii=False)
    response = StreamingResponse(iter([content]), media_type="application/json")
    suffix = f"_campana_{campaign_id}" if campaign_id else "_global"
    response.headers["Content-Disposition"] = f"attachment; filename=reporte_estadistico_piloto{suffix}.json"
    return response


@app.get("/api/admin/export/anova", dependencies=[Depends(require_admin)])
def export_anova_csv(campaign_id: Optional[int] = None):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    where_sql = "WHERE q.campaign_id = ?" if campaign_id is not None else ""
    params = [campaign_id] if campaign_id is not None else []

    cursor.execute(
        f"""
        SELECT 
            j.username as judge_name,
            q.id as question_id,
            q.is_calibration,
            r.source_type as variant,
            c.name as criterion_name,
            e.score,
            e.latency_ms as latency
        FROM evaluations e
        JOIN judges j ON e.judge_id = j.id
        JOIN responses r ON e.response_id = r.id
        JOIN questions q ON r.question_id = q.id
        JOIN rubric_criteria c ON e.criterion_id = c.id
        {where_sql}
        ORDER BY j.id, q.id, r.source_type, c.order_index ASC
        """,
        params,
    )
    rows = cursor.fetchall()
    conn.close()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    writer.writerow([
        "juez", "pregunta_id", "es_calibracion", "tipo_fuente", "criterio", "puntuacion", "latencia"
    ])
    
    for r in rows:
        writer.writerow([
            sanitize_csv_cell(r["judge_name"]),
            r["question_id"],
            r["is_calibration"],
            sanitize_csv_cell(r["variant"]),
            sanitize_csv_cell(r["criterion_name"]),
            r["score"],
            r["latency"],
        ])
        
    output.seek(0)
    
    response = StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv"
    )
    response.headers["Content-Disposition"] = "attachment; filename=resultados_anova.csv"
    return response


# Servir frontend estático de la plataforma de evaluación
frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
if frontend_dir.exists():
    logger.info(f"Servir archivos de la plataforma de evaluación desde: {frontend_dir}")
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
else:
    logger.warning(f"Directorio de frontend de evaluación no encontrado en: {frontend_dir}")
