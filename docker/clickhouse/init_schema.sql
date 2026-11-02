-- ClickHouse Schema for Agentic YouTube Analyzer & Fact-Checking Platform
CREATE DATABASE IF NOT EXISTS youtube_agent;

SET allow_experimental_json_type = 1;

CREATE TABLE IF NOT EXISTS youtube_agent.video_analyses (
    channel_id LowCardinality(String),
    channel_name String,
    video_id String,
    video_title String,
    overall_accuracy_score UInt8,
    has_paid_promotion Bool,
    disclosed_properly Bool,
    sponsors Array(LowCardinality(String)),
    collaborators Array(String),
    topic_tags Array(LowCardinality(String)),
    analysis_payload JSON,
    analyzed_at DateTime64(3, 'UTC') DEFAULT now64()
)
ENGINE = ReplacingMergeTree(analyzed_at)
ORDER BY (channel_id, video_id);
