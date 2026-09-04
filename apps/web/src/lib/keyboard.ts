/**
 * Keyboard shortcut helpers shared by the shell's global bindings.
 */

/**
 * True when a keydown originated inside a text-editing surface, where bare
 * single-key shortcuts (`/`, `[`, `c`) must not fire.
 */
export function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  const tag = target.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable;
}

/** Cmd on macOS, Ctrl elsewhere, with Alt excluded so AltGr chords pass through. */
export function isModifierChord(event: KeyboardEvent): boolean {
  return (event.metaKey || event.ctrlKey) && !event.altKey;
}
