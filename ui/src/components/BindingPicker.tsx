import { ExternalLink, Link2, Search, Unlink } from "lucide-react";
import { useEffect, useId, useState, type FormEvent } from "react";
import { useCandidates, useCreateBinding, useDeleteBinding } from "../api/client";
import type { BindingProvider, BindingView, ItemView, MediaType } from "../api/types";
import { Field } from "./form/Field";
import { Segmented } from "./form/Segmented";
import { Modal } from "./Modal";
import { useToast } from "./Toast";

const SEARCHABLE: Record<MediaType, string[]> = {
  anime: ["jikan", "anilist", "tmdb"],
  "standard-tv": ["tmdb"],
  "standard-movie": ["tmdb"],
};

/** The id scheme a search provider's results bind as. */
const BIND_AS: Record<string, BindingProvider> = { jikan: "mal", anilist: "anilist", tmdb: "tmdb" };

/**
 * Pick the provider record an item should resolve to. Search first (ranked
 * candidates with posters), or type an id straight in.
 *
 * `schemes` comes from the API rather than a table here, because only the
 * providers know what they can resolve: offering an id nothing reads meant
 * the pin was stored and then silently ignored on every run.
 *
 * The provider choices are seeded from `type` once, at mount: render it with
 * `key={type}` so a library whose type arrives after the first paint gets a
 * fresh picker instead of one stuck on the placeholder type.
 */
