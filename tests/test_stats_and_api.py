import os
import sys
import tempfile
import unittest
from pathlib import Path

# Configurar DB temporal antes de importar database y app
_TMP_DIR = tempfile.mkdtemp(prefix="eval_test_")
os.environ["EVAL_DB_PATH"] = os.path.join(_TMP_DIR, "test_eval.db")

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from stats import (
    krippendorff_alpha_from_units,
    compute_fleiss_kappa,
    compute_gwet_ac2,
    compute_quadratic_weighted_kappa,
    compute_icc_two_way_random,
    compute_variant_inferential_tests,
    compute_judge_diagnostics,
    compute_disagreement_audit,
)
from database import init_db, hash_password, verify_password, create_auth_token, verify_auth_token
import app as eval_app


class TestStatsEngine(unittest.TestCase):
    def test_krippendorff_alpha_perfect_and_levels(self):
        perfect_units = [[5, 5, 5], [1, 1, 1], [3, 3, 3]]
        self.assertEqual(krippendorff_alpha_from_units(perfect_units, "nominal"), 1.0)
        self.assertEqual(krippendorff_alpha_from_units(perfect_units, "ordinal"), 1.0)
        self.assertEqual(krippendorff_alpha_from_units(perfect_units, "interval"), 1.0)

        # Dataset clásico de Krippendorff con valores faltantes
        units = [
            [1, 1, 1],
            [2, 2, 3, 2],
            [3, 3, 3, 3],
            [3, 3, 3, 3],
            [2, 2, 2, 2],
            [1, 2, 3, 4],
            [4, 4, 4, 4],
            [1, 1, 2, 1],
            [2, 2, 2],
            [5, 5, 5],
            [1],  # unidad con 1 solo observador -> se ignora
            [3, 3, 3],
        ]
        a_nom = krippendorff_alpha_from_units(units, "nominal")
        a_ord = krippendorff_alpha_from_units(units, "ordinal")
        a_int = krippendorff_alpha_from_units(units, "interval")
        self.assertAlmostEqual(a_nom, 0.7409, places=3)
        self.assertTrue(0.80 <= a_ord <= 0.82)
        self.assertTrue(0.83 <= a_int <= 0.85)
        self.assertLess(a_nom, a_ord)
        self.assertLess(a_ord, a_int)

    def test_weighted_kappa_and_icc_and_gwet(self):
        a = [1, 2, 3, 4, 5, 4, 5]
        b = [1, 2, 3, 4, 4, 4, 5]
        kw = compute_quadratic_weighted_kappa(a, b)
        self.assertIsNotNone(kw)
        self.assertGreater(kw, 0.90)

        evals = []
        for idx, (sa, sb) in enumerate(zip(a, b), start=1):
            evals.append({"judge_id": 1, "response_id": idx, "criterion_id": 1, "score": sa})
            evals.append({"judge_id": 2, "response_id": idx, "criterion_id": 1, "score": sb})

        icc = compute_icc_two_way_random(evals)
        self.assertIsNotNone(icc["icc_2_1"])
        self.assertGreater(icc["icc_2_1"], 0.85)
        self.assertGreater(icc["icc_2_k"], icc["icc_2_1"])

        gwet = compute_gwet_ac2(evals)
        self.assertIsNotNone(gwet)
        self.assertGreater(gwet, 0.80)

    def test_friedman_and_wilcoxon_holm(self):
        evals = []
        # 8 bloques donde 'rag' y 'professional' superan claramente a 'naive'
        for b in range(1, 9):
            evals.append({"judge_id": 1, "question_id": b, "criterion_id": 1, "response_id": b * 10 + 1, "source_type": "naive", "score": 2})
            evals.append({"judge_id": 1, "question_id": b, "criterion_id": 1, "response_id": b * 10 + 2, "source_type": "rag", "score": 5})
            evals.append({"judge_id": 1, "question_id": b, "criterion_id": 1, "response_id": b * 10 + 3, "source_type": "professional", "score": 5})

        inf = compute_variant_inferential_tests(evals)
        self.assertEqual(inf["n_blocks"], 8)
        self.assertTrue(inf["significant"])
        self.assertLess(inf["friedman_p"], 0.01)

    def test_auth_helpers(self):
        h = hash_password("secreto123")
        self.assertTrue(verify_password("secreto123", h))
        self.assertFalse(verify_password("otro", h))
        # Compatibilidad con texto plano legado
        self.assertTrue(verify_password("juez123", "juez123"))

        tok = create_auth_token(26, "judge", ttl_sec=60)
        self.assertEqual(verify_auth_token(tok), 26)
        self.assertIsNone(verify_auth_token(tok + "corrupt"))


