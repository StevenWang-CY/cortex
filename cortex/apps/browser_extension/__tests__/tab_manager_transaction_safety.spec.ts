import { beforeEach, describe, expect, it, vi } from "vitest";

describe("tab manager transaction safety", () => {
    beforeEach(() => {
        vi.resetModules();
    });

    it("does not claim a group exists when the grouping call failed", async () => {
        globalThis.__cortexChrome.tabs.group.mockRejectedValue(
            new Error("grouping unavailable"),
        );
        const { groupSpecificTabs } = await import("../tab-manager");

        await expect(groupSpecificTabs([2, 3], "Focus"))
            .resolves.toBeNull();
        expect(globalThis.__cortexChrome.tabGroups.update).not.toHaveBeenCalled();
    });

    it("retains the exact group identity when presentation setup is incomplete", async () => {
        globalThis.__cortexChrome.tabs.group.mockResolvedValue(41);
        globalThis.__cortexChrome.tabGroups.update.mockRejectedValue(
            new Error("collapse failed"),
        );
        const { groupSpecificTabs } = await import("../tab-manager");

        await expect(groupSpecificTabs([2, 3], "Focus"))
            .resolves.toBe(41);
    });

    it("surfaces the created group identity if its durable checkpoint fails", async () => {
        globalThis.__cortexChrome.tabs.group.mockResolvedValue(43);
        const {
            groupSpecificTabs,
            IndeterminateBrowserMutationError,
        } = await import("../tab-manager");

        const operation = groupSpecificTabs(
            [2, 3],
            "Focus",
            "blue",
            async () => { throw new Error("storage unavailable"); },
        );
        await expect(operation).rejects.toMatchObject({
            name: "IndeterminateBrowserMutationError",
            inverse: { tabIds: [2, 3], groupId: 43 },
        });
        await operation.catch((error: unknown) => {
            expect(error).toBeInstanceOf(IndeterminateBrowserMutationError);
        });
        expect(globalThis.__cortexChrome.tabs.ungroup).toHaveBeenCalledWith([2, 3]);
    });

    it("keeps a failed restore snapshot so a transient ungroup error can retry", async () => {
        globalThis.__cortexChrome.tabs.query.mockResolvedValue([
            { id: 1, active: true, url: "https://docs.example", windowId: 1 },
            { id: 2, active: false, url: "https://news.example", windowId: 1 },
        ] as chrome.tabs.Tab[]);
        globalThis.__cortexChrome.tabs.group.mockResolvedValue(51);
        const tabManager = await import("../tab-manager");
        const applied = await tabManager.hideNonActiveTabs("intervention-retry");
        expect(applied?.groupId).toBe(51);

        globalThis.__cortexChrome.tabs.query.mockResolvedValue([
            { id: 2, groupId: 51 },
        ] as chrome.tabs.Tab[]);
        globalThis.__cortexChrome.tabs.ungroup.mockRejectedValueOnce(
            new Error("temporary failure"),
        );
        await expect(tabManager.restoreHiddenTabs("intervention-retry"))
            .resolves.toBe(false);
        expect(tabManager.getSnapshot("intervention-retry")?.groupId).toBe(51);

        globalThis.__cortexChrome.tabs.ungroup.mockResolvedValue(undefined);
        await expect(tabManager.restoreHiddenTabs("intervention-retry"))
            .resolves.toBe(true);
        expect(tabManager.getSnapshot("intervention-retry")).toBeNull();
    });
});
