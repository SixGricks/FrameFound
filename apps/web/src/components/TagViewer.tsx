"use client";

// The Tags page's viewer: the shared MediaPreview, with its line under the
// question allowed to run onto a second line.
//
// That line says who suggested the picture, how sure it was and, for a
// local model, which model — "… · suggested by local model (made up) · 93% ·
// gemma-4-e4b-it-4bit". The shared viewer keeps its line to one row and cuts
// it short, which suits a filename. QA found that on a phone it cut this one
// after "suggested by local", so a touch user, who has no tooltip, could not
// read the very thing the line is there to say. The wrapping is scoped to this
// viewer (`.tag-viewer` in globals.css) and holds at every width, since a
// tablet held upright cut it too (every other viewer wraps it on a phone
// only). The line's full text as a title is MediaPreview's own now.

import MediaPreview, { type MediaPreviewProps } from "@/components/MediaPreview";

export default function TagViewer(props: MediaPreviewProps) {
  return (
    // `display: contents`: the box is only a hook for the style, never a
    // box on the page.
    <div className="tag-viewer">
      <MediaPreview {...props} />
    </div>
  );
}
