package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"strings"

	"github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"
)

// Deps bundles the external clients used by the tool handlers.
type Deps struct {
	Tavily *TavilyClient
	Social *SocialSearcher
}

// NewMCPServerInstance builds the verify-social MCP server.
func NewMCPServerInstance(deps Deps) *server.MCPServer {
	s := server.NewMCPServer("verify-social-mcp", "1.0.0")

	s.AddTool(mcp.NewTool("search_web_factcheck",
		mcp.WithDescription("Search the web via Tavily for evidence to verify a factual claim. Returns an LLM-parsed answer plus ranked sources with URLs."),
		mcp.WithString("query", mcp.Required(), mcp.Description("Fact-checking search query.")),
		mcp.WithString("search_depth", mcp.Description("'basic' (1 credit, default) or 'advanced' (2 credits).")),
		mcp.WithArray("domains", mcp.WithStringItems(), mcp.Description("Optional domains to restrict the search to, e.g. ['reddit.com'].")),
	), func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		query, _ := req.RequireString("query")
		if strings.TrimSpace(query) == "" {
			return mcp.NewToolResultError("query parameter is required and cannot be empty"), nil
		}
		depth := req.GetString("search_depth", "basic")
		if depth != "basic" && depth != "advanced" {
			return mcp.NewToolResultError("search_depth must be 'basic' or 'advanced'"), nil
		}
		if deps.Tavily == nil {
			return mcp.NewToolResultError("Tavily client is not configured"), nil
		}
		res, err := deps.Tavily.Search(ctx, query, depth, req.GetStringSlice("domains", nil))
		if err != nil {
			return mcp.NewToolResultError(fmt.Sprintf("web search failed: %v", err)), nil
		}
		return jsonResult(res), nil
	})

	s.AddTool(mcp.NewTool("search_social_reactions",
		mcp.WithDescription("Search Hacker News, Bluesky, Reddit and Stack Overflow concurrently for community discussion about a topic. Platforms that fail are listed in 'errors' without failing the call."),
		mcp.WithString("topic_query", mcp.Required(), mcp.Description("Topic or claim to look up.")),
		mcp.WithArray("platforms", mcp.WithStringItems(), mcp.Description("Subset of: hackernews, bluesky, reddit, stackexchange. Defaults to all.")),
	), func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		query, _ := req.RequireString("topic_query")
		if strings.TrimSpace(query) == "" {
			return mcp.NewToolResultError("topic_query parameter is required and cannot be empty"), nil
		}
		if deps.Social == nil {
			return mcp.NewToolResultError("social searcher is not configured"), nil
		}
		platforms := req.GetStringSlice("platforms", nil)
		if len(platforms) == 0 {
			platforms = SupportedPlatforms
		}
		return jsonResult(deps.Social.Search(ctx, query, platforms)), nil
	})

	return s
}

func jsonResult(v any) *mcp.CallToolResult {
	data, err := json.Marshal(v)
	if err != nil {
		return mcp.NewToolResultError(fmt.Sprintf("failed to serialize result: %v", err))
	}
	return mcp.NewToolResultText(string(data))
}

func main() {
	log.SetOutput(os.Stderr)

	deps := Deps{
		Social: NewSocialSearcher(os.Getenv("REDDIT_CLIENT_ID"), os.Getenv("REDDIT_CLIENT_SECRET"), os.Getenv("REDDIT_USER_AGENT")),
	}
	if key := os.Getenv("TAVILY_API_KEY"); key != "" {
		deps.Tavily = &TavilyClient{APIKey: key}
		deps.Social.Tavily = deps.Tavily
	} else {
		log.Println("[WARN] TAVILY_API_KEY not set; search_web_factcheck will return errors")
	}

	if err := server.ServeStdio(NewMCPServerInstance(deps)); err != nil {
		log.Fatalf("server error: %v", err)
	}
}
