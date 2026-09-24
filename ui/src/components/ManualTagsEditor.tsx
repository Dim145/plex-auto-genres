import { Undo2 } from "lucide-react";
import { useId, useMemo, useState, type FormEvent } from "react";
import { useDeleteManual, useItemTags, useSetManual } from "../api/client";
import type { ItemView } from "../api/types";
import { foldName, tagKey, uniqueBy } from "../lib/tags";
import { describedBy, Field } from "./form/Field";
import { Segmented } from "./form/Segmented";
import { TagInput } from "./form/TagInput";
import { Modal } from "./Modal";
import { useToast } from "./Toast";

type Mode = "adjust" | "lock";
type TagField = "genres" | "collections";

const listed = (names: string[]) => names.map((n) => `“${n}”`).join(", ");

/** What the next run will do, in one sentence. */
function describe({
  field,
  locking,
  exact,
  adds,
  drops,
  keepsExisting,
  hasDecision,
}: {
  field: TagField;
  locking: boolean;
  exact: string[];
  adds: string[];
  drops: string[];
  keepsExisting: boolean;
  hasDecision: boolean;
}): string {
  if (locking && field === "genres") {
    return exact.length
      ? `The next run writes exactly ${listed(exact)}, and no source is asked about this item any more.`
      : "The next run clears this item's genres, and no source is asked about it any more.";
  }
  const decided = [drops.length ? `never ${listed(drops)}` : "", adds.length ? `always ${listed(adds)}` : ""]
    .filter(Boolean)
    .join(", and ");
  if (locking) {
    return `No source is asked about this item any more${decided ? `: ${decided}` : ""}. Collections are never cleared, so its other ones stay.`;
  }
  const from = keepsExisting ? "what the item carries and what the sources return" : "what the sources return";
  if (!decided) {
    return hasDecision ? "Nothing decided: saving hands the item back to the sources." : `Nothing decided yet. The next run writes ${from}.`;
  }
  return `The next run writes ${from}, ${decided}.`;
}

/**
 * Decide an item's genres (or collections) by hand, in the terms Kometa uses
 * for the same job: some always written, some never written, or the whole
 * list fixed. Whatever is decided here outranks the sources until it is
 * handed back; the next run applies it.
 *
 * Mount one per item (`key` it by the item): its state starts from that
 * item's decision, and a closed editor costs nothing.
 */