export function BindingPicker({
  library,
  type,
  item,
  schemes,
  preferredProvider,
  onClose,
}: {
  library: string;
  type: MediaType;
  item: ItemView | null;
  schemes: BindingProvider[];
  preferredProvider?: string;
  onClose: () => void;
}) {
  const id = useId();
  const toast = useToast();
  const create = useCreateBinding();
  const remove = useDeleteBinding();
  // Held here rather than read from `item`, which is the row as it was when
  // the modal opened and does not follow what is pinned inside it.
  const [pins, setPins] = useState<BindingView[]>([]);
  // Searching a catalogue whose ids this library cannot be bound to is a dead
  // end: the result would be refused on the way back in.
  const bindable = SEARCHABLE[type].filter((p) => schemes.includes(BIND_AS[p]!));
  const providers = bindable.length ? bindable : SEARCHABLE[type];
  // Both choices are *derived* from what the library allows, not frozen at
  // mount: `schemes` arrives with the items query, a moment after the picker
  // opens, and a choice made before that would be refused on the way back in.
  const [pickedSearch, setPickedSearch] = useState<string | null>(null);
  const provider =
    [pickedSearch, preferredProvider].find((p) => p && providers.includes(p)) ?? providers[0]!;
  const [text, setText] = useState("");
  const [year, setYear] = useState<string>("");
  const [submitted, setSubmitted] = useState<{ q: string; year: number | null } | null>(null);
  const [pickedScheme, setPickedScheme] = useState<BindingProvider | null>(null);
  const manualProvider: BindingProvider =
    (pickedScheme && schemes.includes(pickedScheme) ? pickedScheme : schemes[0]) ?? "tmdb";
  const [manualId, setManualId] = useState("");
  const [note, setNote] = useState("");

  // Reset the form for each item and search right away with its own title.
  useEffect(() => {
    if (!item) return;
    setText(item.title);
    setYear(item.year ? String(item.year) : "");
    setSubmitted({ q: item.title, year: item.year });
    setManualId("");
    setNote("");
    setPins(item.bindings);
  }, [item]);

  const candidates = useCandidates(submitted ? { q: submitted.q, type, provider, year: submitted.year } : null);

  const search = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted({ q: text.trim(), year: year ? Number(year) : null });
  };

  // An item may pin one id per source, so binding leaves the picker open:
  // a series on two catalogues is named on each before you are done.
  const bind = async (bindProvider: BindingProvider, providerId: string, why?: string) => {
    if (!item) return;
    try {
      const created = await create.mutateAsync({ library, media_key: item.media_key, provider: bindProvider, provider_id: providerId, note: why || null });
      setPins((prev) => [...prev.filter((p) => p.provider !== bindProvider), created]);
      setManualId("");
      toast("ok", "Bound", `${item.title} → ${bindProvider}://${providerId}. Applied on the next run.`);
    } catch (err) {
      toast("fail", "Could not bind", (err as Error).message);
    }
  };

  const unpin = async (pin: BindingView) => {
    if (!item) return;
    try {
      await remove.mutateAsync({ library, mediaKey: item.media_key, provider: pin.provider });
      setPins((prev) => prev.filter((p) => p.provider !== pin.provider));
      toast("ok", "Unpinned", `${pin.provider}://${pin.provider_id}`);
    } catch (err) {
      toast("fail", "Could not unpin", (err as Error).message);
    }
  };

  return (
    <Modal open={item !== null} onClose={onClose} eyebrow={`Bind · ${library}`} title={item ? <>{item.title} {item.year && <span className="muted">({item.year})</span>}</> : ""} wide>
      {item && (
        <div className="picker">
          <form className="picker__search" onSubmit={search}>
            {providers.length > 1 && (
              <Segmented name={`${id}-prov`} ariaLabel="Search provider" value={provider} onChange={setPickedSearch} options={providers.map((p) => ({ value: p, label: p }))} />
            )}
            <input className="input" aria-label="Title to search" value={text} onChange={(e) => setText(e.target.value)} placeholder="title…" />
            <input className="input picker__year" aria-label="Year" inputMode="numeric" value={year} onChange={(e) => setYear(e.target.value.replace(/\D/g, "").slice(0, 4))} placeholder="year" />
            <button type="submit" className="button" disabled={!text.trim()}>
              <Search size={14} aria-hidden="true" /> Search
            </button>
          </form>

          <div className="picker__results" aria-live="polite">
            {candidates.isPending && submitted ? (
              <p className="faint mono">Searching {provider}…</p>
            ) : candidates.isError ? (
              <p className="tone-fail mono">{(candidates.error as Error).message}</p>
            ) : candidates.data && candidates.data.length === 0 ? (
              <p className="muted">Nothing on {provider} for “{submitted?.q}”. Try another spelling, drop the year, or enter an id below.</p>
            ) : (
              <ul className="cands">
                {(candidates.data ?? []).map((c) => (
                  <li key={`${c.provider}-${c.provider_id}`} className="cand">
                    {c.image ? <img className="cand__img" src={c.image} alt="" loading="lazy" width={60} height={90} /> : <span className="cand__img cand__img--none" aria-hidden="true" />}
                    <div className="cand__main">
                      <div className="cand__title">
                        {c.title} {c.year && <span className="muted">({c.year})</span>}
                        {c.score != null && <span className="chip chip--quiet">{c.score}/10</span>}
                      </div>
                      <div className="cand__meta mono faint">
                        {c.provider}:{c.provider_id}
                        {c.url && (
                          <a href={c.url} target="_blank" rel="noreferrer" className="cand__link">
                            open <ExternalLink size={11} aria-hidden="true" />
                          </a>
                        )}
                      </div>
                      {c.synopsis && <p className="cand__synopsis">{c.synopsis}</p>}
                      {c.genres.length > 0 && <p className="chips">{c.genres.slice(0, 6).map((g) => <span key={g} className="chip chip--quiet">{g}</span>)}</p>}
                    </div>
                    <button
                      type="button"
                      className="button button--sm"
                      disabled={create.isPending || !BIND_AS[c.provider]}
                      title={BIND_AS[c.provider] ? undefined : `Results from ${c.provider} cannot be bound yet`}
                      onClick={() => {
                        const scheme = BIND_AS[c.provider];
                        if (scheme) void bind(scheme, c.provider_id, `picked from ${c.provider} search`);
                      }}
                    >
                      <Link2 size={12} aria-hidden="true" /> Use this
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <form
            className="picker__manual"
            onSubmit={(e) => {
              e.preventDefault();
              if (manualId.trim()) void bind(manualProvider, manualId.trim(), note.trim());
            }}
          >
            <div className="label">Or enter an id</div>
            <div className="picker__manual-row">
              <Segmented name={`${id}-manual`} ariaLabel="Id scheme" value={manualProvider} onChange={setPickedScheme} options={schemes.map((p) => ({ value: p, label: p }))} />
              <input className="input mono" aria-label="Provider id" value={manualId} onChange={(e) => setManualId(e.target.value)} placeholder="id" />
              <button type="submit" className="button button--ghost" disabled={!manualId.trim() || create.isPending}>
                Bind
              </button>
            </div>
            <Field id={`${id}-note`} label="Note" help="Why this binding exists; shown in the bindings list.">
              <input id={`${id}-note`} className="input input--sm" value={note} onChange={(e) => setNote(e.target.value)} placeholder="optional" />
            </Field>
          </form>

          {pins.length > 0 && (
            <div className="pins">
              <div className="label">Pinned ids</div>
              <ul className="pins__list">
                {pins.map((pin) => (
                  <li key={pin.provider} className="pins__row">
                    <span className="mono">{pin.provider}://{pin.provider_id}</span>
                    {pin.note && <span className="faint pins__note">{pin.note}</span>}
                    <button
                      type="button"
                      className="iconbtn"
                      aria-label={`Remove the ${pin.provider} id`}
                      title={`Remove the ${pin.provider} id`}
                      onClick={() => void unpin(pin)}
                      disabled={remove.isPending}
                    >
                      <Unlink size={14} aria-hidden="true" />
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <p className="faint">
            Keyed by <code className="mono">{item.media_key}</code>. One id per source: the next
            run resolves this item through them instead of its Plex GUID or a title search, and a
            merged library reads the exact record on each.
          </p>
        </div>
      )}
    </Modal>
  );
}