class TestBackendFlow(unittest.TestCase):
    def setUp(self):
        init_db()
        from generate_dummy_eval_platform import populate_dummy_data
        populate_dummy_data()

    def test_admin_metrics_and_login_and_export(self):
        user_resp = eval_app.login(eval_app.LoginRequest(username="admin", password=os.getenv("EVAL_ADMIN_PASSWORD", "admin123")))
        self.assertEqual(user_resp.role, "admin")
        self.assertIsNotNone(user_resp.token)

        metrics = eval_app.get_admin_metrics(campaign_id=2)
        self.assertIn("agreement", metrics)
        self.assertIn("inferential", metrics)
        self.assertIn("pilot_management", metrics)
        self.assertIsNotNone(metrics["agreement"]["krippendorff_ordinal"])
        self.assertGreater(metrics["pilot_management"]["completion_pct"], 0)

    def test_ux_flow_navigation_and_admin_endpoints(self):
        # Obtener ID real de juez1
        j1_login = eval_app.login(eval_app.LoginRequest(username="juez1", password="juez123"))
        j1_id = j1_login.id
        j1_ctx = {"id": j1_id, "username": "juez1", "role": "judge"}

        # 1. Judge metrics include estimated_remaining_sec and rubric guide
        j_metrics = eval_app.get_judge_metrics(current_user=j1_ctx)
        self.assertIn("estimated_remaining_sec", j_metrics)
        self.assertIn("rubric", j_metrics)
        self.assertGreater(len(j_metrics["rubric"]), 0)

        # 2. Reviewing a previously evaluated question (question_id=1) returns existing_evaluations and navigation links
        q_review = eval_app.get_next_evaluation(question_id=1, current_user=j1_ctx)
        self.assertEqual(q_review["status"], "pending")
        self.assertEqual(q_review["question_id"], 1)
        self.assertIn("existing_evaluations", q_review)
        self.assertGreater(len(q_review["existing_evaluations"]), 0)
        self.assertIn("prev_question_id", q_review)
        self.assertIn("next_question_id", q_review)

        # 3. Campaign readiness & active toggle
        camps = eval_app.list_campaigns()
        self.assertGreater(len(camps), 0)
        self.assertIn("is_ready", camps[0])
        self.assertIn("questions_count", camps[0])
        toggled = eval_app.toggle_campaign_active(camps[0]["id"])
        self.assertIn("is_active", toggled)
        # Restore active state
        eval_app.toggle_campaign_active(camps[0]["id"])

        # 4. Toggle calibration flag on question
        cal_res = eval_app.toggle_question_calibration(1, eval_app.CalibrationToggleRequest(is_calibration=False))
        self.assertEqual(cal_res["status"], "success")

        # 5. Reset judge password and verify login with new password, then restore
        eval_app.reset_judge_password(j1_id, eval_app.PasswordResetRequest(password="nuevaClave2026"))
        j_login = eval_app.login(eval_app.LoginRequest(username="juez1", password="nuevaClave2026"))
        self.assertEqual(j_login.id, j1_id)
        eval_app.reset_judge_password(j1_id, eval_app.PasswordResetRequest(password="juez123"))

    def test_security_hardening(self):
        from fastapi import HTTPException
        from database import get_db_connection, PASSWORD_PREFIX

        # 1. Verificar que init_db migró todas las contraseñas a PBKDF2-HMAC-SHA256
        conn = get_db_connection()
        rows = conn.execute("SELECT username, password FROM judges").fetchall()
        conn.close()
        self.assertGreater(len(rows), 0)
        for r in rows:
            self.assertTrue(
                r["password"].startswith(PASSWORD_PREFIX),
                f"Password for {r['username']} was not hashed with PBKDF2",
            )

        # 2. Verificar que X-User-Id sin token HMAC es rechazado (prevención de Account Takeover / CWE-287)
        with self.assertRaises(HTTPException) as ctx:
            eval_app.get_current_user(authorization=None, x_user_id="1", token=None)
        self.assertEqual(ctx.exception.status_code, 401)

        # 3. Verificar que Bearer token válido autentica correctamente
        j1_login = eval_app.login(eval_app.LoginRequest(username="juez1", password="juez123"))
        user_dict = eval_app.get_current_user(
            authorization=f"Bearer {j1_login.token}", x_user_id=None, token=None
        )
        self.assertEqual(user_dict["id"], j1_login.id)

        # 4. Verificar prevención de IDOR en heartbeat de sesión (juez2 no puede alterar sesión de juez1)
        s_resp = eval_app.start_judge_session(
            eval_app.SessionStartRequest(campaign_id=2),
            current_user={"id": j1_login.id, "role": "judge"},
        )
        with self.assertRaises(HTTPException) as idor_ctx:
            eval_app.judge_session_heartbeat(
                eval_app.SessionHeartbeatRequest(session_id=s_resp.session_id),
                current_user={"id": 9999, "role": "judge"},
            )
        self.assertEqual(idor_ctx.exception.status_code, 404)

        # 5. Verificar prevención de IDOR cruzado entre pregunta y response_id en submit_evaluation
        with self.assertRaises(HTTPException) as sub_ctx:
            eval_app.submit_evaluation(
                eval_app.EvaluationSubmit(
                    question_id=1,
                    evaluations=[
                        eval_app.SingleResponseRating(
                            response_id=99999,
                            ratings=[eval_app.ScoreRating(criterion_id=1, score=4)],
                        )
                    ],
                ),
                current_user={"id": j1_login.id, "role": "judge"},
            )
        self.assertEqual(sub_ctx.exception.status_code, 400)

        # 6. Verificar protección contra CSV Formula Injection (CWE-1236)
        self.assertEqual(eval_app.sanitize_csv_cell("=CMD|' /C calc'!A0"), "'=CMD|' /C calc'!A0")
        self.assertEqual(eval_app.sanitize_csv_cell("+SUM(A1:A2)"), "'+SUM(A1:A2)")
        self.assertEqual(eval_app.sanitize_csv_cell("@IMPORTXML()"), "'@IMPORTXML()")
        self.assertEqual(eval_app.sanitize_csv_cell("Comentario normal"), "Comentario normal")

        # 7. Verificar Rate Limiting contra fuerza bruta en /api/auth/login (CWE-307)
        eval_app._failed_login_attempts.clear()
        for _ in range(eval_app.MAX_FAILED_LOGINS):
            with self.assertRaises(HTTPException) as bad_login:
                eval_app.login(eval_app.LoginRequest(username="juez1", password="wrong_password"))
            self.assertEqual(bad_login.exception.status_code, 400)

        with self.assertRaises(HTTPException) as locked_login:
            eval_app.login(eval_app.LoginRequest(username="juez1", password="juez123"))
        self.assertEqual(locked_login.exception.status_code, 429)
        eval_app._failed_login_attempts.clear()


if __name__ == "__main__":
    unittest.main()
