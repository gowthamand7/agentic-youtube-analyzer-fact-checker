package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"

	"github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"
)

// NewSandboxMCPServer builds the MCP server instance for sandbox-mcp.
func NewSandboxMCPServer(runner CodeRunner) *server.MCPServer {
	s := server.NewMCPServer("sandbox-mcp", "1.0.0")

	s.AddTool(mcp.NewTool(
		"execute_python_audit",
		mcp.WithDescription("Execute Python code in an isolated Docker container sandbox and return stdout, stderr, exit code, and execution time."),
		mcp.WithString("code",
			mcp.Required(),
			mcp.Description("Python source code to execute."),
		),
		mcp.WithNumber("timeout_sec",
			mcp.Description("Execution timeout in seconds (1-60, default 10)."),
		),
	), func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		return handleExecutePython(ctx, req, runner)
	})

	return s
}

func main() {
	log.SetOutput(os.Stderr)

	runner, err := NewDockerRunner()
	if err != nil {
		log.Fatalf("failed to initialize docker runner: %v", err)
	}

	// Verify Docker daemon is reachable at startup.
	if pingErr := runner.Ping(context.Background()); pingErr != nil {
		log.Fatalf("Docker daemon unreachable: %v", pingErr)
	}

	s := NewSandboxMCPServer(runner)
	if err := server.ServeStdio(s); err != nil {
		log.Fatalf("server error: %v", err)
	}
}

// handleExecutePython dispatches the execute_python_audit tool call.
func handleExecutePython(ctx context.Context, req mcp.CallToolRequest, runner CodeRunner) (*mcp.CallToolResult, error) {
	code, err := req.RequireString("code")
	if err != nil || code == "" {
		return mcp.NewToolResultError("argument 'code' is required and must be a non-empty string"), nil
	}

	timeoutSec := req.GetInt("timeout_sec", DefaultTimeoutSec)

	result, err := runner.RunPython(ctx, code, timeoutSec)
	if err != nil {
		return mcp.NewToolResultError(fmt.Sprintf("Docker sandbox daemon unavailable: %v", err)), nil
	}

	out, _ := json.Marshal(result)
	return mcp.NewToolResultText(string(out)), nil
}
