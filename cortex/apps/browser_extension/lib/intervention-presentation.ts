/** Side-effect-free intervention presentation/cooldown state model. */

export interface ActiveInterventionRecord {
    plan: Record<string, unknown>;
    correlation_id: string;
    mountedAt: number;
}

export type PresentationSuppression = "intervention" | "url" | null;

const DEFAULT_INTERVENTION_COOLDOWN_MS = 30 * 60 * 1_000;
const DEFAULT_URL_COOLDOWN_MS = 10 * 60 * 1_000;
const MAX_COOLDOWN_MS = 24 * 60 * 60 * 1_000;
const MAX_DISMISSALS = 512;

function boundedCooldown(value: unknown, fallback: number): number {
    return typeof value === "number" && Number.isFinite(value) && value >= 0
        ? Math.min(value, MAX_COOLDOWN_MS)
        : fallback;
}

function hostname(raw: unknown): string | null {
    if (typeof raw !== "string" || raw.length === 0) return null;
    try {
        return new URL(raw).hostname;
    } catch {
        return null;
    }
}

export class InterventionPresentationState {
    private mounted: ActiveInterventionRecord | null = null;
    private readonly interventionDismissals = new Map<string, number>();
    private readonly urlDismissals = new Map<string, number>();
    private interventionCooldownMs = DEFAULT_INTERVENTION_COOLDOWN_MS;
    private urlCooldownMs = DEFAULT_URL_COOLDOWN_MS;

    get active(): ActiveInterventionRecord | null {
        return this.mounted;
    }

    mount(
        plan: Record<string, unknown>,
        correlationId: string,
        mountedAt = Date.now(),
    ): ActiveInterventionRecord {
        this.mounted = {
            plan: { ...plan },
            correlation_id: correlationId,
            mountedAt,
        };
        return this.mounted;
    }

    clear(): ActiveInterventionRecord | null {
        const previous = this.mounted;
        this.mounted = null;
        return previous;
    }

    configureCooldowns(args: {
        interventionMs?: unknown;
        urlMs?: unknown;
    }): void {
        this.interventionCooldownMs = boundedCooldown(
            args.interventionMs,
            this.interventionCooldownMs,
        );
        this.urlCooldownMs = boundedCooldown(args.urlMs, this.urlCooldownMs);
    }

    suppression(
        interventionId: string,
        triggerUrl: unknown,
        now = Date.now(),
    ): PresentationSuppression {
        this.prune(now);
        const interventionAt = this.interventionDismissals.get(interventionId);
        if (
            interventionAt !== undefined
            && now - interventionAt < this.interventionCooldownMs
        ) return "intervention";
        const urlKey = hostname(triggerUrl);
        const urlAt = urlKey ? this.urlDismissals.get(urlKey) : undefined;
        return urlAt !== undefined && now - urlAt < this.urlCooldownMs
            ? "url"
            : null;
    }

    dismiss(
        interventionId: string,
        triggerUrl: unknown,
        now = Date.now(),
    ): void {
        if (interventionId) this.interventionDismissals.set(interventionId, now);
        const urlKey = hostname(triggerUrl);
        if (urlKey) this.urlDismissals.set(urlKey, now);
        this.prune(now);
        while (this.interventionDismissals.size > MAX_DISMISSALS) {
            const oldest = this.interventionDismissals.keys().next().value;
            if (typeof oldest !== "string") break;
            this.interventionDismissals.delete(oldest);
        }
        while (this.urlDismissals.size > MAX_DISMISSALS) {
            const oldest = this.urlDismissals.keys().next().value;
            if (typeof oldest !== "string") break;
            this.urlDismissals.delete(oldest);
        }
    }

    hydrateCooldowns(args: {
        interventions?: readonly [string, number][];
        urls?: readonly [string, number][];
    }): void {
        this.interventionDismissals.clear();
        this.urlDismissals.clear();
        for (const [key, timestamp] of args.interventions || []) {
            if (key && Number.isFinite(timestamp) && timestamp >= 0) {
                this.interventionDismissals.set(key, timestamp);
            }
        }
        for (const [key, timestamp] of args.urls || []) {
            if (key && Number.isFinite(timestamp) && timestamp >= 0) {
                this.urlDismissals.set(key, timestamp);
            }
        }
        this.prune(Date.now());
    }

    cooldownSnapshot(): {
        interventions: [string, number][];
        urls: [string, number][];
    } {
        return {
            interventions: [...this.interventionDismissals.entries()],
            urls: [...this.urlDismissals.entries()],
        };
    }

    private prune(now: number): void {
        for (const [key, timestamp] of this.interventionDismissals) {
            if (now - timestamp >= this.interventionCooldownMs) {
                this.interventionDismissals.delete(key);
            }
        }
        for (const [key, timestamp] of this.urlDismissals) {
            if (now - timestamp >= this.urlCooldownMs) {
                this.urlDismissals.delete(key);
            }
        }
    }
}
