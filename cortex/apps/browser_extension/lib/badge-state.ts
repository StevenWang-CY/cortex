/**
 * Toolbar badge priority. Two events used to fight over one badge (a
 * pending intervention "1" and an unread recap "✓"); whichever wrote last
 * won and clearing one silently erased the other. The badge is now derived
 * from a tiny state record with a fixed priority: a pending intervention
 * outranks an unread recap, and clearing the intervention reveals the recap
 * again if it is still unread.
 */

export type BadgeText = "1" | "✓" | "";

/** The two flags, in a shape that survives a `chrome.storage` round trip. */
export interface BadgeSnapshot {
    intervention: boolean;
    recap: boolean;
}

export class BadgeState {
    private intervention = false;
    private recap = false;

    setIntervention(pending: boolean): BadgeText {
        this.intervention = pending;
        return this.text();
    }

    setRecap(unread: boolean): BadgeText {
        this.recap = unread;
        return this.text();
    }

    text(): BadgeText {
        if (this.intervention) return "1";
        if (this.recap) return "✓";
        return "";
    }

    /**
     * The flags, for persistence.
     *
     * These lived only in module memory, while `chrome.action.setBadgeText`
     * persists across service-worker restarts. After an MV3 eviction the
     * record read `recap: false` even though the "✓" was still painted on the
     * toolbar and `cortex.lastRecap` was still in `chrome.storage.local`, so
     * the first `setIntervention(false)` on the restarted worker recomputed
     * `text()` as `""` and wiped the unread-recap signal — the same
     * fight-over-one-badge regression this class was written to end, just
     * across a restart instead of across two call sites.
     */
    snapshot(): BadgeSnapshot {
        return { intervention: this.intervention, recap: this.recap };
    }

    hydrate(snapshot: Partial<BadgeSnapshot> | undefined): void {
        if (!snapshot) return;
        if (typeof snapshot.intervention === "boolean") {
            this.intervention = snapshot.intervention;
        }
        if (typeof snapshot.recap === "boolean") this.recap = snapshot.recap;
    }
}
