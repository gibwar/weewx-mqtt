# Copyright 2013-2022 Matthew Wall
# Distributed under the terms of the GNU Public License (GPLv3)
"""
Upload data to MQTT server

This service requires the python bindings for mqtt:

   pip install paho-mqtt

Minimal configuration:

[StdRestful]
    [[MQTT]]
        server_url = mqtt://username:password@localhost:1883/
        topic = weather
        unit_system = METRIC

Other MQTT options can be specified:

[StdRestful]
    [[MQTT]]
        ...
        qos = 1        # options are 0, 1, 2
        retain = true  # options are true or false
        publish_availability = true # options are true or false

The observations can be sent individually, or in an aggregated packet:

[StdRestful]
    [[MQTT]]
        ...
        aggregation = individual, aggregate # individual, aggregate, or both

Bind to loop packets or archive records:

[StdRestful]
    [[MQTT]]
        ...
        binding = loop # options are loop or archive

Use the inputs map to customize name, format, or unit for any observation.
Note that starting with v0.24, option 'units' was renamed to 'unit', although
either will be accepted.

[StdRestful]
    [[MQTT]]
        ...
        unit_system = METRIC # default to metric
        [[[inputs]]]
            [[[[outTemp]]]]
                name = inside_temperature  # use a label other than outTemp
                format = %.2f              # two decimal places of precision
                unit = degree_F            # convert outTemp to F, others in C
            [[[[windSpeed]]]]
                unit = knot  # convert the wind speed to knots

Use TLS to encrypt connection to broker.  The TLS options will be passed to
Paho client tls_set method.  Refer to Paho client documentation for details:

  https://eclipse.org/paho/clients/python/docs/

[StdRestful]
    [[MQTT]]
        ...
        [[[tls]]]
            # CA certificates file (mandatory)
            ca_certs = /etc/ssl/certs/ca-certificates.crt
            # PEM encoded client certificate file (optional)
            certfile = /home/user/.ssh/id.crt
            # private key file (optional)
            keyfile = /home/user/.ssh/id.key
            # Certificate requirements imposed on the broker (optional).
            #   Options are 'none', 'optional' or 'required'.
            #   Default is 'required'.
            cert_reqs = required
            # SSL/TLS protocol (optional).
            #   Options include sslv2, sslv23, sslv3, tls, tlsv1, tlsv11,
            #   tlsv12.
            #   Default is 'tlsv12'
            #   Not all options are supported by all systems.
            #   OpenSSL version till 1.0.0.h supports sslv2, sslv3 and tlsv1
            #   OpenSSL >= 1.0.1 supports tlsv11 and tlsv12
            #   OpenSSL >= 1.1.1 support TLSv1.3 (use tls_version = tls)
            #   Check your OpenSSL protocol support with:
            #   openssl s_client -help 2>&1  > /dev/null | egrep "\-(ssl|tls)[^a-z]"
            tls_version = tlsv12
            # Allowable encryption ciphers (optional).
            #   To specify multiple cyphers, delimit with commas and enclose
            #   in quotes.
            #ciphers =

Home Assistant configuration

MQTT discovery configuration is available through some extra configuration parameters.
Due to the nature of not every sensor being useful in HA beyond weewx there is the need
to provide a list of sensors you want to show up in HA. There is interaction with
obs_to_upload and the input dictionary used for regular MQTT configuration. If you have
obs_to_upload to a value other than "all" a sensor must be added to both the [[[input]]]
AND [[[[Sensors]]]] dictionaries.

An optional Device configuration is available if you would like to fill out extra device
information related to the sensors.

[StdRestful]
    [[MQTT]]
        unit_system = US  # required for Home Assistant
        publish_availability = true  # highly recommended for good HA interaction

        [[[HomeAssistant]]]
            enable = true
            # HA topic to post discovery data to. Optional. Defaults to homeassistant
            topic = homeassistant
            # string value to prepend to all sensors to help ensure uniqueness.
            # Optional. Defaults to "weewx"
            sensor_prefix = vantagepro2

            [[[[Device]]]]
                configuration_url = https://example.com/wx
                # suggested to set this if using more than one weewx instance, default
                # is "weewx"
                identifier = weewx_vantagepro2
                manufacturer = Davis Instruments
                model = Vantage Pro2
                name = Davis Vantage Pro2
                suggested_area = Outside
                sw_version = 3.88

            [[[[Sensors]]]]
                [[[[[outTemp]]]]]
                    name = Outside Temperature  # required
                [[[[[barometer]]]]]
                    name = Barometric Pressure
                [[[[[rain]]]]]
                    name = Rain
                    icon = mdi:weather-rainy
                [[[[[rxCheckPercent]]]]]
                    name = RX %
                    icon = mdi:radio-handheld
                    entity_category = diagnostic

Device and Sensor override values are available at:
    https://www.home-assistant.io/integrations/sensor.mqtt/#configuration-variables

The Device dictionary is a direct map to the configuration linked above and no
additional processing is done. Each Sensor can override the values, though an attempt
has been made to minimize the amount of configuration needed and sane default values are
provided for the sensors (ie: temperature and pressure).

The only required sensor value is "name", all others can override the defaults if
needed. Available overrides for sensors are:
    - unit_of_measurement (a basic unit map is provided below with a few defaults)
    - device_class (this has limited utility outside of temperature and pressure which
      are already handled)
    - value_template
    - object_id (useful if you decide to change the name of the device, otherwise the
      default value is sufficient)
    - icon (very handy to have sensors show up with the requested icon, though that can
      still be done through the UI later)
    - entity_category (used to move sensors to "diagnostic")
"""

