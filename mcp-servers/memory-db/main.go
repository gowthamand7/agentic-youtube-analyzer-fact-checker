package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"strings"
	"time"

	"github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"
)

// ParseVideoAnalysisPayload extracts fields and prepares a VideoAnalysisRecord from JSON input.
func ParseVideoAnalysisPayload(raw string) (*VideoAnalysisRecord, error) {
	if strings.TrimSpace(raw) == "" {
		return nil, errors.New("empty payload")
	}

	type rawRecord struct {
		ChannelID            string          `json:"channel_id"`
		ChannelName          string          `json:"channel_name"`
		VideoID              string          `json:"video_id"`
		VideoTitle           string          `json:"video_title"`
		OverallAccuracyScore *float64        `json:"overall_accuracy_score"`
		HasPaidPromotion     *bool           `json:"has_paid_promotion"`
		DisclosedProperly    *bool           `json:"disclosed_properly"`
		Sponsors             []string        `json:"sponsors"`
		Collaborators        []string        `json:"collaborators"`
		TopicTags            []string        `json:"topic_tags"`
		AnalysisPayload      json.RawMessage `json:"analysis_payload"`
		AnalyzedAt           string          `json:"analyzed_at"`
	}

	var rawData rawRecord
	if err := json.Unmarshal([]byte(raw), &rawData); err != nil {
		return nil, fmt.Errorf("failed to parse json payload: %w", err)
	}

	record := &VideoAnalysisRecord{
		ChannelID:     rawData.ChannelID,
		ChannelName:   rawData.ChannelName,
		VideoID:       rawData.VideoID,
		VideoTitle:    rawData.VideoTitle,
		Sponsors:      rawData.Sponsors,
		Collaborators: rawData.Collaborators,
		TopicTags:     rawData.TopicTags,
	}

	if rawData.OverallAccuracyScore != nil {
		score := *rawData.OverallAccuracyScore
		if score < 0 {
			score = 0
		} else if score > 100 {
			score = 100
		}
		record.OverallAccuracyScore = uint8(score)
	}

	if rawData.HasPaidPromotion != nil {
		record.HasPaidPromotion = *rawData.HasPaidPromotion
	}

	if rawData.DisclosedProperly != nil {
		record.DisclosedProperly = *rawData.DisclosedProperly
	}

	if len(rawData.AnalysisPayload) > 0 && string(rawData.AnalysisPayload) != "null" {
		record.AnalysisPayload = string(rawData.AnalysisPayload)
	} else {
		record.AnalysisPayload = raw
	}

	if rawData.AnalyzedAt != "" {
		if t, err := time.Parse(time.RFC3339, rawData.AnalyzedAt); err == nil {
			record.AnalyzedAt = t.UTC()
		} else {
			record.AnalyzedAt = time.Now().UTC()
		}
	} else {
		record.AnalyzedAt = time.Now().UTC()
	}

	return record, nil
}

