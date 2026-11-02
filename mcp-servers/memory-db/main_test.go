package main

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/mark3labs/mcp-go/mcp"
)

// MockDB implements DBService for unit testing without live ClickHouse.
type MockDB struct {
	GetChannelHistoryFunc     func(ctx context.Context, channelID string) (*ChannelHistory, error)
	SaveVideoAnalysisFunc     func(ctx context.Context, record *VideoAnalysisRecord) error
	QueryHistoricalClaimsFunc func(ctx context.Context, topicTag string, limit int) ([]HistoricalClaim, error)
	PingFunc                  func(ctx context.Context) error
	CloseFunc                 func() error
}

func (m *MockDB) GetChannelHistory(ctx context.Context, channelID string) (*ChannelHistory, error) {
	if m.GetChannelHistoryFunc != nil {
		return m.GetChannelHistoryFunc(ctx, channelID)
	}
	return &ChannelHistory{ChannelID: channelID}, nil
}

func (m *MockDB) SaveVideoAnalysis(ctx context.Context, record *VideoAnalysisRecord) error {
	if m.SaveVideoAnalysisFunc != nil {
		return m.SaveVideoAnalysisFunc(ctx, record)
	}
	return nil
}

func (m *MockDB) QueryHistoricalClaims(ctx context.Context, topicTag string, limit int) ([]HistoricalClaim, error) {
	if m.QueryHistoricalClaimsFunc != nil {
		return m.QueryHistoricalClaimsFunc(ctx, topicTag, limit)
	}
	return []HistoricalClaim{}, nil
}

func (m *MockDB) Ping(ctx context.Context) error {
	if m.PingFunc != nil {
		return m.PingFunc(ctx)
	}
	return nil
}

func (m *MockDB) Close() error {
	if m.CloseFunc != nil {
		return m.CloseFunc()
	}
	return nil
}

func TestToolRegistration(t *testing.T) {
	mock := &MockDB{}
	s := NewMCPServerInstance(mock)

	tools := s.ListTools()

	expectedTools := []string{
		"get_channel_history",
		"save_video_analysis",
		"query_historical_claims",
	}

	for _, name := range expectedTools {
		tool, exists := tools[name]
		if !exists || tool == nil {
			t.Fatalf("expected tool %q to be registered, but was not found", name)
		}
		if tool.Tool.Name != name {
			t.Errorf("tool name mismatch: got %q, want %q", tool.Tool.Name, name)
		}
	}
}

func TestGetChannelHistorySuccess(t *testing.T) {
	called := false
	mock := &MockDB{
		GetChannelHistoryFunc: func(ctx context.Context, channelID string) (*ChannelHistory, error) {
			called = true
			if channelID != "UC12345" {
				t.Errorf("unexpected channelID: got %q, want 'UC12345'", channelID)
			}
			return &ChannelHistory{
				ChannelID:             "UC12345",
				TotalVideosAnalyzed:   3,
				AvgAccuracyScore:      88.5,
				SponsoredVideosCount:  1,
				FrequentSponsors:      []string{"BrandX"},
				FrequentCollaborators: []string{"CollabY"},
				RecentRuns: []VideoEvaluationSummary{
					{
						VideoID:              "vid_001",
						VideoTitle:           "Test Video",
						OverallAccuracyScore: 90,
						AnalyzedAt:           time.Now().UTC(),
					},
				},
			}, nil
		},
	}

	s := NewMCPServerInstance(mock)
	tool := s.GetTool("get_channel_history")
	if tool == nil {
		t.Fatal("tool get_channel_history not found")
	}

	req := mcp.CallToolRequest{}
	req.Params.Name = "get_channel_history"
	req.Params.Arguments = map[string]any{
		"channel_id": "UC12345",
	}

	res, err := tool.Handler(context.Background(), req)
	if err != nil {
		t.Fatalf("unexpected handler error: %v", err)
	}
	if !called {
		t.Fatal("expected GetChannelHistory to be called")
	}
	if res.IsError {
		t.Fatalf("expected success, got tool error result: %+v", res)
	}

	if len(res.Content) == 0 {
		t.Fatal("expected non-empty content in tool result")
	}

	textContent, ok := res.Content[0].(mcp.TextContent)
	if !ok {
		t.Fatalf("expected TextContent, got %T", res.Content[0])
	}

	var history ChannelHistory
	if err := json.Unmarshal([]byte(textContent.Text), &history); err != nil {
		t.Fatalf("failed to unmarshal result JSON: %v", err)
	}

	if history.ChannelID != "UC12345" || history.TotalVideosAnalyzed != 3 || history.AvgAccuracyScore != 88.5 {
		t.Errorf("unexpected history content: %+v", history)
	}
}

func TestGetChannelHistoryMissingArg(t *testing.T) {
	mock := &MockDB{}
	s := NewMCPServerInstance(mock)
	tool := s.GetTool("get_channel_history")

	req := mcp.CallToolRequest{}
	req.Params.Name = "get_channel_history"
	req.Params.Arguments = map[string]any{}

	res, err := tool.Handler(context.Background(), req)
	if err != nil {
		t.Fatalf("expected handler to return tool error result rather than Go error: %v", err)
	}
	if !res.IsError {
		t.Fatalf("expected error result for missing argument, got: %+v", res)
	}
}

