import { spawnSync } from "node:child_process";
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
  const python = process.env.CORTEX_PYTHON_BIN || "python3";
  const runner = [
    "from cortex.scripts import native_host",
    "import cortex.libs.auth",
    `cortex.libs.auth.load_or_create_token = lambda: ${JSON.stringify(EXPECTED_TOKEN)}`,
    "native_host.log = lambda _message: None",
    "native_host.main()",
  ].join("; ");

  const result = spawnSync(python, ["-c", runner], {
    cwd: repositoryRoot,
    env: {
      ...process.env,
      PYTHONPATH: repositoryRoot,
    },
    input: frame(payload),
    maxBuffer: 1024 * 1024,
    timeout: HOST_PROCESS_TIMEOUT_MS,
    killSignal: "SIGKILL",
  });

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
  expect(result.stdout.length).toBeGreaterThanOrEqual(4);
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
