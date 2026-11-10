"""Where the sun was: elevation from a place and a moment.

The NOAA Solar Calculator's equations (Meeus, as NOAA's spreadsheet sets
them out), as a pure function with no dependency. Accurate to well under a
degree for any date this catalogue will hold — far tighter than the light
classes it feeds, which are several degrees wide.

The classes are photographers' rather than astronomers': golden hour is the
sun below six degrees (long warm light, long shadows), blue hour the
stretch just after it sets. They say when the sun *could* have lit a shot;
whether it did — cloud, haze, rain — is the measured look's job
(media/motion.describe_look).
"""

import math
from datetime import UTC, datetime

LIGHT_CLASSES = ("night", "blue hour", "golden hour", "low sun", "mid sun", "high sun")


def _julian_day(when: datetime) -> float:
    when = when.astimezone(UTC) if when.tzinfo else when.replace(tzinfo=UTC)
    return when.timestamp() / 86400.0 + 2440587.5


def sun_position(when: datetime, lat: float, lon: float) -> tuple[float, float]:
    """(elevation, azimuth) in degrees for a UTC moment at latitude `lat`
    and longitude `lon` (east positive). Elevation includes atmospheric
    refraction, as the sun is seen; azimuth is clockwise from north.
    A naive datetime is taken as UTC."""
    jd = _julian_day(when)
    t = (jd - 2451545.0) / 36525.0  # Julian century

    mean_long = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    mean_anom = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    ecc = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    m = math.radians(mean_anom)
    centre = (
        math.sin(m) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * m) * (0.019993 - 0.000101 * t)
        + math.sin(3 * m) * 0.000289
    )
    true_long = mean_long + centre
    omega = 125.04 - 1934.136 * t
    app_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    mean_obliq = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
    obliq = mean_obliq + 0.00256 * math.cos(math.radians(omega))
    decl = math.degrees(math.asin(math.sin(math.radians(obliq)) * math.sin(math.radians(app_long))))

    y = math.tan(math.radians(obliq / 2)) ** 2
    l0 = math.radians(mean_long)
    eq_time = 4 * math.degrees(
        y * math.sin(2 * l0)
        - 2 * ecc * math.sin(m)
        + 4 * ecc * y * math.sin(m) * math.cos(2 * l0)
        - 0.5 * y * y * math.sin(4 * l0)
        - 1.25 * ecc * ecc * math.sin(2 * m)
    )  # minutes

    utc = when.astimezone(UTC) if when.tzinfo else when
    minutes = utc.hour * 60 + utc.minute + utc.second / 60 + utc.microsecond / 6e7
    true_solar = (minutes + eq_time + 4 * lon) % 1440
    hour_angle = true_solar / 4 - 180 if true_solar >= 0 else true_solar / 4 + 180

    phi = math.radians(lat)
    d = math.radians(decl)
    h = math.radians(hour_angle)
    cos_zen = math.sin(phi) * math.sin(d) + math.cos(phi) * math.cos(d) * math.cos(h)
    zenith = math.degrees(math.acos(max(-1.0, min(1.0, cos_zen))))
    elevation = 90 - zenith

    # Refraction lifts the sun as seen near the horizon (NOAA's piecewise fit).
    if elevation > 85:
        refraction = 0.0
    else:
        te = math.tan(math.radians(elevation))
        if elevation > 5:
            refraction = 58.1 / te - 0.07 / te**3 + 0.000086 / te**5
        elif elevation > -0.575:
            refraction = 1735 + elevation * (
                -518.2 + elevation * (103.4 + elevation * (-12.79 + elevation * 0.711))
            )
        else:
            refraction = -20.772 / te
        refraction /= 3600

    sin_zen = math.sin(math.radians(zenith))
    if abs(sin_zen) < 1e-9:
        azimuth = 180.0
    else:
        cos_az = (math.sin(phi) * math.cos(math.radians(zenith)) - math.sin(d)) / (
            math.cos(phi) * sin_zen
        )
        az = math.degrees(math.acos(max(-1.0, min(1.0, cos_az))))
        azimuth = (az + 180) % 360 if hour_angle > 0 else (540 - az) % 360
    return elevation + refraction, azimuth


def light_class(elevation: float) -> str:
    """A light class for a sun elevation in degrees."""
    if elevation < -6:
        return "night"
    if elevation < -4:
        return "blue hour"
    if elevation < 6:
        return "golden hour"
    if elevation < 20:
        return "low sun"
    if elevation < 45:
        return "mid sun"
    return "high sun"