try:
    import queue as Queue
except ImportError:
    import Queue

try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse

import paho.mqtt.client as mqtt
import random
import socket
import sys
import time

try:
    import cjson as json
    setattr(json, 'dumps', json.encode)
    setattr(json, 'loads', json.decode)
except (ImportError, AttributeError):
    try:
        import simplejson as json
    except ImportError:
        import json

import weewx
import weewx.restx
import weewx.units
from weeutil.weeutil import to_int, to_bool, accumulateLeaves

VERSION = "0.24"

if weewx.__version__ < "3":
    raise weewx.UnsupportedFeature("weewx 3 is required, found %s" %
                                   weewx.__version__)

try:
    # weewx4 logging
    import weeutil.logger
    import logging
    log = logging.getLogger(__name__)
    def logdbg(msg):
        log.debug(msg)
    def loginf(msg):
        log.info(msg)
    def logerr(msg):
        log.error(msg)
except ImportError:
    # old-style weewx logging
    import syslog
    def logmsg(level, msg):
        syslog.syslog(level, 'restx: MQTT: %s' % msg)
    def logdbg(msg):
        logmsg(syslog.LOG_DEBUG, msg)
    def loginf(msg):
        logmsg(syslog.LOG_INFO, msg)
    def logerr(msg):
        logmsg(syslog.LOG_ERR, msg)


def _compat(d, old_label, new_label):
    if old_label in d and new_label not in d:
        d.setdefault(new_label, d[old_label])
        d.pop(old_label)

def _obfuscate_password(url):
    parts = urlparse(url)
    if parts.password is not None:
        # split out the host portion manually. We could use
        # parts.hostname and parts.port, but then you'd have to check
        # if either part is None. The hostname would also be lowercased.
        host_info = parts.netloc.rpartition('@')[-1]
        parts = parts._replace(netloc='{}:xxx@{}'.format(
            parts.username, host_info))
        url = parts.geturl()
    return url

