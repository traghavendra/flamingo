#    (c) Copyright 2025 Hewlett Packard Enterprise Development LP
#    All Rights Reserved.
#
#    Copyright 2012 OpenStack Foundation
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
        # self.lookup_service = fczm_utils.create_lookup_service()
        self.protocol = constants.NVMEOF_TCP

    def _do_setup(self, common):
        self.nvme_ips = {}
        self.nvme_ports = []
        common.client_login()
        try:
            self.get_nvme_ips_and_ports(common)
        finally:
            self._logout(common)

    def get_nvme_ips_and_ports(self, common):
        # map nvme_ip-> ip_port
        #             -> nsp
        nvme_ip_list = {}
        temp_nvme_ip = {}

        backend_conf = common._client_conf
        client_obj = common.client

        conf_ips = backend_conf['hpe3par_nvme_ips']
        self.nvme_ports = client_obj.match_conf_ips_with_array(
            conf_ips, temp_nvme_ip, nvme_ip_list)
        LOG.debug("nvme_ports: %(ports)s", {'ports': self.nvme_ports})

        # lets see if there are invalid nvme IPs left in the temp dict
        if len(temp_nvme_ip) > 0:
            LOG.warning("Found invalid nvme IP address(s) in "
                        "configuration option(s) hpe3par_nvme_ips '%s.'",
                        (", ".join(temp_nvme_ip)))

        if not len(nvme_ip_list):
            msg = _('At least one valid nvme IP address must be set.')
            LOG.error(msg)
            raise exception.InvalidInput(reason=msg)

        LOG.debug("nvme_ip_list: %(ip_list)s", {'ip_list': nvme_ip_list})
        self.nvme_ips[common._client_conf['hpe3par_api_url']] = (
            nvme_ip_list)

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
            LOG.debug("connector: %(connector)s", {'connector': connector})
            host = self._create_host(common, volume, connector)

            # Grab the nvme ip details from cinder.conf
            nvme_ips = self.nvme_ips[common._client_conf['hpe3par_api_url']]

            multipath = connector.get('multipath')

            host_nqn = connector['nqn']
            client_obj = common.client

            portals = []
            target_nqns = []

            self._create_vlun(
                volume, common, host,
                nvme_ips, self.nvme_ports, multipath,
                portals, target_nqns)

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

    def _create_host(self, common, volume, connector):
        """Check whether host exists with same hostname

           if found return host
           else create new host

        """
        host = None
        domain = None
        hostname = common._safe_hostname(connector, self.configuration)
        cpg = common.get_cpg(volume, allowSnap=True)
        domain = common.get_domain(cpg)

        client_obj = common.client
        nqn = connector['nqn']
        try:
            host = client_obj.getHost(hostname)
            LOG.debug("host is present")
            return host
        except hpeexceptions.HTTPNotFound:
            LOG.debug("host doesn't exist. Creating host")
            try:
                client_obj.createHost(hostname, nqn=nqn,
                                      optional={'domain': domain})
            except Exception as ex:
                LOG.error("Exception occurred: %(ex)s", {'ex': str(ex)})
                raise

            host = client_obj.getHost(hostname)
            return host
        except Exception as ex:
            LOG.error("Exception occurred: %(ex)s", {'ex': str(ex)})
            raise

    def _create_vlun(self, volume, common, host,
                     nvme_ips, ready_ports, multipath,
                     target_portals, target_nqns):

        # Target portal ips are defined in cinder.conf.
        target_portal_ips = list(nvme_ips.keys())
        cinder_conf_ips = []
        if multipath:
            # consider all ips
            cinder_conf_ips = target_portal_ips
        else:
            # consider only the first ip
            cinder_conf_ips.append(target_portal_ips[0])

        client_obj = common.client
        vol_name_3par = common._get_3par_vol_name(volume)

        # Collect all existing VLUNs for this volume/host combination.
        existing_vluns = client_obj.find_existing_vluns(vol_name_3par, host)
        LOG.debug("existing_vluns: %(ev)s", {'ev': existing_vluns})

        # Cycle through each ready nvme port and determine if a new
        # VLUN should be created or an existing one used.
        lun_id = None
        for port in ready_ports:
            nvme_ip = port['nodeWWN']
            if nvme_ip in cinder_conf_ips:
                port_nqn = ''

                ret_vals = client_obj.create_vlun_nvme(lun_id, vol_name_3par, host,
                    existing_vluns, nvme_ip, nvme_ips)
                lun_id = ret_vals[0]
                port_nqn = ret_vals[1]

                target_portals.append(
                    (nvme_ip, nvme_ips[nvme_ip]['ip_port'], 'tcp') 
                    )
                target_nqns.append(port_nqn)
                # target_luns.append(lun_id)
            else:
                LOG.debug("nvme IP: '%s' was not found in "
                          "hpe3par_nvme_ips list defined in "
                          "cinder.conf.", nvme_ip)

