/** Reliable, runtime-validated access to the Cortex native messaging host. */

import { NATIVE_HOST_ID } from "../config";
import {
    decodeNativeHostResponse,
    type NativeHostResponse,
} from "./native-contract";
import { getLastRuntimeError } from "./chrome-runtime";

export type NativeHostRequest =
    | { command: "status" }
    | { command: "launch"; project_root?: string }
    | { command: "stop" }
    | { command: "get_auth_token" }
    | { command: "raise_dashboard"; target: "dashboard" | "history" };

export interface NativeHostSendOptions {
    timeoutMs?: number;
}

const DEFAULT_TIMEOUT_MS = 8_000;

/**
 * Send one request and require a valid, matching native-host response.
 *
 * The Chrome callback is the only safe window in which to read
 * ``runtime.lastError``. A timeout also prevents an unhealthy executable from
 * leaving the MV3 worker or popup action pending forever.
 */
export function sendNativeHostMessage(
    request: NativeHostRequest,
    options: NativeHostSendOptions = {},
): Promise<NativeHostResponse> {
    const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    return new Promise((resolve, reject) => {
        let settled = false;
        const finish = (
            callback: () => void,
        ): void => {
            if (settled) return;
            settled = true;
            clearTimeout(timeout);
            callback();
        };
        const timeout = setTimeout(() => {
            finish(() => reject(new Error("native_host_timeout")));
        }, timeoutMs);

        try {
            chrome.runtime.sendNativeMessage(
                NATIVE_HOST_ID,
                request,
                (rawResponse: unknown) => {
                    const lastError = getLastRuntimeError();
                    if (lastError) {
                        finish(() => reject(new Error(
                            lastError.message || "native_host_unavailable",
                        )));
                        return;
                    }
                    try {
                        const response = decodeNativeHostResponse(rawResponse);
                        if (response.command === "error") {
                            throw new Error(response.error);
                        }
                        if (response.command !== request.command) {
                            throw new Error("unexpected_native_host_response");
                        }
                        finish(() => resolve(response));
                    } catch (error) {
                        finish(() => reject(
                            error instanceof Error
                                ? error
                                : new Error(String(error)),
                        ));
                    }
                },
            );
        } catch (error) {
            finish(() => reject(
                error instanceof Error ? error : new Error(String(error)),
            ));
        }
    });
}
