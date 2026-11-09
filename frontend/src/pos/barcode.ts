// Same match keys as the server (backend/app/core/barcode.py), so a till offline finds
// an item from any of its 11 / 12 / 13-digit forms.

export function checkDigit(body: string): number {
  let total = 0;
  [...body].reverse().forEach((d, i) => { total += Number(d) * (i % 2 === 0 ? 3 : 1); });
  return (10 - (total % 10)) % 10;
}

const validCheck = (code: string) => Number(code.at(-1)) === checkDigit(code.slice(0, -1));

export function keysFor(raw: string): string[] {
  const code = raw.replace(/\s+/g, "");
  if (!/^\d{11,13}$/.test(code)) return [`X:${code.toUpperCase()}`];
  const keys = new Set<string>();
  if (code.length === 13) keys.add(code.slice(0, 12));
  else if (code.length === 12) {
    keys.add(code);
    if (validCheck(code)) keys.add("0" + code.slice(0, 11));
  } else keys.add("0" + code);
  return [...keys].sort();
}
