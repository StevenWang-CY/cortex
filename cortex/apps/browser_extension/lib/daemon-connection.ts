/** Connection/auth primitives for the Cortex MV3 daemon client. */

export interface SequencedWireFrame {
    type?: string;
    sequence?: number;
    event_id?: string;
}

export class SerialCommandQueue {
    private chain: Promise<void> = Promise.resolve();

    constructor(private readonly onError: (error: unknown) => void) {}

    enqueue(operation: () => Promise<void>): Promise<void> {
        const scheduled = this.chain.then(operation);
        this.chain = scheduled.catch(this.onError);
        return this.chain;
    }
}

export class ReconnectBackoff {
    private delay: number;

    constructor(
        readonly initialDelay: number,
        readonly maximumDelay: number,
    ) {
        if (initialDelay <= 0 || maximumDelay < initialDelay) {
            throw new RangeError("invalid reconnect backoff bounds");
        }
        this.delay = initialDelay;
    }

    reset(): void {
        this.delay = this.initialDelay;
    }

    takeAndAdvance(): number {
        const current = this.delay;
        this.delay = Math.min(this.delay * 2, this.maximumDelay);
        return current;
    }

    get current(): number {
        return this.delay;
    }
}

export class FrameReplayGuard {
    private readonly lastByType: Record<string, number> = {};
    private readonly eventIds = new Set<string>();
    private readonly eventOrder: string[] = [];

    constructor(private readonly maximumEventIds = 512) {
        if (maximumEventIds < 1) throw new RangeError("event-id bound must be positive");
    }

    accept(frame: SequencedWireFrame): boolean {
        if (typeof frame.event_id === "string" && frame.event_id.length > 0) {
            if (this.eventIds.has(frame.event_id)) return false;
            this.eventIds.add(frame.event_id);
            this.eventOrder.push(frame.event_id);
            if (this.eventOrder.length > this.maximumEventIds) {
                const evicted = this.eventOrder.shift();
                if (evicted) this.eventIds.delete(evicted);
            }
        }
        const sequence = typeof frame.sequence === "number" ? frame.sequence : 0;
        if (sequence <= 0 || !frame.type) return true;
        const previous = this.lastByType[frame.type] ?? 0;
        if (sequence <= previous) return false;
        this.lastByType[frame.type] = sequence;
        return true;
    }

    reset(): void {
        for (const key of Object.keys(this.lastByType)) delete this.lastByType[key];
        this.eventIds.clear();
        this.eventOrder.length = 0;
    }

    lastSequence(messageType: string): number {
        return this.lastByType[messageType] ?? 0;
    }
}

export class ParseErrorWindow {
    private timestamps: number[] = [];

    constructor(
        private readonly windowMs: number,
        private readonly threshold: number,
    ) {
        if (windowMs <= 0 || threshold < 1) {
            throw new RangeError("invalid parse-error window");
        }
    }

    record(now = Date.now()): boolean {
        this.timestamps.push(now);
        this.timestamps = this.timestamps.filter(
            (timestamp) => now - timestamp <= this.windowMs,
        );
        return this.timestamps.length >= this.threshold;
    }

    reset(): void {
        this.timestamps = [];
    }

    get count(): number {
        return this.timestamps.length;
    }
}

export function newWireId(): string {
    try {
        if (typeof globalThis.crypto?.randomUUID === "function") {
            return globalThis.crypto.randomUUID();
        }
    } catch {
        // Older extension runtimes may not expose Web Crypto.
    }
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (char) => {
        const random = (Math.random() * 16) | 0;
        const value = char === "x" ? random : (random & 0x3) | 0x8;
        return value.toString(16);
    });
}

function validClientInstanceId(value: unknown): value is string {
    return typeof value === "string"
        && value.length >= 8
        && value.length <= 128
        && /^[A-Za-z0-9._:-]+$/.test(value);
}

export class ClientIdentityStore {
    private pending: Promise<string> | null = null;

    constructor(
        private readonly storageKey = "cortex_client_instance_id_v1",
        private readonly prefix = "browser_",
    ) {}

    async get(): Promise<string> {
        if (this.pending) return this.pending;
        this.pending = (async () => {
            const stored = await chrome.storage.local.get(this.storageKey);
            const existing = stored[this.storageKey];
            if (validClientInstanceId(existing)) return existing;
            const created = `${this.prefix}${newWireId()}`;
            await chrome.storage.local.set({ [this.storageKey]: created });
            return created;
        })();
        try {
            return await this.pending;
        } catch (error) {
            this.pending = null;
            throw error;
        }
    }
}

export interface WireMetadataOptions {
    schemaVersion: string;
    protocolVersion: () => string;
    bootId?: string;
}

export class WireEnvelopeEncoder {
    readonly bootId: string;

    constructor(private readonly options: WireMetadataOptions) {
        this.bootId = options.bootId || newWireId();
    }

    encode<T extends Record<string, unknown>>(message: T): T & Record<string, unknown> {
        const unixMs = Date.now();
        const monotonicMs = typeof globalThis.performance?.now === "function"
            ? globalThis.performance.now()
            : 0;
        return {
            ...message,
            schema_version: this.options.schemaVersion,
            protocol_version: this.options.protocolVersion(),
            event_id: newWireId(),
            sent_at_unix_ms: unixMs,
            sent_at_mono_ns: Math.max(0, Math.round(monotonicMs * 1_000_000)),
            boot_id: this.bootId,
            timestamp: unixMs / 1000,
        };
    }
}
