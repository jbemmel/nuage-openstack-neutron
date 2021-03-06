# Copyright 2018 NOKIA
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

import logging
import random

from neutron_lib import constants as lib_constants
from neutron_lib.db import api as db_api
from oslo_config import cfg
from oslo_utils import excutils
import six

from nuage_neutron.plugins.common import nuagedb
from nuage_neutron.vsdclient.common.cms_id_helper import get_vsd_external_id
from nuage_neutron.vsdclient.common import constants
from nuage_neutron.vsdclient.common import helper
from nuage_neutron.vsdclient.common import nuagelib
from nuage_neutron.vsdclient.common import pg_helper
from nuage_neutron.vsdclient import restproxy

PROTO_NAME_TO_NUM = lib_constants.IP_PROTOCOL_MAP
VSD_RESP_OBJ = constants.VSD_RESP_OBJ
NUAGE_SUPPORTED_ETHERTYPES = constants.NUAGE_SUPPORTED_ETHERTYPES
NOT_SUPPORTED_ACL_ATTR_MSG = constants.NOT_SUPPORTED_ACL_ATTR_MSG
NUAGE_ACL_PROTOCOL_ANY_MAPPING = constants.NUAGE_ACL_PROTOCOL_ANY_MAPPING
RES_POLICYGROUPS = constants.RES_POLICYGROUPS
NOTHING_TO_UPDATE_ERR_CODE = constants.VSD_NO_ATTR_CHANGES_TO_MODIFY_ERR_CODE
MIN_SG_PRI = 0
MAX_SG_PRI = 1000000000
ICMP_PROTOCOL_NUMS = [PROTO_NAME_TO_NUM['icmp'],
                      PROTO_NAME_TO_NUM['ipv6-icmp'],
                      PROTO_NAME_TO_NUM['icmpv6']]
STATEFUL_ICMP_V4_TYPES = [8, 13, 15, 17]
STATEFUL_ICMP_V6_TYPES = [128]

ANY_IPV4_IP = constants.ANY_IPV4_IP
ANY_IPV6_IP = constants.ANY_IPV6_IP

LOG = logging.getLogger(__name__)


