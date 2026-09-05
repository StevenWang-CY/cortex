/**
 * Toolbar badge priority. Two events used to fight over one badge (a
 * pending intervention "1" and an unread recap "✓"); whichever wrote last
 * won and clearing one silently erased the other. The badge is now derived
 * from a tiny state record with a fixed priority: a pending intervention
 * outranks an unread recap, and clearing the intervention reveals the recap
 * again if it is still unread.
 */

export type BadgeText = "1" | "✓" | "";

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
}
