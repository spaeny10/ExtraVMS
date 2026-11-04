import { createContext } from "react";
import type { SiteApi } from "./api";

/** A public, expiring link to a copy of an export (the hub's shares.py). The server's own UI has none: the hub provides
 *  ShareContext for the server pages it shows. */
export type ShareOptions = { days: 1 | 7 | 30; passcode?: string; note?: string };
export type ShareResult = { url: string; expires_at: number };
export type Sharer = (o: ShareOptions) => Promise<ShareResult>;

/** For an export made by a server (its client, the export's id): how to share it, or null where sharing isn't offered. */
export const ShareContext = createContext<((site: SiteApi, exportId: string) => Sharer | null) | null>(null);

export const SHARE_DAYS: { days: 1 | 7 | 30; label: string }[] = [
  { days: 1, label: "24 hours" }, { days: 7, label: "7 days" }, { days: 30, label: "30 days" },
];
