/**
 * Pure helpers for fleet actions, which are planned and run only on Customer › Actions (FleetActionsPage):
 *  - looksLikeInstruction: Find's Ask box uses it to send an instruction here instead of asking the servers
 *    (mirrors fleet_actions._clean on the hub: polite padding stripped, questions never count, an action verb first);
 *  - actionsHref / textFromSearch: the Actions page link with the instruction prefilled (?text=), never auto-planned;
 *  - instructionKey: Enter plans, Shift+Enter is a new line;
 *  - parserLabel, outcomeText, whereText, actionText: the card's "Read by ..." line and the Action log's cells.
 */
import type { ActionRecent } from "../api";

export const ACTIONS_PATH = "/customer/actions";
const MAX_TEXT = 500;

const POLITE = /^\s*(please|pls|kindly|ok|okay|now|go ahead and|can you|could you|would you|will you|i want to|i'd like to|i would like to|we need to|let's|lets)\b[\s,]*/i;
const QUESTION = /^\s*(how|what|what's|whats|when|where|who|whom|whose|why|which|did|does|do(?!\s+not\b)|is|are|was|were|has|have|had|show|list|find|search|any|anyone|anybody|count|tell|give|should|shall|may|might)\b/i;
const VERB_FIRST = /^(migrate|move|transfer|relocate|retire|decommission|rename|set|keep|retain|add|lock|protect|quiet|mute|silence|snooze|hush|unmute|stop\s+describing|start\s+describing|describe\s+only)\b/i;

/** Does this Ask text read as a fleet instruction ("Migrate Ironsight to Hailo T1", "Quiet alerts tonight")? */
export function looksLikeInstruction(text: string): boolean {
  let t = text.trim();
  let polite = false;
  for (let m = POLITE.exec(t); m && t.slice(m[0].length); m = POLITE.exec(t)) { t = t.slice(m[0].length); polite = true; }
  t = t.trim();
  if (!t || QUESTION.test(t) || (t.endsWith("?") && !polite)) return false;
  return VERB_FIRST.test(t);
}

/** The Actions page, with `text` prefilled in its instruction box (nothing is planned until the user presses Plan). */
export function actionsHref(text?: string): string {
  const t = (text ?? "").trim().slice(0, MAX_TEXT);
  return t ? `${ACTIONS_PATH}?text=${encodeURIComponent(t)}` : ACTIONS_PATH;
}

/** The prefilled instruction from the page's query string ("" when none). */
export function textFromSearch(search: string): string {
  return (new URLSearchParams(search).get("text") ?? "").slice(0, MAX_TEXT);
}

/** What a key press in the instruction box does: "plan" on Enter, null otherwise (Shift+Enter: the textarea's new line). */
export function instructionKey(e: { key: string; shiftKey: boolean; isComposing?: boolean }): "plan" | null {
  return e.key === "Enter" && !e.shiftKey && !e.isComposing ? "plan" : null;
}

/** Which parser read the sentence, as the card says it. */
export function parserLabel(parser: string | undefined | null): string | null {
  return parser === "ai" ? "Read by the AI" : parser === "rules" ? "Read by the rule parser" : null;
}

/** The action in plain text (the audit row's action without its "fleet action: " prefix). */
export function actionText(r: Pick<ActionRecent, "action">): string {
  return r.action.replace(/^fleet action: /, "");
}

/** Server(s) and Site of a log row: "Harbor · Echo, Delta", "every server", or "—". */
export function whereText(r: Pick<ActionRecent, "servers" | "location">): string {
  const servers = (r.servers ?? []).join(", ");
  const loc = r.location && r.location !== servers ? r.location : "";
  return [loc, servers].filter(Boolean).join(" · ") || "—";
}

/** The Action log's outcome cell: Done / Failed: reason / Refused: reason / Undone by X at T. */
export function outcomeText(r: Pick<ActionRecent, "outcome" | "status" | "reason" | "undone_by" | "undone_at">, fmt: (ts: number) => string): string {
  const outcome = r.outcome ?? (r.status === 200 ? "done" : "failed");
  if (outcome === "refused") return `Refused: ${r.reason || "not allowed"}`;
  if (outcome === "failed") return `Failed: ${r.reason || "see the details"}`;
  if (r.undone_by) return `Undone by ${r.undone_by}${r.undone_at ? ` at ${fmt(r.undone_at)}` : ""}`;
  return "Done";
}
