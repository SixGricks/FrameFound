"use client";

// A face, cropped out of the frame it was found in.
//
// The crop is computed by the API and served as a square JPEG. This used to be
// done here in CSS and it was wrong in two independent ways: it cropped from
// the *asset's* thumbnail rather than the frame the face was detected in — so
// any face found partway through a video showed unrelated content — and the
// tile's `object-fit: cover` cropped the image before the box maths applied,
// so the normalised coordinates no longer referred to what was on screen.
//
// Server-side, the client needs to know no geometry at all, and it downloads a
// small square thumbnail per face instead of a full frame image. Still nothing
// is stored: the crop is computed per request from a frame already on disk.

import { faceCropUrl, type FaceRef } from "@/lib/api";

export default function FaceCrop({
  face,
  size = 92,
}: {
  face: FaceRef;
  size?: number;
}) {
  return (
    <div
      className="facecrop"
      style={{ width: size, height: size }}
      title={face.filename}
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        // Twice the displayed size, so it stays sharp on a high-density screen.
        src={faceCropUrl(face.face_id, Math.min(512, size * 2))}
        alt=""
        loading="lazy"
        decoding="async"
      />
    </div>
  );
}
