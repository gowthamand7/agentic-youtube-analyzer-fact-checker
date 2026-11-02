package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/client"
	"github.com/docker/docker/pkg/stdcopy"
)

const (
	DefaultPythonImage = "python:3.12-slim"
	DefaultTimeoutSec  = 10
	MinTimeoutSec      = 1
	MaxTimeoutSec      = 60
	MemoryLimitBytes   = 268435456 // 256MB
	NanoCPUsLimit      = 500000000 // 0.5 CPU
	TmpfsMountSpec     = "rw,noexec,nosuid,size=32m"
)

// AuditResult captures the results of running code inside the sandbox.
type AuditResult struct {
	Stdout          string `json:"stdout"`
	Stderr          string `json:"stderr"`
	ExitCode        int    `json:"exit_code"`
	ExecutionTimeMs int64  `json:"execution_time_ms"`
	TimedOut        bool   `json:"timed_out"`
}

// CodeRunner abstracts execution of Python code in a sandbox.
type CodeRunner interface {
	RunPython(ctx context.Context, code string, timeoutSec int) (*AuditResult, error)
	Ping(ctx context.Context) error
}

// DockerRunner executes Python code inside an isolated Docker container.
type DockerRunner struct {
	client *client.Client
	image  string
}

// NewDockerRunner creates a new DockerRunner with API version negotiation.
func NewDockerRunner() (*DockerRunner, error) {
	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		return nil, fmt.Errorf("failed to create docker client: %w", err)
	}
	return &DockerRunner{
		client: cli,
		image:  DefaultPythonImage,
	}, nil
}

// NewDockerRunnerWithClient creates a DockerRunner with an existing client.
func NewDockerRunnerWithClient(cli *client.Client, image string) *DockerRunner {
	if image == "" {
		image = DefaultPythonImage
	}
	return &DockerRunner{
		client: cli,
		image:  image,
	}
}

// Ping checks whether the Docker daemon is reachable.
func (d *DockerRunner) Ping(ctx context.Context) error {
	if d.client == nil {
		return errors.New("docker client is nil")
	}
	_, err := d.client.Ping(ctx)
	return err
}

// ClampTimeout bounds timeoutSec between MinTimeoutSec and MaxTimeoutSec.
func ClampTimeout(timeoutSec int) int {
	if timeoutSec <= 0 {
		return DefaultTimeoutSec
	}
	if timeoutSec < MinTimeoutSec {
		return MinTimeoutSec
	}
	if timeoutSec > MaxTimeoutSec {
		return MaxTimeoutSec
	}
	return timeoutSec
}

// RunPython runs python code in an isolated container according to security constraints.
func (d *DockerRunner) RunPython(ctx context.Context, code string, timeoutSec int) (*AuditResult, error) {
	if d.client == nil {
		return nil, errors.New("docker client is nil")
	}

	timeoutSec = ClampTimeout(timeoutSec)
	execCtx, cancel := context.WithTimeout(ctx, time.Duration(timeoutSec)*time.Second)
	defer cancel()

	startTime := time.Now()

	containerConfig := &container.Config{
		Image:        d.image,
		Cmd:          []string{"python", "-c", code},
		Tty:          false,
		AttachStdout: true,
		AttachStderr: true,
	}

	hostConfig := &container.HostConfig{
		NetworkMode: container.NetworkMode("none"),
		Resources: container.Resources{
			Memory:   MemoryLimitBytes,
			NanoCPUs: NanoCPUsLimit,
		},
		ReadonlyRootfs: true,
		Tmpfs: map[string]string{
			"/tmp": TmpfsMountSpec,
		},
		AutoRemove: true,
	}

	networkingConfig := &network.NetworkingConfig{}

	resp, err := d.client.ContainerCreate(execCtx, containerConfig, hostConfig, networkingConfig, nil, "")
	if err != nil {
		return nil, fmt.Errorf("failed to create container: %w", err)
	}
	containerID := resp.ID

	// If AutoRemove is true, when the container stops, Docker daemon cleans it up.
	// But if execution times out or context is cancelled, we ensure the container is killed/removed.
	defer func() {
		cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cleanupCancel()
		_ = d.client.ContainerKill(cleanupCtx, containerID, "SIGKILL")
		_ = d.client.ContainerRemove(cleanupCtx, containerID, container.RemoveOptions{Force: true})
	}()

	attachResp, err := d.client.ContainerAttach(execCtx, containerID, container.AttachOptions{
		Stream: true,
		Stdout: true,
		Stderr: true,
	})
	if err != nil {
		return nil, fmt.Errorf("failed to attach to container: %w", err)
	}
	defer attachResp.Close()

	if err := d.client.ContainerStart(execCtx, containerID, container.StartOptions{}); err != nil {
		return nil, fmt.Errorf("failed to start container: %w", err)
	}

	var stdoutBuf, stderrBuf bytes.Buffer
	copyDone := make(chan error, 1)
	go func() {
		_, copyErr := stdcopy.StdCopy(&stdoutBuf, &stderrBuf, attachResp.Reader)
		copyDone <- copyErr
	}()

	statusCh, errCh := d.client.ContainerWait(execCtx, containerID, container.WaitConditionNotRunning)

	var exitCode int
	var timedOut bool

	select {
	case <-execCtx.Done():
		timedOut = true
		exitCode = 124
		_ = d.client.ContainerKill(context.Background(), containerID, "SIGKILL")
	case waitErr := <-errCh:
		if waitErr != nil {
			// Check if context expired
			if errors.Is(execCtx.Err(), context.DeadlineExceeded) || errors.Is(execCtx.Err(), context.Canceled) {
				timedOut = true
				exitCode = 124
			} else {
				return nil, fmt.Errorf("container wait error: %w", waitErr)
			}
		}
	case waitResp := <-statusCh:
		exitCode = int(waitResp.StatusCode)
	}

	// Wait briefly for log copying to complete
	select {
	case <-copyDone:
	case <-time.After(2 * time.Second):
	}

	duration := time.Since(startTime).Milliseconds()

	return &AuditResult{
		Stdout:          stdoutBuf.String(),
		Stderr:          stderrBuf.String(),
		ExitCode:        exitCode,
		ExecutionTimeMs: duration,
		TimedOut:        timedOut,
	}, nil
}
