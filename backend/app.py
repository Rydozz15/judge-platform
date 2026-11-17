import logging
import json
import random
import csv
import io
from typing import List, Dict, Optional, Any
from fastapi import FastAPI, HTTPException, Header, Depends, status, UploadFile, File, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pathlib import Path

from database import init_db, get_db_connection

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("evaluation_app")

# Inicializar DB SQLite
init_db()

app = FastAPI(
    title="MentorIA Blind Evaluation Platform API",
    description="API para la evaluación triple-ciega de respuestas (Naive, RAG, Experto Humano)",
    version="0.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- PYDANTIC SCHEMAS ----------------

class LoginRequest(BaseModel):
    username: str
    password: str

class UserResponse(BaseModel):
    id: int
    username: str
    display_name: str
    role: str

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
    responses: List[ResponseCreate]

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

def get_current_user(x_user_id: Optional[str] = Header(None, alias="X-User-Id")):
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Header X-User-Id faltante.")
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, display_name, role, is_active FROM judges WHERE id = ?", (x_user_id,))
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
def login(req: LoginRequest):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, username, display_name, role, is_active FROM judges WHERE username = ? AND password = ?",
        (req.username, req.password)
    )
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        raise HTTPException(status_code=400, detail="Usuario o contraseña incorrectos.")
    if not row["is_active"]:
        raise HTTPException(status_code=400, detail="Esta cuenta de juez se encuentra inactiva.")
        
    return UserResponse(
        id=row["id"],
        username=row["username"],
        display_name=row["display_name"],
        role=row["role"]
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
    
    return SessionStartResponse(session_id=session_id, status="started")

@app.post("/api/evaluation/session/heartbeat")
def judge_session_heartbeat(req: SessionHeartbeatRequest, current_user: dict = Depends(get_current_user)):
    session_id = req.session_id
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Get current session
    cursor.execute("SELECT started_at, last_active, session_duration_sec FROM judge_sessions WHERE id = ?", (session_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Sesión no encontrada.")
        
    # Calculate time delta since last active in seconds
    cursor.execute("""
        UPDATE judge_sessions 
        SET last_active = datetime('now'),
            session_duration_sec = session_duration_sec + (strftime('%s', 'now') - strftime('%s', last_active))
        WHERE id = ?
    """, (session_id,))
    conn.commit()
    conn.close()
    
    return {"status": "ok"}

@app.get("/api/evaluation/next")
def get_next_evaluation(campaign_id: Optional[int] = None, current_user: dict = Depends(get_current_user)):
    judge_id = current_user["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if campaign_id is None:
        cursor.execute("SELECT id FROM campaigns WHERE is_active = 1 LIMIT 1")
        camp_row = cursor.fetchone()
        campaign_id = camp_row["id"] if camp_row else 1
        
    # 1. Obtener todas las preguntas activas para esta campaña
    cursor.execute("SELECT id, question_text FROM questions WHERE is_active = 1 AND campaign_id = ? ORDER BY order_index ASC, id ASC", (campaign_id,))
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
    
    # Buscar la primera pregunta no completada
    target_question = None
    for q in all_questions:
        if q["id"] not in completed_ids:
            target_question = q
            break
            
    # Calcular progreso
    progress = {
        "completed": len(completed_ids),
        "total": len(all_questions)
    }
    
    if not target_question:
        conn.close()
        return {"status": "completed", "progress": progress}
        
    question_id = target_question["id"]
    
    # 3. Obtener o crear orden aleatorio de respuestas para este juez y pregunta
    cursor.execute(
        "SELECT response_order FROM judge_question_order WHERE judge_id = ? AND question_id = ?",
        (judge_id, question_id)
    )
    order_row = cursor.fetchone()
    
    if not order_row:
        # Obtener las N respuestas de esta pregunta
        cursor.execute("SELECT id FROM responses WHERE question_id = ?", (question_id,))
        response_rows = cursor.fetchall()
        response_ids = [r["id"] for r in response_rows]
        
        # Aleatorizar
        random.shuffle(response_ids)
        response_order_str = json.dumps(response_ids)
        
        # Guardar en DB
        cursor.execute(
            """
            INSERT INTO judge_question_order (judge_id, question_id, response_order, is_completed)
            VALUES (?, ?, ?, 0)
            """,
            (judge_id, question_id, response_order_str)
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
    
    conn.close()
    
    return {
        "status": "pending",
        "question_id": question_id,
        "question_text": target_question["question_text"],
        "responses": shuffled_responses,
        "rubric": criteria,
        "progress": progress
    }

@app.post("/api/evaluation/submit")
def submit_evaluation(req: EvaluationSubmit, current_user: dict = Depends(get_current_user)):
    judge_id = current_user["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Guardar cada evaluación de respuesta
        for r_rating in req.evaluations:
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
                        r_rating.latency_ms
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
    return {"status": "success", "message": "Evaluación guardada correctamente."}

# ---------------- ADMIN CRUD: JUDGES ----------------

@app.get("/api/admin/judges", dependencies=[Depends(require_admin)])
def list_judges():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, display_name, role, is_active, created_at FROM judges ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/admin/judges", dependencies=[Depends(require_admin)])
def create_judge(judge: JudgeCreate):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO judges (username, password, display_name, role) VALUES (?, ?, ?, ?)",
            (judge.username, judge.password, judge.display_name, judge.role)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="El nombre de usuario ya está registrado.")
    conn.close()
    return {"status": "success", "message": "Juez creado exitosamente."}

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
def list_questions():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT id, question_text, order_index, is_active FROM questions ORDER BY order_index ASC, id DESC")
    questions_rows = cursor.fetchall()
    
    result = []
    for q in questions_rows:
        cursor.execute("SELECT id, response_text, source_type FROM responses WHERE question_id = ?", (q["id"],))
        resp_rows = cursor.fetchall()
        result.append({
            "id": q["id"],
            "question_text": q["question_text"],
            "order_index": q["order_index"],
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
        # Insertar pregunta
        cursor.execute("INSERT INTO questions (question_text) VALUES (?)", (q_req.question_text,))
        question_id = cursor.lastrowid
        
        # Insertar las N respuestas (variantes dinámicas)
        for resp in q_req.responses:
            cursor.execute(
                """
                INSERT INTO responses (question_id, response_text, source_type, source_metadata)
                VALUES (?, ?, ?, ?)
                """,
                (question_id, resp.response_text, resp.source_type, resp.source_metadata)
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        logger.error(f"Error creating question: {e}")
        raise HTTPException(status_code=500, detail="Error interno al guardar la pregunta.")
        
    conn.close()
    return {"status": "success", "message": "Pregunta y respuestas creadas correctamente."}

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
        # Get mapped criteria for this campaign
        cursor.execute(
            """
            SELECT c.id, c.name FROM rubric_criteria c
            JOIN campaign_rubric cr ON c.id = cr.criterion_id
            WHERE cr.campaign_id = ?
            """,
            (camp["id"],)
        )
        criteria = [{"id": r["id"], "name": r["name"]} for r in cursor.fetchall()]
        
        result.append({
            "id": camp["id"],
            "name": camp["name"],
            "description": camp["description"],
            "deadline": camp["deadline"],
            "is_active": bool(camp["is_active"]),
            "created_at": camp["created_at"],
            "criteria": criteria
        })
        
    conn.close()
    return result

@app.post("/api/admin/campaigns", dependencies=[Depends(require_admin)])
def create_campaign(req: CampaignCreate):
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
            r["judge_name"], r["question_id"], r["question_text"], r["source_type"],
            r["criterion_name"], r["score"], r["comment"], r["created_at"]
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
    contents = await file.read()
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
    
    # Verificar si la campaña existe
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
        raise HTTPException(status_code=400, detail=f"Error al procesar el archivo CSV: {str(e)}")
    finally:
        conn.close()
        
    return {
        "status": "success",
        "message": f"CSV importado correctamente. {rows_parsed} preguntas ingresadas.",
        "parsed_count": rows_parsed
    }

@app.post("/api/admin/rubric/import-csv", dependencies=[Depends(require_admin)])
async def import_rubric_csv(campaign_id: Optional[int] = None, file: UploadFile = File(...)):
    contents = await file.read()
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
        
    return {
        "status": "success",
        "message": f"Rúbrica importada correctamente. {rows_parsed} criterios ingresados/actualizados.",
        "parsed_count": rows_parsed
    }

@app.get("/api/admin/campaigns/{campaign_id}/questions", dependencies=[Depends(require_admin)])
def list_campaign_questions(campaign_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, question_text, order_index, is_active FROM questions WHERE campaign_id = ? ORDER BY order_index ASC, id DESC", (campaign_id,))
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
        SELECT c.id, c.name, c.description 
        FROM campaigns c
        INNER JOIN campaign_judges cj ON c.id = cj.campaign_id
        WHERE c.is_active = 1 AND cj.judge_id = ?
    """, (judge_id,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/api/evaluation/metrics")
def get_judge_metrics(campaign_id: Optional[int] = None, current_user: dict = Depends(get_current_user)):
    judge_id = current_user["id"]
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if campaign_id is None:
        cursor.execute("SELECT id FROM campaigns WHERE is_active = 1 LIMIT 1")
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
    
    conn.close()
    
    return {
        "completed": completed_questions,
        "total": total_questions,
        "pending": pending_questions,
        "avg_latency_sec": avg_latency_sec,
        "avg_score": avg_score
    }

@app.get("/api/admin/metrics", dependencies=[Depends(require_admin)])
def get_admin_metrics(campaign_id: Optional[int] = None):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Base filter
    where_clause = ""
    params = []
    
    kappa_where_clause = ""
    kappa_params = []
    
    if campaign_id is not None:
        where_clause = "JOIN responses r ON e.response_id = r.id JOIN questions q ON r.question_id = q.id WHERE q.campaign_id = ?"
        params.append(campaign_id)
        
        cursor.execute("SELECT COUNT(*) FROM questions WHERE campaign_id = ? AND is_calibration = 1", (campaign_id,))
        has_calibration = cursor.fetchone()[0] > 0
        if has_calibration:
            kappa_where_clause = "JOIN responses r ON e.response_id = r.id JOIN questions q ON r.question_id = q.id WHERE q.campaign_id = ? AND q.is_calibration = 1"
            kappa_params.append(campaign_id)
        else:
            kappa_where_clause = where_clause
            kappa_params = params
    else:
        cursor.execute("SELECT COUNT(*) FROM questions WHERE is_calibration = 1")
        has_calibration = cursor.fetchone()[0] > 0
        if has_calibration:
            kappa_where_clause = "JOIN responses r ON e.response_id = r.id JOIN questions q ON r.question_id = q.id WHERE q.is_calibration = 1"
        else:
            kappa_where_clause = ""
            
    # 1. Fleiss' Kappa y Evaluaciones (filtrado por calibración si existe la bandera en la campaña)
    cursor.execute(
        f"SELECT e.judge_id, e.response_id, e.criterion_id, e.score, e.latency_ms FROM evaluations e {kappa_where_clause}",
        kappa_params
    )
    rows_kappa = cursor.fetchall()
    evaluations_kappa = [dict(r) for r in rows_kappa]
    kappa = compute_fleiss_kappa(evaluations_kappa)
    
    # Evaluaciones totales (sin filtrar por calibración)
    cursor.execute(
        f"SELECT e.judge_id, e.response_id, e.criterion_id, e.score, e.latency_ms FROM evaluations e {where_clause}",
        params
    )
    rows = cursor.fetchall()
    evaluations = [dict(r) for r in rows]
    kappa = compute_fleiss_kappa(evaluations)
    
    latencies = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
    avg_latency = sum(latencies)/len(latencies) if latencies else 0.0
    median_latency = compute_median(latencies) if latencies else 0.0
    
    # 2. Sesiones de Jueces
    session_params = []
    session_where = ""
    if campaign_id is not None:
        session_where = "WHERE s.campaign_id = ?"
        session_params.append(campaign_id)
        
    cursor.execute(
        f"""
        SELECT 
            j.display_name,
            j.username,
            s.started_at,
            s.last_active,
            s.completed_at,
            s.session_duration_sec,
            (SELECT COUNT(DISTINCT e.response_id) FROM evaluations e JOIN responses r ON e.response_id = r.id JOIN questions q ON r.question_id = q.id WHERE e.judge_id = j.id {("AND q.campaign_id = ?" if campaign_id else "")}) as evaluated_questions
        FROM judges j
        JOIN judge_sessions s ON j.id = s.judge_id
        {session_where}
        ORDER BY s.last_active DESC
        """,
        (session_params + [campaign_id]) if campaign_id else session_params
    )
    sessions = [dict(r) for r in cursor.fetchall()]
    
    # 3. Métricas de fuentes (Puntaje promedio, mediana y varianza por source_type)
    source_where = ""
    source_params = []
    if campaign_id is not None:
        source_where = "WHERE q.campaign_id = ?"
        source_params.append(campaign_id)
        
    # Calculemos varianza en Python para ser robustos y evitar complejidad SQL
    cursor.execute(
        f"""
        SELECT r.source_type, e.score 
        FROM evaluations e 
        JOIN responses r ON e.response_id = r.id 
        JOIN questions q ON r.question_id = q.id
        {source_where}
        """,
        source_params
    )
    source_scores = {}
    for r in cursor.fetchall():
        source_scores.setdefault(r["source_type"], []).append(r["score"])
        
    source_metrics = []
    for src, scores in source_scores.items():
        avg = sum(scores) / len(scores) if scores else 0.0
        var = sum((x - avg)**2 for x in scores) / (len(scores) - 1) if len(scores) > 1 else 0.0
        med = compute_median(scores)
        source_metrics.append({
            "source_type": src,
            "avg_score": round(avg, 2),
            "median_score": med,
            "variance": round(var, 3),
            "count": len(scores)
        })
        
    # 4. Leve análisis de justificaciones (comentarios no vacíos)
    cursor.execute(
        f"""
        SELECT r.source_type, e.comment, q.question_text 
        FROM evaluations e
        JOIN responses r ON e.response_id = r.id
        JOIN questions q ON r.question_id = q.id
        WHERE e.comment IS NOT NULL AND e.comment != '' {("AND q.campaign_id = ?" if campaign_id else "")}
        LIMIT 30
        """,
        source_params
    )
    comments = [dict(r) for r in cursor.fetchall()]
    
    # 5. Progreso frente a fecha límite (deadline)
    deadline = None
    days_left = None
    if campaign_id is not None:
        cursor.execute("SELECT deadline FROM campaigns WHERE id = ?", (campaign_id,))
        camp_row = cursor.fetchone()
        if camp_row:
            deadline = camp_row["deadline"]
            if deadline:
                # Calcular días restantes en SQLite
                try:
                    cursor.execute("SELECT CAST(julianday(?) - julianday('now') AS INTEGER)", (deadline,))
                    days_row = cursor.fetchone()
                    if days_row:
                        days_left = days_row[0]
                except Exception:
                    pass
                    
    conn.close()
    
    return {
        "fleiss_kappa": kappa,
        "average_latency_ms": avg_latency,
        "median_latency_ms": median_latency,
        "total_evaluations": len(evaluations),
        "sessions": sessions,
        "source_metrics": source_metrics,
        "justifications": comments,
        "deadline": deadline,
        "days_left": days_left
    }

@app.get("/api/admin/export/anova", dependencies=[Depends(require_admin)])
def export_anova_csv():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute(
        """
        SELECT 
            j.username as judge_name,
            q.id as question_id,
            r.source_type as variant,
            c.name as criterion_name,
            e.score,
            e.latency_ms as latency
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
        "juez", "pregunta_id", "tipo_fuente", "criterio", "puntuacion", "latencia"
    ])
    
    for r in rows:
        writer.writerow([
            r["judge_name"], r["question_id"], r["variant"],
            r["criterion_name"], r["score"], r["latency"]
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
