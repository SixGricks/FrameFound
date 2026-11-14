// Browse's page arrows, kept free of imports so `npm test` can load them.

/** A Browse query without its page: two queries with the same filters
 *  count the same assets, whatever page each is on. */
export function filtersOf(query: string): string {
  const usp = new URLSearchParams(query);
  usp.delete("page");
  return usp.toString();
}

/**
 * The page one arrow press leads to, from the page `query` is on.
 *
 * `pages` is how many pages these filters have, or null while that is not
 * known: on a reload or a link (nothing counted yet) and after a failed
 * load. Only a known count may hold a press back — with none, "5 / 1" read
 * as page 1 of 1, and ← from page 5 went to page 1 instead of 4.
 */
export function turnTo(query: string, by: number, pages: number | null): number {
  const asked = Number(new URLSearchParams(query).get("page") ?? "1") || 1;
  const wanted = Math.max(1, asked + by);
  // From a page past the end (an old link, fewer assets since), ← lands on
  // the last page there is: the nearest one that shows anything.
  return pages === null ? wanted : Math.min(pages, wanted);
}
