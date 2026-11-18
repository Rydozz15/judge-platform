import sqlite3
import json
import random
import os
from pathlib import Path
from database import init_db, get_db_connection

def populate_dummy_data():
    # 1. Initialize DB to ensure schema is correct
    print("Inicializando base de datos...")
    init_db()
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 2. Check or create Campaign
    cursor.execute("SELECT id FROM campaigns WHERE name = 'Campaña Piloto Tesis';")
    row = cursor.fetchone()
    if not row:
        cursor.execute("""
        INSERT INTO campaigns (name, description, deadline, is_active)
        VALUES ('Campaña Piloto Tesis', 'Evaluación ciega del impacto de RAG en la mentoría académica', '2026-12-31', 1);
        """)
        campaign_id = cursor.lastrowid
        print(f"Campaña creada con ID: {campaign_id}")
    else:
        campaign_id = row[0]
        print(f"Campaña encontrada con ID: {campaign_id}")
        
    # Associate default rubric criteria with this campaign
    cursor.execute("SELECT id FROM rubric_criteria;")
    criteria_ids = [r[0] for r in cursor.fetchall()]
    for c_id in criteria_ids:
        cursor.execute("""
        INSERT OR IGNORE INTO campaign_rubric (campaign_id, criterion_id)
        VALUES (?, ?);
        """, (campaign_id, c_id))
        
    # 3. Create Judges
    judges = [
        ("admin", "admin123", "Administrador Principal", "admin"),
        ("juez1", "juez123", "Dr. Alejandro Silva (Mentor)", "judge"),
        ("juez2", "juez123", "Dra. Carmen Gloria (Mentora)", "judge")
    ]
    
    judge_ids = {}
    for username, password, display_name, role in judges:
        cursor.execute("SELECT id FROM judges WHERE username = ?;", (username,))
        row = cursor.fetchone()
        if not row:
            cursor.execute("""
            INSERT INTO judges (username, password, display_name, role, is_active)
            VALUES (?, ?, ?, ?, 1);
            """, (username, password, display_name, role))
            judge_ids[username] = cursor.lastrowid
            print(f"Juez creado: {username}")
        else:
            judge_ids[username] = row[0]
            print(f"Juez encontrado: {username}")
            
    # 4. Create Questions
    sample_questions = [
        "¿Cómo puedo manejar que en los comités de asignación de carga docente siempre me dan más horas de pregrado que a mis colegas hombres?",
        "Tengo problemas para equilibrar mi investigación con las labores administrativas del departamento. ¿Qué puedo hacer?",
        "Siento el síndrome del impostor al postular a mi primer fondo de investigación FONDECYT Iniciación.",
        "Un colega del departamento se adjudica ideas mías en las reuniones de equipo. ¿Cómo abordo esto?",
        "¿Cuáles son los canales formales en Chile para denunciar discriminación o acoso laboral de género?"
    ]
    
    question_ids = []
    for idx, q_text in enumerate(sample_questions):
        cursor.execute("SELECT id FROM questions WHERE question_text = ?;", (q_text,))
        row = cursor.fetchone()
        if not row:
            is_calib = 1 if idx < 2 else 0
            cursor.execute("""
            INSERT INTO questions (campaign_id, question_text, order_index, is_calibration, is_active)
            VALUES (?, ?, ?, ?, 1);
            """, (campaign_id, q_text, idx, is_calib))
            q_id = cursor.lastrowid
            question_ids.append(q_id)
            print(f"Pregunta creada: '{q_text[:40]}...'")
        else:
            q_id = row[0]
            question_ids.append(q_id)
            print(f"Pregunta encontrada: '{q_text[:40]}...'")
            
    # 5. Create Responses for each question
    # We create 3 variants per question: 'naive' (GPT sin RAG), 'rag' (ADAM con RAG), 'professional' (Experto humano)
    variants = {
        "naive": "Respuesta genérica (Naive). Debes hablar con recursos humanos o tu director. Intenta organizar mejor tu tiempo y establecer prioridades básicas.",
        "rag": "Respuesta RAG (ADAM). De acuerdo con el reglamento de carrera académica, la asignación debe ser proporcional. Sugiero: 1. Presentar datos comparativos de los últimos 3 años. 2. Apelar formalmente al comité citando la sección de equidad. 3. Consultar con la Oficina de Género de la Universidad.",
        "professional": "Respuesta Humana Experta. Lamento mucho tu situación. Te sugiero documentar detalladamente las horas asignadas a cada miembro, pedir una reunión formal de departamento con orden del día transparente y buscar respaldo en la vicerrectoría académica."
    }
    
    response_ids = {} # map q_id -> list of response dicts/ids
    for q_id in question_ids:
        response_ids[q_id] = []
        cursor.execute("SELECT id, source_type FROM responses WHERE question_id = ?;", (q_id,))
        existing_responses = {r["source_type"]: r["id"] for r in cursor.fetchall()}
        
        for source_type, base_text in variants.items():
            if source_type in existing_responses:
                response_ids[q_id].append({
                    "id": existing_responses[source_type],
                    "source_type": source_type
                })
            else:
                resp_text = f"{base_text} [Ref: Pregunta {q_id}]"
                cursor.execute("""
                INSERT INTO responses (question_id, response_text, source_type, source_metadata)
                VALUES (?, ?, ?, ?);
                """, (q_id, resp_text, source_type, json.dumps({"generator": "simulated"})))
                resp_id = cursor.lastrowid
                response_ids[q_id].append({
                    "id": resp_id,
                    "source_type": source_type
                })
                print(f"  Respuesta creada para pregunta {q_id} ({source_type})")
                
    # 6. Create judge question order and campaign_judges for judges
    for judge_name in ["juez1", "juez2"]:
        j_id = judge_ids[judge_name]
        cursor.execute("""
        INSERT OR IGNORE INTO campaign_judges (campaign_id, judge_id)
        VALUES (?, ?);
        """, (campaign_id, j_id))
        for q_id in question_ids:
            # Randomize order of the responses
            resps = response_ids[q_id]
            resp_ids_list = [r["id"] for r in resps]
            random.shuffle(resp_ids_list)
            
            cursor.execute("""
            INSERT OR IGNORE INTO judge_question_order (judge_id, question_id, response_order, is_completed)
            VALUES (?, ?, ?, 0);
            """, (j_id, q_id, json.dumps(resp_ids_list)))
            
    # 7. Add some evaluations from juez1 ( Alejandro) and juez2 ( Carmen)
    # Let's say juez1 evaluated all questions, and juez2 evaluated 3 questions.
    # Scores: rag and professional should get high scores (4-5), naive should get lower scores (2-3)
    score_mapping = {
        "naive": (2, 3),
        "rag": (4, 5),
        "professional": (4, 5)
    }
    
    # We get criteria ids
    cursor.execute("SELECT id FROM rubric_criteria;")
    criteria_list = [r[0] for r in cursor.fetchall()]
    
    eval_count = 0
    for judge_name in ["juez1", "juez2"]:
        j_id = judge_ids[judge_name]
        # Juez1 does questions 0, 1, 2, 3, 4
        # Juez2 does questions 0, 1, 2
        qs_to_eval = question_ids if judge_name == "juez1" else question_ids[:3]
        
        for q_id in qs_to_eval:
            for resp in response_ids[q_id]:
                resp_id = resp["id"]
                stype = resp["source_type"]
                min_s, max_s = score_mapping[stype]
                
                # Check if already evaluated
                cursor.execute("SELECT COUNT(*) FROM evaluations WHERE judge_id = ? AND response_id = ?;", (j_id, resp_id))
                if cursor.fetchone()[0] == 0:
                    for crit_id in criteria_list:
                        score = random.randint(min_s, max_s)
                        latency = random.randint(3000, 12000)
                        comment = f"El evaluador opina que esta respuesta es adecuada para la categoría."
                        
                        cursor.execute("""
                        INSERT INTO evaluations (judge_id, response_id, criterion_id, score, comment, latency_ms)
                        VALUES (?, ?, ?, ?, ?, ?);
                        """, (j_id, resp_id, crit_id, score, comment, latency))
                        eval_count += 1
                        
            # Mark this question as completed for this judge
            cursor.execute("""
            UPDATE judge_question_order
            SET is_completed = 1
            WHERE judge_id = ? AND question_id = ?;
            """, (j_id, q_id))
            
    print(f"Pobladas {eval_count} evaluaciones de prueba.")
    conn.commit()
    conn.close()
    print("Base de datos de la plataforma de evaluación poblada correctamente.")

if __name__ == "__main__":
    populate_dummy_data()
