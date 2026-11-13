/** Node and entry ids made by the browser: 32 hex digits (contract 5.3). */
export function newId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes); // available on plain-HTTP pages, unlike crypto.randomUUID
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}
