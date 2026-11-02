package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/mark3labs/mcp-go/mcp"
)

func newTestSocial(t *testing.T, mux *http.ServeMux) *SocialSearcher {
	t.Helper()
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return &SocialSearcher{
		HTTP:          srv.Client(),
		HNBaseURL:     srv.URL + "/hn",
		BlueskyURL:    srv.URL + "/bsky",
		RedditURL:     srv.URL + "/reddit",
		RedditAuthURL: srv.URL + "/reddit-auth",
		StackURL:      srv.URL + "/stack",
		UserAgent:     "test-agent",
	}
}

func TestTavilySearchRequestAndParse(t *testing.T) {
	var got map[string]any
	mux := http.NewServeMux()
	mux.HandleFunc("/search", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer tvly-key" {
			t.Errorf("missing bearer auth, got %q", r.Header.Get("Authorization"))
		}
		_ = json.NewDecoder(r.Body).Decode(&got)
		_, _ = w.Write([]byte(`{"answer":"yes","results":[{"title":"T","url":"https://a.com","content":"c","score":0.9}]}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	c := &TavilyClient{BaseURL: srv.URL, APIKey: "tvly-key", HTTP: srv.Client()}
	res, err := c.Search(context.Background(), "q", "advanced", []string{"reddit.com"})
	if err != nil {
		t.Fatal(err)
	}
	if got["query"] != "q" || got["search_depth"] != "advanced" || got["include_answer"] != true {
		t.Errorf("bad request body: %v", got)
	}
	if d, _ := got["include_domains"].([]any); len(d) != 1 || d[0] != "reddit.com" {
		t.Errorf("domains not forwarded: %v", got["include_domains"])
	}
	if res.Answer != "yes" || len(res.Results) != 1 || res.Results[0].URL != "https://a.com" {
		t.Errorf("bad parse: %+v", res)
	}
}

func TestTavilyErrorStatus(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, `{"detail":"bad key"}`, http.StatusUnauthorized)
	}))
	defer srv.Close()
	c := &TavilyClient{BaseURL: srv.URL, APIKey: "x", HTTP: srv.Client()}
	if _, err := c.Search(context.Background(), "q", "basic", nil); err == nil || !strings.Contains(err.Error(), "401") {
		t.Errorf("expected 401 error, got %v", err)
	}
}

func TestTavilyMissingKey(t *testing.T) {
	c := &TavilyClient{}
	if _, err := c.Search(context.Background(), "q", "basic", nil); err == nil {
		t.Error("expected error for missing API key")
	}
}

func TestSocialFanOutMergesAndDegrades(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/hn/search", func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"hits":[{"title":"HN story","url":"https://x.com","points":50,"num_comments":10,"objectID":"1","author":"a"}]}`))
	})
	mux.HandleFunc("/bsky/xrpc/app.bsky.feed.searchPosts", func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"posts":[{"uri":"at://did:plc:abc/app.bsky.feed.post/3k","author":{"handle":"u.bsky.social"},"record":{"text":"hello"},"likeCount":3}]}`))
	})
	mux.HandleFunc("/reddit/search.json", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("User-Agent") != "test-agent" {
			t.Errorf("reddit UA missing")
		}
		_, _ = w.Write([]byte(`{"data":{"children":[{"data":{"title":"R post","permalink":"/r/x/comments/1/","subreddit":"x","score":9,"num_comments":2,"selftext":"body"}}]}}`))
	})
	mux.HandleFunc("/stack/search/advanced", func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	})
	s := newTestSocial(t, mux)

	res := s.Search(context.Background(), "topic", []string{"hackernews", "bluesky", "reddit", "stackexchange"})
	if len(res.Results) != 3 {
		t.Fatalf("want 3 merged results, got %d: %+v", len(res.Results), res.Results)
	}
	if _, bad := res.Errors["stackexchange"]; !bad || len(res.Errors) != 1 {
		t.Errorf("want only stackexchange error, got %v", res.Errors)
	}
	byPlatform := map[string]SocialItem{}
	for _, it := range res.Results {
		byPlatform[it.Platform] = it
	}
	if byPlatform["bluesky"].URL != "https://bsky.app/profile/u.bsky.social/post/3k" {
		t.Errorf("bad bluesky url: %q", byPlatform["bluesky"].URL)
	}
	if byPlatform["reddit"].URL != "https://www.reddit.com/r/x/comments/1/" {
		t.Errorf("bad reddit url: %q", byPlatform["reddit"].URL)
	}
	if byPlatform["hackernews"].URL != "https://x.com" {
		t.Errorf("bad hn url: %q", byPlatform["hackernews"].URL)
	}
}

func TestSocialUnknownPlatform(t *testing.T) {
	s := newTestSocial(t, http.NewServeMux())
	res := s.Search(context.Background(), "t", []string{"myspace"})
	if _, ok := res.Errors["myspace"]; !ok {
		t.Errorf("expected unsupported platform error, got %v", res.Errors)
	}
}

func TestRedditOAuthWhenConfigured(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/reddit-auth/api/v1/access_token", func(w http.ResponseWriter, r *http.Request) {
		u, p, ok := r.BasicAuth()
		if !ok || u != "id" || p != "secret" {
			t.Errorf("bad basic auth")
		}
		_, _ = w.Write([]byte(`{"access_token":"tok","expires_in":3600}`))
	})
	mux.HandleFunc("/reddit/search", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "bearer tok" {
			t.Errorf("expected bearer token, got %q", r.Header.Get("Authorization"))
		}
		_, _ = w.Write([]byte(`{"data":{"children":[{"data":{"title":"auth post","permalink":"/r/y/","subreddit":"y"}}]}}`))
	})
	s := newTestSocial(t, mux)
	s.RedditClientID, s.RedditClientSecret = "id", "secret"
	res := s.Search(context.Background(), "t", []string{"reddit"})
	if len(res.Results) != 1 || res.Results[0].Title != "auth post" {
		t.Errorf("unexpected: %+v", res)
	}
}

func callTool(t *testing.T, name string, args map[string]any, deps Deps) *mcp.CallToolResult {
	t.Helper()
	s := NewMCPServerInstance(deps)
	tool := s.GetTool(name)
	if tool == nil {
		t.Fatalf("tool %s not registered", name)
	}
	req := mcp.CallToolRequest{}
	req.Params.Name = name
	req.Params.Arguments = args
	res, err := tool.Handler(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	return res
}

func TestToolRegistration(t *testing.T) {
	s := NewMCPServerInstance(Deps{})
	for _, n := range []string{"search_web_factcheck", "search_social_reactions"} {
		if s.GetTool(n) == nil {
			t.Errorf("tool %s missing", n)
		}
	}
}

func TestSearchWebFactcheckHandler(t *testing.T) {
	if res := callTool(t, "search_web_factcheck", map[string]any{"query": ""}, Deps{}); !res.IsError {
		t.Error("empty query must be an error")
	}
	if res := callTool(t, "search_web_factcheck", map[string]any{"query": "x"}, Deps{}); !res.IsError {
		t.Error("missing tavily client must be an error")
	}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"results":[]}`))
	}))
	defer srv.Close()
	tv := &TavilyClient{BaseURL: srv.URL, APIKey: "k", HTTP: srv.Client()}
	if res := callTool(t, "search_web_factcheck", map[string]any{"query": "x", "search_depth": "weird"}, Deps{Tavily: tv}); !res.IsError {
		t.Error("invalid depth must be an error")
	}
	if res := callTool(t, "search_web_factcheck", map[string]any{"query": "x", "domains": []any{"a.com"}}, Deps{Tavily: tv}); res.IsError {
		t.Errorf("unexpected error: %+v", res.Content)
	}
}

func TestSearchSocialHandlerDefaultsPlatforms(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/hn/search", func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte(`{"hits":[]}`)) })
	s := newTestSocial(t, mux)
	if res := callTool(t, "search_social_reactions", map[string]any{"topic_query": "x"}, Deps{Social: s}); res.IsError {
		t.Fatalf("unexpected error: %+v", res.Content)
	}
	if res := callTool(t, "search_social_reactions", map[string]any{"topic_query": " "}, Deps{Social: s}); !res.IsError {
		t.Error("blank query must be an error")
	}
}
