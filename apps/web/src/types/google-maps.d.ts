// A deliberately narrow declaration of the Google Maps JS API.
//
// @types/google.maps would be the usual answer, but it is a large surface for
// the handful of calls the places map makes, and adding it means regenerating
// the lockfile. This declares exactly what is used and nothing else, so an
// accidental reach for an untyped part of the API is a compile error rather
// than a silent `any`.

export interface LatLngLiteral {
  lat: number;
  lng: number;
}

export interface GMap {
  panTo(position: LatLngLiteral): void;
  fitBounds(bounds: GLatLngBounds, padding?: number): void;
}

export interface GLatLngBounds {
  extend(position: LatLngLiteral): void;
}

export interface GMarker {
  setMap(map: GMap | null): void;
  addListener(event: string, handler: () => void): void;
}

export interface GoogleMapsApi {
  maps: {
    Map: new (
      element: HTMLElement,
      options: {
        mapTypeId?: string;
        streetViewControl?: boolean;
        mapTypeControl?: boolean;
        fullscreenControl?: boolean;
      },
    ) => GMap;
    Marker: new (options: {
      position: LatLngLiteral;
      map: GMap;
      title?: string;
      label?: { text: string; color?: string; fontSize?: string; fontWeight?: string };
    }) => GMarker;
    LatLngBounds: new () => GLatLngBounds;
  };
}

declare global {
  interface Window {
    google?: GoogleMapsApi;
    /** Shared across components so the script tag is only injected once. */
    __framefoundMapsLoading?: Promise<void>;
  }
}
