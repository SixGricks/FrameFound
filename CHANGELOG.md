# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Slideshow rendering: propose a themed selection, review it, render an MP4.
  Ken Burns per still, theme colour grade, crossfades, optional operator-supplied
  music. `slideshows` table (migration 0013), `/api/v1/slideshows`, and a
  Slideshows page. Assembled from per-slide pieces and stitched with the concat
  demuxer, so peak memory does not grow with the length of the slideshow —
  see [docs/video-generation.md](docs/video-generation.md) for the measurements
  that ruled out the single-filter-graph design.
- Manage → Basemaps page: download, size and delete for offline map regions.
  The API already existed; only the UI was missing.
- Milestone 0: repository scaffolding, architecture docs, ADRs, threat model,
  Docker Compose stack definition, backend and frontend skeletons, CI pipeline.

- Autocomplete on tag entry and person naming. Typing surfaces what already
  exists, so a second "power broom" tag or a fourth person called "Dad" can be
  merged at the moment of creation rather than discovered later. Naming a
  person offers a **Merge into** action per match.

- Editing panels for Adobe Premiere Pro (UXP) and Lightroom Classic (Lua SDK),
  plus the `/api/v1/panel` surface they share and scoped, revocable **panel
  tokens** managed under Security. Results carry a path already translated for
  the workstation through the `path_mappings` profiles, which had existed since
  Milestone 2 with nothing consuming them. See [docs/panels.md](docs/panels.md).

### Fixed
- Face thumbnails showed unrelated content. Faces are detected in *sampled
  frames*, but the grid cropped from the asset's thumbnail — a different
  picture — so any face found partway through a video showed whatever was at
  that spot in an unrelated frame (161 of 307 faces on the reference install).
  Compounding it, `object-fit: cover` cropped the tile before the box maths
  applied. Crops are now cut server-side from the correct frame; nothing is
  stored, they are computed per request.
- Faces below 40px are no longer embedded. ArcFace upscales to 112×112, so a
  seven-pixel face produced an embedding that was almost entirely
  interpolation and quietly polluted every cluster it joined.
- FFmpeg's thread cap reached only the decoder. `-threads` is a per-file
  option, so inserting it before `-i` left x264 running one thread per core on
  every proxy transcode while the setting appeared to be in force — measured at
  810 MB against 470 MB once actually applied.
