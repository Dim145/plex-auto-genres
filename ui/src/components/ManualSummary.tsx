import { Hand, Lock } from "lucide-react";
import type { ReactNode } from "react";
import type { ManualView } from "../api/types";

/**
 * One line for what a person decided about an item, the same in a library's
 * item list and on the Overrides page. Only a genre lock is an exact list:
 * collections are never cleared, so a lock there adds and refuses names and
 * leaves the item's other collections alone.
 */
export function ManualSummary({
  manual,
  field,
  children,
}: {
  manual: ManualView;
  field: "genres" | "collections";
  children?: ReactNode;
}) {
  const exact = manual.locked && field === "genres";
  return (
    <div className="item__manual" title={manual.note ?? undefined}>
      {manual.locked ? <Lock size={12} aria-hidden="true" /> : <Hand size={12} aria-hidden="true" />}
      <span>{exact ? "exactly" : manual.locked ? "sources not asked" : "by hand"}</span>
      {exact && manual.added.length === 0 && <span className="faint">no genres</span>}
      {manual.added.map((g) => (
        <span key={`+${g}`} className="chip chip--add">
          {exact ? g : `+${g}`}
        </span>
      ))}
      {!exact &&
        manual.removed.map((g) => (
          <span key={`-${g}`} className="chip chip--drop">
            <span className="sr-only">never </span>
            {g}
          </span>
        ))}
      {children}
    </div>
  );
}