class NuagePolicyGroups(object):
    def __init__(self, restproxy):
        self.restproxy = restproxy
        self.flow_logging_enabled = cfg.CONF.PLUGIN.flow_logging_enabled
        self.stats_collection_enabled = (cfg.CONF.PLUGIN.
                                         stats_collection_enabled)

    @staticmethod
    def _get_vsd_external_id(id, pg_type):
        if pg_type == constants.HARDWARE:
            prefix = 'hw:'
        elif pg_type == constants.INTERCONNECT:
            prefix = 'ic:'
        else:
            prefix = ''
        return get_vsd_external_id(prefix + id)

    def _create_nuage_secgroup(self, params, raise_on_pg_exists=False):
        rtr_id = params['nuage_router_id']
        l2dom_id = params['nuage_l2dom_id']
        sg_id = params.get('sg_id')
        sg_type = params.get('sg_type', constants.SOFTWARE)

        prefix = 'hw:' if sg_type == 'HARDWARE' else ''
        req_params = {
            'name': params['name'],
            'sg_id': sg_id,
            'sg_type': sg_type,
            'externalID': prefix + params.get(
                'externalID', get_vsd_external_id(params.get('sg_id')))
        }
        if rtr_id:
            req_params['domain_id'] = rtr_id
        elif l2dom_id:
            req_params['domain_id'] = l2dom_id

        response = None
        nuage_policygroup = nuagelib.NuagePolicygroup(create_params=req_params)
        if rtr_id:
            response = self.restproxy.post(
                nuage_policygroup.post_resource(),
                nuage_policygroup.post_data(),
                ignore_err_codes=(None if raise_on_pg_exists else
                                  [restproxy.REST_PG_EXISTS_ERR_CODE]))
        elif l2dom_id:
            response = self.restproxy.post(
                nuage_policygroup.post_resource_l2dom(),
                nuage_policygroup.post_data(),
                ignore_err_codes=(None if raise_on_pg_exists else
                                  [restproxy.REST_PG_EXISTS_ERR_CODE]))
        return response[0]['ID']

    def delete_nuage_policy_group(self, nuage_policy_id):
        nuage_policygroup = nuagelib.NuagePolicygroup()
        self.restproxy.delete(nuage_policygroup.delete_resource(
            nuage_policy_id))

    def delete_policy_group(self, id):
        nuage_policygroup = self.get_sg_policygroup_mapping(id)
        if nuage_policygroup:
            l3dom_policygroups = nuage_policygroup['l3dom_policygroups']
            l2dom_policygroups = nuage_policygroup['l2dom_policygroups']
            for policygroup in l3dom_policygroups + l2dom_policygroups:
                self.delete_nuage_policy_group(policygroup['policygroup_id'])

    @staticmethod
    def _validate_nuage_port_range(rule):
        if not rule['protocol']:
            msg = "protocol type required when port range is specified"
            raise restproxy.ResourceConflictException(msg)
        ip_proto = rule['protocol']
        if ip_proto in ['tcp', 'udp']:
            if (rule.get('port_range_min') is not None and
                    rule['port_range_min'] == 0):
                msg = ("Invalid port range, Port Number(0) must be between 1 "
                       "and 65535")
                raise restproxy.ResourceConflictException(msg)

    def validate_nuage_sg_rule_definition(self, sg_rule):
        if 'ethertype' in sg_rule:
            if str(sg_rule['ethertype']) not in NUAGE_SUPPORTED_ETHERTYPES:
                raise restproxy.ResourceConflictException(
                    NOT_SUPPORTED_ACL_ATTR_MSG % sg_rule['ethertype'])
        if (sg_rule.get('port_range_min') is None and
                sg_rule.get('port_range_max') is None):
            return
        self._validate_nuage_port_range(sg_rule)

    @staticmethod
    def _get_ethertype(ethertype):
        if ethertype == constants.OS_IPV4:
            return constants.IPV4_ETHERTYPE
        elif ethertype == constants.OS_IPV6:
            return constants.IPV6_ETHERTYPE
        else:
            raise restproxy.ResourceConflictException(
                NOT_SUPPORTED_ACL_ATTR_MSG % ethertype)

    def _map_nuage_sgrule(self, params):
        sg_rule = params['neutron_sg_rule']
        np_id = params['np_id']
        policygroup_id = params['policygroup_id']
        l2dom_dhcp_managed = params.get('dhcp_managed')
        sg_type = params.get('sg_type', constants.SOFTWARE)
        is_hardware = sg_type == constants.HARDWARE
        network_type = 'ENDPOINT_DOMAIN'
        if l2dom_dhcp_managed == 'unmanaged' or is_hardware:
            network_type = 'ANY'
        if is_hardware:
            stateful = False
        elif 'security_group_id' in sg_rule:
            stateful = self.get_sg_stateful_value(sg_rule['security_group_id'])
        else:
            # e.g. in case of external security group
            stateful = True
        nuage_match_info = {
            'etherType': constants.IPV4_ETHERTYPE,
            'protocol': 'ANY',
            'networkType': network_type,
            'locationType': 'POLICYGROUP',
            'locationID': policygroup_id,
            'action': 'FORWARD',
            'stateful': stateful,
            'DSCP': '*',
            'flowLoggingEnabled':
                self.flow_logging_enabled and not is_hardware,
            'statsLoggingEnabled':
                self.stats_collection_enabled and not is_hardware,
            'priority': random.randint(MIN_SG_PRI, MAX_SG_PRI),
        }

        min_port = max_port = None
        for key, value in list(six.iteritems(sg_rule)):
            if value is None:
                continue
            if str(key) == 'ethertype':
                nuage_match_info['etherType'] = self._get_ethertype(
                    sg_rule['ethertype'])
            elif str(key) == 'protocol':
                try:
                    # protocol passed in rule create is integer representation
                    nuage_match_info['protocol'] = int(value)
                    if nuage_match_info['protocol'] in (
                            [PROTO_NAME_TO_NUM['tcp'],
                             PROTO_NAME_TO_NUM['udp']]):
                        nuage_match_info['sourcePort'] = '*'
                        nuage_match_info['destinationPort'] = '*'
                except (ValueError, TypeError):
                    # protocol passed in rule create is string representation
                    if value == "ANY":
                        continue
                    proto = str(value)
                    if (proto == 'icmp' and
                            sg_rule['ethertype'] == constants.OS_IPV6):
                        proto = 'ipv6-icmp'  # change 1 into 58
                    nuage_match_info['protocol'] = PROTO_NAME_TO_NUM[proto]
                    if value in ['tcp', 'udp']:
                        nuage_match_info['sourcePort'] = '*'
                        nuage_match_info['destinationPort'] = '*'
            elif str(key) == 'remote_ip_prefix':
                netid = pg_helper.create_nuage_prefix_macro(self.restproxy,
                                                            sg_rule, np_id)
                nuage_match_info['networkID'] = netid
                nuage_match_info['networkType'] = "ENTERPRISE_NETWORK"
            elif str(key) == 'remote_group_id':
                rtr_id = params.get('l3dom_id')
                l2dom_id = params.get('l2dom_id')
                if rtr_id:
                    remote_policygroup_id = (
                        pg_helper.get_remote_policygroup_id(
                            self.restproxy,
                            value, constants.DOMAIN, rtr_id,
                            params.get('remote_group_name')))
                else:
                    remote_policygroup_id = (
                        pg_helper.get_remote_policygroup_id(
                            self.restproxy,
                            value, constants.L2DOMAIN, l2dom_id,
                            params.get('remote_group_name')))
                if not is_hardware:
                    nuage_match_info['networkID'] = remote_policygroup_id
                    nuage_match_info['networkType'] = "POLICYGROUP"
            elif str(key) == 'remote_external_group':
                nuage_match_info['networkID'] = value
                nuage_match_info['networkType'] = "POLICYGROUP"
            elif str(key) == 'port_range_max':
                max_port = str(value)
            elif str(key) == 'port_range_min':
                min_port = str(value)
        if min_port and max_port:
            if nuage_match_info['protocol'] in \
                    [PROTO_NAME_TO_NUM['tcp'], PROTO_NAME_TO_NUM['udp']]:
                port_str = min_port
                if int(min_port) != int(max_port):
                    port_str = port_str + '-' + max_port
                nuage_match_info['sourcePort'] = '*'
                nuage_match_info['destinationPort'] = port_str
        if nuage_match_info['protocol'] in ICMP_PROTOCOL_NUMS:
            if min_port:
                nuage_match_info['ICMPType'] = min_port
            if max_port:
                nuage_match_info['ICMPCode'] = max_port
            if not min_port and not max_port:
                nuage_match_info['stateful'] = False
            elif (sg_rule['ethertype'] == constants.OS_IPV4 and
                    int(min_port) not in STATEFUL_ICMP_V4_TYPES):
                nuage_match_info['stateful'] = False
            elif (sg_rule['ethertype'] == constants.OS_IPV6 and
                    int(min_port) not in STATEFUL_ICMP_V6_TYPES):
                nuage_match_info['stateful'] = False
        return nuage_match_info

    def _get_ingress_egress_ids(self, rtr_id=None, l2dom_id=None):
        nuage_ibacl_id = nuage_obacl_id = None
        if rtr_id:
            nuage_ibacl_id = pg_helper.get_l3dom_inbound_acl_id(
                self.restproxy,
                rtr_id)
            nuage_obacl_id = pg_helper.get_l3dom_outbound_acl_id(
                self.restproxy,
                rtr_id)

            if not nuage_ibacl_id and not nuage_obacl_id:
                msg = ("Router %s does not have ACL mapping"
                       % rtr_id)
                raise restproxy.ResourceConflictException(msg)
        elif l2dom_id:
            nuage_ibacl_id = pg_helper.get_l2dom_inbound_acl_id(
                self.restproxy,
                l2dom_id)
            nuage_obacl_id = pg_helper.get_l2dom_outbound_acl_id(
                self.restproxy,
                l2dom_id)
            if not nuage_ibacl_id and not nuage_obacl_id:
                msg = ("L2Domain %s of Security Group does not have ACL "
                       "mapping") % l2dom_id
                raise restproxy.ResourceConflictException(msg)
        return nuage_ibacl_id, nuage_obacl_id

    def _create_nuage_sgrules_bulk(self, params):
        rtr_id = params['nuage_router_id']
        l2dom_id = params['nuage_l2dom_id']
        nuage_policygroup_id = params.get('nuage_policygroup_id')
        l3dom_policygroup = l2dom_policygroup = []
        if rtr_id:
            l3dom_policygroup = [{
                'l3dom_id': rtr_id,
                'policygroup_id': nuage_policygroup_id
            }]
        elif l2dom_id:
            l2dom_policygroup = [{
                'l2dom_id': l2dom_id,
                'policygroup_id': nuage_policygroup_id
            }]

        policygroup = {
            'l3dom_policygroups': l3dom_policygroup,
            'l2dom_policygroups': l2dom_policygroup
        }

        sg_rules = params.get('sg_rules')
        if sg_rules:
            for rule in sg_rules:
                params = {
                    'policygroup': policygroup,
                    'neutron_sg_rule': rule,
                    'sg_type': params.get('sg_type', constants.SOFTWARE)
                }
                if ('ethertype' in rule and
                        str(rule['ethertype']) not in
                        NUAGE_SUPPORTED_ETHERTYPES):
                    continue
                self.create_nuage_sgrule(params)

    def create_nuage_sgrule(self, params):
        neutron_sg_rule = params['neutron_sg_rule']
        policygroup_list = params['policygroup']
        l3dom_policygroup_list = policygroup_list['l3dom_policygroups']
        l2dom_policygroup_list = policygroup_list['l2dom_policygroups']
        sg_type = params.get('sg_type')
        remote_group_name = params.get('remote_group_name')
        external_id = params.get('externalID')
        legacy = params.get('legacy', False)
        for l3dom_policygroup in l3dom_policygroup_list:
            nuage_ibacl_id, nuage_obacl_id = self._get_ingress_egress_ids(
                rtr_id=l3dom_policygroup['l3dom_id'])
            np_id = helper.get_l3domain_np_id(self.restproxy,
                                              l3dom_policygroup['l3dom_id'])
            if not np_id:
                msg = "Net Partition not found for l3domain %s " \
                      % l3dom_policygroup['l3dom_id']
                raise restproxy.ResourceNotFoundException(msg)

            acl_mapping = {
                'nuage_iacl_id': nuage_ibacl_id,
                'nuage_oacl_id': nuage_obacl_id
            }

            sg_rule = dict(neutron_sg_rule)
            params = {
                'direction': sg_rule.get('direction'),
                'acl_mapping': acl_mapping,
                'neutron_sg_rule': sg_rule,
                'np_id': np_id,
                'policygroup_id': l3dom_policygroup['policygroup_id'],
                'rule_id': sg_rule.get('id'),
                'l3dom_id': l3dom_policygroup['l3dom_id'],
                'externalID': external_id,
                'legacy': legacy
            }
            if sg_type:
                params['sg_type'] = sg_type
            if remote_group_name:
                params['remote_group_name'] = remote_group_name
            self._create_nuage_sgrule_process(params)

        for l2dom_policygroup in l2dom_policygroup_list:
            nuage_ibacl_id, nuage_obacl_id = self._get_ingress_egress_ids(
                l2dom_id=l2dom_policygroup['l2dom_id'])

            fields = ['parentID', 'DHCPManaged']
            l2dom_fields = helper.get_l2domain_fields_for_pg(
                self.restproxy, l2dom_policygroup['l2dom_id'], fields)
            np_id = l2dom_fields['parentID']
            dhcp_managed = l2dom_fields['DHCPManaged']
            if not dhcp_managed:
                dhcp_managed = "unmanaged"
            if not np_id:
                msg = "Net Partition not found for l2domain %s " \
                      % l2dom_policygroup['l2dom_id']
                raise restproxy.ResourceNotFoundException(msg)
            acl_mapping = {
                'nuage_iacl_id': nuage_ibacl_id,
                'nuage_oacl_id': nuage_obacl_id
            }
            sg_rule = dict(neutron_sg_rule)
            params = {
                'direction': sg_rule.get('direction'),
                'acl_mapping': acl_mapping,
                'neutron_sg_rule': sg_rule,
                'np_id': np_id,
                'policygroup_id': l2dom_policygroup['policygroup_id'],
                'rule_id': sg_rule.get('id'),
                'dhcp_managed': dhcp_managed,
                'l2dom_id': l2dom_policygroup['l2dom_id'],
                'externalID': external_id,
                'legacy': legacy
            }
            if sg_type:
                params['sg_type'] = sg_type
            if remote_group_name:
                params['remote_group_name'] = remote_group_name
            self._create_nuage_sgrule_process(params)

    def _create_nuage_sgrule_process(self, params):
        sg_rule = params['neutron_sg_rule']
        reverse = False
        if params.get('sg_type') == constants.HARDWARE:
            if not params.get('legacy', False):
                if not sg_rule.get('remote_ip_prefix'):
                    if sg_rule.get('ethertype') == constants.OS_IPV6:
                        sg_rule['remote_ip_prefix'] = ANY_IPV6_IP
                    else:
                        sg_rule['remote_ip_prefix'] = ANY_IPV4_IP
                if not sg_rule.get('protocol'):
                    sg_rule['protocol'] = "ANY"
        else:
            if (not sg_rule.get('remote_group_id') and
                    not sg_rule.get('remote_ip_prefix')):
                if sg_rule.get('ethertype') == constants.OS_IPV6:
                    sg_rule['remote_ip_prefix'] = ANY_IPV6_IP
                else:
                    sg_rule['remote_ip_prefix'] = ANY_IPV4_IP
            if not sg_rule.get('protocol'):
                sg_rule['protocol'] = "ANY"

            # As VSP does not support stateful icmp with ICMPv4 type not in
            # [8,13,15,17], to be compatible with upstream openstack, create 2
            # non stateful icmp rules in egress and ingress direction for such
            # type; for valid icmp types create single stateful icmp rule.
            # Same for ICMPv6 echo (128) type.

            if 'icmp' in sg_rule.get('protocol'):  # both v4 and v6
                port_min = sg_rule.get('port_range_min')  # type
                port_max = sg_rule.get('port_range_max')  # code
                sg_id = sg_rule['security_group_id']
                stateful = self.get_sg_stateful_value(sg_id)
                # reverse = stateful AND no-can-do-stateful
                reverse = (stateful and
                           (  # no-can-do as unspecified ~ wildcard
                              not port_min and not port_max or
                              # no-can-do as icmpv4 type no-can-do stateful
                              sg_rule['ethertype'] == constants.OS_IPV4 and
                              port_min not in STATEFUL_ICMP_V4_TYPES or
                              # no-can-do as icmpv6 type no-can-do stateful
                              sg_rule['ethertype'] == constants.OS_IPV6 and
                              port_min not in STATEFUL_ICMP_V6_TYPES
                           ))

        # create the configured rule
        self._create_nuage_sgrule(params)

        if reverse:
            # create reverse rule (must be done secondly as data is altered)
            if sg_rule['direction'] == 'ingress':
                sg_rule['direction'] = 'egress'
                params['direction'] = 'egress'
                self._create_nuage_sgrule(params)
            elif sg_rule['direction'] == 'egress':
                sg_rule['direction'] = 'ingress'
                params['direction'] = 'ingress'
                self._create_nuage_sgrule(params)

    def _create_nuage_sgrule(self, params):
        # neutron ingress is nuage egress and vice versa
        if params['neutron_sg_rule']['direction'] == 'ingress':
            acl_id = params['acl_mapping']['nuage_oacl_id']
        else:
            acl_id = params['acl_mapping']['nuage_iacl_id']
        req_params = {
            'acl_id': acl_id,
        }
        nuage_aclrule = nuagelib.NuageACLRule(create_params=req_params)
        nuage_match_info = self._map_nuage_sgrule(params)
        nuage_match_info['externalID'] = (self._get_vsd_external_id(
            params.get('rule_id'), params.get('sg_type', constants.SOFTWARE))
            if params.get('rule_id') else
            params.get('externalID'))

        # neutron ingress is nuage egress and vice versa
        if params['neutron_sg_rule']['direction'] == 'ingress':
            url = nuage_aclrule.eg_post_resource()
        else:
            url = nuage_aclrule.in_post_resource()

        attempts = 3
        for i in range(attempts):
            try:
                return self.restproxy.post(url, nuage_match_info)[0]['ID']
            except restproxy.RESTProxyError as e:
                if (e.code == restproxy.REST_CONFLICT and
                        e.vsd_code ==
                        constants.VSD_PRIORITY_CONFLICT_ERR_CODE):
                    nuage_match_info['priority'] = random.randint(MIN_SG_PRI,
                                                                  MAX_SG_PRI)
                else:
                    raise
        raise restproxy.ResourceConflictException(
            "Failed to create aclentrytemplate after %s attempts "
            "due to priority conflict" % attempts)

    def update_vports_in_policy_group(self, pg_id, vport_list):
        policygroups = nuagelib.NuagePolicygroup()
        self.restproxy.put(
            policygroups.get_policygroups(pg_id) + '/vports' +
            '?responseChoice=1',
            vport_list)

    def _delete_nuage_sgrule(self, id, direction):
        nuage_aclrule = nuagelib.NuageACLRule()
        # neutron ingress is nuage egress and vice versa
        if direction == 'ingress':
            self.restproxy.delete(nuage_aclrule.eg_delete_resource(id))
        else:
            self.restproxy.delete(nuage_aclrule.in_delete_resource(id))

    def delete_nuage_sgrule(self, sg_rules, sg_type):
        for rule in sg_rules:
            params = {
                'rule_id': rule['id'],
                'direction': rule['direction'],
                'sg_type': sg_type
            }
            sgrule_acl = self.get_sgrule_acl_mapping_for_ruleid(params)
            if sgrule_acl:
                for acl_id in sgrule_acl:
                    self._delete_nuage_sgrule(acl_id, rule['direction'])
            # this handles the case where, rule with protocol icmp and
            # ICMP type not in [8,13,15,17] with ingress direction
            # has an icmp rule in egress and vice versa
            sg_id = rule['security_group_id']
            stateful = self.get_sg_stateful_value(sg_id)
            if (rule.get('protocol') == 'icmp' and stateful and
                    ((rule['ethertype'] == constants.OS_IPV4 and
                      rule.get('port_range_min')
                      not in STATEFUL_ICMP_V4_TYPES) or
                     (rule['ethertype'] == constants.OS_IPV6 and
                      rule.get('port_range_min')
                      not in STATEFUL_ICMP_V6_TYPES))):
                if rule['direction'] == 'egress':
                    params = {
                        'rule_id': rule['id'],
                        'direction': 'ingress',
                        'sg_type': sg_type
                    }
                else:
                    params = {
                        'rule_id': rule['id'],
                        'direction': 'egress',
                        'sg_type': sg_type
                    }
                sgrule_acl = self.get_sgrule_acl_mapping_for_ruleid(params)
                # To do(Divya): try to use rule['direction'] instead of
                # params['direction']
                if sgrule_acl:
                    for acl_id in sgrule_acl:
                        self._delete_nuage_sgrule(acl_id, params['direction'])

    def update_vport_policygroups(self, vport_id, policygroup_ids):
        policygroups = nuagelib.NuagePolicygroup()
        self.restproxy.put(
            policygroups.put_child_resource(nuagelib.NuageVPort.resource,
                                            vport_id),
            policygroup_ids)

    def update_policygroup(self, policygroup_id, data):
        policygroups = nuagelib.NuagePolicygroup()
        return self.restproxy.put(
            policygroups.put_resource(policygroup_id),
            data)

    def get_rate_limit(self, vport_id, neutron_fip_id):
        create_params = {'vport_id': vport_id,
                         'externalID': get_vsd_external_id(neutron_fip_id)}
        qos = nuagelib.NuageVportQOS(create_params)
        qoses = self.restproxy.get(qos.get_all_resource(),
                                   extra_headers=qos.extra_headers_get(),
                                   required=True)
        if not qoses:
            raise qos.get_rest_proxy_error()
        fip_rate_values = {}
        egress_value = qoses[0]['FIPPeakInformationRate']
        ingress_value = qoses[0]['EgressFIPPeakInformationRate']
        fip_rate_values["egress_value"] = float(
            egress_value) * 1000 if egress_value != (
                '%s' % constants.INFINITY) else -1
        if ingress_value:
            fip_rate_values["ingress_value"] = float(
                ingress_value) * 1000 if ingress_value != (
                '%s' % constants.INFINITY) else -1
        return fip_rate_values

    def create_update_rate_limiting(self, fip_rate_values, vport_id,
                                    neutron_fip_id):
        data = {}
        for direction, value in six.iteritems(fip_rate_values):
            if float(value) == -1:
                value = constants.INFINITY
            elif 'kbps' in direction:
                value = float(value) / 1000
            if 'ingress' in direction:
                data["EgressFIPPeakInformationRate"] = value
            elif 'egress' in direction:
                data["FIPPeakInformationRate"] = value
        create_params = {'vport_id': vport_id,
                         'externalID': get_vsd_external_id(neutron_fip_id)}
        qos = nuagelib.NuageVportQOS(create_params)
        qoses = self.restproxy.get(qos.get_all_resource(),
                                   extra_headers=qos.extra_headers_get(),
                                   required=True)
        # 'required' refers to vport, not the qoses
        if not qoses:
            self.add_rate_limiting(data, vport_id, neutron_fip_id)
            return

        qos_obj = qoses[0]
        create_params = {'qos_id': qos_obj['ID']}
        qos = nuagelib.NuageVportQOS(create_params)
        self.restproxy.put(qos.put_resource(), data)

    def add_rate_limiting(self, rate_limit_values, vport_id, neutron_fip_id):
        data = {"FIPPeakBurstSize": 100,
                "EgressFIPPeakBurstSize": 100,
                "FIPRateLimitingActive": True,
                "active": True,
                "externalID": get_vsd_external_id(neutron_fip_id)}
        data.update(rate_limit_values)
        qos = nuagelib.NuageVportQOS({"name": "FIP Rate Limiting",
                                      "vport_id": vport_id}, data)
        self.restproxy.post(qos.post_resource(),
                            qos.post_data())

    def delete_rate_limiting(self, vport_id, neutron_fip_id):
        create_params = {'vport_id': vport_id,
                         'externalID': get_vsd_external_id(neutron_fip_id)}
        qos = nuagelib.NuageVportQOS(create_params)
        qoses = self.restproxy.get(qos.get_all_resource(),
                                   required=True)
        if qoses:
            create_params = {'qos_id': qoses[0]['ID']}
            qos = nuagelib.NuageVportQOS(create_params)
            self.restproxy.delete(qos.delete_resource())

    def get_sg_policygroup_by_external_id(self, sg_id,
                                          sg_type=constants.SOFTWARE,
                                          required=False):
        req_params = {
            'externalID': self._get_vsd_external_id(sg_id, sg_type)
        }
        nuage_policygroup = nuagelib.NuagePolicygroup(create_params=req_params)
        nuage_policygroup_extra_headers = nuage_policygroup.extra_headers_get()
        response = self.restproxy.get(
            nuage_policygroup.get_all_resources(),
            extra_headers=nuage_policygroup_extra_headers,
            required=required)
        return response

    def get_sg_policygroup_mapping(self, sg_id, sg_type=constants.SOFTWARE):
        response = self.get_sg_policygroup_by_external_id(sg_id,
                                                          sg_type=sg_type,
                                                          required=True)
        l3dom_policygroup_list = []
        l2dom_policygroup_list = []
        for policygroup in response:
            if policygroup['parentType'] == constants.DOMAIN:
                l3dom_policygroup = {
                    'l3dom_id': policygroup['parentID'],
                    'policygroup_id': policygroup['ID']
                }
                l3dom_policygroup_list.append(l3dom_policygroup)
            elif policygroup['parentType'] == constants.L2DOMAIN:
                l2dom_policygroup = {
                    'l2dom_id': policygroup['parentID'],
                    'policygroup_id': policygroup['ID']
                }
                l2dom_policygroup_list.append(l2dom_policygroup)

        if not l3dom_policygroup_list and not l2dom_policygroup_list:
            result = None
        else:
            result = {
                'l3dom_policygroups': l3dom_policygroup_list,
                'l2dom_policygroups': l2dom_policygroup_list
            }
        return result

    def get_sgrule_acl_mapping_for_ruleid(self, params, **filters):
        filters['externalID'] = self._get_vsd_external_id(params['rule_id'],
                                                          params['sg_type'])
        nuage_aclrule = nuagelib.NuageACLRule()
        headers = nuage_aclrule.extra_header_filter(**filters)
        # neutron ingress is egress on nuage and vice versa
        if params['direction'] == 'ingress':
            url = nuage_aclrule.eg_get_all_resources()
        else:
            url = nuage_aclrule.in_get_all_resources()

        acls = self.restproxy.get(url, extra_headers=headers)
        return [acl['ID'] for acl in acls]

    def _get_ingressacl_by_policygroup_id(self, inaclid, policygroup_id):
        req_params = {
            'acl_id': inaclid
        }
        nuage_acl = nuagelib.NuageACLRule(create_params=req_params)
        return self.restproxy.get(
            nuage_acl.in_post_resource(),
            extra_headers=nuage_acl.extra_headers_get_locationID(
                policygroup_id),
            required=True)

    def _get_egressacl_by_policygroup_id(self, egaclid, policygroup_id):
        req_params = {
            'acl_id': egaclid
        }
        nuage_acl = nuagelib.NuageACLRule(create_params=req_params)

        return self.restproxy.get(
            nuage_acl.eg_post_resource(),
            extra_headers=nuage_acl.extra_headers_get_locationID(
                policygroup_id),
            required=True)

    def _get_ingressacl_by_remote_policygroup_id(self, inaclid,
                                                 policygroup_id):
        req_params = {
            'acl_id': inaclid
        }
        nuage_acl = nuagelib.NuageACLRule(create_params=req_params)

        return self.restproxy.get(
            nuage_acl.in_post_resource(),
            extra_headers=nuage_acl.extra_headers_get_network_id(
                policygroup_id),
            required=True)

    def _get_egressacl_by_remote_policygroup_id(self, egaclid, policygroup_id):
        req_params = {
            'acl_id': egaclid
        }
        nuage_acl = nuagelib.NuageACLRule(create_params=req_params)

        return self.restproxy.get(
            nuage_acl.eg_post_resource(),
            extra_headers=nuage_acl.extra_headers_get_network_id(
                policygroup_id),
            required=True)

    def _check_policygroup_is_empty(self, policygroup_id, resource_type,
                                    resource_id):
        # get ingress/egress acl template
        if resource_type == constants.L2DOMAIN:
            nuage_ibacl_id = pg_helper.get_l2dom_inbound_acl_id(
                self.restproxy, resource_id)
            nuage_obacl_id = pg_helper.get_l2dom_outbound_acl_id(
                self.restproxy, resource_id)
        else:
            nuage_ibacl_id = pg_helper.get_l3dom_inbound_acl_id(
                self.restproxy, resource_id)
            nuage_obacl_id = pg_helper.get_l3dom_outbound_acl_id(
                self.restproxy, resource_id)

        # get ingress/egress aclrules for policygroup_id
        in_acls = self._get_ingressacl_by_policygroup_id(nuage_ibacl_id,
                                                         policygroup_id)
        eg_acls = self._get_egressacl_by_policygroup_id(nuage_obacl_id,
                                                        policygroup_id)
        return len(in_acls) == 0 and len(eg_acls) == 0

    def _map_security_group_to_policygroup(self, security_group):
        return {
            'description': security_group['name'],
            'name': security_group['id'],
            'externalID': get_vsd_external_id(security_group['id']),
        }

    def create_security_group(self, parent_resource, parent_id,
                              security_group, sg_type=constants.SOFTWARE):
        vsd_data = self._map_security_group_to_policygroup(security_group)
        vsd_data['type'] = sg_type
        resource = nuagelib.Policygroup()
        return self.restproxy.post(
            resource.post_url(parent_resource.resource, parent_id),
            vsd_data)[0]

    def process_port_create_security_group(self, params):
        to_rollback = []
        vsd_subnet = params['vsd_subnet']
        sg = params['sg']
        sg_rules = params['sg_rules']
        sg_type = params.get('sg_type', constants.SOFTWARE)
        l3dom_id = None
        if vsd_subnet['type'] == constants.SUBNET:
            zone = helper.get_nuage_zone_by_id(
                self.restproxy, vsd_subnet['parentID'])
            l3dom_id = zone['nuage_parent_id']
            ext_id = 'hw:%s' % zone['nuage_external_id']
            nuage_policygroup_id = pg_helper.get_l3dom_policygroup_by_sgid(
                self.restproxy, l3dom_id, sg['id'], sg_type)
        else:
            nuage_policygroup_id = pg_helper.get_l2dom_policygroup_by_sgid(
                self.restproxy, vsd_subnet['ID'], sg['id'], sg_type)
            ext_id = 'hw:%s' % vsd_subnet['externalID']

        create_params = {
            'nuage_router_id': l3dom_id,
            'nuage_l2dom_id': vsd_subnet['ID'],
            'name': sg['name'],
            'sg_id': sg['id'],
            'sg_rules': sg_rules,
            'sg_type': sg_type
        }
        if not nuage_policygroup_id:
            try:
                nuage_policygroup_id = self._create_nuage_secgroup(
                    create_params, raise_on_pg_exists=True)
                rollback_resource = {
                    'resource': RES_POLICYGROUPS,
                    'resource_id': nuage_policygroup_id
                }
                to_rollback.append(rollback_resource)
                create_params['nuage_policygroup_id'] = nuage_policygroup_id
                self._create_nuage_sgrules_bulk(create_params)
                if sg_type == constants.HARDWARE:
                    if vsd_subnet['type'] == constants.SUBNET:
                        parent_id = l3dom_id
                        parent_type = constants.NETWORK_TYPE_L3
                    else:
                        parent_id = vsd_subnet['ID']
                        parent_type = constants.NETWORK_TYPE_L2
                    acl_tmpl_name = 'hw:%s' % parent_id
                    deny_all_tmpl = self.create_nuage_acl_tmplt(
                        parent_id,
                        parent_type,
                        ext_id,
                        constants.NUAGE_ACL_EGRESS,
                        name=acl_tmpl_name,
                        priority=1)
                    self.create_default_deny_rule(parent_id,
                                                  parent_type,
                                                  deny_all_tmpl,
                                                  constants.NUAGE_ACL_EGRESS,
                                                  nuage_policygroup_id)
                    # TODO(gridinv): do we need rollback for it?
            except restproxy.RESTProxyError as e:
                if (e.code == restproxy.REST_CONFLICT and
                        e.vsd_code == restproxy.REST_PG_EXISTS_ERR_CODE):
                    if l3dom_id:
                        return self.get_child_policy_groups(
                            nuagelib.NuageL3Domain.resource, l3dom_id,
                            externalID=ext_id)[0]['ID']
                    else:
                        return self.get_child_policy_groups(
                            nuagelib.NuageL2Domain.resource, vsd_subnet['ID'],
                            externalID=ext_id)[0]['ID']
                else:
                    raise

            except Exception:
                with excutils.save_and_reraise_exception():
                    helper.process_rollback(self.restproxy, to_rollback)
        return nuage_policygroup_id

    def create_nuage_acl_tmplt(self, parent_id, parent_type, os_resource_id,
                               direction, name=None, priority=None):
        req_params = {
            'parent_id': parent_id,
            'name': name if name is not None else parent_id,
            'externalID': get_vsd_external_id(os_resource_id)
        }
        if priority:
            req_params['priority'] = priority
        if direction == constants.NUAGE_ACL_EGRESS:
            nuageacl = nuagelib.NuageOutboundACL(create_params=req_params)
        else:
            nuageacl = nuagelib.NuageInboundACL(create_params=req_params)
        if parent_type == constants.NETWORK_TYPE_L2:
            rest_path = nuageacl.post_resource_l2()
            rest_data = nuageacl.post_data_default_l2()
        else:
            rest_path = nuageacl.post_resource_l3()
            rest_data = nuageacl.post_data_default_l3()
        return self.restproxy.post(
            rest_path, rest_data,
            on_res_exists=self.restproxy.acltmpl_retrieve_by_priority,
            ignore_err_codes=[restproxy.REST_DUPLICATE_ACL_PRIORITY])[0]

    def create_default_deny_rule(self, parent_id, parent_type,
                                 acl_tpml_id, direction, pg_id):
        req_params = {
            'acl_id': acl_tpml_id['ID'],
        }
        nuage_aclrule = nuagelib.NuageACLRule(create_params=req_params)
        nuage_match_info = {
            'etherType': '0x0800',
            'protocol': 'ANY',
            'networkType': 'ANY',
            'locationType': 'POLICYGROUP',
            'locationID': pg_id,
            'action': 'DROP',
            'stateful': False,
            'DSCP': '*',
            'flowLoggingEnabled': False,
            'statsLoggingEnabled': False,
            'priority': random.randint(MIN_SG_PRI, MAX_SG_PRI)
        }
        if direction == constants.NUAGE_ACL_EGRESS:
            url = nuage_aclrule.eg_post_resource()
        else:
            url = nuage_aclrule.in_post_resource()

        attempts = 3
        for i in range(attempts):
            try:
                return self.restproxy.post(url, nuage_match_info)[0]['ID']
            except restproxy.RESTProxyError as e:
                if (e.code == restproxy.REST_CONFLICT and
                        e.vsd_code ==
                        constants.VSD_PRIORITY_CONFLICT_ERR_CODE):
                    nuage_match_info['priority'] = random.randint(MIN_SG_PRI,
                                                                  MAX_SG_PRI)
                else:
                    raise
        raise restproxy.ResourceConflictException(
            "Failed to create aclentrytemplate after %s attempts "
            "due to priority conflict" % attempts)

    def get_policygroup_vport_mapping_by_port_id(self, vport_id):
        nuage_vport = nuagelib.NuageVPort()

        policygroups = self.restproxy.get(
            nuage_vport.get_vport_policygroup_resource(vport_id),
            required=True)
        return [{'nuage_policygroup_id': pg['ID']} for pg in policygroups]

    # deprecated
    def delete_port_security_group_bindings(self, params):
        try:
            nuage_port = helper.get_nuage_vport_by_neutron_id(self.restproxy,
                                                              params)
        except restproxy.RESTProxyError as e:
            if e.code == 404:
                return
            else:
                raise e

        if nuage_port and nuage_port.get('ID'):
            nuage_vport_id = nuage_port['ID']
            policygroup_vport_list = (
                self.get_policygroup_vport_mapping_by_port_id(nuage_vport_id))
            if policygroup_vport_list:
                self.update_vport_policygroups(nuage_vport_id, [])
                # check for eager cleanup
                for pg_vport in policygroup_vport_list:
                    params = {"policygroup_id":
                              pg_vport['nuage_policygroup_id']}
                    nuage_vport = nuagelib.NuageVPort(create_params=params)
                    vports = self.restproxy.get(
                        nuage_vport.get_vports_for_policygroup(),
                        required=True)
                    if not vports:
                        # pg no longer in use - delete it
                        self.delete_nuage_policy_group(
                            pg_vport['nuage_policygroup_id'])

    def check_unused_policygroups(self, securitygroup_ids, sg_type):
        if not securitygroup_ids:
            return
        vsd_policygroup = nuagelib.NuagePolicygroup()
        filters = ["externalID IS '%s'" % self._get_vsd_external_id(sg_id,
                                                                    sg_type)
                   for sg_id in securitygroup_ids]
        header = {'X-Nuage-Filter': " or ".join(filters)}
        policygroups = self.restproxy.get(vsd_policygroup.get_all_resources(),
                                          extra_headers=header)
        for policygroup in policygroups:
            pg_vports = self.restproxy.get(
                nuagelib.NuageVPort().get_child_resource(
                    vsd_policygroup.resource,
                    policygroup['ID']))
            if not pg_vports:
                # pg no longer in use - delete it
                try:
                    self.delete_nuage_policy_group(policygroup['ID'])
                except restproxy.RESTProxyError as e:
                    if e.vsd_code == constants.VSD_PG_IN_USE:
                        # concurrenct usage of policygroup, ignore
                        continue
                    else:
                        raise

    @staticmethod
    def get_sg_stateful_value(sg_id):
        session = db_api.get_reader_session()
        value = nuagedb.get_nuage_sg_parameter(session, sg_id, 'STATEFUL')
        session.close()
        return not (value and value.parameter_value == '0')

    def create_nuage_external_security_group(self, params):
        l2dom_id = params.get('l2dom_id')
        l3dom_id = params.get('l3dom_id')

        req_params = {
            'name': params['name'],
            'description': params.get('description'),
            'extended_community': params.get('extended_community'),
            'externalID': get_vsd_external_id(params.get('externalID'))
        }
        if l3dom_id:
            req_params['domain_id'] = l3dom_id
        elif l2dom_id:
            req_params['domain_id'] = l2dom_id

        nuage_policygroup = nuagelib.NuagePolicygroup(create_params=req_params)
        if l3dom_id:
            response = self.restproxy.post(
                nuage_policygroup.post_resource(),
                nuage_policygroup.post_data_ext_sg())
        elif l2dom_id:
            response = self.restproxy.post(
                nuage_policygroup.post_resource_l2dom(),
                nuage_policygroup.post_data_ext_sg())
        return response

    def get_nuage_external_security_group(self, ext_sg_id):
        ext_policygroup = nuagelib.NuagePolicygroup()
        is_external = "true"
        extra_headers = ext_policygroup.extra_headers_get_external(is_external)

        pol_groups = self.restproxy.get(
            ext_policygroup.get_resource(ext_sg_id),
            extra_headers=extra_headers,
            required=True)
        return pol_groups[0] if pol_groups else None

    def get_nuage_external_security_groups(self, params):
        ext_policygroup = nuagelib.NuagePolicygroup()
        is_external = "true"
        extra_headers = ext_policygroup.extra_headers_get_external(is_external)
        if not params:
            pol_groups = self.restproxy.get(
                ext_policygroup.get_all_resources(),
                extra_headers=extra_headers,
                required=True)
            return pol_groups[0] if pol_groups else None
        if params.get('name'):
            extra_headers = (
                ext_policygroup.extra_headers_get_name_and_external(
                    params.get('name'), is_external))
            pol_groups = self.restproxy.get(
                ext_policygroup.get_all_resources(),
                extra_headers=extra_headers,
                required=True)
        elif params.get('id'):
            ext_policygroup_id = params.get('id')
            pol_groups = self.restproxy.get(
                ext_policygroup.get_resource(ext_policygroup_id),
                extra_headers=extra_headers,
                required=True)
        elif params.get('subnet'):
            subnet_mapping = params.get('subnet_mapping')
            l2dom_id = helper.get_nuage_subnet(
                self.restproxy, subnet_mapping)['ID']
            req_params = {
                'domain_id': l2dom_id
            }
            ext_policygroup.create_params = req_params
            pol_groups = self.restproxy.get(
                ext_policygroup.post_resource_l2dom(),
                extra_headers=extra_headers,
                required=True)
        elif params.get('router'):
            l3dom_id = helper.get_l3domid_by_router_id(self.restproxy,
                                                       params.get('router'))
            req_params = {
                'domain_id': l3dom_id
            }
            ext_policygroup.create_params = req_params
            pol_groups = self.restproxy.get(
                ext_policygroup.post_resource(),
                extra_headers=extra_headers,
                required=True)

        return pol_groups if pol_groups else None

    def delete_nuage_external_security_group(self, ext_sg_id):
        self.delete_nuage_policy_group(ext_sg_id)

    def _process_external_sg_rule(self, ext_sg_rule):
        nuage_policygroup = nuagelib.NuagePolicygroup()
        if ext_sg_rule['locationID']:
            pol_groups = self.restproxy.get(
                nuage_policygroup.get_resource(ext_sg_rule['locationID']))
            ext_sg_rule['origin_group_id'] = pol_groups[0]['name']
        if ext_sg_rule['networkType'] == 'POLICYGROUP' and (
                ext_sg_rule['networkID']):
            pol_groups = self.restproxy.get(
                nuage_policygroup.get_resource(ext_sg_rule['networkID']))
            ext_sg_rule['remote_group_id'] = pol_groups[0]['name']

        return ext_sg_rule

    def _create_nuage_external_sg_rule_params(self, ext_sg_rule, parent,
                                              parent_type):
        np_id = acl_id = l2dom_fields = None
        if parent_type == constants.DOMAIN:
            if ext_sg_rule['direction'] == 'ingress':
                acl_id = pg_helper.get_l3dom_inbound_acl_id(
                    self.restproxy, parent)
            else:
                acl_id = pg_helper.get_l3dom_outbound_acl_id(
                    self.restproxy, parent)
            np_id = helper.get_l3domain_np_id(self.restproxy, parent)
        elif parent_type == constants.L2DOMAIN:
            if ext_sg_rule['direction'] == 'ingress':
                acl_id = pg_helper.get_l2dom_inbound_acl_id(
                    self.restproxy, parent)
            else:
                acl_id = pg_helper.get_l2dom_outbound_acl_id(
                    self.restproxy, parent)
            fields = ['parentID', 'DHCPManaged']
            l2dom_fields = helper.get_l2domain_fields_for_pg(
                self.restproxy, parent, fields)
            np_id = l2dom_fields['parentID']
            if not l2dom_fields['DHCPManaged']:
                l2dom_fields['DHCPManaged'] = "unmanaged"

        origin_policygroup_id = pg_helper.get_remote_policygroup_id(
            self.restproxy,
            ext_sg_rule['origin_group_id'], parent_type,
            parent,
            None)
        params = {
            'acl_id': acl_id,
            'direction': ext_sg_rule.get('direction'),
            'neutron_sg_rule': ext_sg_rule,
            'policygroup_id': origin_policygroup_id,
            'np_id': np_id
        }
        if parent_type == constants.L2DOMAIN:
            params['l2dom_id'] = parent
            params['dhcp_managed'] = l2dom_fields['DHCPManaged']
        else:
            params['l3dom_id'] = parent
        return params

    def create_nuage_external_sg_rule(self, params):
        external_sg_id = params['remote_external_group_id']
        external_sg = self.get_nuage_external_security_group(external_sg_id)
        params['remote_external_group'] = external_sg_id
        parent = external_sg['parentID']
        parent_type = external_sg['parentType']

        rule_params = self._create_nuage_external_sg_rule_params(
            params, parent, parent_type)
        rule_params['remote_external_group_name'] = external_sg['name']
        req_params = {
            'acl_id': rule_params['acl_id'],
        }
        nuage_aclrule = nuagelib.NuageACLRule(create_params=req_params)
        nuage_match_info = self._map_nuage_sgrule(rule_params)
        nuage_match_info['externalID'] = external_sg['externalID']

        # neutron ingress is nuage egress and vice versa
        if params['direction'] == 'ingress':
            acls = self.restproxy.post(nuage_aclrule.in_post_resource(),
                                       nuage_match_info)
        else:
            acls = self.restproxy.post(nuage_aclrule.eg_post_resource(),
                                       nuage_match_info)
        if acls:
            return self._process_external_sg_rule(acls[0])

    def get_nuage_external_sg_rules(self, params):
        in_acl_id = None
        ob_acl_id = None
        external_sg_id = params['external_group']
        external_sg = self.get_nuage_external_security_group(external_sg_id)

        parent = external_sg['parentID']
        parent_type = external_sg['parentType']
        if parent_type == constants.DOMAIN:
            in_acl_id = pg_helper.get_l3dom_inbound_acl_id(self.restproxy,
                                                           parent)
            ob_acl_id = pg_helper.get_l3dom_outbound_acl_id(self.restproxy,
                                                            parent)
        elif parent_type == constants.L2DOMAIN:
            in_acl_id = pg_helper.get_l2dom_inbound_acl_id(
                self.restproxy, parent)
            ob_acl_id = pg_helper.get_l2dom_outbound_acl_id(
                self.restproxy, parent)

        # get ingress/egress aclrules for policygroup_id
        in_acls = self._get_ingressacl_by_remote_policygroup_id(
            in_acl_id, external_sg_id)
        eg_acls = self._get_egressacl_by_remote_policygroup_id(
            ob_acl_id, external_sg_id)
        rules = []
        for in_acl in in_acls:
            rule = self._process_external_sg_rule(in_acl)
            rule['direction'] = 'ingress'
            rules.append(rule)
        for eg_acl in eg_acls:
            rule = self._process_external_sg_rule(eg_acl)
            rule['direction'] = 'egress'
            rules.append(rule)
        return rules

    def get_nuage_external_sg_rule(self, ext_rule_id):
        nuage_aclrule = nuagelib.NuageACLRule()
        try:
            acl = self.restproxy.get(
                nuage_aclrule.in_delete_resource(ext_rule_id),
                required=True)[0]
            acl['direction'] = 'ingress'
        except restproxy.ResourceNotFoundException:
            acl = self.restproxy.get(
                nuage_aclrule.eg_delete_resource(ext_rule_id),
                required=True)[0]
            acl['direction'] = 'egress'
        return self._process_external_sg_rule(acl)

    def delete_nuage_external_sg_rule(self, ext_rule_id):
        nuage_aclrule = nuagelib.NuageACLRule()
        try:
            self.restproxy.delete(
                nuage_aclrule.in_delete_resource(ext_rule_id))
        except restproxy.RESTProxyError:
            self.restproxy.delete(
                nuage_aclrule.eg_delete_resource(ext_rule_id))

    def create_nuage_sec_grp_for_no_port_sec(self, params):
        l2dom_id = params['l2dom_id']
        rtr_id = params['rtr_id']
        sg_type = params['sg_type']
        append_str = ('' if sg_type == constants.SOFTWARE else
                      '_' + constants.HARDWARE)
        params_sg = {
            'nuage_l2dom_id': l2dom_id,
            'nuage_router_id': rtr_id,
            'name': constants.NUAGE_PLCY_GRP_ALLOW_ALL + append_str,
            'sg_id': constants.NUAGE_PLCY_GRP_ALLOW_ALL,
            'sg_type': sg_type
        }
        to_rollback = []
        try:
            params['sg_id'] = self._create_nuage_secgroup(
                params_sg, raise_on_pg_exists=True)
            rollback_resource = {
                'resource': RES_POLICYGROUPS,
                'resource_id': params['sg_id']
            }
            to_rollback.append(rollback_resource)
        except restproxy.RESTProxyError as e:
            if (e.code == restproxy.REST_CONFLICT and
                    e.vsd_code == restproxy.REST_PG_EXISTS_ERR_CODE):
                external_id = self._get_vsd_external_id(
                    params_sg['sg_id'], params_sg['sg_type'])
                if rtr_id:
                    return self.get_child_policy_groups(
                        nuagelib.NuageL3Domain.resource, rtr_id,
                        externalID=external_id)[0]['ID']
                else:
                    return self.get_child_policy_groups(
                        nuagelib.NuageL2Domain.resource, l2dom_id,
                        externalID=external_id)[0]['ID']
            else:
                raise
        try:
            self.create_nuage_sec_grp_rule_for_no_port_sec(params)
        except Exception:
            with excutils.save_and_reraise_exception():
                helper.process_rollback(self.restproxy, to_rollback)
        return params['sg_id']

    def create_nuage_sec_grp_for_sfc(self, params):
        l2dom_id = params['l2dom_id']
        rtr_id = params['rtr_id']
        params_sg = {
            'nuage_l2dom_id': l2dom_id,
            'nuage_router_id': rtr_id,
            'name': params['name'],
            'sg_id': params['externalID'],
            'sg_type': params['sg_type'],
            'description': params['description']
        }
        return self._create_nuage_secgroup(params_sg)

    def create_nuage_sec_grp_rule_for_no_port_sec(self, params):
        nuage_ibacl_details = {}
        nuage_obacl_id = None
        pg_id = params['sg_id']
        l2dom_id = params['l2dom_id']
        rtr_id = params['rtr_id']
        if l2dom_id:
            nuage_ibacl_details = pg_helper.get_inbound_acl_details(
                self.restproxy, l2dom_id, type=constants.L2DOMAIN)
            nuage_obacl_id = pg_helper.get_l2dom_outbound_acl_id(
                self.restproxy, l2dom_id)
        elif rtr_id:
            nuage_ibacl_details = pg_helper.get_inbound_acl_details(
                self.restproxy, rtr_id)
            nuage_obacl_id = pg_helper.get_l3dom_outbound_acl_id(
                self.restproxy, rtr_id)
        nuage_ibacl_id = nuage_ibacl_details.get('ID')
        req_params = {'acl_id': nuage_ibacl_id}
        extra_params = {'locationID': pg_id,
                        'externalID': get_vsd_external_id(
                            constants.NUAGE_PLCY_GRP_ALLOW_ALL),
                        'flowLoggingEnabled': self.flow_logging_enabled,
                        'statsLoggingEnabled': self.stats_collection_enabled}
        for ethertype in constants.NUAGE_SUPPORTED_ETHERTYPES_IN_HEX:
            extra_params['etherType'] = ethertype
            nuage_ib_aclrule = nuagelib.NuageACLRule(
                create_params=req_params,
                extra_params=extra_params)
            self.restproxy.post(nuage_ib_aclrule.in_post_resource(),
                                nuage_ib_aclrule.post_data_for_spoofing())
        req_params = {'acl_id': nuage_obacl_id}
        for ethertype in constants.NUAGE_SUPPORTED_ETHERTYPES_IN_HEX:
            extra_params['etherType'] = ethertype
            nuage_ob_aclrule = nuagelib.NuageACLRule(
                create_params=req_params,
                extra_params=extra_params)
            self.restproxy.post(nuage_ob_aclrule.eg_post_resource(),
                                nuage_ob_aclrule.post_data_for_spoofing())

    def get_policy_group(self, id, required=False, **filters):
        policy_group = nuagelib.NuagePolicygroup()
        policy_groups = self.restproxy.get(
            policy_group.get_resource(id),
            extra_headers=policy_group.extra_header_filter(**filters),
            required=required)
        if policy_groups:
            return policy_groups[0]

    def get_policy_groups(self, required=False, **filters):
        policy_group = nuagelib.NuagePolicygroup()
        return self.restproxy.get(
            policy_group.get_all_resources(),
            extra_headers=policy_group.extra_header_filter(**filters),
            required=required)

    def get_policy_groups_by_single_filter(self, filters, required=False):
        policy_group = nuagelib.NuagePolicygroup()
        return self.restproxy.get(
            policy_group.get_all_resources(),
            extra_headers=policy_group.single_filter_header(**filters),
            required=required)

    def get_child_policy_groups(self, parent_resource, parent_id,
                                required=False, **filters):
        policy_group = nuagelib.NuagePolicygroup()
        return self.restproxy.get(
            policy_group.get_child_resource(parent_resource, parent_id),
            extra_headers=policy_group.extra_header_filter(**filters),
            required=required)


