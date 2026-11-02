package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
)

const defaultTavilyURL = "https://api.tavily.com"

// TavilyClient queries the Tavily Search REST API.
type TavilyClient struct {
	BaseURL string
	APIKey  string
	HTTP    *http.Client
}

// TavilyResult is a single web evidence entry.
type TavilyResult struct {
	Title   string  `json:"title"`
	URL     string  `json:"url"`
	Content string  `json:"content"`
	Score   float64 `json:"score"`
}

// TavilyResponse is the subset of the Tavily response consumed by the agent.
type TavilyResponse struct {
	Query   string         `json:"query,omitempty"`
	Answer  string         `json:"answer,omitempty"`
	Results []TavilyResult `json:"results"`
}

// Search runs a Tavily query. depth must be "basic" or "advanced".
func (c *TavilyClient) Search(ctx context.Context, query, depth string, domains []string) (*TavilyResponse, error) {
	if c.APIKey == "" {
		return nil, errors.New("TAVILY_API_KEY is not configured")
	}
	base := c.BaseURL
	if base == "" {
		base = defaultTavilyURL
	}

	body := map[string]any{
		"query":          query,
		"search_depth":   depth,
		"include_answer": true,
		"max_results":    5,
	}
	if len(domains) > 0 {
		body["include_domains"] = domains
	}
	payload, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, base+"/search", bytes.NewReader(payload))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+c.APIKey)

	client := c.HTTP
	if client == nil {
		client = http.DefaultClient
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("tavily request failed: %w", err)
	}
	defer resp.Body.Close()

	raw, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("tavily returned status %d: %s", resp.StatusCode, truncate(string(raw), 300))
	}

	var out TavilyResponse
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("invalid tavily response: %w", err)
	}
	if out.Results == nil {
		out.Results = []TavilyResult{}
	}
	return &out, nil
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}
