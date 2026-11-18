"""
Motor estadístico de acuerdo inter-jueces y métricas de gestión de pilotos.

Implementa en Python puro (sin requerir compilación externa):
- Alpha de Krippendorff (nominal, ordinal, intervalo) con manejo de datos faltantes y N variable
  + intervalos de confianza al 95 % por bootstrap sobre unidades.
- Kappa de Fleiss (nominal, generalizado para N variable de jueces por ítem).
- AC2 de Gwet (ponderado ordinal, resistente a la paradoja de prevalencia del Kappa).
- Kappa de Cohen ponderado (cuadrático) por pares de jueces (matriz juez x juez).
- Coeficiente de Correlación Intraclase: ICC(2,1) e ICC(2,k) (acuerdo absoluto, dos vías).
- Porcentaje de acuerdo exacto y acuerdo dentro de ±1 punto.
- Test no paramétrico de Friedman + post-hoc de Wilcoxon con corrección de Holm-Bonferroni.
- Diagnósticos de calidad por juez (severidad/lenidad z-score, straight-lining, speeders, consenso).
- Consola de auditoría de desacuerdos (ítems con mayor dispersión entre jueces).
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Utilidades descriptivas básicas
# ---------------------------------------------------------------------------

def compute_mean(values: List[float]) -> float:
    if not values:
        return 0.0
    return float(sum(values) / len(values))


def compute_median(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return float(s[mid])
    return float((s[mid - 1] + s[mid]) / 2.0)


def compute_variance(values: List[float], sample: bool = True) -> float:
    n = len(values)
    if n <= 1:
        return 0.0
    mean = compute_mean(values)
    denom = (n - 1) if sample else n
    return float(sum((x - mean) ** 2 for x in values) / denom)


def compute_sd(values: List[float], sample: bool = True) -> float:
    return math.sqrt(max(0.0, compute_variance(values, sample=sample)))


def compute_iqr(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    s = sorted(values)
    n = len(s)
    q1 = compute_median(s[: n // 2])
    q3 = compute_median(s[(n + 1) // 2 :])
    return float(q3 - q1)


def compute_ci95_mean(values: List[float]) -> Tuple[float, float]:
    n = len(values)
    if n == 0:
        return (0.0, 0.0)
    mean = compute_mean(values)
    if n == 1:
        return (round(mean, 3), round(mean, 3))
    se = compute_sd(values, sample=True) / math.sqrt(n)
    # Aproximación t para muestras pequeñas, 1.96 para n >= 30
    t_crit = 1.96 if n >= 30 else (2.776 if n <= 5 else 2.131 if n <= 15 else 2.042)
    return (round(mean - t_crit * se, 3), round(mean + t_crit * se, 3))


def interpret_agreement(value: Optional[float], metric: str = "krippendorff") -> Dict[str, str]:
    """
    Devuelve etiqueta e interpretación estándar para pilotos:
    - Krippendorff (1980, 2004): >= 0.80 fiable, 0.667-0.80 tentativo, < 0.667 bajo.
    - Landis & Koch (1977) para Kappa/AC2/ICC: >= 0.80 casi perfecto, 0.61-0.80 sustancial,
      0.41-0.60 moderado, 0.21-0.40 regular, <= 0.20 leve/pobre.
    """
    if value is None:
        return {"level": "sin_datos", "label": "Datos insuficientes", "badge": "secondary"}

    if metric == "krippendorff":
        if value >= 0.80:
            return {"level": "alto", "label": "Fiable (≥ 0.80)", "badge": "success"}
        if value >= 0.667:
            return {"level": "tentativo", "label": "Tentativo / Aceptable para piloto (0.67–0.79)", "badge": "warning"}
        if value >= 0.40:
            return {"level": "moderado_bajo", "label": "Moderado-Bajo (requiere calibrar rúbrica)", "badge": "warning"}
        return {"level": "bajo", "label": "Bajo (< 0.40, recalibrar jueces)", "badge": "danger"}

    if value >= 0.80:
        return {"level": "casi_perfecto", "label": "Excelente (≥ 0.80)", "badge": "success"}
    if value >= 0.61:
        return {"level": "sustancial", "label": "Sustancial (0.61–0.79)", "badge": "success"}
    if value >= 0.41:
        return {"level": "moderado", "label": "Moderado (0.41–0.60)", "badge": "warning"}
    if value >= 0.21:
        return {"level": "regular", "label": "Regular (0.21–0.40)", "badge": "warning"}
    return {"level": "bajo", "label": "Bajo (≤ 0.20)", "badge": "danger"}


# ---------------------------------------------------------------------------
# 1. Alpha de Krippendorff (nominal, ordinal, intervalo)
# ---------------------------------------------------------------------------

def _extract_unit_ratings(evaluations: List[Dict[str, Any]]) -> List[List[float]]:
    """
    Agrupa calificaciones por unidad de análisis (response_id, criterion_id).
    Solo conserva unidades con al menos 2 calificaciones (valores emparejables).
    """
    by_unit: Dict[Tuple[int, int], List[float]] = defaultdict(list)
    for e in evaluations:
        if e.get("score") is None:
            continue
        unit = (int(e["response_id"]), int(e["criterion_id"]))
        by_unit[unit].append(float(e["score"]))
    return [scores for scores in by_unit.values() if len(scores) >= 2]


def krippendorff_alpha_from_units(
    unit_ratings: List[List[float]],
    level: str = "ordinal",
) -> Optional[float]:
    """
    Calcula el Alpha de Krippendorff exacto mediante la matriz de coincidencias o_{ck}
    sobre unidades con >= 2 observadores (Krippendorff, 2004, Content Analysis).

    Parámetros:
        unit_ratings: Lista de listas; cada sublista contiene los puntajes otorgados
                      a una unidad por los jueces que la evaluaron.
        level: 'nominal' | 'ordinal' | 'interval'
    """
    valid_units = [u for u in unit_ratings if len(u) >= 2]
    if not valid_units:
        return None

    # Recolectar categorías únicas ordenadas
    categories = sorted({val for u in valid_units for val in u})
    k_cat = len(categories)
    if k_cat <= 1:
        # Acuerdo perfecto entre todos los valores emparejables
        return 1.0

    cat_idx = {c: i for i, c in enumerate(categories)}

    # Construir matriz de coincidencias observadas o[c][k] y marginales n_c
    o_mat = [[0.0] * k_cat for _ in range(k_cat)]
    for u in valid_units:
        m_u = len(u)
        # Conteo de frecuencias dentro de la unidad u
        counts: Dict[int, int] = defaultdict(int)
        for val in u:
            counts[cat_idx[val]] += 1

        denom = float(m_u - 1)
        for i_c, n_uc in counts.items():
            # Pares (c, c) dentro de la misma unidad
            if n_uc > 1:
                o_mat[i_c][i_c] += (n_uc * (n_uc - 1)) / denom
            # Pares (c, k) con c != k
            for i_k, n_uk in counts.items():
                if i_c != i_k:
                    o_mat[i_c][i_k] += (n_uc * n_uk) / denom

    n_marginal = [sum(o_mat[c]) for c in range(k_cat)]
    n_total = sum(n_marginal)
    if n_total <= 1.0:
        return None

    # Función de diferencia al cuadrado delta^2(c, k)
    delta_sq = [[0.0] * k_cat for _ in range(k_cat)]
    if level == "nominal":
        for c in range(k_cat):
            for k in range(k_cat):
                delta_sq[c][k] = 0.0 if c == k else 1.0
    elif level == "interval":
        for c in range(k_cat):
            for k in range(k_cat):
                diff = categories[c] - categories[k]
                delta_sq[c][k] = diff * diff
    elif level == "ordinal":
        # Métrica ordinal exacta de Krippendorff:
        # delta_ordinal^2(c, k) = ( sum_{g=min(c,k)}^{max(c,k)} n_g - (n_c + n_k)/2 )^2
        cum = [0.0] * (k_cat + 1)
        for i in range(k_cat):
            cum[i + 1] = cum[i] + n_marginal[i]
        for c in range(k_cat):
            for k in range(c + 1, k_cat):
                sum_between = cum[k + 1] - cum[c]
                d = sum_between - (n_marginal[c] + n_marginal[k]) / 2.0
                d2 = d * d
                delta_sq[c][k] = d2
                delta_sq[k][c] = d2
    else:
        raise ValueError(f"Nivel de medición no soportado: {level}")

    # Discrepancia observada D_o y esperada D_e
    num = 0.0
    den = 0.0
    for c in range(k_cat):
        for k in range(c + 1, k_cat):
            d2 = delta_sq[c][k]
            num += o_mat[c][k] * d2
            den += n_marginal[c] * n_marginal[k] * d2

    if den <= 1e-12:
        return 1.0 if num <= 1e-12 else 0.0

    alpha = 1.0 - (n_total - 1.0) * (num / den)
    return round(float(alpha), 4)


def compute_krippendorff_alpha(
    evaluations: List[Dict[str, Any]],
    level: str = "ordinal",
) -> Optional[float]:
    unit_ratings = _extract_unit_ratings(evaluations)
    return krippendorff_alpha_from_units(unit_ratings, level=level)


def bootstrap_krippendorff_ci(
    evaluations: List[Dict[str, Any]],
    level: str = "ordinal",
    n_boot: int = 200,
    seed: int = 42,
) -> Optional[Tuple[float, float]]:
    """
    Calcula intervalo de confianza al 95 % por bootstrap no paramétrico
    remuestreando unidades de análisis con reemplazo.
    """
    unit_ratings = _extract_unit_ratings(evaluations)
    n_units = len(unit_ratings)
    if n_units < 4:
        return None

    rng = random.Random(seed)
    samples: List[float] = []
    for _ in range(n_boot):
        resampled = [unit_ratings[rng.randrange(n_units)] for _ in range(n_units)]
        val = krippendorff_alpha_from_units(resampled, level=level)
        if val is not None:
            samples.append(val)

    if len(samples) < 10:
        return None
    samples.sort()
    lo_idx = max(0, int(0.025 * len(samples)))
    hi_idx = min(len(samples) - 1, int(0.975 * len(samples)))
    return (round(samples[lo_idx], 4), round(samples[hi_idx], 4))


# ---------------------------------------------------------------------------
# 2. Kappa de Fleiss y AC2 de Gwet
# ---------------------------------------------------------------------------

def compute_fleiss_kappa(
    evaluations: List[Dict[str, Any]],
    min_score: int = 1,
    max_score: int = 5,
) -> Optional[float]:
    """
    Kappa de Fleiss nominal generalizado para múltiples jueces por ítem.
    """
    unit_ratings = _extract_unit_ratings(evaluations)
    if not unit_ratings:
        return None

    categories = list(range(min_score, max_score + 1))
    cat_idx = {c: i for i, c in enumerate(categories)}
    k_cat = len(categories)

    N = len(unit_ratings)
    table: List[List[int]] = []
    total_ratings = 0
    P_list: List[float] = []

    for scores in unit_ratings:
        counts = [0] * k_cat
        for s in scores:
            s_int = int(round(s))
            if s_int in cat_idx:
                counts[cat_idx[s_int]] += 1
        n_i = sum(counts)
        if n_i <= 1:
            continue
        table.append(counts)
        total_ratings += n_i
        sum_sq = sum(c * c for c in counts)
        P_i = (sum_sq - n_i) / (n_i * (n_i - 1))
        P_list.append(P_i)

    if not P_list or total_ratings == 0:
        return None

    P_bar = sum(P_list) / len(P_list)
    p_j = [sum(row[j] for row in table) / total_ratings for j in range(k_cat)]
    P_e = sum(p * p for p in p_j)

    denom = 1.0 - P_e
    if abs(denom) < 1e-9:
        return 1.0 if abs(P_bar - 1.0) < 1e-9 else 0.0
    return round((P_bar - P_e) / denom, 4)


def compute_gwet_ac2(
    evaluations: List[Dict[str, Any]],
    min_score: int = 1,
    max_score: int = 5,
) -> Optional[float]:
    """
    Calcula AC2 de Gwet con pesos cuadráticos ordinales (Gwet, 2014).
    Muy estable incluso cuando los puntajes se concentran en valores altos (4-5).
    """
    unit_ratings = _extract_unit_ratings(evaluations)
    if not unit_ratings:
        return None

    categories = list(range(min_score, max_score + 1))
    q = len(categories)
    if q <= 1:
        return 1.0
    cat_idx = {c: i for i, c in enumerate(categories)}

    # Pesos cuadráticos w_{kl} = 1 - ((k - l)/(q - 1))^2
    w = [[1.0 - ((k - l) / (q - 1)) ** 2 for l in range(q)] for k in range(q)]

    p_a_units: List[float] = []
    pi_k = [0.0] * q

    for scores in unit_ratings:
        counts = [0] * q
        for s in scores:
            s_int = int(round(s))
            if s_int in cat_idx:
                counts[cat_idx[s_int]] += 1
        r_i = sum(counts)
        if r_i < 2:
            continue
        # Acuerdo ponderado en la unidad i
        unit_num = 0.0
        for k in range(q):
            for l in range(q):
                if k == l:
                    unit_num += w[k][k] * counts[k] * (counts[k] - 1)
                else:
                    unit_num += w[k][l] * counts[k] * counts[l]
        p_a_units.append(unit_num / (r_i * (r_i - 1)))
        for k in range(q):
            pi_k[k] += counts[k] / r_i

    n_valid = len(p_a_units)
    if n_valid == 0:
        return None

    pi_k = [val / n_valid for val in pi_k]
    p_a = sum(p_a_units) / n_valid

    # Término de azar de Gwet con pesos ordinales
    t_w = sum(w[k][l] for k in range(q) for l in range(q)) / (q * (q - 1))
    sum_pi = sum(pi_k[k] * (1.0 - pi_k[k]) for k in range(q))
    p_e = t_w * sum_pi

    denom = 1.0 - p_e
    if abs(denom) < 1e-9:
        return 1.0
    return round((p_a - p_e) / denom, 4)


# ---------------------------------------------------------------------------
# 3. Acuerdo porcentual (Exacto y ±1 punto) y Matriz Juez x Juez (Cohen ponderado)
# ---------------------------------------------------------------------------

def compute_percentage_agreements(evaluations: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    """
    Calcula el porcentaje de pares de evaluaciones (sobre el mismo ítem) que:
    - Coinciden exactamente (exact_agreement_pct)
    - Difieren a lo sumo en 1 punto (within_one_pct)
    """
    unit_ratings = _extract_unit_ratings(evaluations)
    if not unit_ratings:
        return {"exact_pct": None, "within_one_pct": None}

    total_pairs = 0
    exact_pairs = 0
    within_one_pairs = 0

    for scores in unit_ratings:
        m = len(scores)
        for i in range(m):
            for j in range(i + 1, m):
                total_pairs += 1
                diff = abs(scores[i] - scores[j])
                if diff < 1e-9:
                    exact_pairs += 1
                if diff <= 1.0 + 1e-9:
                    within_one_pairs += 1

    if total_pairs == 0:
        return {"exact_pct": None, "within_one_pct": None}

    return {
        "exact_pct": round((exact_pairs / total_pairs) * 100.0, 1),
        "within_one_pct": round((within_one_pairs / total_pairs) * 100.0, 1),
    }


def compute_quadratic_weighted_kappa(
    scores_a: List[int],
    scores_b: List[int],
    min_score: int = 1,
    max_score: int = 5,
) -> Optional[float]:
    """
    Kappa de Cohen con ponderación cuadrática entre dos jueces sobre ítems compartidos.
    """
    n = len(scores_a)
    if n < 2 or len(scores_b) != n:
        return None

    categories = list(range(min_score, max_score + 1))
    k = len(categories)
    if k <= 1:
        return 1.0
    idx = {c: i for i, c in enumerate(categories)}

    obs = [[0.0] * k for _ in range(k)]
    hist_a = [0.0] * k
    hist_b = [0.0] * k

    valid_n = 0
    for a, b in zip(scores_a, scores_b):
        ia = idx.get(int(round(a)))
        ib = idx.get(int(round(b)))
        if ia is None or ib is None:
            continue
        obs[ia][ib] += 1.0
        hist_a[ia] += 1.0
        hist_b[ib] += 1.0
        valid_n += 1

    if valid_n < 2:
        return None

    num = 0.0
    den = 0.0
    max_dist_sq = float((k - 1) ** 2)
    for i in range(k):
        for j in range(k):
            w_ij = ((i - j) ** 2) / max_dist_sq
            exp_ij = (hist_a[i] * hist_b[j]) / valid_n
            num += w_ij * obs[i][j]
            den += w_ij * exp_ij

    if den <= 1e-12:
        return 1.0 if num <= 1e-12 else 0.0
    return round(1.0 - (num / den), 4)


def compute_pairwise_judge_matrix(
    evaluations: List[Dict[str, Any]],
    judges_map: Dict[int, Dict[str, Any]],
    min_score: int = 1,
    max_score: int = 5,
) -> List[Dict[str, Any]]:
    """
    Construye el análisis por pares de jueces (Kappa ponderado cuadrático, % exacto, % ±1,
    diferencia media e ítems en común).
    """
    by_judge_unit: Dict[int, Dict[Tuple[int, int], int]] = defaultdict(dict)
    for e in evaluations:
        if e.get("score") is None:
            continue
        j_id = int(e["judge_id"])
        unit = (int(e["response_id"]), int(e["criterion_id"]))
        by_judge_unit[j_id][unit] = int(e["score"])

    judge_ids = sorted(by_judge_unit.keys())
    pairs: List[Dict[str, Any]] = []

    for idx_a in range(len(judge_ids)):
        for idx_b in range(idx_a + 1, len(judge_ids)):
            ja = judge_ids[idx_a]
            jb = judge_ids[idx_b]
            shared_units = sorted(set(by_judge_unit[ja].keys()) & set(by_judge_unit[jb].keys()))
            if not shared_units:
                continue
            sa = [by_judge_unit[ja][u] for u in shared_units]
            sb = [by_judge_unit[jb][u] for u in shared_units]
            kw = compute_quadratic_weighted_kappa(sa, sb, min_score=min_score, max_score=max_score)
            exact = sum(1 for a, b in zip(sa, sb) if a == b) / len(shared_units) * 100.0
            within_one = sum(1 for a, b in zip(sa, sb) if abs(a - b) <= 1) / len(shared_units) * 100.0
            mean_diff = sum(a - b for a, b in zip(sa, sb)) / len(shared_units)

            name_a = judges_map.get(ja, {}).get("display_name") or f"Juez {ja}"
            name_b = judges_map.get(jb, {}).get("display_name") or f"Juez {jb}"

            pairs.append({
                "judge_a_id": ja,
                "judge_a_name": name_a,
                "judge_b_id": jb,
                "judge_b_name": name_b,
                "shared_ratings": len(shared_units),
                "weighted_kappa": kw,
                "exact_pct": round(exact, 1),
                "within_one_pct": round(within_one, 1),
                "mean_diff": round(mean_diff, 2),
            })

    return pairs


# ---------------------------------------------------------------------------
# 4. Coeficiente de Correlación Intraclase: ICC(2,1) e ICC(2,k)
# ---------------------------------------------------------------------------

def compute_icc_two_way_random(evaluations: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    """
    Calcula ICC(2,1) (medida única, acuerdo absoluto) e ICC(2,k) (promedio de k jueces,
    acuerdo absoluto) sobre la submatriz balanceada de unidades evaluadas por los jueces activos.
    (Shrout & Fleiss, 1979; McGraw & Wong, 1996).
    """
    by_unit_judge: Dict[Tuple[int, int], Dict[int, float]] = defaultdict(dict)
    all_judges = set()
    for e in evaluations:
        if e.get("score") is None:
            continue
        j_id = int(e["judge_id"])
        u = (int(e["response_id"]), int(e["criterion_id"]))
        by_unit_judge[u][j_id] = float(e["score"])
        all_judges.add(j_id)

    if len(all_judges) < 2:
        return {"icc_2_1": None, "icc_2_k": None, "n_targets": 0, "n_judges": len(all_judges)}

    judge_list = sorted(all_judges)
    k = len(judge_list)
    # Unidades calificadas por todos los jueces de judge_list
    complete_units = [
        [by_unit_judge[u][j] for j in judge_list]
        for u in sorted(by_unit_judge.keys())
        if all(j in by_unit_judge[u] for j in judge_list)
    ]

    # Si hay algún juez sin evaluaciones completas pero un subconjunto de >=2 jueces sí comparte unidades:
    if len(complete_units) < 2 and k > 2:
        # Buscar los jueces con al menos 2 evaluaciones
        active_judges = sorted(
            j for j in judge_list if sum(1 for u in by_unit_judge if j in by_unit_judge[u]) >= 2
        )
        if len(active_judges) >= 2:
            judge_list = active_judges
            k = len(judge_list)
            complete_units = [
                [by_unit_judge[u][j] for j in judge_list]
                for u in sorted(by_unit_judge.keys())
                if all(j in by_unit_judge[u] for j in judge_list)
            ]

    n = len(complete_units)
    if n < 2 or k < 2:
        return {"icc_2_1": None, "icc_2_k": None, "n_targets": n, "n_judges": k}

    grand_mean = sum(sum(row) for row in complete_units) / (n * k)
    row_means = [sum(row) / k for row in complete_units]
    col_means = [sum(complete_units[i][j] for i in range(n)) / n for j in range(k)]

    ss_total = sum(
        (complete_units[i][j] - grand_mean) ** 2 for i in range(n) for j in range(k)
    )
    ss_rows = k * sum((rm - grand_mean) ** 2 for rm in row_means)
    ss_cols = n * sum((cm - grand_mean) ** 2 for cm in col_means)
    ss_error = max(0.0, ss_total - ss_rows - ss_cols)

    ms_r = ss_rows / (n - 1)
    ms_c = ss_cols / (k - 1)
    ms_e = ss_error / ((n - 1) * (k - 1))

    denom_2_1 = ms_r + (k - 1) * ms_e + (k * (ms_c - ms_e) / n)
    icc_2_1 = (ms_r - ms_e) / denom_2_1 if abs(denom_2_1) > 1e-12 else (1.0 if ms_e < 1e-12 else 0.0)

    denom_2_k = ms_r + ((ms_c - ms_e) / n)
    icc_2_k = (ms_r - ms_e) / denom_2_k if abs(denom_2_k) > 1e-12 else (1.0 if ms_e < 1e-12 else 0.0)

    return {
        "icc_2_1": round(max(-1.0, min(1.0, icc_2_1)), 4),
        "icc_2_k": round(max(-1.0, min(1.0, icc_2_k)), 4),
        "n_targets": n,
        "n_judges": k,
    }


# ---------------------------------------------------------------------------
# 5. Comparación estadística entre variantes (Friedman + Wilcoxon + Holm)
# ---------------------------------------------------------------------------

def _chi2_survival_approx(x: float, df: int) -> float:
    """
    Aproximación de Wilson-Hilferty para P(Chi^2_df >= x) sin dependencias externas.
    """
    if x <= 0 or df <= 0:
        return 1.0
    if df == 1:
        return math.erfc(math.sqrt(x / 2.0))
    if df == 2:
        return math.exp(-x / 2.0)
    z = ((x / df) ** (1.0 / 3.0) - (1.0 - 2.0 / (9.0 * df))) / math.sqrt(2.0 / (9.0 * df))
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _normal_two_sided_p(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2.0))


def _wilcoxon_signed_rank(diffs: List[float]) -> Dict[str, Optional[float]]:
    """
    Test de rangos con signo de Wilcoxon para diferencias emparejadas (con corrección por empates).
    """
    non_zero = [d for d in diffs if abs(d) > 1e-9]
    n = len(non_zero)
    if n < 3:
        return {"w_stat": None, "p_value": None, "n_pairs": len(diffs), "n_non_zero": n}

    abs_vals = sorted((abs(d), d) for d in non_zero)
    ranks = [0.0] * n
    tie_groups: List[int] = []
    i = 0
    while i < n:
        j = i
        while j < n and abs(abs_vals[j][0] - abs_vals[i][0]) < 1e-9:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        for idx in range(i, j):
            ranks[idx] = avg_rank
        if j - i > 1:
            tie_groups.append(j - i)
        i = j

    w_pos = sum(ranks[idx] for idx in range(n) if abs_vals[idx][1] > 0)
    w_neg = sum(ranks[idx] for idx in range(n) if abs_vals[idx][1] < 0)
    w_stat = min(w_pos, w_neg)

    mean_w = n * (n + 1) / 4.0
    tie_corr = sum(t * (t * t - 1) for t in tie_groups) / 48.0
    var_w = (n * (n + 1) * (2 * n + 1)) / 24.0 - tie_corr
    if var_w <= 1e-12:
        return {"w_stat": round(w_stat, 2), "p_value": 1.0, "n_pairs": len(diffs), "n_non_zero": n}

    z = (abs(w_stat - mean_w) - 0.5) / math.sqrt(var_w)
    p = min(1.0, max(0.0, _normal_two_sided_p(z)))
    return {
        "w_stat": round(w_stat, 2),
        "p_value": round(p, 5),
        "n_pairs": len(diffs),
        "n_non_zero": n,
    }


def compute_variant_inferential_tests(evaluations: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Agrupa calificaciones en bloques emparejados (judge_id, question_id, criterion_id)
    y compara las variantes ('source_type') mediante el test de Friedman y comparaciones
    por pares de Wilcoxon con corrección de Holm-Bonferroni.
    """
    blocks: Dict[Tuple[int, int, int], Dict[str, float]] = defaultdict(dict)
    variants_set = set()
    for e in evaluations:
        if e.get("score") is None or not e.get("source_type") or e.get("question_id") is None:
            continue
        block_key = (int(e["judge_id"]), int(e["question_id"]), int(e["criterion_id"]))
        src = str(e["source_type"])
        blocks[block_key][src] = float(e["score"])
        variants_set.add(src)

    variants = sorted(variants_set)
    k = len(variants)
    complete_blocks = [
        [b[v] for v in variants]
        for b in blocks.values()
        if all(v in b for v in variants)
    ]
    n_blocks = len(complete_blocks)

    if k < 2 or n_blocks < 2:
        return {
            "friedman_chi2": None,
            "friedman_p": None,
            "n_blocks": n_blocks,
            "variants": variants,
            "pairwise_wilcoxon": [],
        }

    # Rangos por fila para Friedman (con promedio en empates)
    rank_sums = [0.0] * k
    for row in complete_blocks:
        indexed = sorted(enumerate(row), key=lambda x: x[1])
        row_ranks = [0.0] * k
        i = 0
        while i < k:
            j = i
            while j < k and abs(indexed[j][1] - indexed[i][1]) < 1e-9:
                j += 1
            avg_r = (i + 1 + j) / 2.0
            for m in range(i, j):
                row_ranks[indexed[m][0]] = avg_r
            i = j
        for col_idx in range(k):
            rank_sums[col_idx] += row_ranks[col_idx]

    chi2_f = (12.0 / (n_blocks * k * (k + 1))) * sum(r * r for r in rank_sums) - 3.0 * n_blocks * (k + 1)
    chi2_f = max(0.0, chi2_f)
    p_friedman = _chi2_survival_approx(chi2_f, k - 1)

    # Post-hoc por pares (Wilcoxon + Holm)
    raw_pairs: List[Dict[str, Any]] = []
    for i in range(k):
        for j in range(i + 1, k):
            va, vb = variants[i], variants[j]
            paired_diffs = [
                b[va] - b[vb]
                for b in blocks.values()
                if va in b and vb in b
            ]
            res = _wilcoxon_signed_rank(paired_diffs)
            mean_diff = compute_mean(paired_diffs) if paired_diffs else 0.0
            raw_pairs.append({
                "variant_a": va,
                "variant_b": vb,
                "mean_diff": round(mean_diff, 3),
                "n_pairs": res["n_pairs"],
                "w_stat": res["w_stat"],
                "p_raw": res["p_value"],
            })

    # Corrección de Holm-Bonferroni
    valid_p = [(idx, p["p_raw"]) for idx, p in enumerate(raw_pairs) if p["p_raw"] is not None]
    valid_p.sort(key=lambda x: x[1])
    m_tests = len(valid_p)
    prev_adj = 0.0
    for rank_idx, (orig_idx, p_val) in enumerate(valid_p):
        adj = min(1.0, p_val * (m_tests - rank_idx))
        adj = max(prev_adj, adj)
        prev_adj = adj
        raw_pairs[orig_idx]["p_holm"] = round(adj, 5)
        raw_pairs[orig_idx]["significant"] = adj < 0.05

    for p in raw_pairs:
        if "p_holm" not in p:
            p["p_holm"] = None
            p["significant"] = False

    return {
        "friedman_chi2": round(chi2_f, 3),
        "friedman_p": round(p_friedman, 5),
        "significant": p_friedman < 0.05,
        "n_blocks": n_blocks,
        "variants": variants,
        "pairwise_wilcoxon": raw_pairs,
    }


