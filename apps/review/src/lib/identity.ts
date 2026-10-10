/**
 * Anonymous browser identifier for navigation preferences only. It does not identify a person and does not
 * follow them to another browser or computer.
 */
const KEY = 'annotation.browserId';
let fallback: string | null = null;

export function browserId(): string {
  try {
    let id = localStorage.getItem(KEY);
    if (!id) {
      id = crypto.randomUUID();
      localStorage.setItem(KEY, id);
    }
    return id;
  } catch {
    fallback ??= crypto.randomUUID();
    return fallback;
  }
}

/** A fresh identity for every editor instance, including reloads, so tabs contend for the lease. */
export const newTabId = () => crypto.randomUUID();
