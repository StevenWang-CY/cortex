/** Bounded, session-only browser interaction telemetry. */

const MAX_TRACKED_TABS = 2_048;

export class TabActivationTelemetry {
    private readonly lastActivated = new Map<number, number>();

    recordActivation(tabId: number, now = Date.now()): void {
        if (!Number.isInteger(tabId) || tabId < 0 || !Number.isFinite(now)) return;
        // Reinsertion gives the map deterministic oldest-first eviction.
        this.lastActivated.delete(tabId);
        this.lastActivated.set(tabId, Math.max(0, now));
        while (this.lastActivated.size > MAX_TRACKED_TABS) {
            const oldest = this.lastActivated.keys().next().value;
            if (typeof oldest !== "number") break;
            this.lastActivated.delete(oldest);
        }
    }

    recordRemoval(tabId: number): void {
        this.lastActivated.delete(tabId);
    }

    lastActivation(tabId: number): number | undefined {
        return this.lastActivated.get(tabId);
    }

    hydrate(entries: readonly [number, number][]): void {
        this.lastActivated.clear();
        for (const [tabId, timestamp] of entries.slice(-MAX_TRACKED_TABS)) {
            this.recordActivation(tabId, timestamp);
        }
    }

    entries(): [number, number][] {
        return [...this.lastActivated.entries()];
    }

    snapshot(): ReadonlyMap<number, number> {
        return new Map(this.lastActivated);
    }
}