func TestGetChannelHistoryDBError(t *testing.T) {
	mock := &MockDB{
		GetChannelHistoryFunc: func(ctx context.Context, channelID string) (*ChannelHistory, error) {
			return nil, errors.New("clickhouse connection timeout")
		},
	}
	s := NewMCPServerInstance(mock)
	tool := s.GetTool("get_channel_history")

	req := mcp.CallToolRequest{}
	req.Params.Name = "get_channel_history"
	req.Params.Arguments = map[string]any{
		"channel_id": "UC12345",
	}

	res, err := tool.Handler(context.Background(), req)
	if err != nil {
		t.Fatalf("expected tool error result rather than Go error: %v", err)
	}
	if !res.IsError {
		t.Fatal("expected error result on db error")
	}
}

func TestSaveVideoAnalysisSuccess(t *testing.T) {
	var savedRecord *VideoAnalysisRecord
	mock := &MockDB{
		SaveVideoAnalysisFunc: func(ctx context.Context, record *VideoAnalysisRecord) error {
			savedRecord = record
			return nil
		},
	}

	s := NewMCPServerInstance(mock)
	tool := s.GetTool("save_video_analysis")
	if tool == nil {
		t.Fatal("tool save_video_analysis not found")
	}

	payloadJSON := `{
		"channel_id": "UC12345",
		"channel_name": "Tech Reviews",
		"video_id": "v987",
		"video_title": "Is Python 3.14 really faster?",
		"overall_accuracy_score": 85,
		"has_paid_promotion": true,
		"disclosed_properly": true,
		"sponsors": ["Sponsor Corp"],
		"collaborators": ["Guest Dev"],
		"topic_tags": ["python", "benchmarks"],
		"clickbait_buster": {"verdict": "Yes, but only for tailored tasks"},
		"claims_verified": [
			{
				"timestamp": "01:23",
				"claim": "GIL free mode gives 3x speedup",
				"verdict": "Misleading",
				"evidence": "Benchmarks show 1.2x on CPU bound tasks",
				"sources": ["https://python.org"]
			}
		]
	}`

	req := mcp.CallToolRequest{}
	req.Params.Name = "save_video_analysis"
	req.Params.Arguments = map[string]any{
		"payload_json": payloadJSON,
	}

	res, err := tool.Handler(context.Background(), req)
	if err != nil {
		t.Fatalf("unexpected handler error: %v", err)
	}
	if res.IsError {
		t.Fatalf("expected success, got error result: %+v", res)
	}
	if savedRecord == nil {
		t.Fatal("expected record to be saved")
	}

	if savedRecord.ChannelID != "UC12345" || savedRecord.VideoID != "v987" || savedRecord.OverallAccuracyScore != 85 {
		t.Errorf("saved record mismatch: %+v", savedRecord)
	}
	if len(savedRecord.TopicTags) != 2 || savedRecord.TopicTags[0] != "python" {
		t.Errorf("saved record topic tags mismatch: %+v", savedRecord.TopicTags)
	}
}

func TestSaveVideoAnalysisInvalidJSON(t *testing.T) {
	mock := &MockDB{}
	s := NewMCPServerInstance(mock)
	tool := s.GetTool("save_video_analysis")

	req := mcp.CallToolRequest{}
	req.Params.Name = "save_video_analysis"
	req.Params.Arguments = map[string]any{
		"payload_json": "{not-valid-json",
	}

	res, err := tool.Handler(context.Background(), req)
	if err != nil {
		t.Fatalf("expected tool error result rather than Go error: %v", err)
	}
	if !res.IsError {
		t.Fatal("expected error result on invalid json")
	}
}

func TestQueryHistoricalClaims(t *testing.T) {
	mock := &MockDB{
		QueryHistoricalClaimsFunc: func(ctx context.Context, topicTag string, limit int) ([]HistoricalClaim, error) {
			if topicTag != "benchmark" {
				t.Errorf("unexpected topic tag: got %q, want 'benchmark'", topicTag)
			}
			if limit != 5 {
				t.Errorf("unexpected limit: got %d, want 5", limit)
			}
			return []HistoricalClaim{
				{
					VideoID:    "vid_abc",
					VideoTitle: "Speed Test",
					Timestamp:  "03:10",
					Claim:      "Benchmark X is 10x faster",
					Verdict:    "False",
					Evidence:   "Independent tests show 1.1x speedup",
					Sources:    []string{"https://example.com/test"},
				},
			}, nil
		},
	}

	s := NewMCPServerInstance(mock)
	tool := s.GetTool("query_historical_claims")
	if tool == nil {
		t.Fatal("tool query_historical_claims not found")
	}

	req := mcp.CallToolRequest{}
	req.Params.Name = "query_historical_claims"
	req.Params.Arguments = map[string]any{
		"topic_tag": "benchmark",
		"limit":     5,
	}

	res, err := tool.Handler(context.Background(), req)
	if err != nil {
		t.Fatalf("unexpected handler error: %v", err)
	}
	if res.IsError {
		t.Fatalf("expected success, got error result: %+v", res)
	}

	textContent, ok := res.Content[0].(mcp.TextContent)
	if !ok {
		t.Fatalf("expected TextContent, got %T", res.Content[0])
	}

	var claims []HistoricalClaim
	if err := json.Unmarshal([]byte(textContent.Text), &claims); err != nil {
		t.Fatalf("failed to unmarshal claims: %v", err)
	}
	if len(claims) != 1 || claims[0].VideoID != "vid_abc" {
		t.Errorf("unexpected claims: %+v", claims)
	}
}

