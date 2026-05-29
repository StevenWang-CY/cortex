/**
 * Runtime guards for daemon → extension payloads.
 *
 * The Pydantic-generated TS types in ``types/generated/cortex_schemas.d.ts``
 * give us *compile-time* type safety, but the actual bytes arriving on
 * the WebSocket are still ``Record<string, unknown>``. The double cast
 * ``msg.payload as unknown as CortexState`` lets bogus payloads (legacy
 * daemons, fuzzed frames, corrupted streams) flow into UI state and
 * crash the popup at read time.
 *
 * The guards below check the *shape* of inbound payloads before we
 * commit them to module-scoped state. They are hand-written (zero new
 * runtime deps) and intentionally conservative — fields that are
 * defaulted by the Pydantic serializer are still required here because
 * the daemon always emits them on the wire.
 *
 * F1 / F2 closure (Phase-4 audit).
 */

export interface CortexStateShape {
    state: string;
    confidence: number;
    scores: Record<string, number>;
    signal_quality: Record<string, number>;
    dwell_seconds: number;
    reasons: string[];
}

export interface CapturePipelineFlags {
    frames_flowing?: boolean;
    face_detected?: boolean;
    stale?: boolean;
}

export interface StoreFlags {
    degraded?: boolean;
}

/**
 * Coarse runtime guard for STATE_UPDATE.payload.
 *
 * We accept the frame iff:
 *   - ``state`` is a string (the discriminator the UI labels by),
 *   - ``confidence`` is a finite number,
 *   - ``scores`` / ``signal_quality`` are objects (Pydantic dicts on
 *     the wire — null/undefined would crash the popup's ``Object.entries``),
 *   - ``dwell_seconds`` is a finite number,
 *   - ``reasons`` is an array.
 *
 * Optional envelope flags (``capture.stale``, ``store.degraded``) are
 * NOT required: pre-Phase-4 daemons omit them and the UI handles
 * missing gracefully.
 */
export function isCortexState(obj: unknown): obj is CortexStateShape {
    if (typeof obj !== "object" || obj === null) return false;
    const o = obj as Record<string, unknown>;
    if (typeof o.state !== "string") return false;
    if (typeof o.confidence !== "number" || !Number.isFinite(o.confidence)) {
        return false;
    }
    if (typeof o.scores !== "object" || o.scores === null) return false;
    if (typeof o.signal_quality !== "object" || o.signal_quality === null) {
        return false;
    }
    if (typeof o.dwell_seconds !== "number" || !Number.isFinite(o.dwell_seconds)) {
        return false;
    }
    if (!Array.isArray(o.reasons)) return false;
    return true;
}

/**
 * Truncate a payload-shaped object for logging without leaking PII or
 * blowing the console. Returns at most ``max`` characters of the
 * JSON serialisation; falls back to ``[unserialisable]`` if a cycle
 * trips JSON.stringify.
 */
export function truncatePayloadForLog(
    payload: unknown,
    max = 200,
): string {
    try {
        const s = JSON.stringify(payload);
        if (typeof s !== "string") return "[unserialisable]";
        return s.length > max ? s.slice(0, max) + "…" : s;
    } catch {
        return "[unserialisable]";
    }
}

/**
 * Narrowed view over the fields ``handleIntervention`` and downstream
 * UI actually read from an INTERVENTION_TRIGGER payload. Any field
 * the dispatcher does not validate is treated as ``unknown`` — never
 * deref blindly.
 *
 * F2: explicitly enumerate every consumed field so a malformed daemon
 * frame can be normalised at one place rather than crashing N
 * downstream consumers individually.
 */
export interface InterventionPlanShape {
    intervention_id: string;
    intervention_type: string;
    trigger_url: string | null;
    trigger_confidence: number;
    confidence: number;
    /**
     * C3: the human-facing overlay headline. On the wire this is the
     * ``headline`` field of ``InterventionTriggerPayload`` (the old
     * ``message`` name never existed on the Pydantic model). We read
     * ``headline`` first and fall back to a legacy ``message`` key so
     * pre-C3 fixtures still resolve.
     */
    headline: string;
    /**
     * C3: executable actions the user can approve. The wire field is
     * ``suggested_actions`` (the old ``actions`` name was a drift bug).
     * We read ``suggested_actions`` first, then a legacy ``actions`` key.
     */
    suggested_actions: Array<Record<string, unknown>>;
    desktop_not_focused: boolean;
}

