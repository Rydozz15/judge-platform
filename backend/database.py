import sqlite3
import os
from pathlib import Path

# Ruta de la base de datos: configurable vía EVAL_DB_PATH (por defecto: <repo>/data/evaluation.db)
_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "evaluation.db"
DB_PATH = Path(os.getenv("EVAL_DB_PATH") or _DEFAULT_DB_PATH).expanduser().resolve()

# Contraseña inicial del admin (solo se usa al crear el usuario 'admin' por primera vez)
DEFAULT_ADMIN_PASSWORD = os.getenv("EVAL_ADMIN_PASSWORD", "admin123")

def get_db_connection():
    # Asegurar que el directorio de datos existe
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
    except sqlite3.OperationalError:
        pass
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 0. Tabla de Campañas de Evaluación
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS campaigns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT,
        deadline TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    try:
        cursor.execute("ALTER TABLE campaigns ADD COLUMN deadline TEXT;")
    except sqlite3.OperationalError:
        pass
    
    # Insertar campaña por defecto si no existe
    cursor.execute("SELECT COUNT(*) FROM campaigns WHERE name = 'Campaña General';")
    if cursor.fetchone()[0] == 0:
        cursor.execute("""
        INSERT INTO campaigns (name, description, is_active)
        VALUES ('Campaña General', 'Campaña de evaluación por defecto', 1);
        """)
        conn.commit()

    # Obtener el ID de la campaña por defecto
    cursor.execute("SELECT id FROM campaigns WHERE name = 'Campaña General';")
    default_campaign_id = cursor.fetchone()[0]

    # 1. Tabla de Jueces/Administradores
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS judges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password TEXT NOT NULL, -- Contraseña plana para simplificar el despliegue local de la tesis
        display_name TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'judge', -- 'admin' o 'judge'
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    
    # 2. Tabla de Preguntas (asociadas a campaña)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id INTEGER,
        question_text TEXT NOT NULL,
        order_index INTEGER DEFAULT 0,
        is_calibration INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE SET NULL
    );
    """)
    try:
        cursor.execute("ALTER TABLE questions ADD COLUMN campaign_id INTEGER REFERENCES campaigns(id) ON DELETE SET NULL;")
    except sqlite3.OperationalError:
        pass
        
    try:
        cursor.execute("ALTER TABLE questions ADD COLUMN is_calibration INTEGER DEFAULT 0;")
    except sqlite3.OperationalError:
        pass

    # Asignar campaña por defecto a preguntas existentes que tengan campaign_id NULL
    cursor.execute("UPDATE questions SET campaign_id = ? WHERE campaign_id IS NULL;", (default_campaign_id,))
    
    # 3. Tabla de Respuestas (Dinámicas por pregunta: soporta N variantes como naive, rag, professional, etc.)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS responses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question_id INTEGER NOT NULL,
        response_text TEXT NOT NULL,
        source_type TEXT NOT NULL, -- Identificador de la variante de respuesta (e.g. 'naive', 'rag', 'professional', etc.)
        source_metadata TEXT, -- Información del modelo, fecha, etc. (JSON)
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE
    );
    """)
    
    # 4. Tabla de Criterios de la Rúbrica
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS rubric_criteria (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id INTEGER,
        name TEXT NOT NULL UNIQUE,
        description TEXT,
        min_score INTEGER DEFAULT 1,
        max_score INTEGER DEFAULT 5,
        weight REAL DEFAULT 1.0,
        order_index INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        score_descriptions TEXT,
        FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE SET NULL
    );
    """)
    try:
        cursor.execute("ALTER TABLE rubric_criteria ADD COLUMN campaign_id INTEGER REFERENCES campaigns(id) ON DELETE SET NULL;")
    except sqlite3.OperationalError:
        pass
        
    try:
        cursor.execute("ALTER TABLE rubric_criteria ADD COLUMN score_descriptions TEXT;")
    except sqlite3.OperationalError:
        pass

    # Asignar campaña por defecto a criterios de rúbrica existentes que tengan campaign_id NULL
    cursor.execute("UPDATE rubric_criteria SET campaign_id = ? WHERE campaign_id IS NULL;", (default_campaign_id,))

    # 4b. Tabla de Asociación de Campaña con Criterios de Rúbrica (Muchos a Muchos para flexibilidad)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS campaign_rubric (
        campaign_id INTEGER NOT NULL,
        criterion_id INTEGER NOT NULL,
        PRIMARY KEY (campaign_id, criterion_id),
        FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE,
        FOREIGN KEY (criterion_id) REFERENCES rubric_criteria(id) ON DELETE CASCADE
    );
    """)
    
    # 4c. Tabla de Asociación de Jueces con Campañas
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS campaign_judges (
        campaign_id INTEGER NOT NULL,
        judge_id INTEGER NOT NULL,
        PRIMARY KEY (campaign_id, judge_id),
        FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE,
        FOREIGN KEY (judge_id) REFERENCES judges(id) ON DELETE CASCADE
    );
    """)
    
    # 5. Tabla de Evaluaciones de Jueces
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS evaluations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        judge_id INTEGER NOT NULL,
        response_id INTEGER NOT NULL,
        criterion_id INTEGER NOT NULL,
        score INTEGER NOT NULL,
        comment TEXT,
        latency_ms INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (judge_id) REFERENCES judges(id) ON DELETE CASCADE,
        FOREIGN KEY (response_id) REFERENCES responses(id) ON DELETE CASCADE,
        FOREIGN KEY (criterion_id) REFERENCES rubric_criteria(id) ON DELETE CASCADE,
        UNIQUE(judge_id, response_id, criterion_id) -- Evita doble evaluación del mismo juez
    );
    """)
    try:
        cursor.execute("ALTER TABLE evaluations ADD COLUMN latency_ms INTEGER;")
    except sqlite3.OperationalError:
        pass
    
    # 6. Tabla para fijar y aleatorizar el orden de las respuestas por Juez y Pregunta
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS judge_question_order (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        judge_id INTEGER NOT NULL,
        question_id INTEGER NOT NULL,
        response_order TEXT NOT NULL, -- Guardado como string JSON array, e.g. '[3, 1, 2]'
        is_completed INTEGER DEFAULT 0,
        FOREIGN KEY (judge_id) REFERENCES judges(id) ON DELETE CASCADE,
        FOREIGN KEY (question_id) REFERENCES questions(id) ON DELETE CASCADE,
        UNIQUE(judge_id, question_id)
    );
    """)
    
    # Insertar administrador por defecto si no existe
    cursor.execute("SELECT COUNT(*) FROM judges WHERE username = 'admin';")
    if cursor.fetchone()[0] == 0:
        cursor.execute("""
        INSERT INTO judges (username, password, display_name, role, is_active)
        VALUES ('admin', ?, 'Administrador Principal', 'admin', 1);
        """, (DEFAULT_ADMIN_PASSWORD,))
        
    # Insertar criterios de rúbrica base por defecto si no existen
    cursor.execute("SELECT COUNT(*) FROM rubric_criteria;")
    if cursor.fetchone()[0] == 0:
        base_criteria = [
            ("Relevancia Pedagógica", "Qué tan útil y aplicable es el consejo para la docencia/vida académica chilena.", 1, 5, 1.0, 1),
            ("Tono y Empatía", "Evalúa si el tono es constructivo, cercano y empático, o si es frío e impersonal.", 1, 5, 1.0, 2),
            ("Precisión y Rigor", "Qué tan alineado está con reglamentos institucionales y literatura pedagógica de calidad.", 1, 5, 1.0, 3)
        ]
        for crit in base_criteria:
            cursor.execute("""
            INSERT INTO rubric_criteria (name, description, min_score, max_score, weight, order_index, is_active)
            VALUES (?, ?, ?, ?, ?, ?, 1);
            """, crit)
            criterion_id = cursor.lastrowid
            
            # Actualizar campaign_id e insertar en tabla de asociación many-to-many
            cursor.execute("UPDATE rubric_criteria SET campaign_id = ? WHERE id = ?;", (default_campaign_id, criterion_id))
            cursor.execute("""
            INSERT OR IGNORE INTO campaign_rubric (campaign_id, criterion_id)
            VALUES (?, ?);
            """, (default_campaign_id, criterion_id))

    # Asociar todos los rubric_criteria existentes con la campaña por defecto si no están asociados
    cursor.execute("SELECT id FROM rubric_criteria;")
    criteria_ids = [row[0] for row in cursor.fetchall()]
    for c_id in criteria_ids:
        cursor.execute("""
        INSERT OR IGNORE INTO campaign_rubric (campaign_id, criterion_id)
        VALUES (?, ?);
        """, (default_campaign_id, c_id))

    # 7. Tabla de Sesiones de Jueces
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS judge_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        judge_id INTEGER NOT NULL,
        campaign_id INTEGER NOT NULL,
        started_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_active TEXT DEFAULT CURRENT_TIMESTAMP,
        completed_at TEXT,
        session_duration_sec INTEGER DEFAULT 0,
        FOREIGN KEY (judge_id) REFERENCES judges(id) ON DELETE CASCADE,
        FOREIGN KEY (campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
    );
    """)
            
    conn.commit()
    conn.close()
