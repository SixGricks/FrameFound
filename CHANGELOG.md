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

### Fixed
- FFmpeg's thread cap reached only the decoder. `-threads` is a per-file
  option, so inserting it before `-i` left x264 running one thread per core on
  every proxy transcode while the setting appeared to be in force — measured at
  810 MB against 470 MB once actually applied.
