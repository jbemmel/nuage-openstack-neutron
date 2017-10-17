# Copyright 2017 Nokia
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

import inspect
import netaddr
import time

from oslo_config import cfg
from oslo_log import helpers as log_helpers
from oslo_log import log
from oslo_utils import excutils

from neutron._i18n import _
from neutron.api import extensions as neutron_extensions
from neutron.callbacks import resources
from neutron.db import db_base_plugin_v2
from neutron.extensions import external_net
from neutron.extensions import securitygroup as ext_sg
from neutron.plugins.common import constants as p_constants
from neutron.plugins.ml2 import driver_api as api
from neutron.plugins.ml2 import plugin as ml2_plugin
from neutron.services.trunk import constants as t_consts
from neutron_lib.api.definitions import port_security as portsecurity
from neutron_lib.api.definitions import portbindings
from neutron_lib.api import validators as lib_validators
from neutron_lib import constants as os_constants
from neutron_lib.exceptions import PortInUse
from neutron_lib.plugins import directory

from nuage_neutron.plugins.common.addresspair import NuageAddressPair
from nuage_neutron.plugins.common import config as nuage_config
from nuage_neutron.plugins.common import constants
from nuage_neutron.plugins.common.exceptions import NuageBadRequest
from nuage_neutron.plugins.common.exceptions import \
    NuageDualstackSubnetNotFound
from nuage_neutron.plugins.common.exceptions import NuagePortBound
from nuage_neutron.plugins.common import extensions
from nuage_neutron.plugins.common.extensions import nuage_redirect_target
from nuage_neutron.plugins.common.extensions import nuagefloatingip
from nuage_neutron.plugins.common.extensions import nuagepolicygroup
from nuage_neutron.plugins.common import nuagedb
from nuage_neutron.plugins.common.time_tracker import TimeTracker
from nuage_neutron.plugins.common import utils
from nuage_neutron.plugins.common.utils import handle_nuage_api_errorcode
from nuage_neutron.plugins.common.utils import ignore_no_update
from nuage_neutron.plugins.common.utils import ignore_not_found
from nuage_neutron.plugins.common.validation import Is
from nuage_neutron.plugins.common.validation import IsSet
from nuage_neutron.plugins.common.validation import require
from nuage_neutron.plugins.common.validation import validate

from nuage_neutron.plugins.nuage_ml2 import extensions  # noqa
from nuage_neutron.plugins.nuage_ml2.nuage_ml2_wrapper import NuageML2Wrapper
from nuage_neutron.plugins.nuage_ml2.securitygroup import NuageSecurityGroup
from nuage_neutron.plugins.nuage_ml2 import trunk_driver

from nuage_neutron.vsdclient.common import constants as vsd_constants
from nuage_neutron.vsdclient.common.helper import get_l2_and_l3_sub_id
from nuage_neutron.vsdclient import restproxy


LB_DEVICE_OWNER_V2 = os_constants.DEVICE_OWNER_LOADBALANCERV2
PORT_UNPLUGGED_TYPES = (portbindings.VIF_TYPE_BINDING_FAILED,
                        portbindings.VIF_TYPE_UNBOUND,
                        portbindings.VIF_TYPE_OVS)
DEVICE_OWNER_DHCP = os_constants.DEVICE_OWNER_DHCP

LOG = log.getLogger(__name__)


def _is_ipv4(subnet):
    return subnet['ip_version'] == os_constants.IP_VERSION_4


def _is_ipv6(subnet):
    return subnet['ip_version'] == os_constants.IP_VERSION_6


def _is_trunk_subport(port):
    return t_consts.TRUNK_SUBPORT_OWNER == port.get('device_owner')


