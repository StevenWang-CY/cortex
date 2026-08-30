import { afterEach, describe, expect, it, vi } from "vitest";

import { sendNativeHostMessage } from "../lib/native-messaging";
import { installChromeFake } from "../test/mocks/chrome";

describe("native messaging client", () => {
    afterEach(() => {
        vi.useRealTimers();
    });

    it("accepts a runtime-validated response for the matching command", async () => {
        const fake = installChromeFake();
        fake.runtime.sendNativeMessage.mockImplementation(
            (_host: string, _request: unknown, callback: (value: unknown) => void) => {
                callback({ command: "status", status: "stopped" });
            },
        );

        await expect(sendNativeHostMessage({ command: "status" })).resolves.toEqual({
            command: "status",
            status: "stopped",
        });
        expect(fake.runtime.sendNativeMessage).toHaveBeenCalledWith(
            "com.cortex.launcher",
            { command: "status" },
            expect.any(Function),
        );
    });

    it("reads runtime.lastError inside the native callback", async () => {
        const fake = installChromeFake();
        fake.runtime.sendNativeMessage.mockImplementation(
            (_host: string, _request: unknown, callback: (value: unknown) => void) => {
                fake.runtime.lastError = { message: "Specified native messaging host not found." };
                callback(undefined);
                fake.runtime.lastError = undefined;
            },
        );

        await expect(sendNativeHostMessage({ command: "status" })).rejects.toThrow(
            "Specified native messaging host not found.",
        );
    });

    it("rejects malformed and cross-command responses", async () => {
        const fake = installChromeFake();
        fake.runtime.sendNativeMessage.mockImplementation(
            (_host: string, _request: unknown, callback: (value: unknown) => void) => {
                callback({ command: "stop", status: "stopped" });
            },
        );

        await expect(sendNativeHostMessage({ command: "status" })).rejects.toThrow(
            "unexpected_native_host_response",
        );
    });

    it("rejects when the native host never calls back", async () => {
        vi.useFakeTimers();
        const fake = installChromeFake();
        fake.runtime.sendNativeMessage.mockImplementation(() => undefined);

        const result = sendNativeHostMessage(
            { command: "status" },
            { timeoutMs: 25 },
        );
        const rejection = expect(result).rejects.toThrow("native_host_timeout");
        await vi.advanceTimersByTimeAsync(26);

        await rejection;
    });
});
