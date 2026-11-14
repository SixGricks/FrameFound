// Which key presses a page-level shortcut must leave alone.
//
// Tags and People answer a selection with bare Y / N and clear it with Esc,
// listening on the window. A key typed into a field is not a shortcut, so
// both skipped any event whose target was an <input>. That was one rule too
// wide: when selecting on Tags moved from the picture to a tick box
// (2026-11-12), a click on the box left focus on it — an <input> — and the
// documented "tick a few, press Y" did nothing until the reader clicked
// somewhere else. A tick box takes Space and nothing else; Y, N and Esc mean
// nothing to it.
//
// No imports and no DOM classes, so `node --test` can load this file as it
// is (tests/keys.test.mjs).

// <input> types that are pressed or picked, not typed into. Anything else —
// text, search, number, date, and any type a browser does not know, which it
// treats as text — is typing. A slider is left out on purpose: it takes no
// letters, but its arrows are its own.
const NOT_TYPED_INTO = new Set([
  "button",
  "checkbox",
  "color",
  "file",
  "hidden",
  "image",
  "radio",
  "reset",
  "submit",
]);

/**
 * Whether a key event's target is somewhere the reader types, so a
 * single-letter shortcut must not act on it: a text field, a text area, a
 * drop-down (letters jump to an option), or anything editable in place.
 * False for a tick box or a button, and for a target that is not an element.
 */
export function typingInto(target: EventTarget | null): boolean {
  const el = target as {
    tagName?: unknown;
    type?: unknown;
    isContentEditable?: unknown;
  } | null;
  if (!el || typeof el.tagName !== "string") return false;
  if (el.isContentEditable === true) return true;
  const tag = el.tagName.toUpperCase();
  if (tag === "TEXTAREA" || tag === "SELECT") return true;
  if (tag !== "INPUT") return false;
  const type = typeof el.type === "string" ? el.type.toLowerCase() : "text";
  return !NOT_TYPED_INTO.has(type);
}