class NuageMechanismDriver(NuageML2Wrapper):

    def initialize(self):
        LOG.debug('Initializing driver')
        neutron_extensions.append_api_extensions_path(extensions.__path__)
        self._validate_mech_nuage_configuration()
        self.init_vsd_client()
        self._wrap_vsdclient()
        self._core_plugin = None
        self._default_np_id = None

        NuageSecurityGroup().register()
        NuageAddressPair().register()
        db_base_plugin_v2.AUTO_DELETE_PORT_OWNERS += [
            constants.DEVICE_OWNER_DHCP_NUAGE]
        self.trunk_driver = trunk_driver.NuageTrunkDriver.create(self)

        if nuage_config.is_enabled(constants.DEBUG_TIMING_STATS):
            TimeTracker.start()
        if nuage_config.is_enabled(constants.FEATURE_EXPERIMENTAL_TEST):
            LOG.info("Have a nice day.")

        LOG.debug('Initializing complete')

    @property
    def default_np_id(self):
        if self._default_np_id is None:
            self._default_np_id = directory.get_plugin(
                constants.NUAGE_APIS).default_np_id
        return self._default_np_id

    def _validate_mech_nuage_configuration(self):
        service_plugins = constants.MIN_MECH_NUAGE_SERVICE_PLUGINS_IN_CONFIG
        extensions = constants.MIN_MECH_NUAGE_EXTENSIONS_IN_CONFIG
        self._validate_config_for_nuage_driver(constants.NUAGE_ML2_DRIVER_NAME,
                                               service_plugins,
                                               extensions)

    def _wrap_vsdclient(self):
        """Wraps nuagecient methods with try-except to ignore certain errors.

        When updating an entity on the VSD and there is nothing to actually
        update because the values don't change, VSD will throw an error. This
        is not needed for neutron so all these exceptions are ignored.

        When VSD responds with a 404, this is sometimes good (for example when
        trying to update an entity). Yet sometimes this is not required to be
        an actual exception. When deleting an entity that does no longer exist
        it is fine for neutron. Also when trying to retrieve something from VSD
        having None returned is easier to work with than RESTProxy exceptions.
        """

        methods = inspect.getmembers(self.vsdclient,
                                     lambda x: inspect.ismethod(x))
        for m in methods:
            wrapped = ignore_no_update(m[1])
            if m[0].startswith('get_') or m[0].startswith('delete_'):
                wrapped = ignore_not_found(wrapped)
            setattr(self.vsdclient, m[0], wrapped)

    @handle_nuage_api_errorcode
    @utils.context_log
    @TimeTracker.tracked
    def update_network_precommit(self, context):
        updated_network = context.current
        original_network = context.original
        db_context = context._plugin_context
        _no_action, _is_external_set, _is_shared_set = self._network_no_action(
            original_network, updated_network)
        if _no_action:
            return
        self._validate_update_network(db_context, _is_external_set,
                                      _is_shared_set, updated_network)

    @handle_nuage_api_errorcode
    @utils.context_log
    @TimeTracker.tracked
    def update_network_postcommit(self, context):
        updated_network = context.current
        original_network = context.original
        db_context = context._plugin_context
        _no_action, _is_external_set, _is_shared_set = self._network_no_action(
            original_network, updated_network)
        if _no_action:
            return
        subnets = self.get_subnets_by_network(db_context,
                                              updated_network['id'])
        if not subnets:
            return
        else:
            subn = subnets[0]
        subnet_l2dom = nuagedb.get_subnet_l2dom_by_id(db_context.session,
                                                      subn['id'])
        if subnet_l2dom and _is_external_set:
            LOG.debug("Found subnet %(subn_id)s to l2 domain mapping"
                      " %(nuage_subn_id)s",
                      {'subn_id': subn['id'],
                       'nuage_subn_id':
                           subnet_l2dom['nuage_subnet_id']})
            self.vsdclient.delete_subnet(subn['id'])
            nuagedb.delete_subnetl2dom_mapping(db_context.session,
                                               subnet_l2dom)
            # delete the neutron port that was reserved with IP of
            # the dhcp server that is reserved.
            # Now, this port is not reqd.
            filters = {
                'fixed_ips': {'subnet_id': [subn['id']]},
                'device_owner': [constants.DEVICE_OWNER_DHCP_NUAGE]
            }
            gw_ports = self.get_ports(db_context, filters=filters)
            self._delete_port_gateway(db_context, gw_ports)

            self._add_nuage_sharedresource(subn,
                                           updated_network['id'],
                                           constants.SR_TYPE_FLOATING)

        if _is_shared_set and not updated_network.get(external_net.EXTERNAL):
            for subnet in subnets:
                nuage_subnet_l2dom = nuagedb.get_subnet_l2dom_by_id(
                    db_context.session, subnet['id'])
                if nuage_subnet_l2dom['nuage_l2dom_tmplt_id']:
                    # change of perm only reqd in l2dom case
                    self.vsdclient.change_perm_of_subns(
                        nuage_subnet_l2dom['net_partition_id'],
                        nuage_subnet_l2dom['nuage_subnet_id'],
                        updated_network['shared'],
                        subnet['tenant_id'], remove_everybody=True)

    @handle_nuage_api_errorcode
    @TimeTracker.tracked
    def create_subnet_precommit(self, context):
        subnet = context.current
        network = context.network.current
        db_context = context._plugin_context

        nuagenet_set = lib_validators.is_attr_set(subnet.get('nuagenet'))
        net_part_set = lib_validators.is_attr_set(subnet.get('net_partition'))
        vsd_managed = nuagenet_set and net_part_set

        if nuagenet_set and not net_part_set:
            msg = _("Parameter net-partition required when "
                    "passing nuagenet")
            raise NuageBadRequest(resource='subnet', msg=msg)

        for attribute in ('ipv6_ra_mode', 'ipv6_address_mode'):
            if not lib_validators.is_attr_set(subnet.get(attribute)):
                continue
            if subnet[attribute] != os_constants.DHCPV6_STATEFUL:
                msg = _("Attribute %(attribute)s must be '%(allowed)s' or "
                        "not set.")
                raise NuageBadRequest(
                    resource='subnet',
                    msg=msg % {'attribute': attribute,
                               'allowed': os_constants.DHCPV6_STATEFUL})
        if self.is_vxlan_network(network):
            if vsd_managed:
                self._validate_create_vsd_managed_subnet(
                    db_context, network, subnet)
            else:
                self._validate_create_openstack_managed_subnet(
                    db_context, subnet)
        else:
            if nuagenet_set or net_part_set:
                # Nuage attributes set on non-vxlan network ...
                msg = _("Network should have 'provider:network_type' vxlan or "
                        "have such a segment")
                raise NuageBadRequest(msg=msg)
            else:
                return  # Not for us

        subnets = self.get_subnets(
            db_context,
            filters={'network_id': [subnet['network_id']]})
        subnet_ids = [s['id'] for s in subnets]
        subnet_mappings = nuagedb.get_subnet_l2doms_by_subnet_ids(
            db_context.session,
            subnet_ids)
        if len(set([vsd_managed] + [m['nuage_managed_subnet']
                                    for m in subnet_mappings])) > 1:
            msg = _("Can't mix openstack and vsd managed subnets under 1 "
                    "network.")
            raise NuageBadRequest(resource='subnet', msg=msg)

        ipv4s = len([s for s in subnets if _is_ipv4(s)])
        ipv6s = len([s for s in subnets if _is_ipv6(s)])

        if not (ipv4s <= 1 and ipv6s == 1 or ipv6s == 0):
            msg = _("A network with an ipv6 subnet may only have maximum 1 "
                    "ipv4 and 1 ipv6 subnet")
            raise NuageBadRequest(msg=msg)

        if self.is_vxlan_network(network):
            if vsd_managed:
                self._create_vsd_managed_subnet(db_context, subnet)
            else:
                self._create_openstack_managed_subnet(db_context, subnet)

        # take out underlay extension from the json response
        if subnet.get('underlay') == os_constants.ATTR_NOT_SPECIFIED:
            subnet['underlay'] = None

        if 'underlay' not in subnet:
            subnet['underlay'] = None

    def _create_vsd_managed_subnet(self, context, subnet):
        nuage_subnet_id = subnet['nuagenet']
        original_gateway = subnet['gateway_ip']
        nuage_npid = self._validate_net_partition(subnet, context)
        if not self.vsdclient.check_if_l2Dom_in_correct_ent(
                nuage_subnet_id, {'id': nuage_npid}):
            msg = ("Provided Nuage subnet not in the provided"
                   " Nuage net-partition")
            raise NuageBadRequest(msg=msg)
        subnet_db = nuagedb.get_subnet_l2dom_by_nuage_id(
            context.session, nuage_subnet_id)
        nuage_subnet, shared_subnet = self._get_nuage_subnet(
            subnet_db, nuage_subnet_id)
        self._validate_cidr(subnet, nuage_subnet, shared_subnet)
        self._set_gateway_from_vsd(nuage_subnet, shared_subnet, subnet)
        result = self.vsdclient.attach_nuage_group_to_nuagenet(
            context.tenant, nuage_npid, nuage_subnet_id,
            subnet.get('shared'))
        (nuage_uid, nuage_gid) = result
        try:
            with context.session.begin(subtransactions=True):
                self._update_gw_and_pools(context, subnet, original_gateway)
                self._reserve_dhcp_ip(context, subnet, nuage_subnet,
                                      shared_subnet)
                l2dom_id = None
                if nuage_subnet["type"] == constants.L2DOMAIN:
                    l2dom_id = nuage_subnet_id
                nuagedb.add_subnetl2dom_mapping(
                    context.session, subnet['id'], nuage_subnet_id,
                    nuage_npid, subnet['ip_version'],
                    nuage_user_id=nuage_uid, l2dom_id=l2dom_id,
                    nuage_group_id=nuage_gid, managed=True)
                subnet['vsd_managed'] = True
        except Exception:
            self._cleanup_group(context, nuage_npid, nuage_subnet_id,
                                subnet)
            raise

    def _network_is_external(self, context, net_id):
        return self.core_plugin._network_is_external(context, net_id)

    def update_port_status(self, context, port_id, status, host=None,
                           network=None):
        return self.core_plugin.update_port_status(context, port_id, status,
                                                   host, network)

    def _create_openstack_managed_subnet(self, context, subnet):
        network_external = self._network_is_external(
            context,
            subnet['network_id'])

        if network_external:

            if _is_ipv6(subnet):
                msg = _("Subnet with ip_version 6 is currently not supported "
                        "for router:external networks.")
                raise NuageBadRequest(msg=msg)
            net_id = subnet['network_id']
            self._validate_nuage_sharedresource(context, net_id)
            return self._add_nuage_sharedresource(subnet, subnet['network_id'],
                                                  constants.SR_TYPE_FLOATING)

        net_partition = self._get_net_partition_for_subnet(context, subnet)
        attempt = 0
        while True:
            try:
                with context.session.begin(subtransactions=True):
                    self._create_nuage_subnet(
                        context, subnet, net_partition['id'], None)
                return
            except NuageDualstackSubnetNotFound:
                if attempt < 25:
                    LOG.debug("Retrying due to concurrency.")
                    attempt += 1
                    time.sleep(0.2)
                    continue
                msg = "Failed to create subnet on vsd"
                raise Exception(msg)

    def _get_net_partition_for_subnet(self, context, subnet):
        ent = subnet.get('net_partition', None)
        if not ent:
            net_partition = nuagedb.get_net_partition_by_id(context.session,
                                                            self.default_np_id)
        else:
            net_partition = (
                nuagedb.get_net_partition_by_id(context.session,
                                                subnet['net_partition']) or
                nuagedb.get_net_partition_by_name(context.session,
                                                  subnet['net_partition'])
            )
        if not net_partition:
            msg = _('Either net_partition is not provided with subnet OR '
                    'default net_partition is not created at the start')
            raise NuageBadRequest(resource='subnet', msg=msg)
        return net_partition

    @log_helpers.log_method_call
    def _add_nuage_sharedresource(self, subnet, net_id, fip_type):
        net = netaddr.IPNetwork(subnet['cidr'])
        params = {
            'neutron_subnet': subnet,
            'net': net,
            'type': fip_type,
            'net_id': net_id,
            'underlay_config': cfg.CONF.RESTPROXY.nuage_fip_underlay
        }
        if subnet.get('underlay') in [True, False]:
            params['underlay'] = subnet.get('underlay')
        else:
            subnet['underlay'] = params['underlay_config']
        if subnet.get('nuage_uplink'):
            params['nuage_uplink'] = subnet.get('nuage_uplink')
        elif cfg.CONF.RESTPROXY.nuage_uplink:
            subnet['nuage_uplink'] = cfg.CONF.RESTPROXY.nuage_uplink
            params['nuage_uplink'] = cfg.CONF.RESTPROXY.nuage_uplink

        self.vsdclient.create_nuage_sharedresource(params)

    @log_helpers.log_method_call
    def get_dual_stack_subnet(self, context, neutron_subnet):
        existing_subnet = self._check_existing_subnet_on_network(
            context, neutron_subnet)
        if existing_subnet is None:
            return None
        if existing_subnet["ip_version"] != neutron_subnet["ip_version"]:
            return existing_subnet
        return None

    @log_helpers.log_method_call
    def _get_dhcp_port(self, context, neutron_subnet):
        if neutron_subnet.get('enable_dhcp'):
            last_address = neutron_subnet['allocation_pools'][-1]['end']
            return self._reserve_ip(context,
                                    neutron_subnet,
                                    last_address)
        else:
            LOG.warning(_("CIDR parameter ignored for unmanaged subnet "))
            LOG.warning(_("Allocation Pool parameter ignored"
                          " for unmanaged subnet "))
            return None

    @log_helpers.log_method_call
    def check_if_subnet_is_attached_to_router(self, context, subnet):
        filters = {
            'network_id': [subnet['network_id']],
            'device_owner': [os_constants.DEVICE_OWNER_ROUTER_INTF]
        }
        ports = self.get_ports(context, filters)
        for p in ports:
            for ip in p['fixed_ips']:
                if ip['subnet_id'] in subnet['id']:
                    router_id = nuagedb.get_routerport_by_port_id(
                        context.session, p['id'])['router_id']
                    return True, str(router_id)
        return False, None

    @handle_nuage_api_errorcode
    @log_helpers.log_method_call
    def _create_nuage_subnet(self, context, neutron_subnet, netpart_id,
                             pnet_binding):
        gw_port = None
        r_param = {}
        neutron_net = self.core_plugin.get_network(
            context, neutron_subnet['network_id'])
        is_ipv4 = _is_ipv4(neutron_subnet)
        dual_stack_subnet = self.get_dual_stack_subnet(context, neutron_subnet)

        if not (dual_stack_subnet or is_ipv4):
            return  # ipv6 without existing ipv4 is no-op.
        elif dual_stack_subnet:
            # ipv6 is already present and now check
            # if router interface is attached or not
            router_attached, router_id = \
                self.check_if_subnet_is_attached_to_router(
                    context, dual_stack_subnet)
            if router_attached:
                pnet_binding = nuagedb.get_network_binding(
                    context.session,
                    dual_stack_subnet['network_id'])
                r_param['router_attached'] = True
                r_param['pnet_binding'] = pnet_binding
                r_param['router_id'] = router_id

        # If the request is for IPv4, then the dualstack subnet will be IPv6
        # and vice versa
        if is_ipv4:
            ipv4_subnet, ipv6_subnet = neutron_subnet, dual_stack_subnet
        else:
            ipv4_subnet, ipv6_subnet = dual_stack_subnet, neutron_subnet

        net = netaddr.IPNetwork(ipv4_subnet['cidr'])
        params = {
            'netpart_id': netpart_id,
            'tenant_id': neutron_subnet['tenant_id'],
            'net': net,
            'pnet_binding': pnet_binding,
            'shared': neutron_net['shared'],
            'dhcp_ip': ipv4_subnet['allocation_pools'][-1]['end'] if
            ipv4_subnet['enable_dhcp'] else None,
        }

        if is_ipv4:
            gw_port = self._get_dhcp_port(context, neutron_subnet)
            if gw_port:
                params['dhcp_ip'] = gw_port['fixed_ips'][0]['ip_address']
        else:
            subnet_mapping = nuagedb.get_subnet_l2dom_by_id(
                context.session, ipv4_subnet['id'])

            if subnet_mapping is None:
                raise NuageDualstackSubnetNotFound(
                    resource="Subnet")
            params['mapping'] = subnet_mapping
        params.update(r_param)
        try:
            nuage_subnet = self.vsdclient.create_subnet(
                ipv4_subnet,
                params=params,
                ipv6_subnet=ipv6_subnet)
        except Exception:
            with excutils.save_and_reraise_exception():
                if gw_port:
                    LOG.debug(_("Deleting gw_port %s"), gw_port['id'])
                    # Because we are inside a transaction in a precommit method
                    # you are not allowed to call any of the crud_<resource>
                    # methods of ml2plugin (like delete_port). Because of
                    # neutron's transaction_guard decorator. By calling the
                    # super we avoid this check and go straight to the DB to
                    # delete the port. Similar to how we call the db method for
                    # creating the dhcp port.
                    super(ml2_plugin.Ml2Plugin,
                          self.core_plugin).delete_port(context, gw_port['id'])

        if not is_ipv4 and subnet_mapping:
            # nuage_subnet is None: Copy ipv4 mapping for creating ipv6 mapping
            nuage_subnet = {
                'nuage_l2template_id': subnet_mapping[
                    'nuage_l2dom_tmplt_id'],
                'nuage_userid': subnet_mapping['nuage_user_id'],
                'nuage_groupid': subnet_mapping['nuage_group_id'],
                'nuage_l2domain_id': subnet_mapping['nuage_subnet_id']
            }

        if nuage_subnet:
            self._create_subnet_mapping(context, netpart_id, neutron_subnet,
                                        nuage_subnet)
            if dual_stack_subnet and is_ipv4:
                self._create_subnet_mapping(context, netpart_id,
                                            dual_stack_subnet, nuage_subnet)

    @staticmethod
    def _create_subnet_mapping(context, netpart_id, neutron_subnet,
                               nuage_subnet):
        l2dom_id = nuage_subnet['nuage_l2template_id']
        user_id = nuage_subnet['nuage_userid']
        group_id = nuage_subnet['nuage_groupid']
        nuage_id = nuage_subnet['nuage_l2domain_id']
        with context.session.begin(subtransactions=True):
            nuagedb.add_subnetl2dom_mapping(context.session,
                                            neutron_subnet['id'],
                                            nuage_id,
                                            netpart_id,
                                            neutron_subnet['ip_version'],
                                            l2dom_id=l2dom_id,
                                            nuage_user_id=user_id,
                                            nuage_group_id=group_id)
        neutron_subnet['net_partition'] = netpart_id
        neutron_subnet['nuagenet'] = nuage_id

    @staticmethod
    def _validate_dhcp_opts_changed(original_subnet, updated_subnet):
        if _is_ipv6(original_subnet):
            return False
        for k in ['dns_nameservers', 'host_routes', 'gateway_ip']:
            if original_subnet.get(k) != updated_subnet.get(k):
                return True
        return False

    @utils.context_log
    @TimeTracker.tracked
    def update_subnet_precommit(self, context):
        updated_subnet = context.current
        original_subnet = context.original
        db_context = context._plugin_context
        subnet_mapping = nuagedb.get_subnet_l2dom_by_id(db_context.session,
                                                        updated_subnet['id'])
        net_id = original_subnet['network_id']
        network_external = self._network_is_external(db_context, net_id)
        if not subnet_mapping and not network_external:
            return
        elif subnet_mapping and subnet_mapping['nuage_managed_subnet']:
            raise NuageBadRequest(
                msg=_("Subnet %s is a VSD-managed subnet. Update is not "
                      "supported.") % updated_subnet['id'])

        if network_external:
            return self._update_ext_network_subnet(updated_subnet['id'],
                                                   net_id,
                                                   updated_subnet)
        if subnet_mapping['nuage_managed_subnet']:
            msg = ("Subnet %s is a VSD-Managed subnet."
                   " Update is not supported." % subnet_mapping['subnet_id'])
            raise NuageBadRequest(resource='subnet', msg=msg)
        if not network_external and updated_subnet.get('underlay') is not None:
            msg = _("underlay attribute can not be set for internal subnets")
            raise NuageBadRequest(msg=msg)

        params = {
            'parent_id': subnet_mapping['nuage_subnet_id'],
            'type': subnet_mapping['nuage_l2dom_tmplt_id']
        }
        if _is_ipv6(updated_subnet):
            current_gw = netaddr.IPNetwork(
                original_subnet.get('gateway_ip')) if original_subnet.get(
                'gateway_ip') else None
            updated_gw = netaddr.IPNetwork(
                updated_subnet.get('gateway_ip')) if updated_subnet.get(
                'gateway_ip') else None
            if current_gw != updated_gw:
                params["gatewayv6_changed"] = True
            else:
                return
        else:
            # Nuage plugin only updates dhcp in case of ipv4.
            # In case of IPv6, we don't create DHCP opts to correspond
            # to Gateway IP as upstream code takes care of it.
            # The check whether gateway_ip changed for ipv4 is part of the
            # '_validate_dhcp_opts_changed' code.
            curr_enable_dhcp = original_subnet.get('enable_dhcp')
            updated_enable_dhcp = updated_subnet.get('enable_dhcp')
            if not curr_enable_dhcp and updated_enable_dhcp:
                last_address = updated_subnet['allocation_pools'][-1]['end']
                gw_port = self._reserve_ip(db_context, updated_subnet,
                                           last_address)
                params['net'] = netaddr.IPNetwork(original_subnet['cidr'])
                params['dhcp_ip'] = gw_port['fixed_ips'][0]['ip_address']
            elif curr_enable_dhcp and not updated_enable_dhcp:
                params['dhcp_ip'] = None
                filters = {
                    'fixed_ips': {'subnet_id': [updated_subnet['id']]},
                    'device_owner': [constants.DEVICE_OWNER_DHCP_NUAGE]
                }
                gw_ports = self.core_plugin.get_ports(db_context,
                                                      filters=filters)
                self._delete_port_gateway(db_context, gw_ports)
            dhcp_opts_changed = self._validate_dhcp_opts_changed(
                original_subnet,
                updated_subnet)
            params['dhcp_opts_changed'] = dhcp_opts_changed
        self.vsdclient.update_subnet(updated_subnet, params)

    def _update_ext_network_subnet(self, id, net_id, subnet):
        nuage_params = {
            'subnet_name': subnet.get('name'),
            'net_id': net_id,
            'gateway_ip': subnet.get('gateway_ip')
        }
        self.vsdclient.update_nuage_sharedresource(id, nuage_params)
        nuage_subnet = self.vsdclient.get_sharedresource(id)
        subnet['underlay'] = nuage_subnet['underlay']

    @log_helpers.log_method_call
    def _delete_port_gateway(self, context, ports):
        for port in ports:
            if not port.get('fixed_ips'):
                db_base_plugin_v2.NeutronDbPluginV2.delete_port(
                    self.core_plugin, context, port['id'])

    @utils.context_log
    @TimeTracker.tracked
    def delete_subnet_precommit(self, context):
        """Get subnet_l2dom_mapping for later.

        In postcommit this nuage_subnet_l2dom_mapping is no longer available
        because it is set to CASCADE with the subnet. So this row will be
        deleted prior to delete_subnet_postcommit
        """
        subnet = context.current
        db_context = context._plugin_context
        context.nuage_mapping = nuagedb.get_subnet_l2dom_by_id(
            db_context.session, subnet['id'])
        if not context.nuage_mapping:
            return

        filters = {
            'network_id': [subnet['network_id']],
            'device_owner': [constants.DEVICE_OWNER_DHCP_NUAGE]
        }
        context.nuage_ports = self.get_ports(db_context, filters)

    @handle_nuage_api_errorcode
    @TimeTracker.tracked
    def delete_subnet_postcommit(self, context):
        db_context = context._plugin_context
        subnet = context.current
        mapping = context.nuage_mapping
        network_external = self._network_is_external(
            db_context,
            subnet['network_id'])

        if network_external:
            self.vsdclient.delete_nuage_sharedresource(subnet['id'])
        if not mapping:
            return

        if not mapping['nuage_managed_subnet']:
            if _is_ipv6(subnet):
                self.vsdclient.delete_subnet(subnet['id'], mapping=mapping)
                return
            else:
                self.vsdclient.delete_subnet(subnet['id'])
                ipv6_subnet = self.get_dual_stack_subnet(db_context, subnet)
                if ipv6_subnet:
                    ipv6_mapping = nuagedb.get_subnet_l2dom_by_id(
                        db_context.session,
                        ipv6_subnet['id'])
                    with db_context.session.begin(subtransactions=True):
                        nuagedb.delete_subnetl2dom_mapping(
                            db_context.session,
                            ipv6_mapping)
        else:
            # VSD managed could be ipv6 + ipv4. If only one of the 2 is
            # deleted, the use permission should not be removed yet.
            clean_groups = True
            other_mapping = nuagedb.get_subnet_l2dom_by_nuage_id(
                db_context.session,
                mapping['nuage_subnet_id'])

            if other_mapping is not None:
                other_subnet = context._plugin.get_subnet(
                    db_context,
                    other_mapping['subnet_id'])
                if subnet['tenant_id'] == other_subnet['tenant_id']:
                    clean_groups = False

            if clean_groups:
                self._cleanup_group(db_context,
                                    mapping['net_partition_id'],
                                    mapping['nuage_subnet_id'], subnet)

        self._delete_port_gateway(db_context, context.nuage_ports)

    @handle_nuage_api_errorcode
    @utils.context_log
    @TimeTracker.tracked
    def create_port_postcommit(self, context):
        self._create_port(context._plugin_context,
                          context.current,
                          context.network)

    def _create_port(self, db_context, port, network, update_status=True):
        is_network_external = network._network.get('router:external')
        subnet_mapping = self._validate_port(db_context, port,
                                             constants.BEFORE_CREATE,
                                             is_network_external)
        if not subnet_mapping:
            if len(port['fixed_ips']) == 0:
                nuage_attributes = (nuage_redirect_target.REDIRECTTARGETS,
                                    nuagepolicygroup.NUAGE_POLICY_GROUPS,
                                    nuagefloatingip.NUAGE_FLOATINGIP)
                for attribute in nuage_attributes:
                    if attribute in port:
                        del port[attribute]
            LOG.warn('no subnet_mapping')
            return
        nuage_vport = nuage_vm = np_name = None
        try:
            np_id = subnet_mapping['net_partition_id']
            nuage_subnet, _ = self._get_nuage_subnet(
                subnet_mapping, subnet_mapping['nuage_subnet_id'])
            if port.get('binding:host_id') and self._port_should_have_vm(port):
                self._validate_vmports_same_netpartition(db_context,
                                                         port, np_id)
                desc = ("device_owner:" + constants.NOVA_PORT_OWNER_PREF +
                        "(please do not edit)")
                nuage_vport = self._create_nuage_vport(port, nuage_subnet,
                                                       desc)
                np_name = self.vsdclient.get_net_partition_name_by_id(np_id)
                require(np_name, "netpartition", np_id)
                nuage_vm = self._create_nuage_vm(
                    db_context, port, np_name, subnet_mapping,
                    nuage_vport, nuage_subnet)
            else:
                nuage_vport = self._create_nuage_vport(port, nuage_subnet)

            if (not port[portsecurity.PORTSECURITY] and
                    not subnet_mapping['nuage_managed_subnet']):
                self._process_port_create_secgrp_for_port_sec(db_context, port)
        except Exception:
            if nuage_vm:
                self._delete_nuage_vm(db_context, port, np_name,
                                      subnet_mapping)
            if nuage_vport:
                self.vsdclient.delete_nuage_vport(nuage_vport.get('ID'))
            raise
        rollbacks = []
        try:
            self.nuage_callbacks.notify(resources.PORT, constants.AFTER_CREATE,
                                        self, context=db_context, port=port,
                                        vport=nuage_vport, rollbacks=rollbacks,
                                        subnet_mapping=subnet_mapping)
            if (port.get('nuage_redirect-targets') !=
                    os_constants.ATTR_NOT_SPECIFIED and update_status):
                self.core_plugin.update_port_status(
                    db_context,
                    port['id'],
                    os_constants.PORT_STATUS_ACTIVE)
        except Exception:
            with excutils.save_and_reraise_exception():
                    for rollback in reversed(rollbacks):
                        rollback[0](*rollback[1], **rollback[2])

    @handle_nuage_api_errorcode
    @utils.context_log
    @TimeTracker.tracked
    def update_port_precommit(self, context):
        db_context = context._plugin_context
        port = context.current
        original = context.original

        is_network_external = context.network._network.get('router:external')

        if (len(port['fixed_ips']) == 0 and len(original['fixed_ips']) != 0 or
                self._ipv4_addr_removed_from_dualstack_dhcp_port(
                    original, port)):
            # port no longer belongs to any subnet or dhcp port has regressed
            # to ipv6 only: delete vport.
            self._delete_port(db_context, original)
            return

        if (len(port['fixed_ips']) != 0 and len(original['fixed_ips']) == 0 or
                self._ip4_addr_added_to_dualstack_dhcp_port(
                    original, port)):
            # port didn't belong to any subnet yet, or dhcp port used to be
            # ipv6 only: create vport
            self._create_port(db_context, port, context.network,
                              update_status=False)
            return

        subnet_mapping = self._validate_port(db_context,
                                             port,
                                             constants.BEFORE_UPDATE,
                                             is_network_external)
        if not subnet_mapping:
            return

        self._check_subport_in_use(original, port)

        ips_changed = self._check_ip_update_allowed(db_context, original, port)
        mac_changed = original['mac_address'] != port['mac_address']
        nuage_vport = self._get_nuage_vport(port, subnet_mapping)

        if ips_changed or mac_changed:
            fixed_ips = port['fixed_ips']
            ips = {4: None, 6: None}
            for fixed_ip in fixed_ips:
                subnet = self.core_plugin.get_subnet(db_context,
                                                     fixed_ip['subnet_id'])
                ips[subnet['ip_version']] = fixed_ip['ip_address']
            data = {
                'mac': port['mac_address'],
                'ipv4': ips[4],
                'ipv6': ips[6],
                'nuage_vport_id': nuage_vport['ID']
            }
            if _is_trunk_subport(port):
                self.vsdclient.update_subport(port, nuage_vport, data)
            try:
                self.vsdclient.update_nuage_vm_if(data)
            except restproxy.RESTProxyError as e:
                if e.vsd_code != vsd_constants.VSD_VM_ALREADY_RESYNC:
                    raise

        host_added = host_removed = False
        if not original['binding:host_id'] and port['binding:host_id']:
            host_added = True
        elif original['binding:host_id'] and not port['binding:host_id']:
            host_removed = True
        elif (original['device_owner'] and not port['device_owner'] and
              original['device_owner'] == LB_DEVICE_OWNER_V2):
            host_removed = True
        self._port_device_change(context, db_context, nuage_vport,
                                 original, port,
                                 subnet_mapping, host_added,
                                 host_removed)
        rollbacks = []
        try:
            self.nuage_callbacks.notify(resources.PORT, constants.AFTER_UPDATE,
                                        self.core_plugin, context=db_context,
                                        port=port,
                                        original_port=original,
                                        vport=nuage_vport, rollbacks=rollbacks,
                                        subnet_mapping=subnet_mapping)
            if not subnet_mapping['nuage_managed_subnet']:
                new_sg = port.get('security_groups')
                prt_sec_updt_rqd = (original.get(portsecurity.PORTSECURITY) !=
                                    port.get(portsecurity.PORTSECURITY))
                if prt_sec_updt_rqd and not new_sg:
                    self._process_port_create_secgrp_for_port_sec(db_context,
                                                                  port)
                if prt_sec_updt_rqd:
                    status = (constants.DISABLED
                              if port.get(portsecurity.PORTSECURITY, True)
                              else constants.ENABLED)
                    self.vsdclient.update_mac_spoofing_on_vport(
                        nuage_vport['ID'],
                        status)
        except Exception:
            with excutils.save_and_reraise_exception():
                for rollback in reversed(rollbacks):
                    rollback[0](*rollback[1], **rollback[2])

    @staticmethod
    def _ip4_addr_added_to_dualstack_dhcp_port(original, port):
        original_fixed_ips = original['fixed_ips']
        current_fixed_ips = port['fixed_ips']
        device_owner = port.get('device_owner')
        if device_owner != os_constants.DEVICE_OWNER_DHCP:
            return False  # not a dhcp port

        ipv4s, ipv6s = utils.count_fixed_ips_per_version(
            current_fixed_ips)
        original_ipv4s, original_ipv6s = utils.count_fixed_ips_per_version(
            original_fixed_ips)

        return (ipv4s == 1 and ipv6s == 1
                and original_ipv4s == 0 and original_ipv6s == 1)

    @staticmethod
    def _ipv4_addr_removed_from_dualstack_dhcp_port(original, port):
        original_fixed_ips = original['fixed_ips']
        current_fixed_ips = port['fixed_ips']
        device_owner = port.get('device_owner')
        if device_owner != os_constants.DEVICE_OWNER_DHCP:
            return False  # not a dhcp port

        ipv4s, ipv6s = utils.count_fixed_ips_per_version(
            current_fixed_ips)
        original_ipv4s, original_ipv6s = utils.count_fixed_ips_per_version(
            original_fixed_ips)

        return (ipv4s == 0 and ipv6s == 1
                and original_ipv4s == 1 and original_ipv6s == 1)

    def _port_device_change(self, context, db_context, nuage_vport, original,
                            port, subnet_mapping,
                            host_added=False, host_removed=False):
        if not host_added and not host_removed:
            return
        np_name = self.vsdclient.get_net_partition_name_by_id(
            subnet_mapping['net_partition_id'])
        require(np_name, "netpartition",
                subnet_mapping['net_partition_id'])

        if host_removed:
            if self._port_should_have_vm(original):
                self._delete_nuage_vm(db_context, original,
                                      np_name, subnet_mapping,
                                      is_port_device_owner_removed=True)
        elif host_added:
            self._validate_security_groups(context)
            if self._port_should_have_vm(port):
                nuage_subnet, _ = self._get_nuage_subnet(
                    subnet_mapping, subnet_mapping['nuage_subnet_id'])
                self._create_nuage_vm(db_context, port,
                                      np_name, subnet_mapping, nuage_vport,
                                      nuage_subnet)

    @utils.context_log
    @TimeTracker.tracked
    def delete_port_postcommit(self, context):
        db_context = context._plugin_context
        port = context.current
        self._delete_port(db_context, port)

    def _delete_port(self, db_context, port):
        subnet_mapping = self.get_subnet_mapping_by_port(db_context, port)
        if not subnet_mapping:
            return

        if not utils.needs_vport_creation(port.get('device_owner')):
            # GW host vport cleanup
            self.delete_gw_host_vport(db_context, port, subnet_mapping)
            return

        if port.get('binding:host_id'):
            np_name = self.vsdclient.get_net_partition_name_by_id(
                subnet_mapping['net_partition_id'])
            require(np_name, "netpartition",
                    subnet_mapping['net_partition_id'])
            self._delete_nuage_vm(db_context, port, np_name,
                                  subnet_mapping,
                                  is_port_device_owner_removed=True)
        nuage_vport = self._get_nuage_vport(port, subnet_mapping,
                                            required=False)
        if nuage_vport and nuage_vport.get('type') == constants.VM_VPORT:
            try:
                self.vsdclient.delete_nuage_vport(
                    nuage_vport['ID'])
            except Exception as e:
                LOG.error("Failed to delete vport from vsd {vport id: %s}",
                          nuage_vport['ID'])
                raise e
            rollbacks = []
            try:
                self.nuage_callbacks.notify(
                    resources.PORT, constants.AFTER_DELETE,
                    self.core_plugin, context=db_context,
                    updated_port=port,
                    port=port,
                    subnet_mapping=subnet_mapping)
            except Exception:
                with excutils.save_and_reraise_exception():
                    for rollback in reversed(rollbacks):
                        rollback[0](*rollback[1], **rollback[2])
        else:
            self.delete_gw_host_vport(db_context, port, subnet_mapping)
            return

    @utils.context_log
    def bind_port(self, context):
        vnic_type = context.current.get(portbindings.VNIC_TYPE,
                                        portbindings.VNIC_NORMAL)
        if vnic_type not in self._supported_vnic_types():
            LOG.debug("Cannot bind due to unsupported vnic_type: %s",
                      vnic_type)
            return
        for segment in context.network.network_segments:
            if self._check_segment(segment):
                context.set_binding(segment[api.ID],
                                    portbindings.VIF_TYPE_OVS,
                                    {portbindings.CAP_PORT_FILTER: False},
                                    os_constants.PORT_STATUS_ACTIVE)

    @staticmethod
    def _network_no_action(original_network, updated_network):
        _is_external_set = original_network.get(
            external_net.EXTERNAL) != updated_network.get(
            external_net.EXTERNAL)
        _is_shared_set = original_network.get(
            'shared') != updated_network.get('shared')
        if not (_is_external_set or _is_shared_set):
            return True, _is_external_set, _is_shared_set
        else:
            return False, _is_external_set, _is_shared_set

    def _validate_update_network(self, context, _is_external_set,
                                 _is_shared_set, updated_network):
        subnets = self.get_subnets(
            context, filters={'network_id': [updated_network['id']]})
        for subn in subnets:
            subnet_l2dom = nuagedb.get_subnet_l2dom_by_id(
                context.session, subn['id'])
            if subnet_l2dom and subnet_l2dom.get('nuage_managed_subnet'):
                msg = _('Network %s has a VSD-Managed subnet associated'
                        ' with it') % updated_network['id']
                raise NuageBadRequest(msg=msg)
        if (_is_external_set and subnets and not
                updated_network.get(external_net.EXTERNAL)):
            msg = _('External network with subnets can not be '
                    'changed to non-external network')
            raise NuageBadRequest(msg=msg)
        if (len(subnets) > 1 and _is_external_set and
                updated_network.get(external_net.EXTERNAL)):
            msg = _('Non-external network with more than one subnet '
                    'can not be changed to external network')
            raise NuageBadRequest(msg=msg)

        ports = self.get_ports(context, filters={
            'network_id': [updated_network['id']]})
        for p in ports:
            if _is_external_set and updated_network.get(
                    external_net.EXTERNAL) and p['device_owner'].startswith(
                    constants.NOVA_PORT_OWNER_PREF):
                # Check if there are vm ports attached to this network
                # If there are, then updating the network router:external
                # is not possible.
                msg = (_("Network %s cannot be updated. "
                         "There are one or more ports still in"
                         " use on the network.") % updated_network['id'])
                raise NuageBadRequest(msg=msg)
            elif (p['device_owner'].endswith(
                    resources.ROUTER_INTERFACE) and _is_shared_set):
                msg = (_("Cannot update the shared attribute value"
                         " since subnet with id %s is attached to a"
                         " router.") % p['fixed_ips']['subnet_id'])
                raise NuageBadRequest(msg=msg)

    def _validate_nuage_sharedresource(self, context, net_id):
        filter = {'network_id': [net_id]}
        existing_subn = self.core_plugin.get_subnets(context, filters=filter)
        if len(existing_subn) > 1:
            msg = (_('Only one subnet is allowed per external network %s')
                   % net_id)
            raise NuageBadRequest(msg=msg)

    def _check_existing_subnet_on_network(self, context, subnet):
        subnets = self.get_subnets(
            context,
            filters={'network_id': [subnet['network_id']]})
        other_subnets = (s for s in subnets if s['id'] != subnet['id'])
        return next(other_subnets, None)

    def _validate_create_openstack_managed_subnet(self, context, subnet):
        if (lib_validators.is_attr_set(subnet.get('gateway_ip')) and
                netaddr.IPAddress(subnet['gateway_ip']) not in
                netaddr.IPNetwork(subnet['cidr'])):
            msg = "Gateway IP outside of the subnet CIDR "
            raise NuageBadRequest(resource='subnet', msg=msg)

        if not self._network_is_external(context, subnet['network_id']):
            if lib_validators.is_attr_set(subnet.get('underlay')):
                msg = _("underlay attribute can not be set for "
                        "internal subnets")
                raise NuageBadRequest(resource='subnet', msg=msg)
            if lib_validators.is_attr_set(subnet.get('nuage_uplink')):
                msg = _("nuage_uplink attribute can not be set for "
                        "internal subnets")
                raise NuageBadRequest(resource='subnet', msg=msg)

    @staticmethod
    def _validate_create_vsd_managed_subnet(context, network, subnet):
        # Check for network already linked to VSD subnet
        subnet_mappings = nuagedb.get_subnet_l2dom_by_network_id(
            context.session,
            subnet['network_id'])
        # For loop to guard against inconsistent state -> Should be max 1.
        for mapping in subnet_mappings:
            if mapping['nuage_subnet_id'] != subnet['nuagenet']:
                msg = _("The network already has a subnet linked to a "
                        "different vsd subnet.")
                raise NuageBadRequest(msg=msg)

        # Check for VSD Subnet already linked to OS subnet
        linked_subnet = nuagedb.get_subnet_l2dom_by_nuage_id_and_ipversion(
            context.session, subnet['nuagenet'], subnet['ip_version'])
        if linked_subnet:
            msg = _("Multiple OpenStack Subnets with the same ip version "
                    "cannot be linked to the same Nuage Subnet")
            raise NuageBadRequest(msg=msg)

        subnet_validate = {'net_partition': IsSet(),
                           'nuagenet': IsSet()}
        validate("subnet", subnet, subnet_validate)
        net_validate = {'router:external': Is(False)}
        validate("network", network, net_validate)

    def _validate_net_partition(self, subnet, db_context):
        netpartition_db = nuagedb.get_net_partition_by_name(
            db_context.session, subnet['net_partition'])
        netpartition = self.vsdclient.get_netpartition_by_name(
            subnet['net_partition'])
        require(netpartition, "netpartition", subnet['net_partition'])
        if netpartition_db:
            if netpartition_db['id'] != netpartition['id']:
                net_partdb = nuagedb.get_net_partition_with_lock(
                    db_context.session, netpartition_db['id'])
                nuagedb.delete_net_partition(db_context.session, net_partdb)
                self._add_net_partition(db_context.session, netpartition)
        else:
            self._add_net_partition(db_context.session, netpartition)
        return netpartition['id']

    @staticmethod
    def _validate_security_groups(context):
        port = context.current
        db_context = context._plugin_context
        sg_ids = port[ext_sg.SECURITYGROUPS]
        if not sg_ids:
            return

        baremetal_ports = nuagedb.get_port_bindings_for_sg(
            db_context.session,
            sg_ids,
            [portbindings.VNIC_BAREMETAL],
            bound_only=True)
        if len(baremetal_ports) > 0:
            msg = ("Security Groups for baremetal and normal ports "
                   "are mutualy exclusive")
            raise NuageBadRequest(msg=msg)

    @staticmethod
    def _add_net_partition(session, netpartition):
        return nuagedb.add_net_partition(
            session, netpartition['id'], None, None,
            netpartition['name'], None, None)

    def _get_nuage_subnet(self, subnet_db, nuage_subnet_id):
        # subnet_db will be None in case of 1st time creation of subnet.
        if subnet_db is None:
            nuage_subnet = self.vsdclient.get_subnet_or_domain_subnet_by_id(
                nuage_subnet_id)
        else:
            nuage_subnet = self.vsdclient.get_nuage_subnet_by_id(
                subnet_db)
        require(nuage_subnet, 'subnet or domain', nuage_subnet_id)
        shared = nuage_subnet['associatedSharedNetworkResourceID']
        shared_subnet = None
        if shared:
            shared_subnet = self.vsdclient.get_nuage_sharedresource(shared)
            require(shared_subnet, 'sharednetworkresource', shared)
            shared_subnet['subnet_id'] = shared
        return nuage_subnet, shared_subnet

    def _set_gateway_from_vsd(self, nuage_subnet, shared_subnet, subnet):
        gateway_subnet = shared_subnet or nuage_subnet
        if (_is_ipv6(subnet) and
                nuage_subnet['type'] != constants.L2DOMAIN):
            gw_ip = gateway_subnet['IPv6Gateway']
        elif subnet['enable_dhcp'] and _is_ipv4(subnet):
            if nuage_subnet['type'] == constants.L2DOMAIN:
                gw_ip = self.vsdclient.get_gw_from_dhcp_l2domain(
                    gateway_subnet['ID'])
            else:
                gw_ip = gateway_subnet['gateway']
            gw_ip = gw_ip or None
        else:
            gw_ip = None
            subnet['dns_nameservers'] = []
            LOG.warn("Nuage ml2 plugin will ignore dns_nameservers.")
        subnet['gateway_ip'] = gw_ip

    def _update_gw_and_pools(self, db_context, subnet, original_gateway):
        if original_gateway == subnet['gateway_ip']:
            # The gateway from vsd is what openstack already had.
            return

        if original_gateway != subnet['gateway_ip']:
            # Gateway from vsd is different, we must recalculate the allocation
            # pools.
            new_pools = self._set_allocation_pools(subnet)
            self.core_plugin.ipam._update_subnet_allocation_pools(
                db_context, subnet['id'], {'allocation_pools': new_pools,
                                           'id': subnet['id']})
        LOG.warn("Nuage ml2 plugin will overwrite subnet gateway ip "
                 "and allocation pools")
        db_subnet = self.core_plugin._get_subnet(db_context, subnet['id'])
        update_subnet = {'gateway_ip': subnet['gateway_ip']}
        db_subnet.update(update_subnet)

    def _reserve_dhcp_ip(self, db_context, subnet, nuage_subnet,
                         shared_subnet):
        nuage_subnet = shared_subnet or nuage_subnet
        if nuage_subnet.get('DHCPManaged', True) is False:
            # Nothing to reserve for L2 unmanaged or L3 subnets
            return
        if _is_ipv6(subnet):
            dhcp_ip = nuage_subnet.get('IPv6Gateway')
        else:
            dhcp_ip = nuage_subnet['gateway']
        if dhcp_ip:
            self._reserve_ip(db_context, subnet, dhcp_ip)

    def _set_allocation_pools(self, subnet):
        pools = self.core_plugin.ipam.generate_pools(subnet['cidr'],
                                                     subnet['gateway_ip'])
        subnet['allocation_pools'] = [
            {'start': str(netaddr.IPAddress(pool.first, pool.version)),
             'end': str(netaddr.IPAddress(pool.last, pool.version))}
            for pool in pools]
        return pools

    def _cleanup_group(self, db_context, nuage_npid, nuage_subnet_id, subnet):
        try:
            if db_context.tenant == subnet['tenant_id']:
                tenants = [db_context.tenant]
            else:
                tenants = [db_context.tenant, subnet['tenant_id']]
            self.vsdclient.detach_nuage_group_to_nuagenet(
                tenants, nuage_subnet_id,
                subnet.get('shared'))
        except Exception as e:
            LOG.error("Failed to detach group from vsd subnet {tenant: %s,"
                      " netpartition: %s, vsd subnet: %s}",
                      db_context.tenant, nuage_npid, nuage_subnet_id)
            raise e

    def _check_ip_update_allowed(self, db_context, orig_port, port):
        orig_ips = orig_port.get('fixed_ips')
        if port['device_owner'] == os_constants.DEVICE_OWNER_DHCP:
            return True
        new_ips = port.get('fixed_ips')
        vif_type = orig_port.get(portbindings.VIF_TYPE)
        ips_change = (new_ips is not None and
                      orig_ips != new_ips)
        if ips_change and vif_type not in PORT_UNPLUGGED_TYPES:
            raise NuagePortBound(port_id=orig_port['id'],
                                 vif_type=vif_type,
                                 old_ips=orig_port['fixed_ips'],
                                 new_ips=port['fixed_ips'])
        if ips_change:
            # Only one fixed ip per neutron subnet allowed
            subnets = [ip["subnet_id"] for ip in new_ips]
            if len(set(subnets)) != len(subnets):
                msg = _("It is not allowed to add more than one ip "
                        "per neutron subnet to port {}.").format(port["id"])
                raise NuageBadRequest(msg=msg)

            # Only 1 corresponding VSD subnet allowed
            orig_vsd_subnets = self._get_vsd_subnet_ids_by_port(db_context,
                                                                orig_port)
            new_vsd_subnets = self._get_vsd_subnet_ids_by_port(db_context,
                                                               port)
            if orig_vsd_subnets != new_vsd_subnets:
                msg = _("Updating fixed ip of port {} "
                        "to a different subnet is "
                        "not allowed.").format(port["id"])
                raise NuageBadRequest(msg=msg)

            if len(new_vsd_subnets) != 1:
                msg = _("One neutron port cannot correspond to multiple "
                        "VSD subnets").format(port["id"])
                raise NuageBadRequest(msg=msg)
        return ips_change

    @staticmethod
    def _get_vsd_subnet_ids_by_port(db_context, port):
        subnet_ids = set([x['subnet_id'] for x in port['fixed_ips']])
        subnet_mappings = nuagedb.get_subnet_l2doms_by_subnet_ids(
            db_context.session,
            subnet_ids)
        return set([x['nuage_subnet_id'] for x in subnet_mappings])

    @staticmethod
    def _check_subport_in_use(orig_port, port):
        if _is_trunk_subport(orig_port):
            vif_orig = orig_port.get(portbindings.VIF_TYPE)
            if vif_orig not in PORT_UNPLUGGED_TYPES and port.get('device_id'):
                raise PortInUse(port_id=port['id'],
                                net_id=port['network_id'],
                                device_id='trunk:subport')

    def _validate_port(self, db_context, port, event,
                       is_network_external=False):
        fixed_ips = port.get('fixed_ips', [])
        device_owner = port.get('device_owner')
        is_dhcp_port = (device_owner == os_constants.DEVICE_OWNER_DHCP)
        if len(fixed_ips) == 0:
            return False
        if (len(fixed_ips) == 1 and is_dhcp_port and
                netaddr.valid_ipv6(fixed_ips[0]['ip_address'])):
            # Delayed creation of vport until dualstack
            return False
        if not utils.needs_vport_creation(device_owner):
            return False
        if is_dhcp_port and is_network_external:
            return False

        if len(fixed_ips) == 1 and netaddr.valid_ipv6(
                fixed_ips[0]['ip_address']):
            msg = _("Port can't be a pure ipv6 port. Need ipv4 fixed ip.")
            raise NuageBadRequest(msg=msg)

        if is_network_external:
            msg = "Cannot create port in a FIP pool Subnet"
            raise NuageBadRequest(resource='port', msg=msg)
        if port.get(portbindings.VNIC_TYPE, portbindings.VNIC_NORMAL) \
                not in self._supported_vnic_types():
            return False
        # No update required on port with "network:dhcp:nuage"
        if port.get('device_owner') == constants.DEVICE_OWNER_DHCP_NUAGE:
            return False

        subnet_ids = set()
        for fixed_ip in port['fixed_ips']:
            subnet_ids.add(fixed_ip['subnet_id'])
        subnet_mappings = nuagedb.get_subnet_l2doms_by_subnet_ids(
            db_context.session,
            subnet_ids)

        nuage_managed = []
        vsd_subnet_ids = set()

        for mapping in subnet_mappings:
            nuage_managed.append(mapping['nuage_managed_subnet'])
            vsd_subnet_ids.add(mapping['nuage_subnet_id'])

        if not subnet_mappings:
            return False
        if len(vsd_subnet_ids) > 1 and all(nuage_managed):
            msg = _("Port has fixed ips for multiple vsd subnets.")
            raise NuageBadRequest(msg=msg)
        # It's okay to just return the first mapping because it's only 1 vport
        # on 1 subnet on VSD that has to be made.
        self.nuage_callbacks.notify(resources.PORT, event,
                                    self, context=db_context,
                                    request_port=port)
        return subnet_mappings[0]

    @staticmethod
    def get_subnet_mapping_by_port(db_context, port):
        if port['fixed_ips']:
            subnet_id = port['fixed_ips'][0]['subnet_id']
            subnet_mapping = nuagedb.get_subnet_l2dom_by_id(db_context.session,
                                                            subnet_id)
            return subnet_mapping

    @staticmethod
    def _port_should_have_vm(port):
        device_owner = port['device_owner']
        return ((port.get('device_owner') != constants.DEVICE_OWNER_IRONIC or
                 port.get('device_owner') != t_consts.TRUNK_SUBPORT_OWNER) and
                constants.NOVA_PORT_OWNER_PREF in device_owner or
                LB_DEVICE_OWNER_V2 in device_owner or
                DEVICE_OWNER_DHCP in device_owner)

    def _create_nuage_vm(self, db_context, port, np_name, subnet_mapping,
                         nuage_port, nuage_subnet):
        if port.get('device_owner') in [LB_DEVICE_OWNER_V2, DEVICE_OWNER_DHCP]:
            no_of_ports = 1
            vm_id = port['id']
        else:
            no_of_ports, vm_id = self._get_port_num_and_vm_id_of_device(
                db_context, port)

        fixed_ips = port['fixed_ips']
        subnets = {4: {}, 6: {}}
        ips = {4: None, 6: None}
        for fixed_ip in fixed_ips:
            subnet = self.core_plugin.get_subnet(db_context,
                                                 fixed_ip['subnet_id'])
            subnets[subnet['ip_version']] = subnet
            ips[subnet['ip_version']] = fixed_ip['ip_address']

        # Only when the tenant who creates the port is different from both
        # ipv4 and ipv6 tenant, we have to add extra permissions on the subnet.
        # If one of the 2 subnet tenants matches, permissions will already
        # exist from subnet-create.
        if port['tenant_id'] not in (subnets[4].get('tenant_id'),
                                     subnets[6].get('tenant_id')):
            subnet_tenant_id = subnets[4].get('tenant_id')
        else:
            subnet_tenant_id = port['tenant_id']

        shared = subnets[4].get('shared') or subnets[6].get('shared', False)

        params = {
            'port_id': port['id'],
            'id': vm_id,
            'mac': port['mac_address'],
            'netpart_name': np_name,
            'ipv4': ips[4],
            'ipv6': ips[6],
            'no_of_ports': no_of_ports,
            'tenant': port['tenant_id'],
            'netpart_id': subnet_mapping['net_partition_id'],
            'neutron_id': port['fixed_ips'][0]['subnet_id'],
            'vport_id': nuage_port.get('ID'),
            'subn_tenant': subnet_tenant_id,
            'portOnSharedSubn': shared,
            'dhcp_enabled': subnets[4].get('enable_dhcp'),
            'vsd_subnet': nuage_subnet
        }
        network_details = self.core_plugin.get_network(db_context,
                                                       port['network_id'])
        if network_details['shared']:
            self.vsdclient.create_usergroup(
                port['tenant_id'],
                subnet_mapping['net_partition_id'])
        return self.vsdclient.create_vms(params)

    def _get_port_num_and_vm_id_of_device(self, db_context, port):
        filters = {'device_id': [port.get('device_id')]}
        ports = self.core_plugin.get_ports(db_context, filters)
        ports = [p for p in ports
                 if self._is_port_vxlan_normal(p, db_context) and
                 p['binding:host_id']]
        return len(ports), port.get('device_id')

    def _process_port_create_secgrp_for_port_sec(self, context, port):
        rtr_id = None
        policygroup_ids = []
        port_id = port['id']

        if not port.get('fixed_ips'):
            return self._make_port_dict(port)

        subnet_mapping = nuagedb.get_subnet_l2dom_by_id(
            context.session, port['fixed_ips'][0]['subnet_id'])

        if subnet_mapping:
            l2dom_id, l3dom_id = get_l2_and_l3_sub_id(subnet_mapping)
            if l3dom_id:
                rtr_id = self.vsdclient.get_nuage_domain_id_from_subnet(
                    l3dom_id)

            params = {
                'neutron_port_id': port_id,
                'l2dom_id': l2dom_id,
                'l3dom_id': l3dom_id,
                'rtr_id': rtr_id,
                'type': constants.VM_VPORT,
                'sg_type': constants.SOFTWARE
            }
            nuage_port = self.vsdclient.get_nuage_vport_for_port_sec(params)
            if nuage_port:
                nuage_vport_id = nuage_port.get('ID')
                if port.get(portsecurity.PORTSECURITY):
                    self.vsdclient.update_vport_policygroups(
                        nuage_vport_id, policygroup_ids)
                else:
                    sg_id = (self.vsdclient.
                             create_nuage_sec_grp_for_port_sec(params))
                    if sg_id:
                        params['sg_id'] = sg_id
                        (self.vsdclient.
                         create_nuage_sec_grp_rule_for_port_sec(params))
                        policygroup_ids.append(sg_id)
                        self.vsdclient.update_vport_policygroups(
                            nuage_vport_id, policygroup_ids)

    def _is_port_vxlan_normal(self, port, db_context):
        if port.get('binding:vnic_type') != portbindings.VNIC_NORMAL:
            return False

        return self.is_vxlan_network_by_id(db_context, port.get('network_id'))

    def delete_gw_host_vport(self, context, port, subnet_mapping):
        port_params = {
            'neutron_port_id': port['id']
        }

        # Check if l2domain/subnet exist. In case of router_interface_delete,
        # subnet is deleted and then call comes to delete_port. In that
        # case, we just return
        vsd_subnet = self.vsdclient.get_nuage_subnet_by_id(subnet_mapping)

        if not vsd_subnet:
            return

        port_params['l2dom_id'], port_params['l3dom_id'] = \
            get_l2_and_l3_sub_id(subnet_mapping)
        nuage_vport = self.vsdclient.get_nuage_vport_by_neutron_id(
            port_params, required=False)
        if nuage_vport and (nuage_vport['type'] == constants.HOST_VPORT):
            def_netpart = cfg.CONF.RESTPROXY.default_net_partition_name
            netpart = nuagedb.get_default_net_partition(context, def_netpart)
            self.vsdclient.delete_nuage_gateway_vport(
                context,
                nuage_vport.get('ID'),
                netpart['id'])

    def _delete_nuage_vm(self, db_context, port, np_name, subnet_mapping,
                         is_port_device_owner_removed=False):
        if port.get('device_owner') in [LB_DEVICE_OWNER_V2, DEVICE_OWNER_DHCP]:
            no_of_ports = 1
            vm_id = port['id']
        else:
            no_of_ports, vm_id = self._get_port_num_and_vm_id_of_device(
                db_context, port)
            # In case of device removed, this number should be the amount of
            # vminterfaces on VSD. If it's >1, vsdclient knows there are
            # still other vminterfaces using the VM, and it will not delete the
            # vm. If it's 1 or less. VsdClient will also automatically delete
            # the vm. Because the port count is determined on a database count
            # of ports with device_id X, AND because the update already
            # happened by ml2plugin, AND because we're in the same database
            # transaction, the count here would return 1 less (as the updated
            # port will not be counted because the device_id is already cleared
            if is_port_device_owner_removed:
                no_of_ports += 1

        fixed_ips = port['fixed_ips']
        subnets = {4: {}, 6: {}}
        for fixed_ip in fixed_ips:
            subnet = self.core_plugin.get_subnet(
                db_context, fixed_ip['subnet_id'])
            subnets[subnet['ip_version']] = subnet

        if port['tenant_id'] not in (subnets[4].get('tenant_id'),
                                     subnets[6].get('tenant_id')):
            subnet_tenant_id = subnets[4].get('tenant_id')
        else:
            subnet_tenant_id = port['tenant_id']

        shared = subnets[4].get('shared') or subnets[6].get('shared', False)

        nuage_port = self.vsdclient.get_nuage_port_by_id(
            {'neutron_port_id': port['id']})
        if not nuage_port:
            return
        params = {
            'no_of_ports': no_of_ports,
            'netpart_name': np_name,
            'tenant': port['tenant_id'],
            'nuage_vif_id': nuage_port['nuage_vif_id'],
            'id': vm_id,
            'subn_tenant': subnet_tenant_id,
            'portOnSharedSubn': shared
        }
        if not nuage_port['domainID']:
            params['l2dom_id'] = subnet_mapping['nuage_subnet_id']
        else:
            params['l3dom_id'] = subnet_mapping['nuage_subnet_id'],
        try:
            self.vsdclient.delete_vms(params)
        except Exception:
            LOG.error("Failed to delete vm from vsd {vm id: %s}",
                      vm_id)
            raise

    def _get_nuage_vport(self, port, subnet_mapping, required=True):
        port_params = {'neutron_port_id': port['id']}
        l2dom_id, l3dom_id = get_l2_and_l3_sub_id(subnet_mapping)
        port_params['l2dom_id'] = l2dom_id
        port_params['l3dom_id'] = l3dom_id
        return self.vsdclient.get_nuage_vport_by_neutron_id(
            port_params, required=required)

    @staticmethod
    def _check_segment(segment):
        network_type = segment[api.NETWORK_TYPE]
        return network_type == p_constants.TYPE_VXLAN

    @staticmethod
    def _supported_vnic_types():
        return [portbindings.VNIC_NORMAL]

    def check_vlan_transparency(self, context):
        """Nuage driver vlan transparency support."""
        return True
