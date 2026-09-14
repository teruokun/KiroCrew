/**
 * The ONE `/api/file-read` read for the side panel, and the ONE cache entry it
 * lives in.
 *
 * Three callers open a file tab's content: `usePanelDocumentActions.openFile`
 * (chip / tree click), `ChatPage`'s cold-tab hydration (after a reload), and
 * `MarkdownPanel`'s own disk refresh (Refresh, Cancel, the file watch). They
 * MUST agree on two things or the tab ends up describing a file that no longer
 * matches it:
 *
 *  1. **The result shape**, because they share the `['file-read', path]` cache
 *     entry. A caller that stores a different shape makes the next reader's
 *     fields `undefined`.
 *  2. **The binary verdict**, because it decides whether the panel offers an
 *     editor at all. A refresh that updates the tab but leaves the cache saying
 *     "text" is the worst case: closing and reopening the tab inside the cache
 *     window re-hydrates the stale verdict, the editor comes back over bytes it
 *     cannot represent, and a save overwrites them.
 *
 * So the fetch and the key live here, and every caller goes through both.
 */
import { fileReadUrl } from './fileReadUrl'

/** What every `['file-read', path]` cache entry holds. */
export interface FileReadResult {
  /** The file's text. `''` when the read failed and when the file is binary —
   *  a binary response's body is an envelope, never the file. */
  text: string
  ok: boolean
  status: number
  /** The backend's own verdict for the bytes THIS read saw (`X-File-Binary`).
   *  Read from the header, not the body shape: a `.json` text file is served as
   *  `application/json` too, so the content type cannot tell them apart. */
  binary: boolean
}

/** The entry a caller writes when it has read the file OUTSIDE React Query and
 *  wants the cache to agree. Success only: a failed read leaves the entry alone,
 *  because "unknown" must not overwrite a verdict that was known. */
export function fileReadResultFor(text: string, binary: boolean): FileReadResult {
  return { text, ok: true, status: 200, binary }
}

/** The shared cache key. Never spell `['file-read', path]` by hand. */
export function fileReadQueryKey(filePath: string): readonly [string, string] {
  return ['file-read', filePath]
}

/** Read a file for a panel tab. Never throws for an HTTP status — `ok` carries
 *  it, so each caller decides what a 404 means for its own surface (a
 *  placeholder tab on open, a reported failure on refresh). A transport failure
 *  still rejects, which React Query surfaces as the query's error. */
export async function fetchFileRead(
  filePath: string,
  signal?: AbortSignal,
): Promise<FileReadResult> {
  const url = fileReadUrl(filePath)
  const res = signal ? await fetch(url, { signal }) : await fetch(url)
  const binary = res.headers.get('X-File-Binary') === 'true'
  return {
    text: res.ok && !binary ? await res.text() : '',
    ok: res.ok,
    status: res.status,
    binary,
  }
}
