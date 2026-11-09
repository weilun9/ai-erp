import { useEffect, useRef, useState } from "react";

// Type-ahead search box: types -> waits 250 ms -> asks `search` -> shows up to 8 hits.
// Enter picks the only hit, or hands the raw text to `onEnter` (a scanned barcode).

interface Props<T> {
  search: (q: string) => Promise<T[]>;
  label: (row: T) => string;
  onPick: (row: T) => void;
  onEnter?: (text: string) => Promise<boolean>;  // true = handled, clear the box
  placeholder?: string;
  initial?: string;
  keepText?: boolean;          // party pickers show the chosen name; item pickers clear
  disabled?: boolean;
  autoFocus?: boolean;
  id?: string;
}

export function Picker<T extends { id: string }>(p: Props<T>) {
  const [text, setText] = useState(p.initial ?? "");
  const [hits, setHits] = useState<T[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const typed = useRef(false);
  const search = p.search;

  useEffect(() => {
    setText(p.initial ?? "");
  }, [p.initial]);
  useEffect(() => {
    if (!typed.current || !text.trim()) {
      setHits([]);
      return;
    }
    let live = true;
    const timer = setTimeout(() => {
      search(text.trim())
        .then((rows) => {
          if (live) {
            setHits(rows.slice(0, 8));
            setActive(0);
            setOpen(true);
          }
        })
        .catch(() => live && setHits([]));
    }, 250);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [text, search]);

  const pick = (row: T) => {
    typed.current = false;
    setOpen(false);
    setHits([]);
    setText(p.keepText ? p.label(row) : "");
    p.onPick(row);
  };

  return (
    <div className="picker">
      <input
        id={p.id}
        className="input"
        value={text}
        disabled={p.disabled}
        autoFocus={p.autoFocus}
        placeholder={p.placeholder}
        autoComplete="off"
        onChange={(e) => {
          typed.current = true;
          setText(e.target.value);
        }}
        onFocus={() => hits.length && setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        onKeyDown={async (e) => {
          if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(a + 1, hits.length - 1)); }
          else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
          else if (e.key === "Escape") setOpen(false);
          else if (e.key === "Enter") {
            e.preventDefault();
            if (open && hits[active]) return pick(hits[active]);
            const q = text.trim();
            if (!q) return;
            if (p.onEnter && (await p.onEnter(q))) {
              typed.current = false;
              setText("");
              setHits([]);
              return;
            }
            // Enter before the type-ahead answered: search now, take a single hit directly
            const rows = await search(q).catch(() => [] as T[]);
            if (rows.length === 1) return pick(rows[0]);
            setHits(rows.slice(0, 8));
            setActive(0);
            setOpen(rows.length > 0);
          }
        }}
      />
      {open && hits.length > 0 && (
        <ul className="picker-list" role="listbox">
          {hits.map((h, i) => (
            <li key={h.id} role="option" aria-selected={i === active}
                className={i === active ? "on" : ""}
                onMouseDown={(e) => { e.preventDefault(); pick(h); }}>
              {p.label(h)}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
