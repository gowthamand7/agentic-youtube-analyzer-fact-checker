package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2"
	"github.com/ClickHouse/clickhouse-go/v2/lib/driver"
)

// VideoEvaluationSummary provides compact historical evaluation info for a channel run.
type VideoEvaluationSummary struct {
	VideoID              string    `json:"video_id"`
	VideoTitle           string    `json:"video_title"`
	OverallAccuracyScore uint8     `json:"overall_accuracy_score"`
	HasPaidPromotion     bool      `json:"has_paid_promotion"`
	AnalyzedAt           time.Time `json:"analyzed_at"`
}

// ChannelHistory aggregates creator metrics and past evaluation runs.
type ChannelHistory struct {
	ChannelID             string                   `json:"channel_id"`
	TotalVideosAnalyzed   int                      `json:"total_videos_analyzed"`
	AvgAccuracyScore      float64                  `json:"avg_accuracy_score"`
	SponsoredVideosCount  int                      `json:"sponsored_videos_count"`
	FrequentSponsors      []string                 `json:"frequent_sponsors"`
	FrequentCollaborators []string                 `json:"frequent_collaborators"`
	RecentRuns            []VideoEvaluationSummary `json:"recent_runs"`
}

// VideoAnalysisRecord represents a completed analysis record stored in ClickHouse.
type VideoAnalysisRecord struct {
	ChannelID            string    `json:"channel_id"`
	ChannelName          string    `json:"channel_name"`
	VideoID              string    `json:"video_id"`
	VideoTitle           string    `json:"video_title"`
	OverallAccuracyScore uint8     `json:"overall_accuracy_score"`
	HasPaidPromotion     bool      `json:"has_paid_promotion"`
	DisclosedProperly    bool      `json:"disclosed_properly"`
	Sponsors             []string  `json:"sponsors"`
	Collaborators        []string  `json:"collaborators"`
	TopicTags            []string  `json:"topic_tags"`
	AnalysisPayload      string    `json:"analysis_payload"`
	AnalyzedAt           time.Time `json:"analyzed_at"`
}

// HistoricalClaim represents a previously audited claim returned to the investigator agent.
type HistoricalClaim struct {
	VideoID    string   `json:"video_id"`
	VideoTitle string   `json:"video_title"`
	Timestamp  string   `json:"timestamp"`
	Claim      string   `json:"claim"`
	Verdict    string   `json:"verdict"`
	Evidence   string   `json:"evidence"`
	Sources    []string `json:"sources"`
	TopicTags  []string `json:"topic_tags,omitempty"`
	AnalyzedAt string   `json:"analyzed_at,omitempty"`
}

// DBService defines the storage interface for memory-db-mcp.
type DBService interface {
	GetChannelHistory(ctx context.Context, channelID string) (*ChannelHistory, error)
	SaveVideoAnalysis(ctx context.Context, record *VideoAnalysisRecord) error
	QueryHistoricalClaims(ctx context.Context, topicTag string, limit int) ([]HistoricalClaim, error)
	Close() error
	Ping(ctx context.Context) error
}

// Config holds ClickHouse connection parameters.
type Config struct {
	Host     string
	Port     string
	Database string
	Username string
	Password string
}

// LoadConfigFromEnv reads ClickHouse connection configuration with sensible defaults.
func LoadConfigFromEnv() *Config {
	return &Config{
		Host:     getEnvOrDefault("CLICKHOUSE_HOST", "localhost"),
		Port:     getEnvOrDefault("CLICKHOUSE_PORT", "9000"),
		Database: getEnvOrDefault("CLICKHOUSE_DB", "youtube_agent"),
		Username: getEnvOrDefault("CLICKHOUSE_USER", "admin"),
		Password: getEnvOrDefault("CLICKHOUSE_PASSWORD", "secret"),
	}
}

func getEnvOrDefault(key, defaultVal string) string {
	if val := os.Getenv(key); strings.TrimSpace(val) != "" {
		return strings.TrimSpace(val)
	}
	return defaultVal
}

// ClickHouseDB is the native ClickHouse client implementation of DBService.
type ClickHouseDB struct {
	conn     driver.Conn
	database string
}

// NewClickHouseDB initializes a pooled native TCP ClickHouse connection.
func NewClickHouseDB(cfg *Config) (*ClickHouseDB, error) {
	conn, err := clickhouse.Open(&clickhouse.Options{
		Addr: []string{fmt.Sprintf("%s:%s", cfg.Host, cfg.Port)},
		Auth: clickhouse.Auth{
			Database: cfg.Database,
			Username: cfg.Username,
			Password: cfg.Password,
		},
		DialTimeout:     5 * time.Second,
		MaxOpenConns:    10,
		MaxIdleConns:    5,
		ConnMaxLifetime: time.Hour,
	})
	if err != nil {
		return nil, fmt.Errorf("clickhouse open connection error: %w", err)
	}

	return &ClickHouseDB{conn: conn, database: cfg.Database}, nil
}