class NuageRedirectTargets(object):
    def __init__(self, restproxy):
        self.restproxy = restproxy
        self.flow_logging_enabled = cfg.CONF.PLUGIN.flow_logging_enabled
        self.stats_collection_enabled = (cfg.CONF.PLUGIN.
                                         stats_collection_enabled)

    def create_nuage_redirect_target(self, redirect_target, l2dom_id=None,
                                     domain_id=None):
        rtarget = nuagelib.NuageRedirectTarget()
        if l2dom_id:
            try:
                # Only the subnet redirect target's externalID is
                # network_id@cms_id.
                redirect_target['externalID'] = get_vsd_external_id(
                    redirect_target.get('external_id'))
                return self.restproxy.post(
                    rtarget.post_resource_l2dom(l2dom_id),
                    rtarget.post_rtarget_data(redirect_target))[0]
            except restproxy.ResourceNotFoundException:
                domain_id = helper._get_nuage_domain_id_from_subnet(
                    self.restproxy, l2dom_id)
        if domain_id:
            if redirect_target.get('router_id'):
                redirect_target['externalID'] = get_vsd_external_id(
                    redirect_target.get('router_id'))
            else:
                redirect_target['externalID'] = get_vsd_external_id(
                    redirect_target.get('external_id'))
            return self.restproxy.post(
                rtarget.post_resource_l3dom(domain_id),
                rtarget.post_rtarget_data(redirect_target))[0]

    def create_virtual_ip(self, rtarget_id, vip, vip_port_id):
        rtarget = nuagelib.NuageRedirectTarget()
        return self.restproxy.post(
            rtarget.post_virtual_ip(rtarget_id),
            rtarget.post_virtualip_data(vip, vip_port_id))

    def get_nuage_redirect_target(self, rtarget_id):
        rtarget = nuagelib.NuageRedirectTarget()
        rtarget_resp = self.restproxy.get(
            rtarget.get_redirect_target(rtarget_id))
        if rtarget_resp:
            return rtarget_resp[0]

    def get_nuage_redirect_targets(self, filters):
        rtarget = nuagelib.NuageRedirectTarget()
        extra_headers = rtarget.extra_header_filter(**filters)
        url = rtarget.get_all_redirect_targets()
        return self.restproxy.get(url, extra_headers=extra_headers)

    def get_nuage_redirect_targets_by_single_filter(self, filters,
                                                    required=False):
        rtarget = nuagelib.NuageRedirectTarget()
        extra_headers = rtarget.single_filter_header(**filters)
        url = rtarget.get_all_redirect_targets()
        return self.restproxy.get(url, extra_headers=extra_headers,
                                  required=required)

    def get_child_redirect_targets(self, parent_resource, parent_id,
                                   required=False, **filters):
        redirect_target = nuagelib.NuageRedirectTarget()
        return self.restproxy.get(
            redirect_target.get_child_resource(parent_resource, parent_id),
            extra_headers=redirect_target.extra_header_filter(**filters),
            required=required)

    def delete_nuage_redirect_target(self, rtarget_id):
        rtarget = nuagelib.NuageRedirectTarget()
        self.restproxy.delete(rtarget.delete_redirect_target(rtarget_id))

    def delete_nuage_redirect_target_vip(self, rtarget_vip_id):
        rtarget = nuagelib.NuageRedirectTarget()
        self.restproxy.delete(rtarget.post_virtual_ip(rtarget_vip_id))

    def update_nuage_vport_redirect_target(self, rtarget_id, vport_id):
        rtarget = nuagelib.NuageRedirectTarget()
        self.restproxy.put(rtarget.get_vport_redirect_target(vport_id),
                           rtarget.put_vport_data(rtarget_id))

    def update_redirect_target_vports(self, redirect_target_id,
                                      nuage_port_id_list):
        rtarget = nuagelib.NuageRedirectTarget()
        self.restproxy.put(
            rtarget.get_redirect_target(redirect_target_id) + '/vports',
            nuage_port_id_list)

    def delete_port_redirect_target_bindings(self, params):
        nuage_port = helper.get_nuage_vport_by_neutron_id(self.restproxy,
                                                          params)
        if nuage_port and nuage_port.get('ID'):
            nuage_vport_id = nuage_port['ID']
            rtarget_id = (
                self.get_rtarget_vport_mapping_by_port_id(nuage_vport_id))
            if rtarget_id:
                rtarget_id = None
                self.update_nuage_vport_redirect_target(rtarget_id,
                                                        nuage_vport_id)

    def get_rtarget_vport_mapping_by_port_id(self, vport_id):
        nuage_vport = nuagelib.NuageVPort()
        vports = self.restproxy.get(
            nuage_vport.get_vport_redirect_target_resource(vport_id),
            required=True)
        return vports[0]['ID'] if vports else None

    def create_nuage_redirect_target_rule(self, params, rtarget=None):
        if not rtarget:
            rtarget_id = params['redirect_target_id']
            rtarget = self.get_nuage_redirect_target(rtarget_id)

        parent = rtarget['parentID']
        parent_type = rtarget['parentType']

        fwd_policy_id = helper.get_in_adv_fwd_policy(self.restproxy,
                                                     parent_type,
                                                     parent)
        np_id = None
        if parent_type == constants.DOMAIN:
            if not fwd_policy_id:
                msg = ("Router %s does not have policy mapping") \
                    % parent
                raise restproxy.ResourceConflictException(msg)

            np_id = helper.get_l3domain_np_id(self.restproxy,
                                              parent)
            if not np_id:
                msg = "Net Partition not found for l3domain %s " % parent
                raise restproxy.ResourceNotFoundException(msg)
        elif parent_type == constants.L2DOMAIN:
            if not fwd_policy_id:
                msg = ("L2Domain of redirect target %s does not have policy "
                       "mapping") % parent
                raise restproxy.ResourceConflictException(msg)

            fields = ['parentID', 'DHCPManaged']
            l2dom_fields = helper.get_l2domain_fields_for_pg(self.restproxy,
                                                             parent,
                                                             fields)
            np_id = l2dom_fields['parentID']
            if not np_id:
                msg = "Net Partition not found for l2domain %s " \
                      % parent
                raise restproxy.ResourceNotFoundException(msg)

        if (not params.get('remote_group_id') and
                not params.get('remote_ip_prefix')):
            if params.get('ethertype') == constants.OS_IPV6:
                params['remote_ip_prefix'] = ANY_IPV6_IP
            else:
                params['remote_ip_prefix'] = ANY_IPV4_IP

        rule_params = {
            'rtarget_rule': params,
            'np_id': np_id,
            'parent_type': parent_type,
            'parent': parent
        }
        nuage_fwdrule = nuagelib.NuageAdvFwdRule()
        nuage_match_info = self._map_nuage_redirect_target_rule(rule_params)
        nuage_match_info['externalID'] = rtarget['externalID']

        # neutron ingress is nuage egress and vice versa
        fwd_rules = self.restproxy.post(
            nuage_fwdrule.in_post_resource(fwd_policy_id),
            nuage_match_info)
        return (self._process_redirect_target_rule(fwd_rules[0])
                if fwd_rules else None)

    def add_nuage_sfc_rule(self, fwd_policy, rule_params, np_id):
        fwd_policy_id = fwd_policy['ID']
        if rule_params.get('destination_ip_prefix'):
            netid = pg_helper.create_nuage_prefix_macro(
                self.restproxy, {'remote_ip_prefix': rule_params.get(
                    'destination_ip_prefix')}, np_id)
            rule_params['networkID'] = netid
        nuage_fwdrule = nuagelib.NuageAdvFwdRule()
        if rule_params['protocol'] != "ANY":
            rule_params['protocol'] = (PROTO_NAME_TO_NUM
                                       [rule_params['protocol']])
        rule_params['externalID'] = get_vsd_external_id(
            rule_params['externalID'])
        rule_params['flowLoggingEnabled'] = self.flow_logging_enabled
        rule_params['statsLoggingEnabled'] = self.stats_collection_enabled
        rule = self.restproxy.post(
            nuage_fwdrule.in_post_resource(fwd_policy_id),
            rule_params)
        return rule[0]

    def _map_nuage_redirect_target_rule(self, params):
        np_id = params['np_id']
        rtarget_rule = params.get('rtarget_rule')

        # rtarget_id = rtarget_rule.get('remote_target_id')
        # network_type = 'ENDPOINT_DOMAIN'
        nuage_match_info = {
            'etherType': constants.IPV4_ETHERTYPE,
            'action': rtarget_rule.get('action'),
            'DSCP': '*',
            'protocol': 'ANY',
            'priority': rtarget_rule.get('priority'),
            'flowLoggingEnabled': self.flow_logging_enabled,
            'statsLoggingEnabled': self.stats_collection_enabled,
        }
        min_port = max_port = None
        for key in list(rtarget_rule):
            if rtarget_rule[key] is None:
                continue
            if str(key) == 'protocol':
                nuage_match_info['protocol'] = int(rtarget_rule[key])
                if nuage_match_info['protocol'] in (
                        [PROTO_NAME_TO_NUM['tcp'],
                         PROTO_NAME_TO_NUM['udp']]):
                    nuage_match_info['reflexive'] = True
                    nuage_match_info['sourcePort'] = '*'
                    nuage_match_info['destinationPort'] = '*'
            elif str(key) == 'remote_ip_prefix':
                netid = pg_helper.create_nuage_prefix_macro(
                    self.restproxy, rtarget_rule, np_id)
                nuage_match_info['networkID'] = netid
                nuage_match_info['networkType'] = "ENTERPRISE_NETWORK"
            elif str(key) == 'remote_group_id':
                nuage_match_info['networkID'] = (
                    rtarget_rule['remote_policygroup_id'])
                nuage_match_info['networkType'] = "POLICYGROUP"
            elif str(key) == 'origin_group_id':
                nuage_match_info['locationID'] = (
                    rtarget_rule['origin_policygroup_id'])
                nuage_match_info['locationType'] = "POLICYGROUP"
            elif str(key) == 'port_range_max':
                max_port = str(rtarget_rule[key])
            elif str(key) == 'port_range_min':
                min_port = str(rtarget_rule[key])
            elif str(key) == 'redirect_target_id':
                nuage_match_info['redirectVPortTagID'] = rtarget_rule[key]
        if min_port and max_port:
            if nuage_match_info['protocol'] in [6, 17]:
                port_str = min_port
                if int(min_port) != int(max_port):
                    port_str = port_str + '-' + max_port
                nuage_match_info['sourcePort'] = '*'
                nuage_match_info['destinationPort'] = port_str
        return nuage_match_info

    def _process_redirect_target_rule(self, rtarget_rule):
        nuage_policygroup = nuagelib.NuagePolicygroup()
        if rtarget_rule['locationID']:
            pol_groups = self.restproxy.get(
                nuage_policygroup.get_resource(rtarget_rule['locationID']),
                required=True)
            rtarget_rule['origin_group_id'] = pol_groups[0]['name']
        if rtarget_rule['networkType'] == 'POLICYGROUP' and (
                rtarget_rule['networkID']):
            pol_groups = self.restproxy.get(
                nuage_policygroup.get_resource(rtarget_rule['networkID']),
                required=True)
            rtarget_rule['remote_group_id'] = pol_groups[0]['name']

        return rtarget_rule

    def get_nuage_redirect_target_rules(self, params):
        rtarget_rule = nuagelib.NuageAdvFwdRule()
        parent = parent_type = None
        if params.get('subnet'):
            subnet_mapping = params.get('subnet_mapping')
            parent = helper.get_nuage_subnet(
                self.restproxy, subnet_mapping)['ID']
            parent_type = constants.L2DOMAIN
        elif params.get('router'):
            parent = helper.get_l3domid_by_router_id(self.restproxy,
                                                     params.get('router'))
            parent_type = constants.DOMAIN

        fwd_policy_id = helper.get_in_adv_fwd_policy(self.restproxy,
                                                     parent_type,
                                                     parent)
        adw_rules = self.restproxy.get(
            rtarget_rule.in_post_resource(fwd_policy_id),
            required=True)
        if not adw_rules:
            msg = "Could not find ingressadvfwdentrytemplates for " \
                  "ingressadvfwdtemplate %s "
            raise restproxy.ResourceNotFoundException(msg % fwd_policy_id)
        return [self._process_redirect_target_rule(r)
                for r in adw_rules]

    def get_nuage_redirect_target_rules_by_external_id(self, neutron_id):
        create_params = {'externalID': neutron_id}
        rtarget_rule = nuagelib.NuageAdvFwdRule(create_params=create_params)
        rtarget_rules_resp = self.restproxy.get(
            rtarget_rule.in_get_all_resources(),
            extra_headers=rtarget_rule.extra_headers_get())
        return rtarget_rules_resp

    def get_nuage_redirect_target_rule(self, rtarget_rule_id):
        rtarget_rule = nuagelib.NuageAdvFwdRule()
        adw_rules = self.restproxy.get(
            rtarget_rule.in_get_resource(rtarget_rule_id),
            required=True)
        return self._process_redirect_target_rule(adw_rules[0])

    def delete_nuage_redirect_target_rule(self, rtarget_rule_id):
        rtarget_rule = nuagelib.NuageAdvFwdRule()
        self.restproxy.delete(rtarget_rule.in_delete_resource(rtarget_rule_id))

    def nuage_redirect_targets_on_l2domain(self, l2domid):
        nuagel2dom = nuagelib.NuageL2Domain()
        rts = self.restproxy.get(
            nuagel2dom.nuage_redirect_target_get_resource(l2domid),
            required=True)
        return len(rts) > 0

    def get_redirect_target_vports(self, rtarget_id, required=False):
        vport = nuagelib.NuageVPort(create_params={'rtarget_id': rtarget_id})
        return self.restproxy.get(
            vport.get_vport_for_redirectiontargets(), required=required)
