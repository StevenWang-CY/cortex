/** Typed, fail-closed routing for manifest-authorized browser capabilities. */

export class UnsupportedCapabilityError extends Error {
    constructor(readonly capability: string) {
        super(`unsupported browser capability: ${capability}`);
        this.name = "UnsupportedCapabilityError";
    }
}

export type CapabilityHandlers<
    TAction extends { action_type: string },
    TContext,
    TResult,
> = {
    [TCapability in TAction["action_type"]]: (
        action: TAction,
        context: TContext,
    ) => Promise<TResult>;
};

export class CapabilityExecutor<
    TAction extends { action_type: string },
    TContext,
    TResult,
> {
    constructor(
        private readonly handlers: CapabilityHandlers<TAction, TContext, TResult>,
    ) {}

    async execute(action: TAction, context: TContext): Promise<TResult> {
        const capability = action.action_type as TAction["action_type"];
        const handler = this.handlers[capability];
        if (typeof handler !== "function") {
            throw new UnsupportedCapabilityError(String(action.action_type));
        }
        return handler(action, context);
    }

    capabilities(): TAction["action_type"][] {
        return Object.keys(this.handlers) as TAction["action_type"][];
    }
}