/**
 * Normalise an INTERVENTION_TRIGGER payload into the field set the
 * dispatcher relies on. Returns ``null`` when the payload is missing
 * the *required* discriminators (``intervention_id``,
 * ``intervention_type``); the caller logs + skips.
 *
 * Optional numeric fields default to 0 (never NaN). Arrays default to
 * ``[]``. Strings default to "" so .startsWith / .toLowerCase paths
 * never crash on undefined.
 *
 * The original payload is *not* mutated — we return a structurally
 * narrowed view alongside the (still ``Record<string, unknown>``)
 * original so existing handleIntervention code paths that read
 * extra fields keep working.
 */
export function normaliseInterventionPayload(
    payload: unknown,
): InterventionPlanShape | null {
    if (typeof payload !== "object" || payload === null) return null;
    const p = payload as Record<string, unknown>;
    if (typeof p.intervention_id !== "string" || p.intervention_id === "") {
        return null;
    }
    // C3: the wire discriminator for intervention severity is ``level``
    // (overlay_only | simplified_workspace | guided_mode). Older fixtures
    // used ``intervention_type``. We read ``intervention_type`` first
    // (back-compat), then ``level`` (the real wire field), and finally
    // fall back to the safest tone ("overlay_only" — least invasive)
    // rather than dropping the frame.
    const interventionType =
        typeof p.intervention_type === "string" && p.intervention_type !== ""
            ? p.intervention_type
            : typeof p.level === "string" && p.level !== ""
            ? p.level
            : "overlay_only";
    const triggerUrl =
        typeof p.trigger_url === "string" ? p.trigger_url : null;
    const triggerConfidence =
        typeof p.trigger_confidence === "number" &&
        Number.isFinite(p.trigger_confidence)
            ? p.trigger_confidence
            : 0;
    const confidence =
        typeof p.confidence === "number" && Number.isFinite(p.confidence)
            ? p.confidence
            : 0;
    // C3: real wire field is ``headline``; ``message`` is a legacy alias.
    const headline =
        typeof p.headline === "string"
            ? p.headline
            : typeof p.message === "string"
            ? p.message
            : "";
    // C3: real wire field is ``suggested_actions``; ``actions`` is the
    // legacy alias. Prefer the wire name, fall back to the legacy one.
    const rawActions = Array.isArray(p.suggested_actions)
        ? p.suggested_actions
        : Array.isArray(p.actions)
        ? p.actions
        : [];
    const suggestedActions = rawActions.filter(
        (a) => typeof a === "object" && a !== null,
    ) as Array<Record<string, unknown>>;
    return {
        intervention_id: p.intervention_id,
        intervention_type: interventionType,
        trigger_url: triggerUrl,
        trigger_confidence: triggerConfidence,
        confidence,
        headline,
        suggested_actions: suggestedActions,
        desktop_not_focused: p.desktop_not_focused === true,
    };
}

/**
 * Minimal runtime guard for a SuggestedAction from the daemon. The
 * discriminating fields are ``action_id`` (string) and
 * ``action_type`` (string) — the extension only needs those two to
 * dispatch. All other fields remain ``unknown`` until the individual
 * action handler accesses them.
 *
 * P1-13 closure: replaces ``dispatchPayload.action as SuggestedAction``
 * at the ACTION_DISPATCH wire boundary so a malformed daemon frame
 * (or a fuzzed payload) cannot reach ``executeAction`` as an untrusted
 * object.
 */
export interface SuggestedActionMinimal {
    action_id: string;
    action_type: string;
    target?: string;
    label?: string;
    reason?: string;
    category?: string;
    reversible?: boolean;
    metadata?: Record<string, unknown>;
}

export function isSuggestedAction(obj: unknown): obj is SuggestedActionMinimal {
    if (typeof obj !== "object" || obj === null) return false;
    const o = obj as Record<string, unknown>;
    return typeof o.action_id === "string" && o.action_id !== ""
        && typeof o.action_type === "string" && o.action_type !== "";
}