# some unit labels are rather lengthy.  this reduces them to something shorter.
UNIT_REDUCTIONS = {
    'degree_F': 'F',
    'degree_C': 'C',
    'inch': 'in',
    'mile_per_hour': 'mph',
    'mile_per_hour2': 'mph',
    'km_per_hour': 'kph',
    'km_per_hour2': 'kph',
    'knot': 'knot',
    'knot2': 'knot2',
    'meter_per_second': 'mps',
    'meter_per_second2': 'mps',
    'degree_compass': None,
    'watt_per_meter_squared': 'Wpm2',
    'uv_index': None,
    'percent': None,
    'unix_epoch': None,
    }

# return the units label for an observation
def _get_units_label(obs, unit_system, unit_type=None):
    if unit_type is None:
        (unit_type, _) = weewx.units.getStandardUnitType(unit_system, obs)
    return UNIT_REDUCTIONS.get(unit_type, unit_type)

# get the template for an observation based on the observation key
def _get_template(obs_key, overrides, append_units_label, unit_system):
    tmpl_dict = dict()
    if append_units_label:
        unit_type = overrides.get('unit')
        label = _get_units_label(obs_key, unit_system, unit_type)
        if label is not None:
            tmpl_dict['name'] = "%s_%s" % (obs_key, label)
    for x in ['name', 'format', 'unit']:
        if x in overrides:
            tmpl_dict[x] = overrides[x]
    return tmpl_dict


class MQTT(weewx.restx.StdRESTbase):
    def __init__(self, engine, config_dict):
        """This service recognizes standard restful options plus the following:

        Required parameters:

        server_url: URL of the broker, e.g., something of the form
          mqtt://username:password@localhost:1883/
        Default is None

        Optional parameters:

        unit_system: one of US, METRIC, or METRICWX
        Default is None; units will be those of data in the database

        topic: the MQTT topic under which to post
        Default is 'weather'

        append_units_label: should units label be appended to name
        Default is True

        obs_to_upload: Which observations to upload.  Possible values are
        none or all.  When none is specified, only items in the inputs list
        will be uploaded.  When all is specified, all observations will be
        uploaded, subject to overrides in the inputs list.
        Default is all

        inputs: dictionary of weewx observation names with optional upload
        name, format, and units
        Default is None

        tls: dictionary of TLS parameters used by the Paho client to establish
        a secure connection with the broker.
        Default is None
        """
        super(MQTT, self).__init__(engine, config_dict)
        loginf("service version is %s" % VERSION)
        site_dict = weewx.restx.get_site_dict(config_dict, 'MQTT', 'server_url')
        if not site_dict:
            return

        # for backward compatibility: 'units' is now 'unit_system'
        _compat(site_dict, 'units', 'unit_system')

        site_dict.setdefault('client_id', '')
        site_dict.setdefault('topic', 'weather')
        site_dict.setdefault('append_units_label', True)
        site_dict.setdefault('augment_record', True)
        site_dict.setdefault('obs_to_upload', 'all')
        site_dict.setdefault('retain', False)
        site_dict.setdefault('qos', 0)
        site_dict.setdefault('aggregation', 'individual,aggregate')
        site_dict.setdefault('publish_availability', False)
        site_dict.setdefault('ha', {})

        usn = site_dict.get('unit_system', None)
        if usn is not None:
            site_dict['unit_system'] = weewx.units.unit_constants[usn]

        if 'tls' in config_dict['StdRESTful']['MQTT']:
            site_dict['tls'] = dict(config_dict['StdRESTful']['MQTT']['tls'])

        if 'inputs' in config_dict['StdRESTful']['MQTT']:
            site_dict['inputs'] = dict(config_dict['StdRESTful']['MQTT']['inputs'])
            # In the 'inputs' section, option 'units' is now 'unit'.
            for obs_type in site_dict['inputs']:
                _compat(site_dict['inputs'][obs_type], 'units', 'unit')

        if 'HomeAssistant' in config_dict['StdRESTful']['MQTT']:
            site_dict['ha'] = dict(config_dict['StdRESTful']['MQTT']['HomeAssistant'])

        site_dict['append_units_label'] = to_bool(site_dict.get('append_units_label'))
        site_dict['augment_record'] = to_bool(site_dict.get('augment_record'))
        site_dict['retain'] = to_bool(site_dict.get('retain'))
        site_dict['qos'] = to_int(site_dict.get('qos'))
        site_dict['publish_availability'] = to_bool(site_dict.get('publish_availability'))
        binding = site_dict.pop('binding', 'archive')
        loginf("binding to %s" % binding)

        # if we are supposed to augment the record with data from weather
        # tables, then get the manager dict to do it.  there may be no weather
        # tables, so be prepared to fail.
        try:
            if site_dict.get('augment_record'):
                _manager_dict = weewx.manager.get_manager_dict_from_config(
                    config_dict, 'wx_binding')
                site_dict['manager_dict'] = _manager_dict
        except weewx.UnknownBinding:
            pass

        self.archive_queue = Queue.Queue()
        self.archive_thread = MQTTThread(self.archive_queue, **site_dict)
        self.archive_thread.start()

        if 'archive' in binding:
            self.bind(weewx.NEW_ARCHIVE_RECORD, self.new_archive_record)
        if 'loop' in binding:
            self.bind(weewx.NEW_LOOP_PACKET, self.new_loop_packet)

        if 'topic' in site_dict:
            loginf("topic is %s" % site_dict['topic'])
        if usn is not None:
            loginf("desired unit system is %s" % usn)
        loginf("data will be uploaded to %s" %
               _obfuscate_password(site_dict['server_url']))
        if 'tls' in site_dict:
            loginf("network encryption/authentication will be attempted")

    def new_archive_record(self, event):
        self.archive_queue.put(event.record)

    def new_loop_packet(self, event):
        self.archive_queue.put(event.packet)

    # override shutDown to post clean MQTT disconnect message.
    def shutDown(self):
        self.archive_queue.put({"mqtt_stop": True, "dateTime": time.time()})
        super().shutDown()