// Close closes the underlying ClickHouse connection pool.
func (db *ClickHouseDB) Close() error {
	if db.conn != nil {
		return db.conn.Close()
	}
	return nil
}

// Ping checks whether ClickHouse is reachable.
func (db *ClickHouseDB) Ping(ctx context.Context) error {
	if db.conn == nil {
		return fmt.Errorf("clickhouse connection is nil")
	}
	return db.conn.Ping(ctx)
}

// InitSchema ensures the target database and video_analyses table exist.
func (db *ClickHouseDB) InitSchema(ctx context.Context) error {
	if db.conn == nil {
		return fmt.Errorf("clickhouse connection is nil")
	}

	createDB := fmt.Sprintf("CREATE DATABASE IF NOT EXISTS %s;", db.database)
	if err := db.conn.Exec(ctx, createDB); err != nil {
		return fmt.Errorf("failed to ensure database exists: %w", err)
	}

	createTable := fmt.Sprintf(`
	CREATE TABLE IF NOT EXISTS %s.video_analyses (
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
		analysis_payload String,
		analyzed_at DateTime64(3, 'UTC') DEFAULT now64()
	)
	ENGINE = ReplacingMergeTree(analyzed_at)
	ORDER BY (channel_id, video_id);
	`, db.database)
	if err := db.conn.Exec(ctx, createTable); err != nil {
		return fmt.Errorf("failed to ensure video_analyses table exists: %w", err)
	}

	return nil
}

// GetChannelHistory aggregates historical accuracy score, sponsored video count, frequent sponsors, and previous run records.
func (db *ClickHouseDB) GetChannelHistory(ctx context.Context, channelID string) (*ChannelHistory, error) {
	if db.conn == nil {
		return nil, fmt.Errorf("clickhouse service unavailable")
	}

	channelID = strings.TrimSpace(channelID)
	history := &ChannelHistory{
		ChannelID:             channelID,
		TotalVideosAnalyzed:   0,
		AvgAccuracyScore:      0.0,
		SponsoredVideosCount:  0,
		FrequentSponsors:      []string{},
		FrequentCollaborators: []string{},
		RecentRuns:            []VideoEvaluationSummary{},
	}

	// 1. Query aggregate counts
	aggQuery := fmt.Sprintf(`
	SELECT
		count() AS total_videos_analyzed,
		coalesce(round(avg(overall_accuracy_score), 1), 0.0) AS avg_accuracy_score,
		countIf(has_paid_promotion = 1) AS sponsored_videos_count
	FROM %s.video_analyses FINAL
	WHERE channel_id = ?
	`, db.database)
	row := db.conn.QueryRow(ctx, aggQuery, channelID)
	var total int
	var avgScore float64
	var sponsoredCount int
	if err := row.Scan(&total, &avgScore, &sponsoredCount); err != nil {
		return nil, fmt.Errorf("failed querying channel aggregates: %w", err)
	}

	history.TotalVideosAnalyzed = total
	history.AvgAccuracyScore = avgScore
	history.SponsoredVideosCount = sponsoredCount

	if total == 0 {
		return history, nil
	}

	// 2. Query recent evaluation runs and extract frequent sponsors & collaborators
	runsQuery := fmt.Sprintf(`
	SELECT
		video_id,
		video_title,
		overall_accuracy_score,
		has_paid_promotion,
		analyzed_at,
		sponsors,
		collaborators
	FROM %s.video_analyses FINAL
	WHERE channel_id = ?
	ORDER BY analyzed_at DESC
	LIMIT 10
	`, db.database)
	rows, err := db.conn.Query(ctx, runsQuery, channelID)
	if err != nil {
		return nil, fmt.Errorf("failed querying recent channel runs: %w", err)
	}
	defer rows.Close()

	sponsorCounts := make(map[string]int)
	collabCounts := make(map[string]int)

	for rows.Next() {
		var summary VideoEvaluationSummary
		var sponsors []string
		var collabs []string

		if err := rows.Scan(
			&summary.VideoID,
			&summary.VideoTitle,
			&summary.OverallAccuracyScore,
			&summary.HasPaidPromotion,
			&summary.AnalyzedAt,
			&sponsors,
			&collabs,
		); err != nil {
			return nil, fmt.Errorf("failed scanning recent run row: %w", err)
		}

		history.RecentRuns = append(history.RecentRuns, summary)

		for _, s := range sponsors {
			s = strings.TrimSpace(s)
			if s != "" {
				sponsorCounts[s]++
			}
		}
		for _, c := range collabs {
			c = strings.TrimSpace(c)
			if c != "" {
				collabCounts[c]++
			}
		}
	}

	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("error reading channel runs: %w", err)
	}

	history.FrequentSponsors = topKKeys(sponsorCounts, 5)
	history.FrequentCollaborators = topKKeys(collabCounts, 5)

	return history, nil
}

