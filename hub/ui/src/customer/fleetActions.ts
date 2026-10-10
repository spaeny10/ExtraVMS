/**
 * Pure helpers for fleet actions, which are planned and run only on Customer › Actions (FleetActionsPage):
 *  - looksLikeInstruction: Find's Ask box uses it to send an instruction here instead of asking the servers
 *    (mirrors fleet_actions._clean on the hub: polite padding stripped, questions never count, an action verb first);
 *  - looksLikeRequest: something neither Ask nor Actions can do ("Alert me when someone enters", "Watch for a white
 *    truck"): the twin of site_ask.looks_like_request, whose answer (REQUEST_MESSAGE) says where alert rules live;
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

// Requests to DO something Ask can't (mirrors hub/hub/site_ask.py REQUEST_FIRST / REQUEST_ANYWHERE / PAST_QUESTION)
const ALERTISH = String.raw`(?:alerts?|alarms?|rules?|notifications?|notices?|reminders?|automations?|triggers?|texts?|e-?mails?|messages?)`;
const REQUEST_FIRST = new RegExp(String.raw`^(?:(?:make|create|add|set\s+up|setup|set|build|configure|schedule|program)\b.{0,80}?\b` + ALERTISH + String.raw`\b`
  + String.raw`|(?:can|could|may)\s+(?:i|we)\s+(?:get|have|set\s+up|make|create|receive|add)\b.{0,60}?\b` + ALERTISH + String.raw`\b`
  + String.raw`|(?:alert|notify|warn|text|e-?mail|ping|page|message|call)\s+(?:me|us|someone|security|the\s+\w+)\b`
  + String.raw`|(?:let|tell)\s+(?:me|us)\s+know\b`
  + String.raw`|tell\s+(?:me|us)\s+(?:when|whenever|if|once|as\s+soon\s+as|the\s+(?:moment|minute|next\s+time)|next\s+time)\b`
  + String.raw`|send\s+(?:me|us)\b`
  + String.raw`|watch\s+(?:out|for|over)\b|look\s+out\b|monitor\b|keep\s+(?:an\s+)?eye\b|keep\s+watch\b|be\s+on\s+the\s+lookout\b`
  + String.raw`|remind\b`
  + String.raw`|(?:turn|switch)\s+(?:\S+\s+){0,4}?(?:on|off)\b`
  + String.raw`|(?:delete|remove|erase|wipe|purge)\b`
  + String.raw`|(?:enable|disable|change|modify|edit|adjust|configure|reconfigure|reset|arm|disarm)\b)`, "i");
const REQUEST_ANYWHERE = new RegExp(String.raw`\b(?:(?:alert|notify|warn|text|e-?mail|ping|page)\s+(?:me|us)|let\s+(?:me|us)\s+know|remind\s+(?:me|us)`
  + String.raw`|(?:be|get|been)\s+(?:notified|alerted|pinged|texted|e-?mailed)`
  + String.raw`|(?:get|receive|want|like|need)\s+(?:an?\s+)?(?:alert|notification|text|e-?mail)s?\s+(?:when|whenever|if|for)`
  + String.raw`|(?:set\s+up|create|make|add)\s+(?:an?\s+|the\s+|some\s+)?(?:\w+\s+){0,2}?(?:alerts?|alarms?|notifications?|reminders?))\b`, "i");
const PAST_QUESTION = /^\s*(?:did|was|were|has|have|had|why|who|which|what)\b/i;

/** The text without polite padding ("please", "can you", ...), and whether there was any. */
function unpadded(text: string): { t: string; polite: boolean } {
  let t = text.trim();
  let polite = false;
  for (let m = POLITE.exec(t); m && t.slice(m[0].length); m = POLITE.exec(t)) { t = t.slice(m[0].length); polite = true; }
  return { t: t.trim(), polite };
}

/**
 * Does this ask the system to DO something Ask can't ("Alert me when someone enters", "Can you make an alert if someone
 * is in the kitchen?", "Tell me when…", "Watch for a white truck")? Questions about what happened don't count.
 */
export function looksLikeRequest(text: string): boolean {
  const { t } = unpadded(text);
  if (!t) return false;
  return REQUEST_FIRST.test(t) || (!PAST_QUESTION.test(t) && REQUEST_ANYWHERE.test(t));
}

/** Does this Ask text read as a fleet instruction ("Migrate Ironsight to Hailo T1", "Quiet alerts tonight")? */
export function looksLikeInstruction(text: string): boolean {
  const { t, polite } = unpadded(text);
  if (!t || QUESTION.test(t) || (t.endsWith("?") && !polite) || looksLikeRequest(text)) return false;
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
