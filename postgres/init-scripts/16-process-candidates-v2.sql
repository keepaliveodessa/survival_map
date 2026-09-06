-- Minimal fix: replace only line_merged_valid CTE with stricter filtering
CREATE OR REPLACE FUNCTION process_candidates_v2(
    p_geo_ids            INTEGER[]   DEFAULT NULL,
    p_scores             DOUBLE PRECISION[] DEFAULT NULL,
    p_texts              TEXT[]      DEFAULT NULL,
    p_hint               VARCHAR     DEFAULT NULL,
    p_score_threshold    DOUBLE PRECISION DEFAULT 0.80,
    p_intersection_buffer_m DOUBLE PRECISION DEFAULT 100.0,
    p_max_scatter_m      DOUBLE PRECISION DEFAULT 500.0
)
RETURNS TABLE (
    result_strategy      TEXT,
    result_geom          GEOMETRY,
    result_matches       JSONB,
    result_confidence    DOUBLE PRECISION,
    result_diagnostics   JSONB
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_scores            DOUBLE PRECISION[];
    v_filtered_ids      INTEGER[];
    v_filtered_scores   DOUBLE PRECISION[];
    v_filtered_texts    TEXT[];
    v_candidate_count   INTEGER;
    v_geo_count         INTEGER;
    v_anti_list_m       DOUBLE PRECISION := 3000.0;
    v_anti_list_score   DOUBLE PRECISION := 0.85;
BEGIN
    v_scores := COALESCE(p_scores,
        ARRAY_FILL(1.0::double precision, ARRAY[COALESCE(array_length(p_geo_ids, 1), 0)]));

    IF p_geo_ids IS NULL OR array_length(p_geo_ids, 1) = 0 THEN
        RETURN QUERY SELECT 'random_null'::TEXT, NULL::GEOMETRY, '[]'::JSONB,
            0.0::DOUBLE PRECISION, jsonb_build_object('reason', 'no_candidates');
        RETURN;
    END IF;

    v_candidate_count := array_length(p_geo_ids, 1);

    WITH raw AS (
        SELECT s.id, s.type, s.names, ST_MakeValid(s.geom) AS geom, s.geom_m, u.score, u.text
        FROM geo s
        JOIN unnest(p_geo_ids, v_scores,
            COALESCE(p_texts, ARRAY_FILL(NULL::text, ARRAY[v_candidate_count]))
        ) AS u(id, score, text) ON s.id = u.id
        WHERE u.score >= p_score_threshold AND s.geom IS NOT NULL
    ),
    deduped AS (
        SELECT DISTINCT ON (id) id, type, names, geom, geom_m, score, text
        FROM raw ORDER BY id, score DESC
    ),
    ordered AS (SELECT * FROM deduped ORDER BY score DESC LIMIT 10)
    SELECT COALESCE(array_agg(id ORDER BY score DESC), ARRAY[]::INTEGER[]),
           COALESCE(array_agg(score ORDER BY score DESC), ARRAY[]::DOUBLE PRECISION[]),
           COALESCE(array_agg(text ORDER BY score DESC), ARRAY[]::TEXT[])
    INTO v_filtered_ids, v_filtered_scores, v_filtered_texts
    FROM ordered;

    v_geo_count := array_length(v_filtered_ids, 1);

    IF v_geo_count IS NULL OR v_geo_count = 0 THEN
        RETURN QUERY SELECT 'random_null'::TEXT, NULL::GEOMETRY, '[]'::JSONB,
            0.0::DOUBLE PRECISION, jsonb_build_object('reason', 'no_valid_candidates');
        RETURN;
    END IF;

    RETURN QUERY
    WITH candidates AS (
        SELECT s.id, s.type, s.names, ST_MakeValid(s.geom) AS geom, s.geom_m, u.score, u.text
        FROM geo s
        JOIN unnest(v_filtered_ids, v_filtered_scores, v_filtered_texts)
            AS u(id, score, text) ON s.id = u.id
    ),
    graph_edges AS (
        SELECT a.id AS id_a, b.id AS id_b
        FROM candidates a CROSS JOIN candidates b
        WHERE a.id < b.id AND ST_DWithin(a.geom_m, b.geom_m, p_intersection_buffer_m)
    ),
    best_candidate AS (
        SELECT id, score FROM candidates ORDER BY score DESC LIMIT 1
    ),
    main_component_neighbors AS (
        SELECT bc.id AS root_id, e.id_b AS member_id FROM best_candidate bc JOIN graph_edges e ON e.id_a = bc.id
        UNION
        SELECT bc.id AS root_id, e.id_a AS member_id FROM best_candidate bc JOIN graph_edges e ON e.id_b = bc.id
    ),
    main_component AS (
        SELECT root_id, member_id AS id FROM main_component_neighbors
        UNION
        SELECT mcn.root_id, e.id_b AS id FROM main_component_neighbors mcn JOIN graph_edges e ON e.id_a = mcn.member_id WHERE e.id_b != mcn.root_id
        UNION
        SELECT mcn.root_id, e.id_a AS id FROM main_component_neighbors mcn JOIN graph_edges e ON e.id_b = mcn.member_id WHERE e.id_a != mcn.root_id
    ),
    mc_all AS (SELECT mc.id FROM main_component mc UNION SELECT bc.id FROM best_candidate bc),
    mc_candidates AS (SELECT c.* FROM candidates c WHERE c.id IN (SELECT id FROM mc_all)),

    hypothesis_single AS (
        SELECT 'single_match'::TEXT AS strategy, c.geom, c.score AS total_score,
            jsonb_build_object('type','single_match','geo_id',c.id,'score',c.score) AS diagnostics
        FROM mc_candidates c
    ),

    component_intersections AS (
        SELECT ST_PointOnSurface(ST_Intersection(a.geom, b.geom)) AS pt_m,
            ST_Transform(ST_PointOnSurface(ST_Intersection(a.geom, b.geom)), 3857) AS pt_m_3857
        FROM mc_candidates a CROSS JOIN mc_candidates b
        WHERE a.id < b.id AND ST_Intersects(a.geom, b.geom)
          AND NOT ST_IsEmpty(ST_Intersection(a.geom, b.geom))
    ),
    ci_count AS (SELECT COUNT(*) AS cnt FROM component_intersections),

    -- H2: street_segment — stricter validity filter
    component_lines AS (
        SELECT c.* FROM mc_candidates c
        WHERE ST_GeometryType(c.geom) IN ('ST_LineString', 'ST_MultiLineString')
    ),
    line_merged AS (
        SELECT cl.id AS line_id, cl.names AS line_names, cl.score AS line_score,
            ST_LineMerge(ST_CollectionExtract(cl.geom_m, 2)) AS merged_line
        FROM component_lines cl
    ),
    -- CRITICAL: merged_line must be a valid, non-empty line geometry
    line_merged_valid AS (
        SELECT * FROM line_merged
        WHERE ST_GeometryType(merged_line) = 'ST_LineString'
          AND NOT ST_IsEmpty(merged_line)
          AND ST_NPoints(merged_line) >= 2
    ),
    line_intersection_hits AS (
        SELECT lm.line_id, lm.line_names, lm.line_score,
            COUNT(*) AS hit_count,
            MIN(lf.frac) AS min_frac, MAX(lf.frac) AS max_frac
        FROM line_merged_valid lm
        JOIN LATERAL (
            SELECT ST_LineLocatePoint(lm.merged_line, ci.pt_m_3857) AS frac
            FROM component_intersections ci
            WHERE ST_DWithin(ci.pt_m_3857, lm.merged_line, p_intersection_buffer_m)
        ) lf ON true
        GROUP BY lm.line_id, lm.line_names, lm.line_score
        HAVING COUNT(*) >= 2
    ),
    hypothesis_street_segment AS (
        SELECT 'street_segment'::TEXT AS strategy,
            ST_Transform(ST_SetSRID(
                ST_LineSubstring(lm.merged_line,
                    GREATEST(0.001::DOUBLE PRECISION, LEAST(0.999::DOUBLE PRECISION, lh.min_frac)),
                    GREATEST(0.001::DOUBLE PRECISION, LEAST(0.999::DOUBLE PRECISION, lh.max_frac))
                ), 3857), 4326) AS geom,
            lh.line_score * 0.9 AS total_score,
            jsonb_build_object('type','street_segment','line_id',lh.line_id,
                'line_names',lh.line_names,'hit_count',lh.hit_count) AS diagnostics
        FROM line_intersection_hits lh
        JOIN line_merged_valid lm ON lm.line_id = lh.line_id
    ),

    hypothesis_intersection AS (
        SELECT 'intersection'::TEXT AS strategy,
            (SELECT ST_Transform(ST_SetSRID(ST_Centroid(ST_Collect(ci3.pt_m_3857)), 3857), 4326)
             FROM component_intersections ci3) AS geom,
            (SELECT AVG(c.score) FROM mc_candidates c)::DOUBLE PRECISION AS total_score,
            jsonb_build_object('type','intersection',
                'geo_ids',(SELECT array_agg(id) FROM mc_candidates),
                'spread_m',(SELECT ST_MaxDistance(ST_Collect(ci4.pt_m_3857), ST_Centroid(ST_Collect(ci4.pt_m_3857)))
                            FROM component_intersections ci4),
                'intersection_count',(SELECT cnt FROM ci_count)) AS diagnostics
        WHERE (SELECT ST_MaxDistance(ST_Collect(ci5.pt_m_3857), ST_Centroid(ST_Collect(ci5.pt_m_3857)))
               FROM component_intersections ci5) <= p_intersection_buffer_m * 2
          AND NOT EXISTS (SELECT 1 FROM hypothesis_street_segment)
    ),

    wc_pair_anchors AS (
        SELECT ST_LineInterpolatePoint(ST_ShortestLine(a.geom_m, b.geom_m), 0.5) AS pt_m
        FROM mc_candidates a CROSS JOIN mc_candidates b
        WHERE a.id < b.id AND ST_Distance(a.geom_m, b.geom_m) <= 800.0
    ),
    wc_scatter AS (
        SELECT ST_MaxDistance(ST_Collect(wp.pt_m), ST_Centroid(ST_Collect(wp.pt_m))) AS distance_m
        FROM wc_pair_anchors wp
    ),
    hypothesis_weighted_centroid AS (
        SELECT 'weighted_centroid'::TEXT AS strategy,
            ST_Transform(ST_SetSRID(
                ST_MakePoint(
                    SUM(ST_X(wp.pt_m) * c.score) / NULLIF(SUM(c.score), 0),
                    SUM(ST_Y(wp.pt_m) * c.score) / NULLIF(SUM(c.score), 0)
                ), 3857), 4326) AS geom,
            GREATEST(0.1, (SELECT AVG(c.score) FROM mc_candidates c) * 0.85
                - LEAST(0.3, COALESCE(sc.distance_m, 0) * 0.0004)) AS total_score,
            jsonb_build_object('type','weighted_centroid',
                'geo_ids',(SELECT array_agg(id) FROM mc_candidates),
                'scatter_m',sc.distance_m,
                'candidate_count',(SELECT COUNT(*) FROM mc_candidates)) AS diagnostics
        FROM mc_candidates c
        JOIN wc_pair_anchors wp ON 1=1
        CROSS JOIN wc_scatter sc
        WHERE (SELECT COUNT(*) FROM mc_candidates) >= 2
          AND NOT EXISTS (SELECT 1 FROM component_intersections)
          AND NOT EXISTS (SELECT 1 FROM hypothesis_street_segment)
          AND sc.distance_m <= p_max_scatter_m
        GROUP BY sc.distance_m
    ),

    all_hypotheses AS (
        SELECT * FROM hypothesis_street_segment
        UNION ALL SELECT * FROM hypothesis_intersection
        UNION ALL SELECT * FROM hypothesis_weighted_centroid
        UNION ALL SELECT * FROM hypothesis_single
    ),
    best_hypothesis AS (
        SELECT h.strategy, h.geom, h.total_score, h.diagnostics FROM all_hypotheses h
        ORDER BY CASE h.strategy WHEN 'street_segment' THEN 5 WHEN 'intersection' THEN 4
            WHEN 'weighted_centroid' THEN 3 ELSE 1 END DESC, h.total_score DESC
        LIMIT 1
    ),
    -- Single match: приоритет NLP-score (TASK 3 / Hard Constraint 4-5).
    -- Раньше тип геометрии/длина линии решали раньше score — при scatter >
    -- p_max_scatter_m выбирался НЕ лучший по score кандидат. Теперь:
    -- score DESC → тип линии → длина → geo_id (стабильный tiebreak).
    best_single AS (
        SELECT c.geom, c.score AS total_score,
            jsonb_build_object('type','single_match','geo_id',c.id,'score',c.score) AS diagnostics
        FROM mc_candidates c
        ORDER BY
            c.score DESC,
            CASE WHEN ST_GeometryType(c.geom) IN ('ST_LineString','ST_MultiLineString') THEN 2 ELSE 1 END DESC,
            ST_Length(c.geom_m) DESC, c.id ASC
        LIMIT 1
    ),
    anti_list_trigger AS (
        SELECT bh.strategy, bh.geom, bh.total_score, bh.diagnostics,
            bh.strategy IS NOT NULL AND EXISTS (
                SELECT 1 FROM mc_candidates c
                WHERE c.score >= v_anti_list_score
                  AND ST_Distance(ST_Transform(bh.geom, 3857), c.geom_m) > v_anti_list_m
            ) AS triggered
        FROM best_hypothesis bh
    ),
    anti_list_check AS (
        SELECT
            CASE WHEN alt.triggered THEN 'single_match' ELSE alt.strategy END AS safe_strategy,
            CASE WHEN alt.triggered THEN bs.geom ELSE alt.geom END AS safe_geom,
            CASE WHEN alt.triggered THEN bs.total_score ELSE alt.total_score END AS safe_confidence,
            CASE WHEN alt.triggered THEN bs.diagnostics ELSE alt.diagnostics END AS safe_diagnostics
        FROM anti_list_trigger alt CROSS JOIN best_single bs
    ),
    final_result AS (
        SELECT COALESCE(alc.safe_strategy,'single_match') AS strategy,
            COALESCE(alc.safe_geom, bs.geom) AS geom,
            COALESCE(alc.safe_confidence, bs.total_score) AS confidence,
            COALESCE(alc.safe_diagnostics, bs.diagnostics) AS diagnostics
        FROM anti_list_check alc LEFT JOIN best_single bs ON 1=1
    )
    SELECT fr.strategy, fr.geom,
        (SELECT jsonb_agg(
            jsonb_build_object('geo_id',c.id,'name',c.names[1],'similarity',c.score,'matched_text',c.text)
            ORDER BY c.score DESC) FROM mc_candidates c) AS matches,
        fr.confidence, fr.diagnostics
    FROM final_result fr;
END;
$$;