// SaveVideoAnalysis stores an evaluated video analysis into ClickHouse.
func (db *ClickHouseDB) SaveVideoAnalysis(ctx context.Context, record *VideoAnalysisRecord) error {
	if db.conn == nil {
		return fmt.Errorf("clickhouse service unavailable")
	}
	if record == nil {
		return fmt.Errorf("record cannot be nil")
	}

	if record.Sponsors == nil {
		record.Sponsors = []string{}
	}
	if record.Collaborators == nil {
		record.Collaborators = []string{}
	}
	if record.TopicTags == nil {
		record.TopicTags = []string{}
	}
	if record.AnalyzedAt.IsZero() {
		record.AnalyzedAt = time.Now().UTC()
	}

	insertSQL := fmt.Sprintf(`
	INSERT INTO %s.video_analyses (
		channel_id,
		channel_name,
		video_id,
		video_title,
		overall_accuracy_score,
		has_paid_promotion,
		disclosed_properly,
		sponsors,
		collaborators,
		topic_tags,
		analysis_payload,
		analyzed_at
	) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
	`, db.database)

	err := db.conn.Exec(ctx, insertSQL,
		record.ChannelID,
		record.ChannelName,
		record.VideoID,
		record.VideoTitle,
		record.OverallAccuracyScore,
		record.HasPaidPromotion,
		record.DisclosedProperly,
		record.Sponsors,
		record.Collaborators,
		record.TopicTags,
		record.AnalysisPayload,
		record.AnalyzedAt,
	)
	if err != nil {
		return fmt.Errorf("failed to insert video analysis into clickhouse: %w", err)
	}

	return nil
}

// QueryHistoricalClaims searches past records for previously verified claims matching topicTag.
func (db *ClickHouseDB) QueryHistoricalClaims(ctx context.Context, topicTag string, limit int) ([]HistoricalClaim, error) {
	if db.conn == nil {
		return nil, fmt.Errorf("clickhouse service unavailable")
	}

	topicTag = strings.TrimSpace(topicTag)
	if limit <= 0 {
		limit = 10
	}

	query := fmt.Sprintf(`
	SELECT
		video_id,
		video_title,
		topic_tags,
		analysis_payload,
		analyzed_at
	FROM %s.video_analyses FINAL
	WHERE has(topic_tags, ?) OR arrayExists(x -> lower(x) = lower(?), topic_tags)
	ORDER BY analyzed_at DESC
	LIMIT ?
	`, db.database)

	rows, err := db.conn.Query(ctx, query, topicTag, topicTag, limit*2)
	if err != nil {
		return nil, fmt.Errorf("failed to query historical claims: %w", err)
	}
	defer rows.Close()

	claims := make([]HistoricalClaim, 0)

	type rawClaim struct {
		Timestamp string   `json:"timestamp"`
		Claim     string   `json:"claim"`
		Verdict   string   `json:"verdict"`
		Evidence  string   `json:"evidence"`
		Sources   []string `json:"sources"`
	}

	type payloadWrapper struct {
		ClaimsVerified  []rawClaim `json:"claims_verified"`
		ExtractedClaims []rawClaim `json:"extracted_claims"`
	}

	for rows.Next() {
		var videoID string
		var videoTitle string
		var topicTags []string
		var payloadStr string
		var analyzedAt time.Time

		if err := rows.Scan(&videoID, &videoTitle, &topicTags, &payloadStr, &analyzedAt); err != nil {
			return nil, fmt.Errorf("failed scanning row for historical claims: %w", err)
		}

		if strings.TrimSpace(payloadStr) == "" {
			continue
		}

		var pw payloadWrapper
		if err := json.Unmarshal([]byte(payloadStr), &pw); err != nil {
			// Skip malformed payloads gracefully
			continue
		}

		extracted := pw.ClaimsVerified
		if len(extracted) == 0 {
			extracted = pw.ExtractedClaims
		}

		for _, item := range extracted {
			claims = append(claims, HistoricalClaim{
				VideoID:    videoID,
				VideoTitle: videoTitle,
				Timestamp:  item.Timestamp,
				Claim:      item.Claim,
				Verdict:    item.Verdict,
				Evidence:   item.Evidence,
				Sources:    item.Sources,
				TopicTags:  topicTags,
				AnalyzedAt: analyzedAt.Format(time.RFC3339),
			})

			if len(claims) >= limit {
				return claims, nil
			}
		}
	}

	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("error reading historical claims rows: %w", err)
	}

	return claims, nil
}

// topKKeys sorts map entries by frequency descending and returns top K keys.
func topKKeys(counts map[string]int, k int) []string {
	if len(counts) == 0 {
		return []string{}
	}

	type kv struct {
		Key   string
		Count int
	}

	var pairs []kv
	for key, count := range counts {
		pairs = append(pairs, kv{Key: key, Count: count})
	}

	sort.Slice(pairs, func(i, j int) bool {
		if pairs[i].Count == pairs[j].Count {
			return pairs[i].Key < pairs[j].Key
		}
		return pairs[i].Count > pairs[j].Count
	})

	if len(pairs) < k {
		k = len(pairs)
	}

	keys := make([]string, k)
	for i := 0; i < k; i++ {
		keys[i] = pairs[i].Key
	}
	return keys
}
