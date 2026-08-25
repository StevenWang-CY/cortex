import { describe, expect, it } from "vitest";

import { sanitizeActivityRecord, sanitizeActivityUrl } from "../lib/activity-privacy";

const RAW_RECORD = {
    content_id: "https://example.com/course/1?token=secret&utm_source=mail#answer",
    platform: "course",
    content_type: "code_problem",
    title: "Lesson password=hunter2-value",
    url: "https://alice:hunter2@example.com/course/1?token=secret&utm_source=mail#answer",
    favicon_url: "https://example.com/icon.png",
    position: {
        type: "code_problem",
        stage: "IMPLEMENT",
        wrong_answer_count: 2,
        accepted: false,
        time_elapsed_s: 120,
        code_snapshot: "const token = 'ghp_abcdefghijklmnopqrstuvwxyz123456'",
        injected: "must not survive",
    },
    content_duration_s: 0,
    duration_spent_s: 120,
    session_duration_s: 120,
    first_visited: 1,
    last_visited: 2,
    context_snapshot: "Private page body",
    topic_tags: ["typescript"],
    completion_pct: 10,
    max_completion_pct: 12,
    cognitive_state: "",
    visit_count: 1,
    dismissed: false,
    is_playlist: false,
    playlist_id: "",
    playlist_index: -1,
    related_tabs: [],
};

describe("activity privacy boundary", () => {
    it("strips URL credentials, fragments, tracking, and secret parameters", () => {
        expect(sanitizeActivityUrl(RAW_RECORD.url)).toBe("https://example.com/course/1");
    });

    it("retains bounded activity metadata but drops page content without consent", () => {
        const record = sanitizeActivityRecord(RAW_RECORD, false);
        expect(record).not.toBeNull();
        expect(record?.context_snapshot).toBe("");
        expect(record?.position).not.toHaveProperty("code_snapshot");
        expect(record?.position).not.toHaveProperty("injected");
        expect(record?.title).not.toContain("hunter2-value");
        expect(record?.url).toBe("https://example.com/course/1");
    });

    it("sanitizes and caps explicitly allowed content", () => {
        const record = sanitizeActivityRecord(RAW_RECORD, true);
        expect(record?.context_snapshot).toBe("Private page body");
        expect(record?.position.code_snapshot).toContain("[REDACTED:GITHUB_TOKEN]");
        expect(String(record?.position.code_snapshot).length).toBeLessThanOrEqual(2_000);
    });

    it("rejects restricted URLs and unknown position shapes", () => {
        expect(sanitizeActivityRecord({ ...RAW_RECORD, url: "chrome://settings" }, true))
            .toBeNull();
        expect(sanitizeActivityRecord({
            ...RAW_RECORD,
            position: { type: "future_shape", private: "payload" },
        }, true)).toBeNull();
    });
});
