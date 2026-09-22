-- Training examples table for NLP calibration
-- Stores labeled examples for supervised learning and threshold tuning

CREATE TABLE IF NOT EXISTS training_examples (
    id SERIAL PRIMARY KEY,
    event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    
    -- Labels (filled by human annotator)
    correct_layer VARCHAR(20),          -- bus/cops/traffic/pig/other
    correct_geo_ids INTEGER[],          -- Array of correct geo object IDs
    is_promotional BOOLEAN DEFAULT FALSE,
    has_geo_reference BOOLEAN DEFAULT TRUE,
    
    -- NLP pipeline output (snapshot at labeling time)
    detected_layer VARCHAR(20),
    detected_strategy VARCHAR(50),
    detected_confidence DOUBLE PRECISION,
    detected_geo_ids INTEGER[],
    detected_geo_scores DOUBLE PRECISION[],
    
    -- Metadata
    labeled_by VARCHAR(100),            -- annotator identifier
    labeled_at TIMESTAMPTZ DEFAULT NOW(),
    notes TEXT,
    
    -- Constraints
    CHECK (correct_layer IN ('bus', 'cops', 'traffic', 'pig', 'other', NULL))
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_training_examples_layer 
    ON training_examples(correct_layer);
CREATE INDEX IF NOT EXISTS idx_training_examples_labeled_at 
    ON training_examples(labeled_at);
CREATE INDEX IF NOT EXISTS idx_training_examples_event_id 
    ON training_examples(event_id);

-- View: disagreement between detected and correct labels
CREATE OR REPLACE VIEW training_disagreement AS
SELECT 
    te.id,
    te.text,
    te.correct_layer,
    te.detected_layer,
    CASE WHEN te.correct_layer = te.detected_layer THEN 'correct' ELSE 'wrong' END as layer_match,
    te.correct_geo_ids,
    te.detected_geo_ids,
    te.detected_confidence,
    te.detected_strategy
FROM training_examples te
WHERE te.correct_layer IS NOT NULL;

-- View: summary statistics for calibration
CREATE OR REPLACE VIEW training_summary AS
SELECT 
    COUNT(*) as total_labeled,
    SUM(CASE WHEN correct_layer = detected_layer THEN 1 ELSE 0 END)::FLOAT / 
        NULLIF(COUNT(*), 0) as layer_accuracy,
    AVG(detected_confidence) as avg_confidence,
    SUM(CASE WHEN has_geo_reference = FALSE THEN 1 ELSE 0 END) as no_geo_count,
    SUM(CASE WHEN is_promotional THEN 1 ELSE 0 END) as promotional_count
FROM training_examples
WHERE correct_layer IS NOT NULL;