func TestGracefulDegradationOfflineDB(t *testing.T) {
	// When DB is nil (e.g. failed connection during startup), handler should return clean error
	s := NewMCPServerInstance(nil)

	for _, toolName := range []string{"get_channel_history", "save_video_analysis", "query_historical_claims"} {
		tool := s.GetTool(toolName)
		if tool == nil {
			t.Fatalf("tool %q not found", toolName)
		}

		req := mcp.CallToolRequest{}
		req.Params.Name = toolName
		req.Params.Arguments = map[string]any{
			"channel_id":   "UC123",
			"payload_json": "{}",
			"topic_tag":    "tech",
		}

		res, err := tool.Handler(context.Background(), req)
		if err != nil {
			t.Fatalf("expected tool error result rather than Go error for tool %q: %v", toolName, err)
		}
		if !res.IsError {
			t.Fatalf("expected error result when DB is offline for tool %q", toolName)
		}
		textContent, ok := res.Content[0].(mcp.TextContent)
		if !ok || !strings.Contains(strings.ToLower(textContent.Text), "offline") && !strings.Contains(strings.ToLower(textContent.Text), "unavailable") {
			t.Errorf("expected offline message in error result: %v", textContent.Text)
		}
	}
}

func TestParseVideoAnalysisPayload(t *testing.T) {
	// 1. Empty payload
	if _, err := ParseVideoAnalysisPayload(""); err == nil {
		t.Error("expected error for empty payload")
	}

	// 2. Score clamping and custom timestamp
	raw := `{
		"channel_id": "UC999",
		"channel_name": "TestChan",
		"video_id": "v100",
		"video_title": "Title 100",
		"overall_accuracy_score": 150,
		"has_paid_promotion": false,
		"disclosed_properly": false,
		"sponsors": [],
		"collaborators": [],
		"topic_tags": ["go", "testing"],
		"analyzed_at": "2026-11-01T12:00:00Z"
	}`

	rec, err := ParseVideoAnalysisPayload(raw)
	if err != nil {
		t.Fatalf("unexpected error parsing payload: %v", err)
	}
	if rec.OverallAccuracyScore != 100 {
		t.Errorf("expected score to be clamped to 100, got: %d", rec.OverallAccuracyScore)
	}
	if rec.AnalyzedAt.Format(time.RFC3339) != "2026-11-01T12:00:00Z" {
		t.Errorf("expected parsed analyzed_at, got: %v", rec.AnalyzedAt)
	}
	if rec.AnalysisPayload != raw {
		t.Errorf("expected analysis_payload to default to full raw json")
	}

	// 3. Negative score clamping
	rawNeg := `{"overall_accuracy_score": -10}`
	recNeg, err := ParseVideoAnalysisPayload(rawNeg)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if recNeg.OverallAccuracyScore != 0 {
		t.Errorf("expected score clamped to 0, got %d", recNeg.OverallAccuracyScore)
	}
}

func TestTopKKeys(t *testing.T) {
	// Empty map
	if keys := topKKeys(map[string]int{}, 5); len(keys) != 0 {
		t.Errorf("expected empty keys, got %v", keys)
	}

	// Sorted by count descending, ties alphabetical
	counts := map[string]int{
		"beta":  10,
		"alpha": 10,
		"gamma": 20,
		"delta": 5,
	}

	top2 := topKKeys(counts, 2)
	if len(top2) != 2 || top2[0] != "gamma" || top2[1] != "alpha" {
		t.Errorf("expected [gamma, alpha], got: %v", top2)
	}

	// k larger than map size
	all := topKKeys(counts, 10)
	if len(all) != 4 {
		t.Errorf("expected 4 keys, got %d", len(all))
	}
}

func TestLoadConfigFromEnv(t *testing.T) {
	t.Setenv("CLICKHOUSE_HOST", "test-host")
	t.Setenv("CLICKHOUSE_PORT", "9999")
	t.Setenv("CLICKHOUSE_DB", "custom_db")
	t.Setenv("CLICKHOUSE_USER", "test-user")
	t.Setenv("CLICKHOUSE_PASSWORD", "test-pass")

	cfg := LoadConfigFromEnv()
	if cfg.Host != "test-host" || cfg.Port != "9999" || cfg.Database != "custom_db" ||
		cfg.Username != "test-user" || cfg.Password != "test-pass" {
		t.Errorf("config mismatch: %+v", cfg)
	}
}