class TLSDefaults(object):
    def __init__(self):
        import ssl

        # Paho acceptable TLS options
        self.TLS_OPTIONS = [
            'ca_certs', 'certfile', 'keyfile',
            'cert_reqs', 'tls_version', 'ciphers'
            ]
        # map for Paho acceptable TLS cert request options
        self.CERT_REQ_OPTIONS = {
            'none': ssl.CERT_NONE,
            'optional': ssl.CERT_OPTIONAL,
            'required': ssl.CERT_REQUIRED
            }
        # Map for Paho acceptable TLS version options. Some options are
        # dependent on the OpenSSL install so catch exceptions
        self.TLS_VER_OPTIONS = dict()
        try:
            self.TLS_VER_OPTIONS['tls'] = ssl.PROTOCOL_TLS
        except AttributeError:
            pass
        try:
            # deprecated - use tls instead, or tlsv12 if python < 2.7.13
            self.TLS_VER_OPTIONS['tlsv1'] = ssl.PROTOCOL_TLSv1
        except AttributeError:
            pass
        try:
            # deprecated - use tls instead, or tlsv12 if python < 2.7.13
            self.TLS_VER_OPTIONS['tlsv11'] = ssl.PROTOCOL_TLSv1_1
        except AttributeError:
            pass
        try:
            # deprecated - use tls instead if python >= 2.7.13
            self.TLS_VER_OPTIONS['tlsv12'] = ssl.PROTOCOL_TLSv1_2
        except AttributeError:
            pass
        try:
            # SSLv2 is insecure - this protocol is deprecated
            self.TLS_VER_OPTIONS['sslv2'] = ssl.PROTOCOL_SSLv2
        except AttributeError:
            pass
        try:
            # deprecated - use tls instead, or tlsv12 if python < 2.7.13
            # (alias for PROTOCOL_TLS)
            self.TLS_VER_OPTIONS['sslv23'] = ssl.PROTOCOL_SSLv23
        except AttributeError:
            pass
        try:
            # SSLv3 is insecure - this protocol is deprecated
            self.TLS_VER_OPTIONS['sslv3'] = ssl.PROTOCOL_SSLv3
        except AttributeError:
            pass


