/** MV3-safe persisted worker session repository with defensive decoding. */

export interface PersistedSessionState<TFocus, TUndo> {
    focusSession?: TFocus | null;
    undoStack?: TUndo[];
    dismissedInterventions?: [string, number][];
    dismissedUrlPatterns?: [string, number][];
    quietMode?: boolean;
    tabLastActivated?: [number, number][];
    autoFocusArmed?: boolean;
    autoFocusEndsAt?: number | null;
    autoFocusPreset?: string;
    autoFocusCustomDomains?: string[];
}

export interface DurableAutoFocusState {
    autoFocusArmed: boolean;
    presetName: string;
    presetPatternSources: string[];
}

const SESSION_KEYS = [
    "focusSession", "undoStack", "dismissedInterventions",
    "dismissedUrlPatterns", "quietMode", "tabLastActivated",
    "autoFocusArmed", "autoFocusEndsAt", "autoFocusPreset",
    "autoFocusCustomDomains",
] as const;
const AUTO_FOCUS_STATE_KEY = "cortex_auto_focus_state";
const MAX_COLLECTION_SIZE = 512;

function boundedString(value: unknown, maximum = 256): value is string {
    return typeof value === "string" && value.length <= maximum;
}

function finiteTimestamp(value: unknown): value is number {
    return typeof value === "number"
        && Number.isFinite(value)
        && value >= 0;
}

function stringTimestampEntries(value: unknown): [string, number][] | undefined {
    if (!Array.isArray(value) || value.length > MAX_COLLECTION_SIZE) return undefined;
    const result: [string, number][] = [];
    for (const entry of value) {
        if (
            !Array.isArray(entry)
            || entry.length !== 2
            || !boundedString(entry[0], 2_048)
            || !finiteTimestamp(entry[1])
        ) return undefined;
        result.push([entry[0], entry[1]]);
    }
    return result;
}

function tabTimestampEntries(value: unknown): [number, number][] | undefined {
    if (!Array.isArray(value) || value.length > MAX_COLLECTION_SIZE) return undefined;
    const result: [number, number][] = [];
    for (const entry of value) {
        if (
            !Array.isArray(entry)
            || entry.length !== 2
            || !Number.isInteger(entry[0])
            || entry[0] < 0
            || !finiteTimestamp(entry[1])
        ) return undefined;
        result.push([entry[0], entry[1]]);
    }
    return result;
}

export class BrowserSessionStore {
    private sessionTimer: ReturnType<typeof setTimeout> | null = null;
    private autoFocusTimer: ReturnType<typeof setTimeout> | null = null;

    scheduleSession<TFocus, TUndo>(
        state: PersistedSessionState<TFocus, TUndo>,
    ): void {
        if (this.sessionTimer) clearTimeout(this.sessionTimer);
        this.sessionTimer = setTimeout(() => {
            void chrome.storage.session.set(state).catch(() => undefined);
        }, 500);
    }

    async saveSessionNow<TFocus, TUndo>(
        state: PersistedSessionState<TFocus, TUndo>,
    ): Promise<void> {
        if (this.sessionTimer) clearTimeout(this.sessionTimer);
        this.sessionTimer = null;
        await chrome.storage.session.set(state);
    }

    async loadSession<TFocus, TUndo>(): Promise<PersistedSessionState<TFocus, TUndo>> {
        const raw = await chrome.storage.session.get([...SESSION_KEYS]);
        const decoded: PersistedSessionState<TFocus, TUndo> = {};
        if (raw.focusSession && typeof raw.focusSession === "object") {
            decoded.focusSession = raw.focusSession as TFocus;
        }
        if (
            Array.isArray(raw.undoStack)
            && raw.undoStack.length <= MAX_COLLECTION_SIZE
        ) decoded.undoStack = raw.undoStack as TUndo[];
        decoded.dismissedInterventions = stringTimestampEntries(
            raw.dismissedInterventions,
        );
        decoded.dismissedUrlPatterns = stringTimestampEntries(
            raw.dismissedUrlPatterns,
        );
        decoded.tabLastActivated = tabTimestampEntries(raw.tabLastActivated);
        if (typeof raw.quietMode === "boolean") decoded.quietMode = raw.quietMode;
        if (typeof raw.autoFocusArmed === "boolean") {
            decoded.autoFocusArmed = raw.autoFocusArmed;
        }
        if (raw.autoFocusEndsAt === null || finiteTimestamp(raw.autoFocusEndsAt)) {
            decoded.autoFocusEndsAt = raw.autoFocusEndsAt as number | null;
        }
        if (boundedString(raw.autoFocusPreset, 64)) {
            decoded.autoFocusPreset = raw.autoFocusPreset;
        }
        if (Array.isArray(raw.autoFocusCustomDomains)) {
            decoded.autoFocusCustomDomains = raw.autoFocusCustomDomains
                .filter((value): value is string => boundedString(value, 253))
                .slice(0, 100);
        }
        return decoded;
    }

    scheduleAutoFocus(state: DurableAutoFocusState): void {
        if (this.autoFocusTimer) clearTimeout(this.autoFocusTimer);
        this.autoFocusTimer = setTimeout(() => {
            void chrome.storage.local.set({
                [AUTO_FOCUS_STATE_KEY]: {
                    autoFocusArmed: state.autoFocusArmed,
                    _activeFocusPresetName: state.presetName,
                    activeFocusPresetPatterns: state.presetPatternSources,
                },
            }).catch(() => undefined);
        }, 200);
    }

    async loadAutoFocus(): Promise<DurableAutoFocusState | null> {
        const data = await chrome.storage.local.get(AUTO_FOCUS_STATE_KEY);
        const raw = data[AUTO_FOCUS_STATE_KEY];
        if (!raw || typeof raw !== "object") return null;
        const candidate = raw as Record<string, unknown>;
        const presetName = candidate._activeFocusPresetName;
        if (
            typeof candidate.autoFocusArmed !== "boolean"
            || !boundedString(presetName, 64)
        ) return null;
        const sources = Array.isArray(candidate.activeFocusPresetPatterns)
            ? candidate.activeFocusPresetPatterns
                .filter((value): value is string => boundedString(value, 512))
                .slice(0, 100)
            : [];
        return {
            autoFocusArmed: candidate.autoFocusArmed,
            presetName,
            presetPatternSources: sources,
        };
    }

    cancelPendingWrites(): void {
        if (this.sessionTimer) clearTimeout(this.sessionTimer);
        if (this.autoFocusTimer) clearTimeout(this.autoFocusTimer);
        this.sessionTimer = null;
        this.autoFocusTimer = null;
    }
}
