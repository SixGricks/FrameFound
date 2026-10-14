/** Copy text, on plain HTTP too.
 *
 *  `navigator.clipboard` exists only in a secure context — https, or
 *  localhost — and this app recommends reaching the server over plain http on
 *  the LAN or a tailnet. There, every Copy button did nothing (two of them
 *  threw), including the one on a panel token that is shown exactly once.
 *  Resolves to whether the copy happened, so the caller can say so. */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* insecure context or permission refused: fall through */
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const copied = document.execCommand("copy");
    document.body.removeChild(area);
    return copied;
  } catch {
    return false;
  }
}