class MQTTThread(weewx.restx.RESTThread):

    def __init__(self, queue, server_url,
                 client_id='', topic='', unit_system=None, skip_upload=False,
                 augment_record=True, retain=False, aggregation='individual',
                 inputs={}, obs_to_upload='all', append_units_label=True,
                 manager_dict=None, tls=None, qos=0,
                 post_interval=None, stale=None,
                 log_success=True, log_failure=True,
                 timeout=60, max_tries=3, retry_wait=5,
                 publish_availability=False, ha={},
                 max_backlog=sys.maxsize):
        super(MQTTThread, self).__init__(queue,
                                         protocol_name='MQTT',
                                         manager_dict=manager_dict,
                                         post_interval=post_interval,
                                         max_backlog=max_backlog,
                                         stale=stale,
                                         log_success=log_success,
                                         log_failure=log_failure,
                                         max_tries=max_tries,
                                         timeout=timeout,
                                         retry_wait=retry_wait)
        self.server_url = server_url
        self.client_id = client_id
        self.topic = topic
        self.upload_all = True if obs_to_upload.lower() == 'all' else False
        self.append_units_label = append_units_label
        self.tls_dict = {}
        if tls is not None:
            # we have TLS options so construct a dict to configure Paho TLS
            dflts = TLSDefaults()
            for opt in tls:
                if opt == 'cert_reqs':
                    if tls[opt] in dflts.CERT_REQ_OPTIONS:
                        self.tls_dict[opt] = dflts.CERT_REQ_OPTIONS.get(tls[opt])
                elif opt == 'tls_version':
                    if tls[opt] in dflts.TLS_VER_OPTIONS:
                        self.tls_dict[opt] = dflts.TLS_VER_OPTIONS.get(tls[opt])
                elif opt in dflts.TLS_OPTIONS:
                    self.tls_dict[opt] = tls[opt]
            logdbg("TLS parameters: %s" % self.tls_dict)
        self.inputs = inputs
        self.unit_system = unit_system
        self.augment_record = augment_record
        self.retain = retain
        self.qos = qos
        self.aggregation = aggregation
        self.templates = dict()
        self.skip_upload = skip_upload
        self.mc = None
        self.mc_try_time = 0
        self.publish_availability = publish_availability
        self.stopped = False
        self.ha = ha

    def get_mqtt_client(self):
        if self.mc:
            return
        if time.time() - self.mc_try_time < self.retry_wait:
            return
        client_id = self.client_id
        if not client_id:
            pad = "%032x" % random.getrandbits(128)
            client_id = 'weewx_%s' % pad[:8]
        mc = mqtt.Client(client_id=client_id)
        url = urlparse(self.server_url)
        if url.username is not None and url.password is not None:
            mc.username_pw_set(url.username, url.password)
        # if we have TLS opts configure TLS on our broker connection
        if len(self.tls_dict) > 0:
            mc.tls_set(**self.tls_dict)

        if self.publish_availability:
            mc.will_set(f'{self.topic}/availability', 'offline', retain=True)

        try:
            self.mc_try_time = time.time()
            mc.connect(url.hostname, url.port)
        except (socket.error, socket.timeout, socket.herror) as e:
            logerr('Failed to connect to MQTT server (%s): %s' %
                    (_obfuscate_password(self.server_url), str(e)))
            self.mc = None
            return
        mc.loop_start()
        loginf('client established for %s' %
               _obfuscate_password(self.server_url))
        self.mc = mc

        if self.publish_availability:
            self._set_online(True)

        if self.ha.get('enable'):
            self._publish_ha_config()

    def filter_data(self, record):
        # if uploading everything, we must check the upload variables list
        # every time since variables may come and go in a record.  use the
        # inputs to override any generic template generation.
        if self.upload_all:
            for f in record:
                if f not in self.templates:
                    self.templates[f] = _get_template(f,
                                                      self.inputs.get(f, {}),
                                                      self.append_units_label,
                                                      record['usUnits'])

        # otherwise, create the list of upload variables once, based on the
        # user-specified list of inputs.
        elif not self.templates:
            for f in self.inputs:
                self.templates[f] = _get_template(f, self.inputs[f],
                                                  self.append_units_label,
                                                  record['usUnits'])

        # loop through the templates, populating them with data from the record
        data = dict()
        for k in self.templates:
            try:
                v = float(record.get(k))
                name = self.templates[k].get('name', k)
                fmt = self.templates[k].get('format', '%s')
                to_units = self.templates[k].get('unit')
                if to_units is not None:
                    (from_unit, from_group) = weewx.units.getStandardUnitType(
                        record['usUnits'], k)
                    from_t = (v, from_unit, from_group)
                    v = weewx.units.convert(from_t, to_units)[0]
                s = fmt % v
                data[name] = s
            except (TypeError, ValueError):
                pass
        # FIXME: generalize this
        if 'latitude' in data and 'longitude' in data:
            parts = [str(data['latitude']), str(data['longitude'])]
            if 'altitude_meter' in data:
                parts.append(str(data['altitude_meter']))
            elif 'altitude_foot' in data:
                parts.append(str(data['altitude_foot']))
            data['position'] = ','.join(parts)
        return data

    def process_record(self, record, dbm):
        if self._handle_stop_message(record):
            return
        if self.augment_record and dbm is not None:
            record = self.get_record(record, dbm)
        if self.unit_system is not None:
            record = weewx.units.to_std_system(record, self.unit_system)
        data = self.filter_data(record)
        if weewx.debug >= 2:
            logdbg("data: %s" % data)
        if self.skip_upload:
            loginf("skipping upload")
            return
        self.get_mqtt_client()
        if not self.mc:
            raise weewx.restx.FailedPost('MQTT client not available')
        if self.aggregation.find('aggregate') >= 0:
            tpc = self.topic + '/loop'
            (res, mid) = self.mc.publish(tpc, json.dumps(data),
                                         retain=self.retain, qos=self.qos)
            if res != mqtt.MQTT_ERR_SUCCESS:
                logerr("publish failed for %s: %s" %
                       (tpc, mqtt.error_string(res)))
        if self.aggregation.find('individual') >= 0:
            for key in data:
                tpc = self.topic + '/' + key
                (res, mid) = self.mc.publish(tpc, data[key],
                                             retain=self.retain)
                if res != mqtt.MQTT_ERR_SUCCESS:
                    logerr("publish failed for %s: %s" %
                           (tpc, mqtt.error_string(res)))

    def _get_ha_config_template(self):
        device = self.ha.get('Device', {})
        device_conf = {
            'identifiers': [ device.get('identifier', 'weewx') ]
        }

        if 'configuration_url' in device:
            device_conf['configuration_url'] = str(device['configuration_url'])
        if 'manufacturer' in device:
            device_conf['manufacturer'] = str(device['manufacturer'])
        if 'model' in device:
            device_conf['model'] = str(device['model'])
        if 'name' in device:
            device_conf['name'] = str(device['name'])
        if 'suggested_area' in device:
            device_conf['suggested_area'] = str(device['suggested_area'])
        if 'sw_version' in device:
            device_conf['sw_version'] = str(device['sw_version'])

        default_config = {
            'device': device_conf,
            'enabled_by_default': True,
        }

        if self.publish_availability:
            default_config['availability'] = [{
                'topic': f'{self.topic}/availability',
            }]
            default_config['availability_mode'] = 'all'

        return default_config

    def _handle_stop_message(self, record):
        if self.stopped:
            return True

        if not record.get('mqtt_stop'):
            return False

        # we're stopped
        self.stopped = True
        if not self.mc:
            return True

        loginf('notifying MQTT of shutdown')
        msg = self._set_online(False)
        msg.wait_for_publish()
        self.mc.loop_stop()
        self.mc.disconnect()
        self.mc = None
        return True

    def _publish_ha_config(self):
        loginf('publishing home assistant configuration')
        sensors = self.ha.get('Sensors')
        if not sensors:
            logerr('missing Sensor configuration in Home Assistant')
            return

        if not self.unit_system:
            logerr('missing unit system. this is needed to ensure Home Assistant has the right sensor configuration')
            return

        config_template = self._get_ha_config_template()
        prefix = self.ha.get('sensor_prefix', 'weewx')

        for (key, sensor) in sensors.items():
            (weewx_unit, group) = weewx.units.getStandardUnitType(self.unit_system, key)
            template = _get_template(key, self.inputs.get(key, {}), self.append_units_label, self.unit_system)

            unit_of_measurement = sensor.get('unit_of_measurement', HA_UNIT_MAP.get(weewx_unit, weewx_unit))
            device_class = sensor.get('device_class', HA_DEVICE_CLASS_MAP.get(group))
            value_template = sensor.get('value_template', HA_VALUE_MAP.get(self.unit_system, {}).get(group))

            object_id = sensor.get('object_id', sensor['name'])
            object_id = object_id.strip().replace(' ', '_').lower()

            sensor_config = dict(config_template)
            sensor_config.update({
                'name': sensor['name'],
                'object_id': f'{prefix}_{object_id}',
                'state_class': 'measurement',
                'state_topic': f'{self.topic}/{template.get("name", key)}',
                'unique_id': f'{prefix}_{key}_mqtt',
            })

            if device_class:
                sensor_config['device_class'] = device_class
            if unit_of_measurement:
                sensor_config['unit_of_measurement'] = unit_of_measurement
            if value_template:
                sensor_config['value_template'] = value_template

            if 'icon' in sensor:
                sensor_config['icon'] = sensor['icon']
            if 'entity_category' in sensor:
                sensor_config['entity_category'] = sensor['entity_category']


            ha_topic = self.ha.get('topic', 'homeassistant')
            data = json.dumps(sensor_config)
            self.mc.publish(f'{ha_topic}/sensor/{prefix}/{key}/config', data, retain=True)

    def _set_online(self, online):
        state = 'online' if online else 'offline'
        if self.mc:
            return self.mc.publish(f'{self.topic}/availability', state, retain=True)


