import { Link2, Unlink } from "lucide-react";
import { Link } from "react-router-dom";
import { useBindings, useDeleteBinding } from "../api/client";
import { useConfirm } from "../components/ConfirmDialog";
import { useToast } from "../components/Toast";
import { Empty, ErrorBlock } from "../components/Empty";
import { PageHeader, Panel } from "../components/Panel";
import { Skeleton } from "../components/Skeleton";
import { dateTime, relTime } from "../lib/format";
import { reveal } from "../lib/reveal";

export default function Bindings() {
  const bindings = useBindings();
  const unbind = useDeleteBinding();
  const confirm = useConfirm();
  const toast = useToast();

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
        eyebrow="04 · Bindings"
        title="Manual bindings"
        lede="An item pinned to an exact provider id, overriding the automatic match. Add one from a library's item list."
      />

      <Panel {...reveal(1)}>
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
    </div>
  );
}
