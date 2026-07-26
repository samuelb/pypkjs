"""Push a weather forecast into a running PebbleOS emulator.

    python -m pypkjs.weather [host:port] [--lat LAT --lon LON] [--name NAME]

Without --lat/--lon the host is geolocated by IP (GeoIP), like
navigator.geolocation in the JS runtime. The forecast comes from Open-Meteo
and lands in the watch's WeatherDB, feeding the built-in Weather app, the
launcher glance, and the weather_service_* app API.
"""

import argparse
import logging
import time

from libpebble2.communication import PebbleConnection
from libpebble2.communication.transports.qemu import QemuTransport, MessageTargetQemu
from libpebble2.communication.transports.qemu.protocol import QemuBluetoothConnection

from . import PebbleWeather


class _Shim(object):
    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('qemu', nargs='?', default='localhost:12344',
                        help='QEMU pebble serial endpoint (default: localhost:12344)')
    parser.add_argument('--lat', type=float, help='latitude (skips GeoIP lookup)')
    parser.add_argument('--lon', type=float, help='longitude (skips GeoIP lookup)')
    parser.add_argument('--name', default=None, help='location name shown on the watch')
    parser.add_argument('--units', choices=('celsius', 'fahrenheit'), default=None)
    parser.add_argument('--loop', action='store_true',
                        help='keep running and refresh every 30 minutes')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    host, port = args.qemu.rsplit(':', 1)
    pebble = PebbleConnection(QemuTransport(host, int(port)))
    pebble.connect()
    pebble.run_async()
    pebble.transport.send_packet(QemuBluetoothConnection(connected=True),
                                 target=MessageTargetQemu())
    time.sleep(2)  # let the version/capability handshake finish

    from libpebble2.services.blobdb import BlobDBClient
    runner = _Shim()
    runner.pebble = _Shim()
    runner.pebble.blobdb = BlobDBClient(pebble)

    weather = PebbleWeather(runner, units=args.units)
    if args.lat is not None and args.lon is not None:
        forecast = weather._fetch_forecast(args.lat, args.lon)
        name = args.name or 'Emulator'
        weather._push(weather._pack_entry(forecast, name))
        logging.info("pushed weather for %s: %s", name, forecast)
    else:
        weather.do_refresh()

    if args.loop:
        weather.start()
        while True:
            time.sleep(3600)
    else:
        time.sleep(3)  # let the BlobDB inserts complete


if __name__ == '__main__':
    main()
