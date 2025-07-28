#    (c) Copyright 2025 Hewlett Packard Enterprise Development LP
#    All Rights Reserved.
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
#
"""Volume driver for HPE Alletra MP Storage array.

Set the following in the cinder.conf file along with the required flags:

volume_driver=cinder.volume.drivers.hpe.hpe_3par_nvme_tcp.HPE3PARNVMETCPDriver
"""
try:
    from hpe3parclient import exceptions as hpeexceptions
except ImportError:
    hpeexceptions = None

from oslo_log import log as logging

from cinder.common import constants
from cinder import interface
from cinder.volume.drivers.hpe import hpe_3par_base as hpebasedriver
from cinder.volume import volume_utils

LOG = logging.getLogger(__name__)


@interface.volumedriver
class HPE3PARNVMETCPDriver(hpebasedriver.HPE3PARDriverBase):
    """OpenStack NVMe TCP driver to enable Alletra MP storage array.

    Version history:

    .. code-block:: none

        1.0   - Initial driver

    """

    VERSION = "1.0"

    # The name of the CI wiki page.
    CI_WIKI_NAME = "HPE_Storage_CI"

    def __init__(self, *args, **kwargs):
        super(HPE3PARNVMETCPDriver, self).__init__(*args, **kwargs)
        self.protocol = constants.NVMEOF_TCP

    def _do_setup(self, common):
        self.nvme_ips = {}
        self.nvme_ports = {}
        common.client_login()
        try:
            self.initialize_nvme_ips_and_ports(common)
        finally:
            self._logout(common)

    def initialize_nvme_ips_and_ports(self, common):
        # map nvme_ip -> ip_port
        #             -> nsp
        cinder_conf = common._client_conf
        hpe3par_client = common.client

        # check if nvme_ips (read from cinder.conf) are present on array.
        nvme_ip_list, nvme_port_list = (
            hpe3par_client.get_matched_array_ips_and_ports(cinder_conf))
        storage_system_id = cinder_conf['hpe3par_api_url']
        self.nvme_ips[storage_system_id] = nvme_ip_list
        self.nvme_ports[storage_system_id] = nvme_port_list

        LOG.debug("nvme_ip_list: %(ip_list)s", {'ip_list': nvme_ip_list})
        LOG.debug("nvme_port_list: %(ports)s", {'ports': nvme_port_list})

    @volume_utils.trace
    def initialize_connection(self, volume, connector):
        """Assigns the volume to a server.

        Steps to export a volume on array:
          * Create a host on the array with the target nqn
          * Create a VLUN with the volume we want to export.

        """
        LOG.debug("volume id: %(id)s", {'id': volume['id']})
        common = self._login()

        try:
            LOG.debug("connector: %(conn)s", {'conn': connector})
            
            hpe3par_client = common.client
            host_nqn = connector['nqn']

            hostname = common._safe_hostname(connector, self.configuration)
            cpg = common.get_cpg(volume, allowSnap=True)
            domain = common.get_domain(cpg)

            # Check whether host exists with same hostname
            # if found: use that host
            # else: create new host using nqn and domain
            host = hpe3par_client.create_host_nvme(
                hostname, nqn=host_nqn, domain=domain)

            storage_system_id = common._client_conf['hpe3par_api_url']
            nvme_ips = self.nvme_ips[storage_system_id]
            ready_ports = self.nvme_ports[storage_system_id]

            multipath = connector.get('multipath')
            # multipath = True

            vol_name_3par = common._get_3par_vol_name(volume)

            portals, target_nqns = hpe3par_client.create_vlun_nvme(
                vol_name_3par, host, nvme_ips, ready_ports, multipath)

            info = {'driver_volume_type': 'nvmeof',
                    'data': {'portals': portals,
                             'target_nqn': target_nqns[0],
                             'host_nqn': host_nqn,
                             }
                    }
            LOG.debug("info: %(info)s", {'info': info})
            return info
        finally:
            self._logout(common)

    @volume_utils.trace
    def terminate_connection(self, volume, connector, **kwargs):

        LOG.debug("volume id: %(id)s", {'id': volume['id']})
        common = self._login()

        try:
            LOG.debug("connector: %(conn)s", {'conn': connector})
            
            hpe3par_client = common.client

            host_nqn = connector['nqn']
            hostname = connector['host']
            vol_name_3par = common._get_3par_vol_name(volume)

            hpe3par_client.remove_vlun_nvme(vol_name_3par, hostname, host_nqn)

        finally:
            self._logout(common)
