__author__ = 'pypkjs'

"""Emulates the phone's weather provider.

The real mobile app pushes weather forecasts for the user's weather locations
into the watch's WeatherDB blob database, where the firmware weather service
makes them available to the system UI and, since SDK revision 107, to
third-party apps via ``weather_service_peek()``/``weather_service_subscribe()``.

This module reproduces that behaviour for the emulator: it geolocates the host
(GeoIP, same mechanism as ``navigator.geolocation``), fetches a forecast with
today's sunrise/sunset from the free Open-Meteo API, and inserts a
WeatherDBEntry (version 4) plus the ``weatherApp`` location-ordering pref into
the watch over the BlobDB protocol.
"""

import gevent
import logging
import os
import os.path
import struct
import time
import uuid

import pygeoip
import requests

logger = logging.getLogger("pypkjs.weather")

WEATHER_DB_ID = 0x05        # BlobDBIdWeather
WATCH_APP_PREFS_DB_ID = 0x09  # BlobDBIdWatchAppPrefs
WEATHER_DB_VERSION = 4

# WeatherType values from PebbleOS weather_type_tuples.def
PARTLY_CLOUDY = 0
CLOUDY_DAY = 1
LIGHT_SNOW = 2
LIGHT_RAIN = 3
HEAVY_RAIN = 4
HEAVY_SNOW = 5
GENERIC = 6
SUN = 7
RAIN_AND_SNOW = 8

# WMO weather interpretation codes (Open-Meteo `weather_code`) -> (WeatherType, phrase)
_WMO_CODES = {
    0: (SUN, "Clear"),
    1: (SUN, "Mainly Clear"),
    2: (PARTLY_CLOUDY, "Partly Cloudy"),
    3: (CLOUDY_DAY, "Overcast"),
    45: (CLOUDY_DAY, "Fog"),
    48: (CLOUDY_DAY, "Rime Fog"),
    51: (LIGHT_RAIN, "Light Drizzle"),
    53: (LIGHT_RAIN, "Drizzle"),
    55: (LIGHT_RAIN, "Heavy Drizzle"),
    56: (RAIN_AND_SNOW, "Freezing Drizzle"),
    57: (RAIN_AND_SNOW, "Freezing Drizzle"),
    61: (LIGHT_RAIN, "Light Rain"),
    63: (LIGHT_RAIN, "Rain"),
    65: (HEAVY_RAIN, "Heavy Rain"),
    66: (RAIN_AND_SNOW, "Freezing Rain"),
    67: (RAIN_AND_SNOW, "Freezing Rain"),
    71: (LIGHT_SNOW, "Light Snow"),
    73: (LIGHT_SNOW, "Snow"),
    75: (HEAVY_SNOW, "Heavy Snow"),
    77: (LIGHT_SNOW, "Snow Grains"),
    80: (LIGHT_RAIN, "Light Showers"),
    81: (LIGHT_RAIN, "Showers"),
    82: (HEAVY_RAIN, "Heavy Showers"),
    85: (LIGHT_SNOW, "Snow Showers"),
    86: (HEAVY_SNOW, "Snow Showers"),
    95: (HEAVY_RAIN, "Thunderstorm"),
    96: (HEAVY_RAIN, "Thunderstorm"),
    99: (HEAVY_RAIN, "Thunderstorm"),
}

MAX_LOCATION_NAME = 64
MAX_SHORT_PHRASE = 32


class _RawKey(object):
    """Duck-types uuid.UUID for BlobDBClient, which only reads `.bytes`."""
    def __init__(self, key_bytes):
        self.bytes = key_bytes


