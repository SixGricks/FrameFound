"use client";

// Google Maps configuration. Lives on the Security page because turning it on
// is an outbound-traffic decision, not a display preference: tile requests
// tell Google roughly where you are looking, and a geocode hands over an exact
// coordinate. Both are off until someone chooses them.
//
// Keys are written but never read back — the field always starts empty and
// the card reports only whether a key is stored.

import { useEffect, useState } from "react";

import { api, type MapsSettings } from "@/lib/api";

export default function MapsSettingsCard() {
  const [settings, setSettings] = useState<MapsSettings | null>(null);
  const [browserKey, setBrowserKey] = useState("");
  const [geocodingKey, setGeocodingKey] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.mapsSettings().then(setSettings).catch(() => setSettings(null));
  }, []);

  async function save(patch: Parameters<typeof api.updateMapsSettings>[0], message: string) {
    setBusy(true);
    try {
      setSettings(await api.updateMapsSettings(patch));
      setNote(message);
      setTimeout(() => setNote(null), 4000);
    } catch (err) {
      setNote(err instanceof Error ? err.message : "Could not save");
    } finally {
      setBusy(false);
    }
  }

  if (!settings) return null;

  return (
    <>
      <div className="sectionhead">
        <h2>Maps &amp; geocoding</h2>
        <a
          className="navlink"
          href="https://github.com/SixGricks/FrameFound/blob/main/docs/maps.md"
          target="_blank"
          rel="noreferrer noopener"
        >
          Setup guide ↗
        </a>
        {note && <span className="faint">{note}</span>}
      </div>

      <div className="card">
        <p className="faint" style={{ marginTop: 0, fontSize: "0.86rem" }}>
          Both are optional and off by default. Enabling them sends data to
          Google: map tiles reveal roughly where you are looking, and an address
          lookup sends an exact coordinate. Places works without either — it
          draws positions locally and names shoots from your folder names.
        </p>
        <p className="faint" style={{ fontSize: "0.86rem" }}>
          You need <strong>two</strong> keys from the Google Cloud console, not
          one: a browser key restricted by HTTP referrer, and a geocoding key
          restricted by IP. One key cannot carry both restrictions — a referrer
          rule breaks server-side lookups. The{" "}
          <a
            href="https://github.com/SixGricks/FrameFound/blob/main/docs/maps.md"
            target="_blank"
            rel="noreferrer noopener"
          >
            setup guide
          </a>{" "}
          walks through it, including cost and how to switch providers later.
        </p>

        <label className="field">
          <span>Basemap provider</span>
          <select
            className="select"
            value={settings.provider}
            onChange={(e) =>
              save(
                { provider: e.target.value as MapsSettings["provider"] },
                "Provider changed",
              )
            }
          >
            <option value="none">None — draw positions locally</option>
            <option value="maplibre">Self-hosted tiles (OpenMapTiles / Protomaps)</option>
            <option value="google">Google Maps</option>
          </select>
          <small className="faint">
            Self-hosted tiles are the recommended option: MapLibre renders from
            a style URL you control, so no third party sees where you are
            looking and there is no key and no bill.
          </small>
        </label>

        {settings.provider === "maplibre" && (
          <>
            <label className="field">
              <span>Tile style URL</span>
              <input
                className="input"
                placeholder="https://tiles.your-nas.local/style.json"
                defaultValue={settings.style_url}
                onBlur={(e) =>
                  e.target.value !== settings.style_url &&
                  save({ style_url: e.target.value }, "Style saved")
                }
              />
              <small className="faint">
                A MapLibre style JSON. Point it at your own OpenMapTiles or
                Protomaps server — that single URL decides where every tile
                comes from.
              </small>
            </label>
            <label className="field">
              <span>MapLibre library URL</span>
              <input
                className="input"
                defaultValue={settings.library_url}
                onBlur={(e) =>
                  e.target.value !== settings.library_url &&
                  save({ library_url: e.target.value }, "Library URL saved")
                }
              />
              <small className="faint">
                Defaults to a CDN. Change it to a path on this server for an
                install with no internet access at all.
              </small>
            </label>
          </>
        )}

        {settings.provider === "google" && (
        <label className="field">
          <span>Maps browser key</span>
          <input
            className="input"
            type="password"
            autoComplete="off"
            placeholder={
              settings.browser_key_configured ? "•••••• stored — type to replace" : "Not set"
            }
            value={browserKey}
            onChange={(e) => setBrowserKey(e.target.value)}
          />
          <small className="faint">
            Used by the page to load the map, so it is visible to anyone signed
            in. Restrict it by HTTP referrer in the Google Cloud console.
          </small>
        </label>
        )}

        <label className="field">
          <span>Geocoding key</span>
          <input
            className="input"
            type="password"
            autoComplete="off"
            placeholder={
              settings.geocoding_key_configured ? "•••••• stored — type to replace" : "Not set"
            }
            value={geocodingKey}
            onChange={(e) => setGeocodingKey(e.target.value)}
          />
          <small className="faint">
            Used only from the server for address lookups, so it never reaches a
            browser. Restrict it by IP. Keep it separate from the browser key —
            one key cannot carry both restrictions.
          </small>
        </label>

        <div className="toolbar" style={{ paddingLeft: 0 }}>
          <button
            className="btn"
            disabled={busy || (!browserKey && !geocodingKey)}
            onClick={() => {
              const patch: Record<string, string> = {};
              if (browserKey) patch.browser_key = browserKey;
              if (geocodingKey) patch.geocoding_key = geocodingKey;
              save(patch, "Keys saved").then(() => {
                setBrowserKey("");
                setGeocodingKey("");
              });
            }}
          >
            Save keys
          </button>
          {(settings.browser_key_configured || settings.geocoding_key_configured) && (
            <button
              className="btn"
              disabled={busy}
              onClick={() =>
                save(
                  { browser_key: "", geocoding_key: "", basemap_enabled: false },
                  "Keys removed",
                )
              }
            >
              Remove both keys
            </button>
          )}
        </div>

        <label className="field" data-layout="row">
          <input
            type="checkbox"
            checked={settings.basemap_enabled}
            disabled={busy || !settings.browser_key_configured}
            onChange={(e) =>
              save(
                { basemap_enabled: e.target.checked },
                e.target.checked ? "Basemap on" : "Basemap off",
              )
            }
          />
          <span>
            Show a Google basemap on Places
            {!settings.browser_key_configured && (
              <span className="faint"> — needs a browser key first</span>
            )}
          </span>
        </label>

        <label className="field" data-layout="row">
          <input
            type="checkbox"
            checked={settings.geocode_unnamed_places}
            disabled={busy || !settings.geocoding_key_configured}
            onChange={(e) =>
              save(
                { geocode_unnamed_places: e.target.checked },
                e.target.checked ? "Address lookup on" : "Address lookup off",
              )
            }
          />
          <span>
            Look up an address for places your folders don&rsquo;t name
            {!settings.geocoding_key_configured && (
              <span className="faint"> — needs a geocoding key first</span>
            )}
          </span>
        </label>
      </div>
    </>
  );
}
