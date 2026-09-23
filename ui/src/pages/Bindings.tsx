import { Hand, Link2, Undo2, Unlink } from "lucide-react";
import { Link } from "react-router-dom";
import { useBindings, useConfig, useDeleteBinding, useDeleteManual, useManualTags } from "../api/client";
import type { ManualEntry } from "../api/types";
import { useConfirm } from "../components/ConfirmDialog";
import { ManualSummary } from "../components/ManualSummary";
import { useToast } from "../components/Toast";
import { Empty, ErrorBlock } from "../components/Empty";
import { PageHeader, Panel } from "../components/Panel";
import { Skeleton } from "../components/Skeleton";
import { dateTime, relTime } from "../lib/format";
import { reveal } from "../lib/reveal";

export default function Bindings() {
  const bindings = useBindings();
  const unbind = useDeleteBinding();
  const manual = useManualTags();
  const handBack = useDeleteManual();
  const config = useConfig();
  const confirm = useConfirm();
  const toast = useToast();
  const libraryOf = (name: string) => config.data?.libraries.find((l) => l.library === name);
  const fieldOf = (name: string) => (libraryOf(name)?.useGenres === false ? "collections" : "genres");

  const giveBack = async (entry: ManualEntry) => {
    const name = entry.title ?? entry.media_key;
    const lib = libraryOf(entry.library);
    const merges = !lib || !lib.useGenres || !lib.clearGenres;
    const ok = await confirm({
      title: `Hand ${name} back to the sources?`,
      body: `What was decided by hand is dropped, and the next run asks the sources again.${
        merges && entry.added.length ? " This library merges into what Plex holds, so names added by hand stay on the item until refused." : ""
      }`,
      confirmLabel: "Hand back",
      danger: true,
    });
    if (!ok) return;
    try {
      await handBack.mutateAsync({ library: entry.library, mediaKey: entry.media_key });
      toast("ok", "Handed back to the sources", name);
    } catch (e) {
      toast("fail", "Could not hand back", (e as Error).message);
    }
  };

  // An item may pin one id per source, so a row is a *source's* pin: without
  // the provider the call deletes every pin the item has, which is not what
  // the button next to one row can mean.
  const remove = async (library: string, mediaKey: string, provider: string) => {
    const ok = await confirm({ title: `Remove the ${provider} id pinned on ${mediaKey}?`, body: "Any other id pinned on this item stays. The next run resolves it again from what remains, its GUID, or by title.", confirmLabel: "Remove", danger: true });
    if (!ok) return;
    try {
      await unbind.mutateAsync({ library, mediaKey, provider });
      toast("ok", "Binding removed", `${provider}://`);
    } catch (e) {
      toast("fail", "Could not remove", (e as Error).message);
    }
  };

  return (
    <div className="page">
      <PageHeader
        eyebrow="04 · Overrides"
        title="Manual overrides"
        lede="What a person decided, which the automatic runs do not undo: items pinned to an exact provider id, and genres set by hand. Add either from a library's item list."
      />

      <Panel {...reveal(1)} eyebrow="Matching" title="Pinned ids">
        {bindings.isPending ? (
          <div style={{ display: "grid", gap: 12 }}><Skeleton /><Skeleton /><Skeleton width="70%" /></div>
        ) : bindings.isError ? (
          <ErrorBlock error={bindings.error} onRetry={() => bindings.refetch()} />
        ) : bindings.data.length === 0 ? (
          <Empty icon={<Link2 size={28} strokeWidth={1.5} />} title="No bindings" action={{ to: "/libraries", label: "Browse a library" }}>
            When the automatic match is wrong, open the library's items and pick the right record — or from the CLI:<br />
            <code>plex-auto-genres bind "Animes" "Monster" mal 19</code>
          </Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Library</th>
                  <th scope="col">Item</th>
                  <th scope="col">Bound to</th>
                  <th scope="col">Note</th>
                  <th scope="col">Added</th>
                  <th scope="col"><span className="sr-only">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                {bindings.data.map((b) => (
                  <tr key={`${b.library}/${b.media_key}/${b.provider}`}>
                    <td>{b.library}</td>
                    <td className="mono"><Link to={`/libraries/${encodeURIComponent(b.library)}?status=bound`}>{b.media_key}</Link></td>
                    <td className="mono tone-teal">{b.provider}://{b.provider_id}</td>
                    <td className="muted wrap">{b.note ?? "—"}</td>
                    <td className="mono muted" title={dateTime(b.created_at)}>{relTime(b.created_at)}</td>
                    <td>
                      <button type="button" className="button button--ghost button--sm" onClick={() => void remove(b.library, b.media_key, b.provider)} disabled={unbind.isPending}>
                        <Unlink size={12} aria-hidden="true" /> Unbind
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel {...reveal(2)} eyebrow="Tagging" title="Genres decided by hand">
        {manual.isPending ? (
          <div style={{ display: "grid", gap: 12 }}><Skeleton /><Skeleton width="70%" /></div>
        ) : manual.isError ? (
          <ErrorBlock error={manual.error} onRetry={() => manual.refetch()} />
        ) : manual.data.length === 0 ? (
          <Empty icon={<Hand size={28} strokeWidth={1.5} />} title="Nothing decided by hand" action={{ to: "/libraries", label: "Browse a library" }}>
            When a source keeps a genre wrong or misses one, open the item's genres in its library — or from the CLI:<br />
            <code>plex-auto-genres manual "Animes" "Monster" --add Psychological --remove Kids</code>
          </Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">Item</th>
                  <th scope="col">Decided</th>
                  <th scope="col">Note</th>
                  <th scope="col">Changed</th>
                  <th scope="col"><span className="sr-only">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                {manual.data.map((m) => (
                  <tr key={`${m.library}/${m.media_key}`}>
                    <td>
                      <Link to={`/libraries/${encodeURIComponent(m.library)}?status=manual`}>{m.title ?? m.media_key}</Link>
                      <div className="mono faint">{m.library}{m.title ? ` · ${m.media_key}` : ""}</div>
                    </td>
                    <td className="wrap">
                      <ManualSummary manual={m} field={fieldOf(m.library)} />
                    </td>
                    <td className="muted wrap">{m.note ?? "—"}</td>
                    <td className="mono muted" title={dateTime(m.updated_at)}>{relTime(m.updated_at)}</td>
                    <td>
                      <button type="button" className="button button--ghost button--sm" onClick={() => void giveBack(m)} disabled={handBack.isPending}>
                        <Undo2 size={12} aria-hidden="true" /> Hand back
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
