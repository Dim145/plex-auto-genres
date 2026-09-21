import { AlertTriangle, ArrowDown, ArrowUp, Trash2 } from "lucide-react";
import { useId } from "react";
import type { LibraryRun, MediaType, ValidationIssue } from "../../api/types";
import { Field } from "../../components/form/Field";
import { Segmented } from "../../components/form/Segmented";
import { Toggle } from "../../components/form/Toggle";
import { ANIME_PROVIDER_PRESETS, KEYWORD_SOURCES, TYPES, animeProviders, emptyRules, hasTmdbFallback, issueAt, issuesOwn, providerPresetKey, rulesEmpty, sourceChain, type Help } from "./editor";
import { RulesEditor } from "./RulesEditor";

export function LibraryEditor({
  lib,
  index,
  errors,
  help,
  plexSections,
  onChange,
  onRemove,
  onMove,
  canMoveUp,
  canMoveDown,
  autoFocusName = false,
}: {
  lib: LibraryRun;
  index: number;
  errors: ValidationIssue[];
  help: Help;
  plexSections: string[];
  onChange: (next: LibraryRun) => void;
  onRemove: () => void;
  onMove: (delta: -1 | 1) => void;
  canMoveUp: boolean;
  canMoveDown: boolean;
  autoFocusName?: boolean;
}) {
  const id = useId();
  const loc = ["libraries", index];
  const own = issuesOwn(errors, loc);
  const err = (field: string) => issueAt(errors, [...loc, field]);
  const set = <K extends keyof LibraryRun>(key: K, value: LibraryRun[K]) => onChange({ ...lib, [key]: value });
  const isAnime = lib.type === "anime";
  const listId = `${id}-sections`;
  const chain = sourceChain(lib.providers, lib.type);
  // Keywords exist only where a source has them, so the toggle follows the
  // chain rather than the library type.
  const hasKeywords = chain.some((p) => KEYWORD_SOURCES.includes(p));
  const merging = lib.providerMode === "merge";

  /** Set the source chain, dropping keywords with the source that had them. */
  const setProviders = (next: string[] | null) =>
    onChange({
      ...lib,
      providers: next,
      useKeywords:
        sourceChain(next, lib.type).some((p) => KEYWORD_SOURCES.includes(p)) && lib.useKeywords,
    });

  const changeType = (type: MediaType) => {
    onChange({
      ...lib,
      type,
      // Providers and keywords are type-specific; reset rather than carry
      // an invalid combination across.
      providers: null,
      useKeywords: type === "anime" ? false : lib.useKeywords,
    });
  };

  return (
    <article className={`editor-card ${own.length || errors.length ? "editor-card--invalid" : ""} ${lib.enabled ? "" : "editor-card--off"}`} aria-label={lib.library || `Library ${index + 1}`}>
      {own.length > 0 && (
        <div className="editor-card__banner" role="alert">
          <AlertTriangle size={16} aria-hidden="true" />
          <div>{own.map((e) => e.msg).join(" ")}</div>
        </div>
      )}

      <div className="editor-card__head">
        <Field id={`${id}-name`} label="Plex library" help={help("LibraryRun", "library")} error={err("library")}>
          <input
            id={`${id}-name`}
            className="input"
            value={lib.library}
            list={listId}
            placeholder="Exact name in Plex"
            autoFocus={autoFocusName}
            onChange={(e) => set("library", e.target.value)}
          />
          <datalist id={listId}>
            {plexSections.map((name) => (
              <option key={name} value={name} />
            ))}
          </datalist>
        </Field>
        <div className="editor-card__tools">
          <button type="button" className="iconbtn" aria-label="Move up" disabled={!canMoveUp} onClick={() => onMove(-1)}>
            <ArrowUp size={16} aria-hidden="true" />
          </button>
          <button type="button" className="iconbtn" aria-label="Move down" disabled={!canMoveDown} onClick={() => onMove(1)}>
            <ArrowDown size={16} aria-hidden="true" />
          </button>
          <button type="button" className="iconbtn iconbtn--danger" aria-label={`Remove ${lib.library || "library"}`} onClick={onRemove}>
            <Trash2 size={16} aria-hidden="true" />
          </button>
        </div>
      </div>

      <div className="editor-grid">
        <Field id={`${id}-type`} label="Type" help={help("LibraryRun", "type")} error={err("type")}>
          <Segmented name={`${id}-type`} ariaLabel="Library type" value={lib.type} onChange={changeType} options={TYPES.map((t) => ({ value: t, label: t }))} />
        </Field>

        <Field id={`${id}-providers`} label="Reads from" help={help("LibraryRun", "providers")} error={err("providers")}>
          {isAnime ? (
            <div className="stack stack--tight">
              <Segmented
                name={`${id}-providers`}
                ariaLabel="Providers"
                value={providerPresetKey(lib.providers)}
                onChange={(key) =>
                  setProviders(
                    animeProviders(
                      ANIME_PROVIDER_PRESETS.find((p) => p.key === key)?.value ?? null,
                      hasTmdbFallback(lib.providers),
                    ),
                  )
                }
                options={ANIME_PROVIDER_PRESETS.map((p) => ({ value: p.key, label: p.label, hint: p.hint }))}
              />
              <Toggle
                id={`${id}-tmdb-fallback`}
                checked={hasTmdbFallback(lib.providers)}
                onChange={(on) =>
                  setProviders(
                    animeProviders(
                      ANIME_PROVIDER_PRESETS.find((p) => p.key === providerPresetKey(lib.providers))?.value ?? null,
                      on,
                    ),
                  )
                }
                label={merging ? "…and TMDB" : "…then TMDB"}
                help={merging
                  ? "TMDB is asked for every title too, and its genres join the anime ones. Needs TMDB_API_KEY."
                  : "A last resort when no anime source answers. Returns TMDB's own genres, and needs TMDB_API_KEY."}
              />
              {chain.length > 1 && (
                <Segmented
                  name={`${id}-mode`}
                  ariaLabel="How the sources combine"
                  value={lib.providerMode}
                  onChange={(v) => set("providerMode", v)}
                  options={[
                    { value: "fallback", label: "first answer", hint: "Ask in order, keep the first source that answers" },
                    { value: "merge", label: "merge all", hint: "Ask every source and pool what they return" },
                  ]}
                />
              )}
            </div>
          ) : (
            <div className="mono tone-teal" style={{ minHeight: 38, display: "flex", alignItems: "center" }}>tmdb</div>
          )}
        </Field>

        <Field id={`${id}-writes`} label="Writes to" help={help("LibraryRun", "useGenres")} error={err("useGenres")}>
          <Segmented
            name={`${id}-writes`}
            ariaLabel="Write target"
            value={lib.useGenres ? "genres" : "collections"}
            onChange={(v) => onChange({ ...lib, useGenres: v === "genres", clearGenres: v === "genres" ? lib.clearGenres : false })}
            options={[
              { value: "collections", label: "collections", hint: "Create one collection per genre" },
              { value: "genres", label: "genre field", hint: "Write Plex's own genre tags" },
            ]}
          />
        </Field>

        <div className="toggle-grid">
          <Toggle id={`${id}-enabled`} checked={lib.enabled} onChange={(v) => set("enabled", v)} label="Enabled" help={help("LibraryRun", "enabled")} />
          <Toggle id={`${id}-clear`} checked={lib.clearGenres} disabled={!lib.useGenres} onChange={(v) => set("clearGenres", v)} label="Replace existing genres" help={lib.useGenres ? help("LibraryRun", "clearGenres") : "Only when writing the genre field"} />
          {hasKeywords && (
            <Toggle
              id={`${id}-keywords`}
              checked={lib.useKeywords}
              onChange={(v) => set("useKeywords", v)}
              label="Use keywords, not genres"
              help={isAnime
                ? "TMDB's keywords and AniList's community tags, from whichever of them this library reads. Much finer, and much noisier."
                : help("LibraryRun", "useKeywords")}
            />
          )}
        </div>

        <div>
          <div className="label" style={{ marginBottom: 4 }}>After tagging</div>
          <div className="toggle-grid">
            <Toggle id={`${id}-posters`} checked={lib.setPosters} onChange={(v) => set("setPosters", v)} label="Upload posters" help="From posters/<type>/" />
            <Toggle id={`${id}-sort`} checked={lib.sortCollections} onChange={(v) => set("sortCollections", v)} label="Sort collections" help="Prefix the sort titles" />
            <Toggle id={`${id}-rate`} checked={lib.rateAnime} onChange={(v) => set("rateAnime", v)} label="Set ratings" help={help("LibraryRun", "rateAnime")} />
            <Toggle id={`${id}-ratecol`} checked={lib.createRatingCollections} onChange={(v) => set("createRatingCollections", v)} label="Rating collections" help="1–5 Star Rating collections" />
          </div>
        </div>
      </div>

      <details className="details" open={lib.overrides !== null}>
        <summary>
          Overrides for this library
          {lib.overrides && !rulesEmpty(lib.overrides) && <span className="chip chip--type">active</span>}
        </summary>
        <div className="details__body">
          <p className="field__help">{help("LibraryRun", "overrides")}</p>
          <RulesEditor id={`${id}-ov`} rules={lib.overrides ?? emptyRules()} onChange={(v) => set("overrides", v)} errors={errors} loc={[...loc, "overrides"]} help={help} />
          {lib.overrides && (
            <div>
              <button type="button" className="button button--ghost button--sm" onClick={() => set("overrides", null)}>
                Clear overrides
              </button>
            </div>
          )}
        </div>
      </details>
    </article>
  );
}
