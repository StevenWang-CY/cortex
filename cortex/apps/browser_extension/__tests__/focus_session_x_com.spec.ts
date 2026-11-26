/**
 * `x.com` must match the site, not every host that ends in "x.com".
 *
 * The preset and conditional lists tested `/x\.com/i` against the whole URL, so
 * dropbox.com, fedex.com, box.com and linux.com were treated as distractions
 * during a focus session, and so was any URL that mentioned one of them.
 */
import { describe, expect, it } from "vitest";
import { isDistractionForSession, resolveFocusPreset } from "../lib/focus-session";

const session = { goal: "" } as Parameters<typeof isDistractionForSession>[0]["session"];

function distracting(url: string, preset: string): boolean {
    return isDistractionForSession({ url, session, presetPatterns: resolveFocusPreset(preset) });
}

describe("x.com in the focus presets", () => {
    const presets = ["developer", "student", "writer"];

    it.each([
        "https://x.com/",
        "https://x.com/home",
        "http://x.com",
        "https://www.x.com/some/post?ref=1",
        "https://mobile.x.com/explore",
        "https://X.COM/Home",
        "https://x.com:443/home",
    ])("treats %s as the social site", (url) => {
        for (const preset of presets) expect(distracting(url, preset)).toBe(true);
    });

    it.each([
        "https://www.dropbox.com/home",
        "https://dropbox.com/s/abc/file.pdf",
        "https://www.fedex.com/en-us/tracking.html",
        "https://app.box.com/folder/0",
        "https://www.linux.com/training/",
        "https://phoenix.com/",
        "https://example.org/search?q=x.com",
        "https://example.org/redirect?to=https://x.com/home",
        "https://notx.com.example.org/",
    ])("does not treat %s as x.com", (url) => {
        for (const preset of presets) expect(distracting(url, preset)).toBe(false);
        // The conditional list (applied with or without a preset) agrees.
        expect(isDistractionForSession({ url, session: null })).toBe(false);
    });
});
