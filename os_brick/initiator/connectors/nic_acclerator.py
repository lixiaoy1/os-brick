# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


import json
import os
import re
import requests

from oslo_log import log as logging

from os_brick import exception
from os_brick.i18n import _
from os_brick import initiator
from os_brick.initiator.connectors import base
from os_brick import utils

LOG = logging.getLogger(__name__)


class NICAccelerator(base.BaseLinuxConnector):
    """"Connector class to attach/detach volumes through smartnic."""

    def __init__(self, root_helper, driver=None, use_multipath=False,
                 device_scan_attempts=initiator.DEVICE_SCAN_ATTEMPTS_DEFAULT,
                 *args, **kwargs):

        super(NICAccelerator, self).__init__(root_helper, driver=driver,
                                             device_scan_attempts=
                                             device_scan_attempts,
                                             *args, **kwargs)
        self.url = ('http://%(ip)s:%(port)s/' %
                    {'ip': kwargs['spdk_rpc_ip'],
                     'port': kwargs['spdk_rpc_port']})
        self.auth = (kwargs['spdk_rpc_username'],
                     kwargs['spdk_rpc_password'])
        self.vhostSCSI = kwargs['vhost']
        self.pciAddress = kwargs['pciAddress']

    @staticmethod
    def get_connector_properties(root_helper, *args, **kwargs):
        """The connector properties, currently work for RBD."""
        """TODO: work for iscsi. Need ssh to smartnic to get connectors'
           properties."""
        return {}

    def _rpc_call(self, method, params=None):
        payload = {}
        payload['jsonrpc'] = '2.0'
        payload['id'] = 1
        payload['method'] = method
        if params is not None:
            payload['params'] = params

        req = requests.post(self.url,
                            data=json.dumps(payload),
                            auth=self.auth,
                            verify=self.configuration.driver_ssl_cert_verify,
                            timeout=30)

        if not req.ok:
            raise exception.VolumeBackendAPIException(
                data=_('SPDK target responded with error: %s') % req.text)

        return req.json()['result']

    def _extract_pci_address(self, path):
        # TODO(lixiaoy1)
        pci = (r'/pci[0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,2}/([0-9a-fA-F]{1,4}):'
               r'([0-9a-fA-F]{1,2}):([0-9a-fA-F]{1,2})\.([0-7])/')
        pci_address = re.search(pci, path)
        if len(pci_address) == 0:
            return "", ""

        remainder = re.sub(pci, "", path)
        return pci_address, remainder

    def _extract_scsi(self, info):
        # TODO(lixiaoy1)
        scsi = r'/target\d+:\d+:\d+/\d+:\d+:(\d+):(\d+)/block/'
        parts = re.search(scsi, info)
        if len(parts) == 0:
            return "", ""
        return parts[1], parts[2]

    def _find_dev(self, sys_path, pciAddress, scsi_disk):
        for retry in range(1, self.device_scan_attempts + 1):
            files = os.listdir(sys_path)
            for entry in files:
                target = os.readlink(entry)
                if not target:
                    continue
                currentAddr, remainder = self._extract_pci_address(target)
                if not currentAddr or currentAddr != pciAddress:
                    continue
                if scsi_disk:
                    currentSCSI = self._extract_scsi(remainder)
                    if currentSCSI != scsi_disk:
                        continue
                return entry
        return ""

    @utils.trace
    def connect_volume(self, connection_properties):
        """Connect to a RBD volume.

        :param connection_properties: The dictionary that describes all
                                      of the target volume attributes.
        :type connection_properties: dict
        :returns: dict
        """
        # Create bdev
        try:
            user = connection_properties['auth_username']
            pool, volume = connection_properties['name'].split('/')
            cluster_name = connection_properties['cluster_name']
            monitor_ips = connection_properties['hosts']
            monitor_ports = connection_properties['ports']
            keyring = connection_properties.get('keyring')
        except (KeyError, ValueError):
            msg = _("Connect volume failed, malformed connection properties.")
            raise exception.BrickException(msg=msg)

        bdev_name = pool + volume
        conf = self._create_ceph_conf(monitor_ips, monitor_ports,
                                      str(cluster_name), user,
                                      keyring)

        params = {'pool_name': pool,
                  'rbd_name': volume,
                  'config': conf,
                  'user': user,
                  'name': bdev_name,
                  'block_size': 512,
                  }
        self._rpc_call('construct_rbd_bdev', params)

        # Create vhost target
        target = 0
        for target in range(0, 8):
            params = {'ctrlr': self.vhost,
                      'scsi_target_num': target,
                      'bdev_name': bdev_name,
                      }
            try:
                self._rpc_call('add_vhost_scsi_lun', params)
                break
            except Exception:
                pass

        if target >= 8:
            msg = _("Connect volume failed, can't add to vhost.")
            raise exception.BrickException(msg=msg)

        # Find local path of the disk
        device = self._find_dev("/dev/sys", self.pci_address, target)
        if not device:
            msg = _("Connect volume failed, can't find local path.")
            raise exception.BrickException(msg=msg)

        return {'path': device}

    @utils.trace
    def disconnect_volume(self, connection_properties, device_info,
                          force=False, ignore_errors=False):
        """Disconnect a volume.

        :param connection_properties: The dictionary that describes all
                                      of the target volume attributes.
        :type connection_properties: dict
        :param device_info: historical difference, but same as connection_props
        :type device_info: dict
        """
        # delete vhost target
        # extract target
        currentAddr, remainder = self._extract_pci_address(device_info['path'])
        if not currentAddr or currentAddr != self.pciAddress:
            msg = _("Disconnect volume failed, can't find device.")
            raise exception.BrickException(msg=msg)
        currentSCSI = self._extract_scsi(remainder)
        if currentSCSI == "":
            msg = _("Disconnect volume failed, can't find scsi.")
            raise exception.BrickException(msg=msg)

        params = {'ctrlr': self.vhost,
                  'scsi_target_num': currentSCSI,
                  }
        self._rpc_call('remove_vhost_scsi_target', params)

        # delete bdev
        try:
            pool, volume = connection_properties['name'].split('/')
        except (KeyError, ValueError):
            msg = _("Connect volume failed, malformed connection properties.")
            raise exception.BrickException(msg=msg)
        bdev = pool + volume

        params = {'name': bdev}
        self._rpc_call('delete_rbd_bdev', params)

    def extend_volume(self, connection_properties):
        # TODO(walter-boring): is this possible?
        raise NotImplementedError