HA_DEVICE_CLASS_MAP = {
    'group_pressure': 'pressure',
    'group_temperature': 'temperature',
}

HA_UNIT_MAP = {
    'degree_C': '°C',
    'degree_F': '°F',
    'degree_compass': '°',
    'inch': 'in',
    'km_per_hour': 'kph',
    'mile_per_hour': 'mph',
    'percent': '%',
    'uv_index': 'UV',
    'watt_hour': 'Wh',
    'watt_per_meter_squared': 'Wpm²',
    'watt_second': 'Ws',
}

HA_VALUE_MAP = {
    weewx.US: {
        'group_percent': '{{ value | int }}',
        'group_pressure': '{{ value | float(default=0) | round(2) }}',
        'group_rain': '{{ value | float(default=0) | round(3) }}',
        'group_temperature': '{{ value | float(default=0) }}',
    },
    weewx.METRIC: {
        'group_percent': '{{ value | int }}',
        'group_pressure': '{{ value | float(default=0) }}',
        'group_rain': '{{ value | float(default=0) }}',
        'group_temperature': '{{ value | float(default=0) }}',
    },
    weewx.METRICWX: {
        'group_percent': '{{ value | int }}',
        'group_pressure': '{{ value | float(default=0) }}',
        'group_rain': '{{ value | float(default=0) }}',
        'group_temperature': '{{ value | float(default=0) }}',
    },
}