export function ManualTagsEditor({
  library,
  field,
  keepsExisting,
  prefix,
  item,
  vocabulary,
  onClose,
}: {
  library: string;
  field: TagField;
  /** The library merges into what Plex holds rather than replacing it. */
  keepsExisting: boolean;
  /** Put on every name the app writes; a name matches a tag with it or without it. */
  prefix: string;
  item: ItemView;
  /** Names seen around this item, offered as suggestions. */
  vocabulary: string[];
  onClose: () => void;
}) {
  const id = useId();
  const toast = useToast();
  const save = useSetManual();
  const handBack = useDeleteManual();
  const saved = item.manual;
  // Genres can be fixed as a whole list. Collections are never cleared, so a
  // lock there keeps the same two lists and only stops asking the sources.
  const exactList = field === "genres";
  // The items page came from Plex's library listing, which shows only the
  // first few tags of an item; the item's own page has them all. A list
  // fixed from the listing would have dropped the ones nobody saw.
  const all = useItemTags(library, item.rating_key);
  const listed = field === "genres" ? item.current_genres : item.current_collections;
  const current = all.data ? (field === "genres" ? all.data.genres : all.data.collections) : listed;
  const reading = all.isPending;
  const key = (name: string) => tagKey(name, prefix);
  const has = (list: string[], name: string) => list.some((x) => key(x) === key(name));
  const without = (list: string[], name: string) => list.filter((x) => key(x) !== key(name));

  const savedExact = Boolean(saved?.locked && exactList);
  const [mode, setMode] = useState<Mode>(saved?.locked ? "lock" : "adjust");
  const [adds, setAdds] = useState<string[]>(saved && !savedExact ? saved.added : []);
  const [drops, setDrops] = useState<string[]>(saved && !savedExact ? saved.removed : []);
  // Until it is edited, the fixed list is what the item carries, adjusted:
  // the usual edit is "these, minus one, plus one". Names keep their spelling.
  const [draft, setDraft] = useState<string[] | null>(savedExact && saved ? saved.added : null);
  const exact = draft ?? uniqueBy([...current.filter((t) => !has(drops, t)), ...adds], key);
  const [note, setNote] = useState(saved?.note ?? "");

  const locking = mode === "lock";
  const fixing = locking && exactList;

  // In Plex now: a click takes one off (or puts it back) without typing it.
  const toggleCurrent = (name: string) => {
    if (fixing) {
      setDraft(has(exact, name) ? without(exact, name) : [...exact, name]);
    } else if (has(drops, name)) {
      setDrops(without(drops, name));
    } else {
      setDrops([...drops, name]);
      setAdds(without(adds, name));
    }
  };

  const clash = fixing ? undefined : adds.find((a) => has(drops, a));
  // A name with no letter or digit matches no tag; the server refuses it too.
  const unmatchable = (fixing ? exact : [...adds, ...drops]).find((n) => !foldName(n));
  const problem = clash
    ? `“${clash}” is also in “Never write”; keep one.`
    : unmatchable
      ? `“${unmatchable}” has no letter or digit to match a tag by.`
      : undefined;
  const decidesNothing = !locking && adds.length === 0 && drops.length === 0;
  const busy = save.isPending || handBack.isPending;
  const suggestions = useMemo(
    () => uniqueBy([...current, ...(item.state?.genres ?? []), ...vocabulary], (n) => tagKey(n, prefix)),
    [current, item.state, vocabulary, prefix],
  );

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (problem) return;
    try {
      const result = await save.mutateAsync({
        library,
        media_key: item.media_key,
        added: fixing ? exact : adds,
        removed: fixing ? [] : drops,
        locked: locking,
        note: note.trim() || null,
        title: item.year ? `${item.title} (${item.year})` : item.title,
      });
      toast("ok", result ? "Saved" : "Handed back to the sources", result ? `${item.title}: applied on the next run.` : item.title);
      onClose();
    } catch (err) {
      toast("fail", "Could not save", (err as Error).message);
    }
  };

  const giveBack = async () => {
    try {
      await handBack.mutateAsync({ library, mediaKey: item.media_key });
      toast(
        "ok",
        "Handed back to the sources",
        `${item.title}: the sources decide its ${field} again from the next run.` +
          (keepsExisting && saved?.added.length ? " This library merges, so what was added stays until refused." : ""),
      );
      onClose();
    } catch (err) {
      toast("fail", "Could not hand back", (err as Error).message);
    }
  };

  const summary = describe({ field, locking, exact, adds, drops, keepsExisting, hasDecision: Boolean(saved) });
  const addsId = `${id}-adds`;
  const dropsId = `${id}-drops`;
  const exactId = `${id}-exact`;
  const noteId = `${id}-note`;

  return (
    <Modal open onClose={onClose} eyebrow={`By hand · ${library}`} title={<>{item.title} {item.year && <span className="muted">({item.year})</span>}</>}>
      <form className="manual" onSubmit={submit}>
        <Segmented
          name={`${id}-mode`}
          ariaLabel="How much to decide"
          value={mode}
          onChange={setMode}
          options={[
            { value: "adjust", label: "Add or refuse", hint: "Keep asking the sources; add some names and refuse others." },
            exactList
              ? { value: "lock", label: "Set exactly", hint: "Stop asking the sources about this item; the list is yours." }
              : { value: "lock", label: "Stop asking", hint: "Stop asking the sources about this item; its other collections stay." },
          ]}
        />

        <div>
          <div className="label">In Plex now</div>
          {reading && <p className="faint">Reading all of its {field} from Plex…</p>}
          {all.isError && (
            <p className="tone-amber">Plex did not return the item's full list, so some of its {field} may be missing here.</p>
          )}
          {reading ? null : current.length ? (
            <div className="manual__chips" role="group" aria-label={`${field} in Plex now`}>
              {current.map((name) => {
                const off = fixing ? !has(exact, name) : has(drops, name);
                return (
                  <button
                    key={name}
                    type="button"
                    className={`chip manual__chip ${off ? "is-off" : ""}`}
                    aria-pressed={!off}
                    title={off ? "Click to keep it" : fixing ? "Click to leave it out" : "Click to never write it"}
                    onClick={() => toggleCurrent(name)}
                  >
                    {name}
                  </button>
                );
              })}
            </div>
          ) : (
            <p className="faint">No {field} on this item yet.</p>
          )}
        </div>

        {fixing ? (
          <Field id={exactId} label="Genres" help="Exactly these, in this order. Leave it empty to clear the item's genres." error={problem}>
            <TagInput id={exactId} value={exact} onChange={setDraft} placeholder="a name, then Enter" suggestions={suggestions} describedBy={describedBy(exactId, true, problem)} splitOnComma={false} autoFocus />
          </Field>
        ) : (
          <>
            <Field
              id={addsId}
              label="Always write"
              help="Added whatever the sources say, even when they have nothing for this title. The ignore and replace rules and the genre cap do not touch these."
              error={problem}
            >
              <TagInput id={addsId} value={adds} onChange={setAdds} placeholder="a name, then Enter" suggestions={suggestions} describedBy={describedBy(addsId, true, problem)} splitOnComma={false} autoFocus />
            </Field>
            <Field id={dropsId} label="Never write" help="Kept off even when a source returns it, and taken off if it is already there.">
              <TagInput id={dropsId} value={drops} onChange={setDrops} placeholder="a name, then Enter" suggestions={suggestions} describedBy={describedBy(dropsId, true)} splitOnComma={false} />
            </Field>
          </>
        )}

        <Field id={noteId} label="Note" help="Why, for whoever reads the list of overrides later.">
          <input id={noteId} className="input input--sm" value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} placeholder="optional" aria-describedby={describedBy(noteId, true)} />
        </Field>

        <p className="manual__summary" aria-live="polite">
          {summary}
        </p>

        <div className="dialog__actions manual__actions">
          {saved && (
            <button
              type="button"
              className="button button--ghost manual__handback"
              onClick={() => void giveBack()}
              disabled={busy}
              title={keepsExisting ? "The sources decide again from the next run. This library merges, so names added here stay until refused." : "The sources decide again from the next run."}
            >
              <Undo2 size={14} aria-hidden="true" /> Hand back to the sources
            </button>
          )}
          <button type="button" className="button button--ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="button" disabled={busy || reading || Boolean(problem) || (decidesNothing && !saved)}>
            {save.isPending ? "Saving…" : "Save"}
          </button>
        </div>
        <p className="faint manual__key">
          Keyed by <code className="mono">{item.media_key}</code>. Applied on the library's next run.
        </p>
      </form>
    </Modal>
  );
}