// NewMCPServerInstance configures the MCP server and registers all memory-db tools.
func NewMCPServerInstance(db DBService) *server.MCPServer {
	s := server.NewMCPServer("memory-db-mcp", "1.0.0")

	// 1. Tool: get_channel_history
	toolGetChannelHistory := mcp.NewTool("get_channel_history",
		mcp.WithDescription("Retrieves aggregated creator history including total analyzed videos, average accuracy score, sponsored video counts, frequent sponsors, and previous evaluation runs."),
		mcp.WithString("channel_id",
			mcp.Required(),
			mcp.Description("The YouTube channel ID (e.g., 'UCxxxxxxxx')"),
		),
	)

	s.AddTool(toolGetChannelHistory, func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		if db == nil {
			return mcp.NewToolResultError("ClickHouse database service is currently offline or unavailable"), nil
		}

		channelID, err := req.RequireString("channel_id")
		if err != nil || strings.TrimSpace(channelID) == "" {
			return mcp.NewToolResultError("channel_id parameter is required and cannot be empty"), nil
		}

		history, err := db.GetChannelHistory(ctx, channelID)
		if err != nil {
			return mcp.NewToolResultError(fmt.Sprintf("database error fetching channel history: %v", err)), nil
		}

		data, err := json.Marshal(history)
		if err != nil {
			return mcp.NewToolResultError(fmt.Sprintf("failed to serialize channel history: %v", err)), nil
		}

		return mcp.NewToolResultText(string(data)), nil
	})

	// 2. Tool: save_video_analysis
	toolSaveVideoAnalysis := mcp.NewTool("save_video_analysis",
		mcp.WithDescription("Stores an audited video evaluation into ClickHouse memory database for longitudinal creator tracking."),
		mcp.WithString("payload_json",
			mcp.Required(),
			mcp.Description("Full JSON payload containing video metadata, overall accuracy score, sponsors, topic tags, claims verified, and evaluation details."),
		),
	)

	s.AddTool(toolSaveVideoAnalysis, func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		if db == nil {
			return mcp.NewToolResultError("ClickHouse database service is currently offline or unavailable"), nil
		}

		payloadJSON, err := req.RequireString("payload_json")
		if err != nil || strings.TrimSpace(payloadJSON) == "" {
			return mcp.NewToolResultError("payload_json parameter is required and cannot be empty"), nil
		}

		record, err := ParseVideoAnalysisPayload(payloadJSON)
		if err != nil {
			return mcp.NewToolResultError(fmt.Sprintf("invalid payload_json: %v", err)), nil
		}

		if err := db.SaveVideoAnalysis(ctx, record); err != nil {
			return mcp.NewToolResultError(fmt.Sprintf("database error saving video analysis: %v", err)), nil
		}

		resp := map[string]any{
			"status":     "success",
			"channel_id": record.ChannelID,
			"video_id":   record.VideoID,
			"message":    "video analysis recorded successfully",
		}
		data, _ := json.Marshal(resp)
		return mcp.NewToolResultText(string(data)), nil
	})

	// 3. Tool: query_historical_claims
	toolQueryHistoricalClaims := mcp.NewTool("query_historical_claims",
		mcp.WithDescription("Searches past evaluated videos for previously audited claims matching a topic tag to avoid duplicate web searches."),
		mcp.WithString("topic_tag",
			mcp.Required(),
			mcp.Description("Topic tag to filter historical claims (e.g., 'python', 'ai', 'crypto')"),
		),
		mcp.WithInteger("limit",
			mcp.Description("Maximum number of claims to retrieve (default: 10)"),
		),
	)

	s.AddTool(toolQueryHistoricalClaims, func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		if db == nil {
			return mcp.NewToolResultError("ClickHouse database service is currently offline or unavailable"), nil
		}

		topicTag, err := req.RequireString("topic_tag")
		if err != nil || strings.TrimSpace(topicTag) == "" {
			return mcp.NewToolResultError("topic_tag parameter is required and cannot be empty"), nil
		}

		limit := req.GetInt("limit", 10)
		if limit <= 0 {
			limit = 10
		}

		claims, err := db.QueryHistoricalClaims(ctx, topicTag, limit)
		if err != nil {
			return mcp.NewToolResultError(fmt.Sprintf("database error querying historical claims: %v", err)), nil
		}

		data, err := json.Marshal(claims)
		if err != nil {
			return mcp.NewToolResultError(fmt.Sprintf("failed to serialize historical claims: %v", err)), nil
		}

		return mcp.NewToolResultText(string(data)), nil
	})

	return s
}

func main() {
	cfg := LoadConfigFromEnv()

	var db DBService
	chDB, err := NewClickHouseDB(cfg)
	if err != nil {
		log.Printf("[WARN] Failed to initialize ClickHouse connection pool: %v. Running in degraded mode.", err)
	} else {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		if err := chDB.Ping(ctx); err != nil {
			log.Printf("[WARN] ClickHouse ping failed: %v. Database will operate with fallback error handling.", err)
			chDB.Close()
		} else {
			if err := chDB.InitSchema(ctx); err != nil {
				log.Printf("[WARN] ClickHouse schema init failed: %v", err)
			}
			db = chDB
			defer chDB.Close()
		}
		cancel()
	}

	mcpServer := NewMCPServerInstance(db)

	if err := server.ServeStdio(mcpServer); err != nil {
		log.Fatalf("Fatal error serving stdio: %v", err)
		os.Exit(1)
	}
}
