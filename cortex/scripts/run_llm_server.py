"""
Cortex LLM Server Manager — Start/Verify vLLM on gwhiz1

Connects to the remote GPU server via SSH and manages the vLLM
inference server running Qwen-3-8B. Can start, stop, check status,
and test with a sample request.

Usage:
    python -m cortex.scripts.run_llm_server          # check status
    python -m cortex.scripts.run_llm_server --start   # start vLLM
    python -m cortex.scripts.run_llm_server --test    # test inference
    python -m cortex.scripts.run_llm_server --stop    # stop vLLM
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys

import httpx

from cortex.libs.config.settings import get_config

logger = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cortex LLM server manager",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--start", action="store_true",
        help="Start vLLM server on remote host",
    )
    group.add_argument(
        "--stop", action="store_true",
        help="Stop vLLM server on remote host",
    )
    group.add_argument(
        "--test", action="store_true",
        help="Send a test inference request",
    )
    group.add_argument(
        "--status", action="store_true", default=True,
        help="Check server status (default)",
    )
    parser.add_argument(
        "--local", action="store_true",
        help="Check/manage local Ollama instead of remote vLLM",
    )
    return parser.parse_args()


def _ssh_command(user: str, host: str, cmd: str) -> tuple[int, str, str]:
    """Run a command on the remote host via SSH."""
    result = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", f"{user}@{host}", cmd],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode, result.stdout, result.stderr


async def check_remote_status(
    host: str,
    port: int,
    ssh_user: str,
) -> dict[str, object]:
    """Check if vLLM is running on the remote host."""
    info: dict[str, object] = {
        "host": host,
        "port": port,
        "reachable": False,
        "vllm_running": False,
        "models": [],
    }

    # Check SSH connectivity
    try:
        rc, stdout, stderr = _ssh_command(
            ssh_user, host, "echo ok"
        )
        info["reachable"] = rc == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return info

    if not info["reachable"]:
        return info

    # Check if vLLM process is running
    try:
        rc, stdout, _ = _ssh_command(
            ssh_user, host, "pgrep -f 'vllm.entrypoints' || echo 'not_running'"
        )
        info["vllm_running"] = "not_running" not in stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Try to query models endpoint through tunnel
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"http://localhost:{port}/v1/models")
            if resp.status_code == 200:
                data = resp.json()
                info["models"] = [
                    m.get("id", "unknown")
                    for m in data.get("data", [])
                ]
    except Exception:
        pass

    return info


async def check_local_status(
    host: str, port: int
) -> dict[str, object]:
    """Check if local Ollama is running."""
    info: dict[str, object] = {
        "host": host,
        "port": port,
        "reachable": False,
        "models": [],
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"http://{host}:{port}/api/tags")
            if resp.status_code == 200:
                info["reachable"] = True
                data = resp.json()
                info["models"] = [
                    m.get("name", "unknown")
                    for m in data.get("models", [])
                ]
    except Exception:
        pass

    return info


def start_remote_vllm(
    ssh_user: str,
    host: str,
    port: int,
    model_name: str,
) -> bool:
    """Start vLLM on the remote host."""
    print(f"Starting vLLM on {host} with model {model_name}...")

    vllm_cmd = (
        f"nohup python -m vllm.entrypoints.openai.api_server "
        f"--model {model_name} "
        f"--port {port} "
        f"--max-model-len 4096 "
        f"--gpu-memory-utilization 0.9 "
        f"> /tmp/vllm-cortex.log 2>&1 &"
    )

    try:
        rc, stdout, stderr = _ssh_command(ssh_user, host, vllm_cmd)
        if rc == 0:
            print("vLLM start command sent. Server may take 30-60s to load model.")
            print(f"Check logs on remote: ssh {ssh_user}@{host} tail -f /tmp/vllm-cortex.log")
            return True
        else:
            print(f"Failed to start vLLM: {stderr}")
            return False
    except subprocess.TimeoutExpired:
        print("SSH command timed out")
        return False


def stop_remote_vllm(ssh_user: str, host: str) -> bool:
    """Stop vLLM on the remote host."""
    print(f"Stopping vLLM on {host}...")

    try:
        rc, stdout, stderr = _ssh_command(
            ssh_user, host, "pkill -f 'vllm.entrypoints' || echo 'not_running'"
        )
        if "not_running" in stdout:
            print("vLLM was not running")
        else:
            print("vLLM stopped")
        return True
    except subprocess.TimeoutExpired:
        print("SSH command timed out")
        return False


async def test_inference(
    port: int,
    *,
    local: bool = False,
    model_name: str = "qwen3-8b",
) -> None:
    """Send a test inference request."""
    if local:
        url = f"http://localhost:{port}/api/chat"
        payload = {
            "model": model_name,
            "messages": [
                {"role": "user", "content": "Say hello in one sentence."}
            ],
            "stream": False,
        }
    else:
        url = f"http://localhost:{port}/v1/chat/completions"
        payload = {
            "model": model_name,
            "messages": [
                {"role": "user", "content": "Say hello in one sentence."}
            ],
            "max_tokens": 50,
            "temperature": 0.3,
        }

    print(f"Sending test request to {url}...")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

            if local:
                content = data.get("message", {}).get("content", "")
            else:
                choices = data.get("choices", [])
                content = (
                    choices[0].get("message", {}).get("content", "")
                    if choices
                    else ""
                )

            print(f"Response: {content}")
            print("Test PASSED")

    except httpx.HTTPStatusError as e:
        print(f"HTTP error: {e.response.status_code}")
        print(f"Body: {e.response.text[:500]}")
    except httpx.ConnectError:
        print("Connection failed. Is the SSH tunnel running?")
        print("  Run: ./cortex/scripts/setup_ssh_tunnel.sh")
    except Exception as e:
        print(f"Error: {e}")


async def _async_main(args: argparse.Namespace) -> None:
    config = get_config()

    if args.local:
        local_cfg = config.llm.local
        if args.test:
            await test_inference(
                local_cfg.port,
                local=True,
                model_name=local_cfg.model,
            )
        else:
            info = await check_local_status(local_cfg.host, local_cfg.port)
            _print_status(info, local=True)
        return

    remote_cfg = config.llm.remote

    if args.start:
        start_remote_vllm(
            remote_cfg.ssh_user,
            remote_cfg.host,
            remote_cfg.port,
            config.llm.model_name,
        )
    elif args.stop:
        stop_remote_vllm(remote_cfg.ssh_user, remote_cfg.host)
    elif args.test:
        await test_inference(
            remote_cfg.port,
            model_name=config.llm.model_name,
        )
    else:
        info = await check_remote_status(
            remote_cfg.host,
            remote_cfg.port,
            remote_cfg.ssh_user,
        )
        _print_status(info)


def _print_status(info: dict[str, object], *, local: bool = False) -> None:
    kind = "Local Ollama" if local else "Remote vLLM"
    print(f"=== {kind} Status ===")
    print(f"  Host:      {info['host']}:{info['port']}")

    reachable = info.get("reachable", False)
    status = "REACHABLE" if reachable else "UNREACHABLE"
    print(f"  Status:    {status}")

    if not local and "vllm_running" in info:
        running = info["vllm_running"]
        print(f"  vLLM:      {'RUNNING' if running else 'STOPPED'}")

    models = info.get("models", [])
    if models:
        print(f"  Models:    {', '.join(str(m) for m in models)}")
    else:
        print("  Models:    (none detected)")


def main() -> None:
    """Entry point for run_llm_server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    args = _parse_args()
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
