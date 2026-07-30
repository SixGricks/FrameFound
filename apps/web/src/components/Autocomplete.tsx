"use client";

// A text field that looks things up as you type.
//
// Shared by tag entry and person naming because both exist to answer the same
// question at the same moment: *does this already exist?* A catalogue that
// only tells you after the fact accumulates four tags called "power broom" and
// four people called "Dad", each with a fraction of the evidence and none of
// them the one you wanted.
//
// Deliberately does not auto-select. The suggestion is offered, never applied
// — typing a new name that happens to share a prefix with an old one must not
// silently become the old one.

import { useCallback, useEffect, useRef, useState } from "react";

// Long enough that the request follows the typist rather than each keystroke,
// short enough that the list is there by the time they stop to look.
const DEBOUNCE_MS = 180;

export interface Suggestion {
  id: string;
  label: string;
  hint?: string;
  exact?: boolean;
}

export default function Autocomplete({
  value,
  onChange,
  onPick,
  fetcher,
  placeholder,
  ariaLabel,
  disabled = false,
  minChars = 1,
  renderExtra,
}: {
  value: string;
  onChange: (next: string) => void;
  /** Chose an existing thing, rather than typing a new one. */
  onPick: (suggestion: Suggestion) => void;
  fetcher: (query: string) => Promise<Suggestion[]>;
  placeholder?: string;
  ariaLabel: string;
  disabled?: boolean;
  minChars?: number;
  /** Extra controls per row — "merge into this one", and the like. */
  renderExtra?: (suggestion: Suggestion) => React.ReactNode;
}) {
  const [items, setItems] = useState<Suggestion[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const box = useRef<HTMLDivElement | null>(null);
  // Every request is tagged; only the newest may write. Without this a slow
  // early request can land after a fast later one and repopulate the list with
  // matches for a prefix the operator has already typed past.
  const latest = useRef(0);

  const look = useCallback(
    async (query: string) => {
      const ticket = ++latest.current;
      if (query.trim().length < minChars) {
        if (ticket === latest.current) setItems([]);
        return;
      }
      try {
        const found = await fetcher(query);
        if (ticket === latest.current) {
          setItems(found);
          setActive(-1);
        }
      } catch {
        if (ticket === latest.current) setItems([]);
      }
    },
    [fetcher, minChars],
  );

  useEffect(() => {
    const timer = setTimeout(() => look(value), DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [value, look]);

  // Close when focus or the pointer leaves entirely.
  useEffect(() => {
    function away(event: MouseEvent) {
      if (box.current && !box.current.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, []);

  const visible = open && items.length > 0;

  function onKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (!visible) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((i) => (i + 1) % items.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((i) => (i <= 0 ? items.length - 1 : i - 1));
    } else if (event.key === "Enter" && active >= 0) {
      // Only when something is highlighted. Otherwise Enter submits the form,
      // which is how a genuinely new name gets created.
      const picked = items[active];
      if (!picked) return;
      event.preventDefault();
      onPick(picked);
      setOpen(false);
    } else if (event.key === "Escape") {
      setOpen(false);
    }
  }

  return (
    <div className="autocomplete" ref={box}>
      <input
        className="input"
        style={{ width: "100%" }}
        value={value}
        placeholder={placeholder}
        aria-label={ariaLabel}
        maxLength={120}
        disabled={disabled}
        autoComplete="off"
        role="combobox"
        aria-expanded={visible}
        aria-controls="autocomplete-list"
        onChange={(e) => {
          onChange(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKeyDown}
      />
      {visible && (
        <ul className="autocomplete-list" id="autocomplete-list" role="listbox">
          {items.map((item, index) => (
            <li key={item.id} role="option" aria-selected={index === active}>
              <div className="autocomplete-row" data-active={index === active}>
                <button
                  type="button"
                  className="autocomplete-pick"
                  onMouseEnter={() => setActive(index)}
                  onClick={() => {
                    onPick(item);
                    setOpen(false);
                  }}
                >
                  <span>{item.label}</span>
                  {item.hint && <span className="faint mono">{item.hint}</span>}
                  {item.exact && <span className="pill">exists</span>}
                </button>
                {renderExtra?.(item)}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
