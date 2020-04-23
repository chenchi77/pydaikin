"""Pydaikin base appliance, represent a Daikin device."""

import logging
import socket
from urllib.parse import unquote

from aiohttp import ClientSession, ServerDisconnectedError
from collections import defaultdict
from datetime import datetime, timedelta

import pydaikin.discovery as discovery

_LOGGER = logging.getLogger(__name__)

POWER_CONSUMPTION_MAX_HISTORY = timedelta(hours=3)

ATTR_TOTAL = 'total'
ATTR_COOL = 'cool'
ATTR_HEAT = 'heat'


class Appliance:  # pylint: disable=too-many-public-methods
    """Daikin main appliance class."""

    TRANSLATIONS = {}

    VALUES_TRANSLATION = {}

    VALUES_SUMMARY = []

    INFO_RESOURCES = []

    @classmethod
    def daikin_to_human(cls, dimension, value):
        """Return converted values from Daikin to Human."""
        return cls.TRANSLATIONS.get(dimension, {}).get(value, str(value))

    @classmethod
    def human_to_daikin(cls, dimension, value):
        """Return converted values from Human to Daikin."""
        translations_rev = {
            dim: {v: k for k, v in item.items()}
            for dim, item in cls.TRANSLATIONS.items()
        }
        return translations_rev.get(dimension, {}).get(value, value)

    @classmethod
    def daikin_values(cls, dimension):
        """Return sorted list of translated values."""
        return sorted(list(cls.TRANSLATIONS[dimension].values()))

    @staticmethod
    async def factory(device_id, session=None, **kwargs):
        """Factory to init the corresponding Daikin class."""
        from .daikin_airbase import (  # pylint: disable=import-outside-toplevel
            DaikinAirBase,
        )
        from .daikin_brp069 import (  # pylint: disable=import-outside-toplevel
            DaikinBRP069,
        )
        from .daikin_skyfi import DaikinSkyFi  # pylint: disable=import-outside-toplevel
        from .daikin_brp072c import (  # pylint: disable=import-outside-toplevel
            DaikinBRP072C,
        )

        if 'password' in kwargs and kwargs['password'] is not None:
            appl = DaikinSkyFi(device_id, session, password=kwargs['password'])
        elif 'key' in kwargs and kwargs['key'] is not None:
            appl = DaikinBRP072C(
                device_id, session, key=kwargs['key'], uuid=kwargs.get('uuid'),
            )
        else:  # special case for BRP069 and AirBase
            appl = DaikinBRP069(device_id, session)
            await appl.update_status(appl.HTTP_RESOURCES[:1])
            if appl.values == {}:
                appl = DaikinAirBase(device_id, session)
        await appl.init()
        return appl

    @staticmethod
    def parse_response(response_body):
        """Parse respose from Daikin."""
        response = dict([e.split('=') for e in response_body.split(',')])
        if 'ret' not in response:
            raise ValueError("missing 'ret' field in response")
        if response['ret'] != 'OK':
            return {}
        if 'name' in response:
            response['name'] = unquote(response['name'])
        return response

    @staticmethod
    def translate_mac(value):
        """Return translated MAC address."""
        return ':'.join(value[i : i + 2] for i in range(0, len(value), 2))

    @staticmethod
    def discover_ip(device_id):
        """Return translated name to ip address."""
        try:
            socket.inet_aton(device_id)
            device_ip = device_id  # id is an IP
        except socket.error:
            device_ip = None

        if device_ip is None:
            # id is a common name, try discovery
            device_name = discovery.get_name(device_id)
            if device_name is None:
                # try DNS
                try:
                    device_ip = socket.gethostbyname(device_id)
                except socket.gaierror:
                    raise ValueError("no device found for %s" % device_id)
            else:
                device_ip = device_name['ip']
        return device_id

    def __init__(self, device_id, session=None):
        """Init the pydaikin appliance, representing one Daikin device."""
        self.values = {}
        self.session = session
        self._energy_consumption_history = defaultdict(list)
        if session:
            self._device_ip = device_id
        else:
            self._device_ip = self.discover_ip(device_id)

    def __getitem__(self, name):
        """Return values from self.value."""
        if name in self.values:
            return self.values[name]
        raise AttributeError("No such attribute: " + name)

    async def init(self):
        """Init status."""
        await self.update_status()

    async def _get_resource(self, resource, retries=3):
        """Update resource."""
        try:
            if self.session and not self.session.closed:
                return await self._run_get_resource(resource)
            async with ClientSession() as self.session:
                return await self._run_get_resource(resource)
        except ServerDisconnectedError as error:
            _LOGGER.debug("ServerDisconnectedError %d", retries)
            if retries == 0:
                raise error
            return await self._get_resource(resource, retries=retries - 1)

    async def _run_get_resource(self, resource):
        """Make the http request."""
        async with self.session.get(f'http://{self._device_ip}/{resource}') as resp:
            if resp.status == 200:
                return self.parse_response(await resp.text())
            return {}

    async def update_status(self, resources=None):
        """Update status from resources."""
        if resources is None:
            resources = self.INFO_RESOURCES
        _LOGGER.debug("Updating %s", resources)
        for resource in resources:
            self.values.update(await self._get_resource(resource))

        self._register_energy_consumption_history()

    def _register_energy_consumption_history(self):
        if not self.support_energy_consumption:
            return

        for mode in (ATTR_TOTAL, ATTR_COOL, ATTR_HEAT):
            new_state = (
                datetime.utcnow(),
                self.today_energy_consumption(mode=mode),
                self.yesterday_energy_consumption(mode=mode),
            )

            if len(self._energy_consumption_history[mode]):
                old_state = self._energy_consumption_history[mode][0]
            else:
                old_state = (None, None, None)

            if old_state[1] is not None and new_state[1] == old_state[1]:
                if old_state[2] is not None and new_state[2] == old_state[2]:
                    # State has not changed, nothing to register
                    continue

            self._energy_consumption_history[mode].insert(0, new_state)

            # We can remove very old states (except the latest one)
            idx = min((
                i for i, (dt, _, _) in enumerate(self._energy_consumption_history[mode])
                if dt < datetime.utcnow() - POWER_CONSUMPTION_MAX_HISTORY
            ), default=len(self._energy_consumption_history[mode])) + 1

            self._energy_consumption_history[mode] = self._energy_consumption_history[mode][:idx]

    def show_values(self, only_summary=False):
        """Print values."""
        if only_summary:
            keys = self.VALUES_SUMMARY
        else:
            keys = sorted(self.values.keys())

        for key in keys:
            if key in self.values:
                (k, val) = self._represent(key)
                print("%18s: %s" % (k, val))

    def show_sensors(self):
        data = [
            datetime.utcnow().strftime('%m/%d/%Y %H:%M:%S'),
            f'in_temp={int(self.inside_temperature)}°C'
        ]
        if self.support_outside_temperature:
            data.append(f'out_temp={int(self.outside_temperature)}°C')
        if self.support_energy_consumption:
            data.append(f'total_today={self.today_energy_consumption(ATTR_TOTAL):.01f}kWh')
            data.append(f'cool_today={self.today_energy_consumption(ATTR_COOL):.01f}kWh')
            data.append(f'heat_today={self.today_energy_consumption(ATTR_HEAT):.01f}kWh')
            data.append(f'total_power={self.current_total_power_consumption:.01f}kW')
            data.append(f'cool_power={self.last_hour_cool_power_consumption:.01f}kW')
            data.append(f'heat_power={self.last_hour_heat_power_consumption:.01f}kW')
        print('  '.join(data))

    def _represent(self, key):
        """Return translated value from key."""
        k = self.VALUES_TRANSLATION.get(key, key)

        # adapt the value
        val = self.values[key]

        if key == 'mode' and self.values['pow'] == '0':
            val = 'off'
        elif key == 'mac':
            val = self.translate_mac(val)
            val = unquote(self.values[key]).split(';')
        else:
            val = self.daikin_to_human(key, val)

        _LOGGER.debug('Represent: %s, %s, %s', key, k, val)
        return (k, val)

    def _temperature(self, dimension):
        """Parse temperature."""
        try:
            return float(self.values.get(dimension))
        except ValueError:
            return False

    def _energy_consumption(self, dimension):
        """Parse energy consumption."""
        try:
            return [int(x) for x in self.values.get(dimension).split('/')]
        except ValueError:
            return

    @property
    def support_away_mode(self):
        """Return True if the device support away_mode."""
        return 'en_hol' in self.values

    @property
    def support_fan_rate(self):
        """Return True if the device support setting fan_rate."""
        return 'f_rate' in self.values

    @property
    def support_swing_mode(self):
        """Return True if the device support setting swing_mode."""
        return 'f_dir' in self.values

    @property
    def support_outside_temperature(self):
        """Return True if the device is not an AirBase unit."""
        return self.outside_temperature is not None

    @property
    def support_energy_consumption(self):
        """Return True if the device supports energy consumption monitoring."""
        return sum(map(int, (
            (self.values.get('previous_year', None) or '0') + '/' +
            (self.values.get('this_year', None) or '0')
        ).split('/'))) > 0

    @property
    def outside_temperature(self):
        """Return current outside temperature."""
        return self._temperature('otemp')

    @property
    def inside_temperature(self):
        """Return current inside temperature."""
        return self._temperature('htemp')

    @property
    def target_temperature(self):
        """Return current target temperature."""
        return self._temperature('stemp')

    def today_energy_consumption(self, mode=ATTR_TOTAL):
        """Return today energy consumption in kWh."""
        if mode == ATTR_TOTAL:
            # Return total energy consumption. Updated in live
            return self._energy_consumption('datas')[-1] / 1000
        elif mode == ATTR_COOL:
            # Return cool energy consumption of this AC. Updated hourly
            return sum(self._energy_consumption('curr_day_cool')) / 10
        elif mode == ATTR_HEAT:
            # Return heat energy consumption of this AC. Updated hourly
            return sum(self._energy_consumption('curr_day_heat')) / 10
        else:
            raise ValueError(f'Unsupported mode {mode}.')

    def yesterday_energy_consumption(self, mode=ATTR_TOTAL):
        """Return yesterday energy consumption in kWh."""
        if mode == ATTR_TOTAL:
            # Return total energy consumption.
            return self._energy_consumption('datas')[-2] / 1000
        elif mode == ATTR_COOL:
            # Return cool energy consumption of this AC.
            return sum(self._energy_consumption('prev_1day_cool')) / 10
        elif mode == ATTR_HEAT:
            # Return heat energy consumption of this AC.
            return sum(self._energy_consumption('prev_1day_heat')) / 10
        else:
            raise ValueError(f'Unsupported mode {mode}.')

    def delta_energy_consumption(self, dt, mode=ATTR_TOTAL, early_break=False):
        """Return the delta energy consumption of a given mode."""
        energy = 0
        history = self._energy_consumption_history[mode]
        for (dt2, st2, sy2), (dt1, st1, sy1) in zip(history, history[1:]):
            if dt2 <= datetime.utcnow() - dt:
                break
            if st2 > st1:
                # Normal behavior, today state is growing
                energy += st2 - st1
            elif sy2 >= st1:
                # If today state is not growing (or even declines), we probably have shifted 1 day
                # Thus we should have yesterday state >= previous today state (in most cases it will ==)
                energy += sy2 - st1
                energy += st2
            else:
                _LOGGER.error('Impossible energy consumption measure')
                return 0
            if early_break:
                break

        return energy

    @property
    def current_total_power_consumption(self):
        """Return the current total power consumption in kW."""
        if not len(self._energy_consumption_history):
            return

        w = timedelta(minutes=30)
        return self.delta_energy_consumption(w, mode='total') * (timedelta(hours=1) / w)

    @property
    def last_hour_cool_power_consumption(self):
        """Return the last hour cool power consumption of a given mode in kWh."""
        if not len(self._energy_consumption_history):
            return

        # We tolerate a 5-minutes margin
        w = timedelta(minutes=65)
        return self.delta_energy_consumption(w, mode=ATTR_COOL, early_break=True)

    @property
    def last_hour_heat_power_consumption(self):
        """Return the last hour heat power consumption of a given mode in kWh."""
        if not len(self._energy_consumption_history):
            return

        # We tolerate a 5-minutes margin
        w = timedelta(minutes=65)
        return self.delta_energy_consumption(w, mode=ATTR_HEAT, early_break=True)

    @property
    def fan_rate(self):
        """Return list of supported fan modes."""
        return list(map(str.title, self.TRANSLATIONS.get('f_rate', {}).values()))

    async def set(self, settings):
        """Set settings on Daikin device."""
        raise NotImplementedError

    async def set_holiday(self, mode):
        """Set holiday mode."""
        raise NotImplementedError

    @property
    def zones(self):
        """Return list of zones."""
        return

    async def set_zone(self, zone_id, status):
        """Set zone status."""
        raise NotImplementedError