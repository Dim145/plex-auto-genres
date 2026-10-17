import { ArrowDown, ArrowUp, Plus, X } from "lucide-react";
import { useEffect, useRef } from "react";
import type { MediaType } from "../../api/types";
import { SOURCES, SOURCE_INFO, defaultChain } from "./editor";

/**
 * The sources a library reads from, in the order they are asked: each can be
 * moved or dropped, and the others this type allows added at the end. A
 * source whose setting is missing from the environment says so where it is
 * chosen, rather than at the first run.
 *
 * Its buttons stay where the keyboard is: an arrow that stops applying is
 * aria-disabled rather than disabled, so a moved source keeps the focus, and
 * a source added or dropped hands it to what now stands for it.
 */
export function SourceChain({
  type,
  chain,
  isDefault,
  missing,
  onChange,
}: {
  type: MediaType;
  chain: string[];
  /** The chain is the type's default rather than one written in the config. */
  isDefault: boolean;
  /** Environment settings that are not set, e.g. `ARR_METADATA_URL`. */
  missing: string[];
  onChange: (next: string[]) => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  /** The control to focus once the change has been drawn. */
  const focusNext = useRef<string | null>(null);
  const offered = SOURCES[type].filter((name) => !chain.includes(name));

  useEffect(() => {
    if (!focusNext.current) return;
    root.current?.querySelector<HTMLElement>(`[data-focus="${focusNext.current}"]`)?.focus();
    focusNext.current = null;
  }, [chain]);

  const change = (next: string[], focus: string) => {
    focusNext.current = focus;
    onChange(next);
  };
  const move = (at: number, delta: -1 | 1) => {
    const to = at + delta;
    if (to < 0 || to >= chain.length) return;
    const next = [...chain];
    [next[at], next[to]] = [next[to]!, next[at]!];
    onChange(next);
  };

  return (
    <div className="chain" ref={root}>
      <ol className="chain__list" role="list" aria-label="Sources, in the order they are asked">
        {chain.map((name, at) => {
          const info = SOURCE_INFO[name];
          const lacking = info?.needs && missing.includes(info.needs) ? info.needs : null;
          const only = chain.length === 1;
          return (
            <li key={name} className="chain__item">
              <span className="chain__rank mono faint" aria-hidden="true">{at + 1}</span>
              <span className="chain__what">
                <span className="chain__head">
                  <span className="chain__name mono tone-teal">{name}</span>
                  {lacking && (
                    <span className="chip chip--warn" title={`${lacking} is not set in the environment`}>
                      needs {lacking}
                    </span>
                  )}
                </span>
                <span className="chain__hint faint" title={info?.hint}>{info?.hint}</span>
              </span>
              <span className="chain__tools">
                <button
                  type="button"
                  className="iconbtn"
                  data-focus={`earlier:${name}`}
                  aria-label={`Ask ${name} earlier`}
                  aria-disabled={at === 0}
                  onClick={() => move(at, -1)}
                >
                  <ArrowUp size={14} aria-hidden="true" />
                </button>
                <button
                  type="button"
                  className="iconbtn"
                  aria-label={`Ask ${name} later`}
                  aria-disabled={at === chain.length - 1}
                  onClick={() => move(at, 1)}
                >
                  <ArrowDown size={14} aria-hidden="true" />
                </button>
                <button
                  type="button"
                  className="iconbtn"
                  aria-label={only ? `${name} is the only source; a library reads from one at least` : `Stop reading from ${name}`}
                  aria-disabled={only}
                  onClick={() => !only && change(chain.filter((_, j) => j !== at), `add:${name}`)}
                >
                  <X size={14} aria-hidden="true" />
                </button>
              </span>
            </li>
          );
        })}
      </ol>
      <div className="chain__add">
        {offered.map((name) => (
          <button
            key={name}
            type="button"
            className="button button--ghost button--sm"
            data-focus={`add:${name}`}
            aria-label={`Add ${name}: ${SOURCE_INFO[name]?.hint ?? name}`}
            title={SOURCE_INFO[name]?.hint}
            onClick={() => change([...chain, name], `earlier:${name}`)}
          >
            <Plus size={12} aria-hidden="true" /> {name}
          </button>
        ))}
        {isDefault ? (
          <span className="faint chain__default">the default for {type}</span>
        ) : (
          <button
            type="button"
            className="button button--ghost button--sm"
            onClick={() => change(defaultChain(type), `earlier:${defaultChain(type)[0]}`)}
          >
            back to the default
          </button>
        )}
      </div>
    </div>
  );
}
