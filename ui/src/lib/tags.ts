/**
 * Near enough the server's `fold` for a hint: case, accents, punctuation and
 * spacing aside. `toLowerCase` is not a casefold, so the two letters that
 * matter most in genre names are mapped by hand ("Straße" meets "Strasse").
 */
export const foldName = (name: string) =>
  name
    .normalize("NFKD")
    .toLowerCase()
    .replace(/ß/g, "ss")
    .replace(/ς/g, "σ")
    .replace(/[^\p{L}\p{N}]/gu, "");

/** The name without the prefix the app writes, when it has it. */
export const bareName = (name: string, prefix: string) =>
  prefix && name.startsWith(prefix) && name !== prefix ? name.slice(prefix.length) : name;

/** What two names for one tag share, prefix or not: the writer matches them this way. */
export const tagKey = (name: string, prefix: string) => foldName(bareName(name, prefix));

/** First spelling of each name under `key`, in order, in one pass. */
export function uniqueBy(names: string[], key: (name: string) => string): string[] {
  const seen = new Set<string>();
  return names.filter((name) => {
    const k = key(name);
    if (!k || seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}
