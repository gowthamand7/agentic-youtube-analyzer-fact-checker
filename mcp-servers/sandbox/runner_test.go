package main

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	"github.com/mark3labs/mcp-go/mcp"
)

// MockRunner implements CodeRunner for unit testing without requiring Docker.
type MockRunner struct {
	result *AuditResult
	err    error
}

func (m *MockRunner) RunPython(_ context.Context, _ string, _ int) (*AuditResult, error) {
	return m.result, m.err
}

func (m *MockRunner) Ping(_ context.Context) error {
	return nil
}

// makeCallToolRequest builds a mcp.CallToolRequest with the given arguments.
func makeCallToolRequest(args map[string]any) mcp.CallToolRequest {
	req := mcp.CallToolRequest{}
	req.Params.Name = "execute_python_audit"
	req.Params.Arguments = args
	return req
}

// TestClampTimeout verifies the timeout clamping logic.
func TestClampTimeout(t *testing.T) {
	tests := []struct {
		in   int
		want int
	}{
		{0, DefaultTimeoutSec},
		{-1, DefaultTimeoutSec},
		{1, 1},
		{10, 10},
		{60, 60},
		{61, MaxTimeoutSec},
		{100, MaxTimeoutSec},
	}
	for _, tc := range tests {
		got := ClampTimeout(tc.in)
		if got != tc.want {
			t.Errorf("ClampTimeout(%d) = %d, want %d", tc.in, got, tc.want)
		}
	}
}

// TestHandleExecutePythonMissingCode verifies that missing 'code' returns an error result.
func TestHandleExecutePythonMissingCode(t *testing.T) {
	runner := &MockRunner{result: &AuditResult{Stdout: "ok", ExitCode: 0}}
	req := makeCallToolRequest(map[string]any{})
	result, err := handleExecutePython(context.Background(), req, runner)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !result.IsError {
		t.Errorf("expected error result for missing code, got: %+v", result)
	}
}

// TestHandleExecutePythonEmptyCode verifies that empty 'code' returns an error result.
func TestHandleExecutePythonEmptyCode(t *testing.T) {
	runner := &MockRunner{result: &AuditResult{}}
	req := makeCallToolRequest(map[string]any{"code": ""})
	result, err := handleExecutePython(context.Background(), req, runner)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !result.IsError {
		t.Errorf("expected error result for empty code, got: %+v", result)
	}
}

// TestHandleExecutePythonSuccess verifies the happy path with a mock runner.
func TestHandleExecutePythonSuccess(t *testing.T) {
	expected := &AuditResult{
		Stdout:          "hello\n",
		Stderr:          "",
		ExitCode:        0,
		ExecutionTimeMs: 42,
		TimedOut:        false,
	}
	runner := &MockRunner{result: expected}
	req := makeCallToolRequest(map[string]any{"code": "print('hello')"})
	result, err := handleExecutePython(context.Background(), req, runner)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.IsError {
		t.Fatalf("expected success result but got error: %+v", result)
	}

	// Parse the JSON text content back.
	if len(result.Content) == 0 {
		t.Fatal("expected non-empty content")
	}
	textContent, ok := result.Content[0].(mcp.TextContent)
	if !ok {
		t.Fatalf("expected TextContent, got %T", result.Content[0])
	}
	var got AuditResult
	if err := json.Unmarshal([]byte(textContent.Text), &got); err != nil {
		t.Fatalf("failed to parse result JSON: %v", err)
	}
	if got.Stdout != expected.Stdout {
		t.Errorf("stdout: got %q, want %q", got.Stdout, expected.Stdout)
	}
	if got.ExitCode != expected.ExitCode {
		t.Errorf("exit_code: got %d, want %d", got.ExitCode, expected.ExitCode)
	}
}

// SpyRunner records arguments passed to RunPython.
type SpyRunner struct {
	result *AuditResult
	onRun  func(ctx, code string, timeoutSec int)
}

func (s *SpyRunner) RunPython(_ context.Context, code string, timeoutSec int) (*AuditResult, error) {
	if s.onRun != nil {
		s.onRun("", code, timeoutSec)
	}
	return s.result, nil
}

func (s *SpyRunner) Ping(_ context.Context) error { return nil }

// TestHandleExecutePythonCustomTimeout verifies timeout_sec parsing.
func TestHandleExecutePythonCustomTimeout(t *testing.T) {
	var calledWith int
	runner := &SpyRunner{
		result: &AuditResult{ExitCode: 0},
		onRun: func(_, code string, tm int) {
			calledWith = tm
		},
	}
	req := makeCallToolRequest(map[string]any{"code": "pass", "timeout_sec": 30})
	_, err := handleExecutePython(context.Background(), req, runner)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if calledWith != 30 {
		t.Errorf("expected timeout_sec=30, runner got %d", calledWith)
	}
}

// TestHandleExecutePythonRunnerError verifies that runner errors return isError=true.
func TestHandleExecutePythonRunnerError(t *testing.T) {
	runner := &MockRunner{err: errors.New("docker unavailable")}
	req := makeCallToolRequest(map[string]any{"code": "print(1)"})
	result, err := handleExecutePython(context.Background(), req, runner)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !result.IsError {
		t.Errorf("expected error result when runner fails, got: %+v", result)
	}
}

// TestToolRegistration verifies execute_python_audit is registered on MCPServer.
func TestToolRegistration(t *testing.T) {
	runner := &MockRunner{}
	s := NewSandboxMCPServer(runner)
	tools := s.ListTools()
	tool, exists := tools["execute_python_audit"]
	if !exists || tool == nil {
		t.Fatal("expected execute_python_audit tool to be registered")
	}
}

// TestLiveDockerExecution runs actual Python code inside Docker.
// Requires Docker daemon to be running and python:3.12-slim image to be available.
func TestLiveDockerExecution(t *testing.T) {
	runner, err := NewDockerRunner()
	if err != nil {
		t.Skipf("skipping live docker test: failed to create runner: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	if pingErr := runner.Ping(ctx); pingErr != nil {
		t.Skipf("skipping live docker test: daemon not reachable: %v", pingErr)
	}

	result, err := runner.RunPython(ctx, "print(2 + 2)", 10)
	if err != nil {
		t.Fatalf("RunPython failed: %v", err)
	}
	if result.ExitCode != 0 {
		t.Errorf("expected exit code 0, got %d. stderr: %s", result.ExitCode, result.Stderr)
	}
	expected := "4\n"
	if result.Stdout != expected {
		t.Errorf("expected stdout %q, got %q", expected, result.Stdout)
	}
	if result.TimedOut {
		t.Error("expected not timed out")
	}
}

// TestLiveDockerTimeout tests that code exceeding timeout is killed.
func TestLiveDockerTimeout(t *testing.T) {
	runner, err := NewDockerRunner()
	if err != nil {
		t.Skipf("skipping live docker test: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	if pingErr := runner.Ping(ctx); pingErr != nil {
		t.Skipf("skipping live docker test: daemon not reachable: %v", pingErr)
	}

	result, err := runner.RunPython(ctx, "import time; time.sleep(30)", 2)
	if err != nil {
		t.Fatalf("RunPython failed: %v", err)
	}
	if !result.TimedOut {
		t.Errorf("expected timed_out=true, got false. exit_code=%d", result.ExitCode)
	}
}
