"use client";

// A face, cropped from the frame it was found in.
//
// No crop is stored or served. The frame is already available through the
// media endpoint, so the box is applied here with CSS — scaling the frame up
// and shifting it so the face fills the square. Keeping a second copy of
// everyone's face on disk would double the most sensitive data in the system
// for no benefit.

import { mediaUrl, type FaceRef } from "@/lib/api";

export default function FaceCrop({
  face,
  size = 92,
}: {
  face: FaceRef;
  size?: number;
}) {
  // A little air around the box: face detectors crop tight to the features,
  // and a portrait with no forehead is hard to recognise.
  const pad = 0.35;
  const w = Math.min(1, face.box_w * (1 + pad * 2));
  const h = Math.min(1, face.box_h * (1 + pad * 2));
  const x = Math.max(0, face.box_x - face.box_w * pad);
  const y = Math.max(0, face.box_y - face.box_h * pad);

  // Zoom so the padded box fills the tile, then offset to bring it into view.
  const scale = 1 / Math.max(w, h);

  return (
    <div
      className="facecrop"
      style={{ width: size, height: size }}
      title={face.filename}
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={mediaUrl(face.asset_id, "thumbnail")}
        alt=""
        loading="lazy"
        decoding="async"
        style={{
          transform: `scale(${scale}) translate(${-x * 100}%, ${-y * 100}%)`,
          transformOrigin: "top left",
        }}
      />
    </div>
  );
}
