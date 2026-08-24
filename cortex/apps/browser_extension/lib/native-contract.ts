/** Runtime decoders for the generated Python native-messaging contract. */

import type {
  DaemonStatusResponse,
  GetAuthTokenResponse,
  LaunchResponse,
  NativeErrorResponse,
  RaiseDashboardResponse,
  StopResponse,
} from "../types/generated/cortex_schemas";

export type NativeHostResponse =
  | LaunchResponse
  | StopResponse
  | DaemonStatusResponse
  | GetAuthTokenResponse
  | RaiseDashboardResponse
  | NativeErrorResponse;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasOnlyKeys(
  value: Record<string, unknown>,
  allowed: readonly string[],
): boolean {
  const allow = new Set(allowed);
  return Object.keys(value).every((key) => allow.has(key));
}

function optionalString(value: unknown, maxLength: number): boolean {
  return (
    value === undefined ||
    value === null ||
    (typeof value === "string" && value.length <= maxLength)
  );
}

/**
 * Decode one response from the Python native host.
 *
 * Generated declarations provide compile-time parity; this function is the
 * runtime boundary that rejects malformed, legacy, or cross-command shapes.
 */
export function decodeNativeHostResponse(value: unknown): NativeHostResponse {
  if (!isRecord(value) || typeof value.command !== "string") {
    throw new Error("invalid_native_host_response");
  }

  switch (value.command) {
    case "launch": {
      if (
        !hasOnlyKeys(value, ["command", "status", "error"]) ||
        !["launched", "already_running", "timeout", "error"].includes(
          String(value.status),
        ) ||
        !optionalString(value.error, 4096)
      ) {
        throw new Error("invalid_launch_response");
      }
      return value as unknown as LaunchResponse;
    }
    case "stop": {
      if (
        !hasOnlyKeys(value, ["command", "status", "error"]) ||
        !["stopped", "error"].includes(String(value.status)) ||
        !optionalString(value.error, 4096)
      ) {
        throw new Error("invalid_stop_response");
      }
      return value as unknown as StopResponse;
    }
    case "status": {
      if (
        !hasOnlyKeys(value, ["command", "status"]) ||
        !["running", "stopped"].includes(String(value.status))
      ) {
        throw new Error("invalid_status_response");
      }
      return value as unknown as DaemonStatusResponse;
    }
    case "get_auth_token": {
      if (
        !hasOnlyKeys(value, ["command", "status", "auth_token"]) ||
        value.status !== "ok" ||
        typeof value.auth_token !== "string" ||
        !/^[0-9a-f]{32,1024}$/.test(value.auth_token)
      ) {
        throw new Error("invalid_auth_token_response");
      }
      return value as unknown as GetAuthTokenResponse;
    }
    case "raise_dashboard": {
      if (
        !hasOnlyKeys(value, ["command", "status", "http_status"]) ||
        value.status !== "ok" ||
        !Number.isInteger(value.http_status) ||
        Number(value.http_status) < 100 ||
        Number(value.http_status) > 599
      ) {
        throw new Error("invalid_raise_dashboard_response");
      }
      return value as unknown as RaiseDashboardResponse;
    }
    case "error": {
      if (
        !hasOnlyKeys(value, [
          "command",
          "status",
          "request_command",
          "error",
          "detail",
        ]) ||
        value.status !== "error" ||
        typeof value.error !== "string" ||
        value.error.length === 0 ||
        value.error.length > 4096 ||
        !optionalString(value.request_command, 64) ||
        !optionalString(value.detail, 8192)
      ) {
        throw new Error("invalid_native_error_response");
      }
      return value as unknown as NativeErrorResponse;
    }
    default:
      throw new Error("unknown_native_host_response");
  }
}

export function decodeGetAuthTokenResponse(
  value: unknown,
): GetAuthTokenResponse {
  const response = decodeNativeHostResponse(value);
  if (response.command === "error") {
    throw new Error(response.error);
  }
  if (response.command !== "get_auth_token") {
    throw new Error("unexpected_native_host_response");
  }
  return response;
}
