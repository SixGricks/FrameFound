// A deliberately narrow declaration of the MapLibre GL JS API.
//
// Loaded at runtime from a configurable URL rather than bundled, so an install
// with no internet can serve it from its own origin. That also means no npm
// dependency and no lockfile churn — the same trade as the Google types.

export interface MlLngLat {
  lng: number;
  lat: number;
}

export interface MlMap {
  addControl(control: object, position?: string): void;
  fitBounds(bounds: [[number, number], [number, number]], options?: object): void;
  flyTo(options: { center: [number, number]; zoom?: number }): void;
  remove(): void;
  on(event: string, handler: () => void): void;
}

export interface MlMarker {
  setLngLat(pos: [number, number]): MlMarker;
  addTo(map: MlMap): MlMarker;
  remove(): void;
  getElement(): HTMLElement;
}

export interface MapLibreApi {
  Map: new (options: {
    container: HTMLElement;
    style: string;
    center?: [number, number];
    zoom?: number;
    attributionControl?: boolean;
  }) => MlMap;
  Marker: new (options?: { element?: HTMLElement; color?: string }) => MlMarker;
  NavigationControl: new () => object;
}

declare global {
  interface Window {
    maplibregl?: MapLibreApi;
    __framefoundMapLibreLoading?: Promise<void>;
  }
}