class PebbleWeather(object):
    # Stable key for the emulated "current location" weather entry.
    LOCATION_UUID = uuid.uuid5(uuid.NAMESPACE_DNS, 'weather.qemu.pypkjs')
    REFRESH_INTERVAL = 30 * 60  # seconds
    RETRY_INTERVAL = 2 * 60

    def __init__(self, runner, units=None):
        self.runner = runner
        self.units = units or os.environ.get('PEBBLE_WEATHER_UNITS', 'celsius')
        if self.units not in ('celsius', 'fahrenheit'):
            self.units = 'celsius'
        self._greenlet = None

    def start(self):
        if self._greenlet is None:
            self._greenlet = gevent.spawn(self._run)

    def stop(self):
        if self._greenlet is not None:
            self._greenlet.kill()
            self._greenlet = None

    def _run(self):
        while True:
            try:
                self.do_refresh()
            except Exception:
                logger.exception("weather refresh failed; retrying in %ss", self.RETRY_INTERVAL)
                gevent.sleep(self.RETRY_INTERVAL)
            else:
                gevent.sleep(self.REFRESH_INTERVAL)

    def do_refresh(self):
        lat, lon, city = self._geolocate()
        forecast = self._fetch_forecast(lat, lon)
        name = city or "Current Location"
        entry = self._pack_entry(forecast, name)
        self._push(entry)
        logger.info("pushed weather for %s: %s", name, forecast)

    def _geolocate(self):
        resp = requests.get('https://api.ipify.org', timeout=10)
        resp.raise_for_status()
        gi = pygeoip.GeoIP(os.path.join(os.path.dirname(__file__), '..', 'javascript',
                                        'navigator', 'GeoLiteCity.dat'))
        record = gi.record_by_addr(resp.text)
        if record is None:
            raise ValueError("could not geolocate %s" % resp.text)
        return record['latitude'], record['longitude'], record.get('city')

    def _fetch_forecast(self, lat, lon):
        resp = requests.get('https://api.open-meteo.com/v1/forecast', params={
            'latitude': '%.4f' % lat,
            'longitude': '%.4f' % lon,
            'current': 'temperature_2m,weather_code',
            'daily': 'weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset',
            'forecast_days': '2',
            'timeformat': 'unixtime',
            'timezone': 'auto',
            'temperature_unit': self.units,
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        current = data['current']
        daily = data['daily']
        current_type, phrase = _WMO_CODES.get(current['weather_code'], (GENERIC, "Unknown"))
        tomorrow_type, _ = _WMO_CODES.get(daily['weather_code'][1], (GENERIC, "Unknown"))
        return {
            'current_temp': int(round(current['temperature_2m'])),
            'current_type': current_type,
            'phrase': phrase,
            'today_high': int(round(daily['temperature_2m_max'][0])),
            'today_low': int(round(daily['temperature_2m_min'][0])),
            'tomorrow_type': tomorrow_type,
            'tomorrow_high': int(round(daily['temperature_2m_max'][1])),
            'tomorrow_low': int(round(daily['temperature_2m_min'][1])),
            'sunrise': daily['sunrise'][0],
            'sunset': daily['sunset'][0],
        }

    def _pack_entry(self, forecast, location_name):
        """Serialises a WeatherDBEntry (version 4) as defined in
        PebbleOS include/pbl/services/blob_db/weather_db.h."""
        name = location_name.encode('utf-8')[:MAX_LOCATION_NAME]
        phrase = forecast['phrase'].encode('utf-8')[:MAX_SHORT_PHRASE]
        fixed = struct.pack('<BhBhhBhhiBii',
                            WEATHER_DB_VERSION,
                            forecast['current_temp'],
                            forecast['current_type'],
                            forecast['today_high'],
                            forecast['today_low'],
                            forecast['tomorrow_type'],
                            forecast['tomorrow_high'],
                            forecast['tomorrow_low'],
                            int(time.time()),
                            1,  # is_current_location
                            forecast['sunrise'],
                            forecast['sunset'])
        pstrings = struct.pack('<H', len(name)) + name + struct.pack('<H', len(phrase)) + phrase
        return fixed + struct.pack('<H', len(pstrings)) + pstrings

    def _push(self, entry):
        blobdb = self.runner.pebble.blobdb
        # The firmware's weather service only surfaces locations present in the
        # "weatherApp" ordering pref (SerializedWeatherAppPrefs); ours is the
        # single, default location.
        prefs = struct.pack('<B', 1) + self.LOCATION_UUID.bytes
        blobdb.insert(WATCH_APP_PREFS_DB_ID, _RawKey(b'weatherApp'), prefs,
                      callback=self._make_result_logger('weatherApp pref'))
        blobdb.insert(WEATHER_DB_ID, self.LOCATION_UUID, entry,
                      callback=self._make_result_logger('weather entry'))

    @staticmethod
    def _make_result_logger(what):
        def handle_result(status):
            logger.debug("blobdb insert of %s: %s", what, status)
        return handle_result