# ---------------------------------------------------------------------------
# 6. Diagnósticos de calidad por juez y consola de auditoría de desacuerdos
# ---------------------------------------------------------------------------

def compute_judge_diagnostics(
    evaluations: List[Dict[str, Any]],
    judges_list: List[Dict[str, Any]],
    sessions_list: List[Dict[str, Any]],
    total_campaign_questions: int,
    pairwise_matrix: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Calcula métricas de avance, actividad, sesgo de severidad/lenidad (z-score)
    y alertas de calidad (speeder, straight-lining, bajo acuerdo) para cada juez.
    """
    all_scores = [float(e["score"]) for e in evaluations if e.get("score") is not None]
    global_mean = compute_mean(all_scores)
    global_sd = compute_sd(all_scores)

    # Promedio por unidad para medir desviación respecto al consenso
    unit_scores: Dict[Tuple[int, int], List[float]] = defaultdict(list)
    for e in evaluations:
        if e.get("score") is not None:
            unit_scores[(int(e["response_id"]), int(e["criterion_id"]))].append(float(e["score"]))
    unit_means = {u: compute_mean(vals) for u, vals in unit_scores.items() if len(vals) >= 2}

    by_judge: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for e in evaluations:
        by_judge[int(e["judge_id"])].append(e)

    # Sesiones más recientes por juez
    latest_session: Dict[int, Dict[str, Any]] = {}
    total_duration_by_judge: Dict[int, int] = defaultdict(int)
    for s in sessions_list:
        jid = int(s["judge_id"])
        total_duration_by_judge[jid] += int(s.get("session_duration_sec") or 0)
        if jid not in latest_session:
            latest_session[jid] = s

    # Kappa ponderado promedio de cada juez con sus pares
    peer_kappas: Dict[int, List[float]] = defaultdict(list)
    for pair in pairwise_matrix:
        if pair.get("weighted_kappa") is not None:
            peer_kappas[pair["judge_a_id"]].append(float(pair["weighted_kappa"]))
            peer_kappas[pair["judge_b_id"]].append(float(pair["weighted_kappa"]))

    diagnostics: List[Dict[str, Any]] = []
    for j in judges_list:
        if j.get("role") == "admin":
            continue
        jid = int(j["id"])
        j_evals = by_judge.get(jid, [])
        j_scores = [float(e["score"]) for e in j_evals if e.get("score") is not None]
        j_questions = {int(e["question_id"]) for e in j_evals if e.get("question_id") is not None}

        # Latencia por pregunta (en evaluations se guarda latency_ms de la pregunta)
        q_latencies: Dict[int, int] = {}
        for e in j_evals:
            if e.get("question_id") is not None and e.get("latency_ms") is not None:
                q_latencies[int(e["question_id"])] = int(e["latency_ms"])
        lat_values_sec = [ms / 1000.0 for ms in q_latencies.values() if ms > 0]
        median_lat_sec = round(compute_median(lat_values_sec), 1) if lat_values_sec else None

        completed_q = len(j_questions)
        progress_pct = round((completed_q / total_campaign_questions) * 100.0, 1) if total_campaign_questions > 0 else 0.0

        mean_score = round(compute_mean(j_scores), 2) if j_scores else None
        sd_score = round(compute_sd(j_scores), 2) if len(j_scores) > 1 else None
        bias_delta = round(mean_score - global_mean, 2) if (mean_score is not None and all_scores) else None
        bias_z = (
            round((mean_score - global_mean) / global_sd, 2)
            if (mean_score is not None and global_sd > 1e-6)
            else 0.0
        )

        if bias_delta is None or len(j_scores) < 3:
            bias_label = "Sin datos"
        elif bias_delta <= -0.50:
            bias_label = "Severo / Estricto"
        elif bias_delta >= 0.50:
            bias_label = "Laxo / Generoso"
        else:
            bias_label = "Balanceado"

        # Desviación absoluta media respecto al consenso del grupo
        mad_list = [
            abs(float(e["score"]) - unit_means[(int(e["response_id"]), int(e["criterion_id"]))])
            for e in j_evals
            if (int(e["response_id"]), int(e["criterion_id"])) in unit_means
        ]
        consensus_mad = round(compute_mean(mad_list), 2) if mad_list else None
        mean_peer_kw = round(compute_mean(peer_kappas[jid]), 3) if peer_kappas.get(jid) else None

        # Alertas automáticas de calidad
        flags: List[Dict[str, str]] = []
        if median_lat_sec is not None and median_lat_sec < 6.0 and completed_q >= 1:
            flags.append({"code": "speeder", "label": "Respuestas muy rápidas (<6s)", "badge": "danger"})
        if sd_score is not None and len(j_scores) >= 6 and sd_score < 0.25:
            flags.append({"code": "straight_lining", "label": "Poca variación (straight-lining)", "badge": "warning"})
        if mean_peer_kw is not None and mean_peer_kw < 0.20:
            flags.append({"code": "low_agreement", "label": "Bajo acuerdo con pares", "badge": "warning"})
        if not flags and len(j_scores) >= 3:
            flags.append({"code": "ok", "label": "Normal", "badge": "success"})

        sess = latest_session.get(jid)
        is_online = bool(sess and sess.get("seconds_since_active") is not None and sess["seconds_since_active"] <= 120)

        diagnostics.append({
            "judge_id": jid,
            "username": j.get("username"),
            "display_name": j.get("display_name"),
            "is_online": is_online,
            "last_active": sess.get("last_active") if sess else None,
            "active_duration_sec": total_duration_by_judge.get(jid, 0),
            "completed_questions": completed_q,
            "total_questions": total_campaign_questions,
            "progress_pct": progress_pct,
            "total_ratings": len(j_scores),
            "median_latency_sec": median_lat_sec,
            "mean_score": mean_score,
            "sd_score": sd_score,
            "bias_delta": bias_delta,
            "bias_z": bias_z,
            "bias_label": bias_label,
            "consensus_mad": consensus_mad,
            "mean_peer_weighted_kappa": mean_peer_kw,
            "flags": flags,
        })

    return diagnostics


def compute_disagreement_audit(
    evaluations: List[Dict[str, Any]],
    top_n: int = 15,
) -> List[Dict[str, Any]]:
    """
    Identifica los ítems (pregunta + variante + criterio) con mayor desacuerdo
    entre jueces (ordenados por rango max-min y desviación estándar) para
    revisión durante sesiones de calibración de pilotos.
    """
    grouped: Dict[Tuple[int, int], List[Dict[str, Any]]] = defaultdict(list)
    for e in evaluations:
        if e.get("score") is None:
            continue
        key = (int(e["response_id"]), int(e["criterion_id"]))
        grouped[key].append(e)

    items: List[Dict[str, Any]] = []
    for (resp_id, crit_id), rows in grouped.items():
        if len(rows) < 2:
            continue
        scores = [int(r["score"]) for r in rows]
        score_range = max(scores) - min(scores)
        sd = compute_sd([float(s) for s in scores])
        if score_range == 0:
            continue
        first = rows[0]
        judge_breakdown = [
            {
                "judge_id": int(r["judge_id"]),
                "judge_name": r.get("judge_name") or f"Juez {r['judge_id']}",
                "score": int(r["score"]),
                "comment": r.get("comment") or "",
            }
            for r in rows
        ]
        items.append({
            "question_id": first.get("question_id"),
            "question_text": first.get("question_text") or "",
            "is_calibration": bool(first.get("is_calibration")),
            "response_id": resp_id,
            "response_text": first.get("response_text") or "",
            "source_type": first.get("source_type") or "",
            "criterion_id": crit_id,
            "criterion_name": first.get("criterion_name") or "",
            "n_judges": len(scores),
            "mean_score": round(compute_mean([float(s) for s in scores]), 2),
            "sd_score": round(sd, 2),
            "range_score": score_range,
            "min_score": min(scores),
            "max_score": max(scores),
            "judge_ratings": judge_breakdown,
        })

    items.sort(key=lambda x: (x["range_score"], x["sd_score"], x["n_judges"]), reverse=True)
    return items[:top_n]
