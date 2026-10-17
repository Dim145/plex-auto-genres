import type { ConfigDocument, ConfigView, GenreRules, LibraryRun, MediaType, ValidationIssue } from "../../api/types";

export const TYPES: MediaType[] = ["anime", "standard-tv", "standard-movie"];

export const emptyRules = (): GenreRules => ({
  ignore: [],
  replace: {},
  sortedPrefix: "",
  sortedCollections: [],
  maxGenres: null,
});

export const newLibrary = (library = ""): LibraryRun => ({
  library,
  type: "anime",
  enabled: true,
  providers: null,
  providerMode: "fallback",
  useGenres: false,
  useKeywords: false,
  clearGenres: false,
  setPosters: false,
  sortCollections: false,
  rateAnime: false,
  createRatingCollections: false,
  overrides: null,
});

export function toDocument(view: ConfigView): ConfigDocument {
  return { version: view.version, defaults: view.defaults, libraries: view.libraries, schedule: view.schedule };
}

/** Common schedules; anything else is "custom". */
export const SCHEDULE_PRESETS: { key: string; label: string; cron: string }[] = [
  { key: "night1", label: "01:00 nightly", cron: "0 1 * * *" },
  { key: "night4", label: "04:00 nightly", cron: "0 4 * * *" },
  { key: "6h", label: "every 6 h", cron: "0 */6 * * *" },
  { key: "weekly", label: "Sun 03:00", cron: "0 3 * * 0" },
];

/** Structural equality; objects are built in a stable key order so this is enough. */
export const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

export const rulesEmpty = (r: GenreRules | null | undefined) =>
  !r ||
  (r.ignore.length === 0 &&
    Object.keys(r.replace).length === 0 &&
    !r.sortedPrefix &&
    r.sortedCollections.length === 0 &&
    r.maxGenres == null);

const locEq = (a: (string | number)[], b: (string | number)[]) =>
  a.length === b.length && a.every((v, i) => v === b[i]);

const locStartsWith = (loc: (string | number)[], prefix: (string | number)[]) =>
  loc.length >= prefix.length && prefix.every((v, i) => v === loc[i]);

/** The message attached exactly to `loc`, if any. */
export const issueAt = (errors: ValidationIssue[], loc: (string | number)[]) =>
  errors.find((e) => locEq(e.loc, loc))?.msg;

/** Every issue at or below `prefix`. */
export const issuesUnder = (errors: ValidationIssue[], prefix: (string | number)[]) =>
  errors.filter((e) => locStartsWith(e.loc, prefix));

/** Issues at `prefix` itself (model-level), not at one of its fields. */
export const issuesOwn = (errors: ValidationIssue[], prefix: (string | number)[]) =>
  errors.filter((e) => locEq(e.loc, prefix));

/** Human label for an issue location, for the summary in the save bar. */
export function describeLoc(loc: (string | number)[], doc: ConfigDocument): string {
  if (loc[0] === "libraries" && typeof loc[1] === "number") {
    const name = doc.libraries[loc[1]]?.library || `library ${loc[1] + 1}`;
    const field = loc.slice(2).join(".");
    return field ? `${name} · ${field}` : name;
  }
  if (loc[0] === "defaults") return `defaults · ${loc.slice(1).join(".")}`;
  return loc.join(".") || "config";
}

/** What a library of each type can read from, in the order they are offered. */
export const SOURCES: Record<MediaType, string[]> = {
  anime: ["jikan", "anilist", "ams", "tmdb"],
  "standard-tv": ["tmdb", "ams"],
  "standard-movie": ["tmdb", "ams"],
};

/** Each source in a line, and the setting it cannot run without. */
export const SOURCE_INFO: Record<string, { hint: string; needs?: string }> = {
  jikan: { hint: "MyAnimeList, through Jikan" },
  anilist: { hint: "AniList, with its community tags" },
  ams: { hint: "Your arr-metadata-server: its catalogue, corrections included", needs: "ARR_METADATA_URL" },
  tmdb: { hint: "The Movie Database. For anime, its own genres, not MyAnimeList's", needs: "TMDB_API_KEY" },
};

/** What a library reads when its chain is left unset. */
export const defaultChain = (type: MediaType): string[] => (type === "anime" ? ["jikan"] : ["tmdb"]);

/**
 * Sources with something finer than genres, which `useKeywords` asks for.
 * Mirrors `Provider.has_keywords`; the server refuses the combination anyway,
 * so the worst a drift here can do is show or hide one toggle.
 */
export const KEYWORD_SOURCES = ["anilist", "tmdb"];

/** What a library actually reads, the way the server resolves it. */
export const sourceChain = (providers: string[] | null, type: MediaType): string[] =>
  providers ?? defaultChain(type);

/**
 * A chain as the config keeps it: null -- "the default" -- when it says
 * nothing the default would not, so the file follows a future default.
 */
export function storedChain(chain: string[], type: MediaType): string[] | null {
  const fallback = defaultChain(type);
  const same = chain.length === fallback.length && chain.every((p, i) => p === fallback[i]);
  return same ? null : chain;
}

// -- JSON Schema access (help text and enums stay in step with the models) ----

interface SchemaProperty {
  description?: string;
  type?: string;
  enum?: string[];
  default?: unknown;
}

export interface JsonSchemaLike {
  $defs?: Record<string, { properties?: Record<string, SchemaProperty> }>;
}

export type Help = (def: "LibraryRun" | "GenreRules" | "ScheduleSettings", field: string) => string | undefined;

export const helpFrom =
  (schema: JsonSchemaLike | undefined): Help =>
  (def, field) =>
    schema?.$defs?.[def]?.properties?.[field]?.description;
