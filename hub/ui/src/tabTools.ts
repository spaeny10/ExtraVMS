/**
 * The right side of a Site's tab row (SitePage): a tab with a toolbar of its own (Live) renders it there through a
 * portal, so the tabs and the toolbar share one row. undefined = no Site page around (the SOC's embedded grid: the
 * toolbar stays where it is); null = the row isn't mounted yet.
 */
import { createContext } from "react";

export const TabToolsSlot = createContext<HTMLElement | null | undefined>(undefined);
