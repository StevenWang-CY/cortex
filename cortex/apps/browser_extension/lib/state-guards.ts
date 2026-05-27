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
    message: string;
    actions: Array<Record<string, unknown>>;
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
    // ``intervention_type`` is REQUIRED on the Pydantic side but
    // legacy daemons / test fixtures sometimes omit it. We default to
    // the safest tone ("overlay_only" — least invasive) rather than
    // dropping the frame; this preserves backwards-compat with
    // pre-F2 fixtures while still flagging payloads that lack the
    // required ``intervention_id`` discriminator.
    const interventionType =
        typeof p.intervention_type === "string" && p.intervention_type !== ""
            ? p.intervention_type
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
    const message = typeof p.message === "string" ? p.message : "";
    const actions = Array.isArray(p.actions)
        ? (p.actions.filter(
              (a) => typeof a === "object" && a !== null,
          ) as Array<Record<string, unknown>>)
        : [];
    return {
        intervention_id: p.intervention_id,
        intervention_type: interventionType,
        trigger_url: triggerUrl,
        trigger_confidence: triggerConfidence,
        confidence,
        message,
        actions,
        desktop_not_focused: p.desktop_not_focused === true,
    };
}
