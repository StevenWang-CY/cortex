import { spawnSync, type SpawnSyncReturns } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import {
  decodeGetAuthTokenResponse,
  decodeNativeHostResponse,
} from "../lib/native-contract";

const EXPECTED_TOKEN = "ab".repeat(32);
const HOST_PROCESS_TIMEOUT_MS = 15_000;
const CONTRACT_TEST_TIMEOUT_MS = HOST_PROCESS_TIMEOUT_MS + 5_000;

function frame(payload: Record<string, unknown>): Buffer {
  const body = Buffer.from(JSON.stringify(payload), "utf8");
  const prefix = Buffer.alloc(4);
  prefix.writeUInt32LE(body.length, 0);
  return Buffer.concat([prefix, body]);
}

function runActualPythonHost(payload: Record<string, unknown>): unknown {
  const repositoryRoot = resolve(process.cwd(), "../../..");
  const projectPython = resolve(repositoryRoot, "cortex/.venv/bin/python");
  const python = process.env.CORTEX_PYTHON_BIN || (existsSync(projectPython) ? projectPython : "python3");
  const profile = mkdtempSync(resolve(tmpdir(), "cortex-native-contract-"));
  const runner = [
    "from cortex.scripts import native_host",
    "import cortex.libs.auth",
    `cortex.libs.auth.load_or_create_token = lambda: ${JSON.stringify(EXPECTED_TOKEN)}`,
    "native_host.log = lambda _message: None",
    "native_host.is_daemon_running = lambda: False",
    "native_host.main()",
  ].join("; ");

  let result: SpawnSyncReturns<Buffer>;
  try {
    result = spawnSync(python, ["-c", runner], {
      cwd: repositoryRoot,
      env: {
        ...process.env,
        PYTHONPATH: repositoryRoot,
        HOME: profile,
        XDG_CONFIG_HOME: resolve(profile, ".config"),
        XDG_DATA_HOME: resolve(profile, ".local/share"),
        XDG_STATE_HOME: resolve(profile, ".local/state"),
        XDG_CACHE_HOME: resolve(profile, ".cache"),
      },
      input: frame(payload),
      maxBuffer: 1024 * 1024,
      timeout: HOST_PROCESS_TIMEOUT_MS,
      killSignal: "SIGKILL",
    });
  } finally {
    rmSync(profile, { recursive: true, force: true });
  }

  if (result.error) {
    throw new Error(
      `native host process failed within ${HOST_PROCESS_TIMEOUT_MS}ms: ${result.error.message}`,
    );
  }
  if (result.status !== 0) {
    throw new Error(
      `native host exited ${String(result.status)}: ${result.stderr.toString("utf8")}`,
    );
  }
  if (result.stdout.length < 4) {
    throw new Error(`native host returned no frame; run make setup or set CORTEX_PYTHON_BIN. ${result.stderr.toString("utf8")}`);
  }
  const responseLength = result.stdout.readUInt32LE(0);
  const body = result.stdout.subarray(4);
  expect(body.length).toBe(responseLength);
  return JSON.parse(body.toString("utf8")) as unknown;
}

describe("native host Python↔TypeScript contract", () => {
  it(
    "decodes the actual framed get_auth_token response",
    () => {
      const raw = runActualPythonHost({ command: "get_auth_token" });
      const response = decodeGetAuthTokenResponse(raw);

      expect(response.command).toBe("get_auth_token");
      expect(response.status).toBe("ok");
      expect(response.auth_token).toBe(EXPECTED_TOKEN);
    },
    CONTRACT_TEST_TIMEOUT_MS,
  );

  it("rejects the legacy Python token field", () => {
    expect(() =>
      decodeGetAuthTokenResponse({
        command: "get_auth_token",
        status: "ok",
        token: EXPECTED_TOKEN,
      }),
    ).toThrow("invalid_auth_token_response");
  });

  it(
    "decodes a framed status response from the actual host",
    () => {
      const raw = runActualPythonHost({ command: "status" });
      const response = decodeNativeHostResponse(raw);

      expect(response.command).toBe("status");
      expect(["running", "stopped"]).toContain(response.status);
    },
    CONTRACT_TEST_TIMEOUT_MS,
  );
});
